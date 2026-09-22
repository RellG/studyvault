import io

import pytest

from app import catalog, markdown, notes_fs
from app.config import settings
from app.routers.search import fts_query, search


@pytest.fixture
def d413(seeded):
    notes_fs.ensure_repo()
    c = catalog.get_course(seeded, "D413")
    notes_fs.ensure_course_files(seeded, c)
    return c


def test_slug_paths(d413):
    assert notes_fs.course_rel_dir(d413) == "term-1/d413-telecomm-and-wireless-communications"
    assert notes_fs.note_path(d413, "notebook") == settings.notes_dir / "term-1/d413-telecomm-and-wireless-communications/notebook.md"
    for f in notes_fs.NOTE_FILES:
        assert (notes_fs.course_dir(d413) / f"{f}.md").exists()


@pytest.mark.parametrize("bad", ["../x", "pa/../../etc", "secrets", "pa/", "/etc/passwd"])
def test_rejects_bad_names(bad):
    with pytest.raises(notes_fs.BadNoteName):
        notes_fs.check_name(bad)


def test_atomic_write_and_read(seeded, d413):
    ver = notes_fs.write_note(seeded, d413, "notebook", "line one\r\nline two\n")
    text, v2 = notes_fs.read_note(d413, "notebook")
    assert text == "line one\nline two\n" and ver == v2
    assert not list(notes_fs.course_dir(d413).glob(".*.tmp"))


def test_conflict_detected(seeded, d413):
    _, base = notes_fs.read_note(d413, "notebook")
    notes_fs.write_note(seeded, d413, "notebook", "edited elsewhere")
    with pytest.raises(notes_fs.NoteConflict):
        notes_fs.write_note(seeded, d413, "notebook", "stale editor", base)
    assert notes_fs.read_note(d413, "notebook")[0] == "edited elsewhere"


def test_append(seeded, d413):
    notes_fs.write_note(seeded, d413, "mistakes", "# Mistakes\n")
    notes_fs.append_note(seeded, d413, "mistakes", "### Q1\n")
    assert notes_fs.read_note(d413, "mistakes")[0] == "# Mistakes\n\n### Q1\n"


def test_index_update_and_search(seeded, d413):
    notes_fs.write_note(seeded, d413, "competencies", "## Wireless\n\n802.11ax uses OFDMA and BSS colouring.\n")
    hits = search(seeded, "ofdma")
    assert len(hits) == 1 and hits[0]["url"] == "/courses/D413/notes/competencies"
    assert "<mark>OFDMA</mark>" in hits[0]["snippet"]
    notes_fs.write_note(seeded, d413, "competencies", "## Wireless\n\nnothing here now\n")
    assert search(seeded, "ofdma") == []
    assert search(seeded, "wire") != []  # prefix match on the last word
    assert search(seeded, "wireless", term_n=2) == []
    assert search(seeded, "wireless", code="D413") != []


def test_fts_query_is_safe():
    assert fts_query('") OR 1=1 --') == '"OR" "1" "1"*'
    assert fts_query("   ") is None


def test_snippet_is_escaped(seeded, d413):
    notes_fs.write_note(seeded, d413, "notebook", "<script>alert(1)</script> vlan trunk")
    hit = search(seeded, "vlan")[0]
    assert "<script>" not in hit["snippet"] and "&lt;script&gt;" in hit["snippet"]


def test_move_on_term_change_keeps_search(seeded, d413):
    notes_fs.write_note(seeded, d413, "notebook", "roaming handoff")
    catalog.move_to_term(seeded, "D413", 2)
    new = catalog.get_course(seeded, "D413")
    assert notes_fs.read_note(new, "notebook")[0] == "roaming handoff"
    assert search(seeded, "roaming")[0]["term_n"] == 2
    log = notes_fs.git("log", "--oneline").stdout
    assert "move D413" in log


def test_reindex_picks_up_outside_edits(seeded, d413):
    (notes_fs.course_dir(d413) / "notebook.md").write_text("edited in vim: beamforming", encoding="utf-8")
    assert search(seeded, "beamforming") == []
    assert notes_fs.reindex_all(seeded) >= 4
    assert len(search(seeded, "beamforming")) == 1


def test_commit_batches(seeded, d413):
    notes_fs.write_note(seeded, d413, "notebook", "a")
    notes_fs.write_note(seeded, d413, "overview", "b")
    assert notes_fs.commit_pending() is True
    last = notes_fs.git("log", "-1", "--format=%s").stdout.strip()
    assert last.startswith("notes: D413") and "notebook.md" in last and "overview.md" in last
    assert notes_fs.commit_pending() is False


def test_markdown_render():
    html = markdown.render("# Title\n\n## Title\n\nSee [[D325]] and [[Z999]].\n\n`[[D325]]`\n\n```\n[[D325]]\n```\n\n<b>x</b>",
                           {"D325"})
    assert '<h1 id="title">' in html and '<h2 id="title-1">' in html
    assert '<a class="course-link" href="/courses/D325">D325</a>' in html
    assert 'class="unknown-course"' in html
    assert "<code>[[D325]]</code>" in html  # not rewritten inside code
    assert "&lt;b&gt;" in html  # raw HTML escaped


def test_note_endpoints(client, data_dir):
    r = client.get("/courses/D413")
    assert r.status_code == 200 and "Overview" in r.text
    r = client.get("/courses/D413/notes/notebook?mode=edit")
    assert r.status_code == 200 and "easymde.min.js" in r.text
    import re
    base = re.search(r'var base = "([0-9a-f]+)"', r.text).group(1)
    r = client.post("/courses/D413/notes/notebook", json={"content": "```bash\nshow ip route\n```\n", "base": base})
    assert r.status_code == 200
    new_base = r.json()["version"]
    assert client.post("/courses/D413/notes/notebook", json={"content": "stale", "base": base}).status_code == 409
    assert client.post("/courses/D413/notes/notebook", json={"content": "ok", "base": new_base}).status_code == 200
    assert client.get("/courses/D413/notes/..%2F..%2Fstudyvault.db").status_code == 404
    assert client.get("/search?q=ok&course=D413").status_code == 200


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_image_upload(client, data_dir):
    r = client.post("/courses/D413/attachments", files={"image": ("net diagram.png", io.BytesIO(PNG), "image/png")})
    assert r.status_code == 200
    path = r.json()["data"]["filePath"]
    assert path.startswith("/attachments/D413/") and path.endswith("-net-diagram.png")
    assert client.get(path).content == PNG
    r = client.post("/courses/D413/attachments", files={"image": ("evil.svg", io.BytesIO(b"<svg/>"), "image/svg+xml")})
    assert r.status_code == 400
    r = client.post("/courses/D413/attachments", files={"image": ("fake.png", io.BytesIO(b"not a png"), "image/png")})
    assert r.status_code == 400
    assert client.get("/attachments/D413/..%2F..%2Fstudyvault.db").status_code == 404


def test_scratch_migrates_to_notebook(seeded, data_dir):
    notes_fs.ensure_repo()
    c = catalog.get_course(seeded, "D413")
    d = notes_fs.course_dir(c)
    d.mkdir(parents=True)
    (d / "scratch.md").write_text("# D413 · Telecomm and Wireless Communications: scratch\n\nmy old notes\n", encoding="utf-8")
    assert notes_fs.migrate_scratch_to_notebook() == 1
    assert not (d / "scratch.md").exists()
    assert (d / "notebook.md").read_text(encoding="utf-8") == "# D413 · Telecomm and Wireless Communications: notebook\n\nmy old notes\n"
    assert "rename scratch.md -> notebook.md" in notes_fs.git("log", "-1", "--format=%s").stdout
    assert notes_fs.migrate_scratch_to_notebook() == 0  # idempotent


def test_old_scratch_url_redirects(client):
    r = client.get("/courses/D413/notes/scratch?mode=edit", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/courses/D413/notes/notebook?mode=edit"
    page = client.get("/courses/D413").text
    assert ">Notebook</a>" in page and ">Scratch</a>" not in page
