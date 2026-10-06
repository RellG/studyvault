"""Notes as sections (Slice 12, step 1): the splitter on messy real-world notes, keeping note_chunks in sync with the
files, linking cards and questions to a section, and the migration leaving existing data alone."""
import json
import shutil
from pathlib import Path

import httpx
import pytest
from starlette.datastructures import FormData

from app import agent, ai, cards, catalog, chunks, db, notes_fs, quizzes
from app.config import settings

# Shaped like a real notebook: a title line, a topic list under a deep heading with closing hashes, bold headings with
# stray markers, an empty `# ` line, a heading with no text under it, and levels that jump around.
MESSY = """# D413 · Telecomm and Wireless Communications: notebook


#### ***PreAssesment***###

- Bandwith
- Signal Attenuation
- VoIP Jitter buffers

# **Course Start**
# **Learning Objectives**
Define telecomm. Define wireless communication.

# **Wired Data Transmission** #1
Ethernet, fiber and coax differ in medium and reach.

## **Satellite Communications **
Geostationary orbit is about 35,786 km up.

# Network Design Choices
#
Choose topology by cost and fault tolerance.

# Multiplexing
```
# not a heading, it's in a code block
```
TDM shares time, FDM shares frequency.

#1 is not a heading either
"""


def by_key(secs):
    return {s["key"]: s for s in secs}


# ---------------------------------------------------------------- splitter

def test_split_messy_notes():
    secs = chunks.split(MESSY, "notebook")
    assert [s["breadcrumb"] for s in secs] == [
        "PreAssesment", "Learning Objectives", "Wired Data Transmission #1",
        "Wired Data Transmission #1 > Satellite Communications", "Network Design Choices", "Multiplexing"]
    assert [s["ord"] for s in secs] == list(range(6))
    s = by_key(secs)
    assert "notebook#preassesment" in s and "Bandwith" in s["notebook#preassesment"]["text"]
    assert s["notebook#network-design-choices"]["text"] == "Choose topology by cost and fault tolerance."  # empty `# ` ignored
    # the code block's "# ..." line stays text, and "#1 ..." is not a heading
    mux = s["notebook#multiplexing"]["text"]
    assert "# not a heading" in mux and "#1 is not a heading either" in mux
    # headings with no text of their own ("Course Start") produce no section
    assert not any("course-start" in k for k in s)


def test_split_title_only_and_text_before_headings():
    assert chunks.split("# D413 · Telecomm: notebook\n\n", "notebook") == []
    secs = chunks.split("Loose first line about OSPF.\n\n## Later\nBody here.\n", "notebook")
    assert [(s["heading"], s["key"]) for s in secs] == [("", "notebook#top"), ("Later", "notebook#later")]
    assert chunks.where(secs[0]) == "(top of file)"


def test_split_ignores_headings_with_nothing_in_them():
    assert chunks.split("## Resources\n\n- \n\n## Plan\n\n***\n", "overview") == []


def test_split_duplicate_headings_get_distinct_keys():
    secs = chunks.split("## Notes\nfirst thing\n\n## Notes\nsecond thing\n", "notebook")
    assert [s["key"] for s in secs] == ["notebook#notes", "notebook#notes~2"]


def test_split_cuts_long_sections_at_paragraphs():
    para = "word " * 300  # 1,500 characters
    secs = chunks.split("## Big\n" + "\n\n".join([para] * 8), "notebook")
    assert [s["key"] for s in secs] == ["notebook#big", "notebook#big.p2", "notebook#big.p3"]  # 3 + 3 + 2 paragraphs
    assert secs[1]["breadcrumb"] == "Big (part 2)"
    assert all(s["chars"] <= chunks.MAX_CHARS for s in secs)
    one_line = chunks.split("## Wall\n" + "x" * 15000, "notebook")  # no breaks to cut at: still bounded
    assert len(one_line) == 3 and all(s["chars"] <= chunks.MAX_CHARS for s in one_line)


def test_hash_ignores_whitespace_but_not_words():
    a = chunks.split("## T\nOSPF is link-state.\n", "notebook")[0]["hash"]
    assert a == chunks.split("## T\n\nOSPF   is link-state.   \n\n", "notebook")[0]["hash"]
    assert a != chunks.split("## T\nOSPF is distance-vector.\n", "notebook")[0]["hash"]


# ---------------------------------------------------------------- kept in sync with the files

@pytest.fixture
def d413(seeded):
    notes_fs.ensure_repo()
    c = catalog.get_course(seeded, "D413")
    notes_fs.ensure_course_files(seeded, c)
    return c


def rows(conn, c, file="notebook"):
    return conn.execute("SELECT key, hash, text FROM note_chunks WHERE course_id = ? AND file = ? ORDER BY ord", (c["id"], file)).fetchall()


def test_new_course_has_no_sections_until_written(seeded, d413):
    assert chunks.outline(seeded, d413["id"], chunks.FILES) == []  # untouched templates are not "notes"


def test_writing_a_note_builds_sections_and_edits_update_them(seeded, d413):
    notes_fs.write_note(seeded, d413, "notebook", MESSY)
    first = {r["key"]: r["hash"] for r in rows(seeded, d413)}
    assert len(first) == 6
    notes_fs.write_note(seeded, d413, "notebook", MESSY.replace("Define telecomm.", "Define telecomm precisely."))
    second = {r["key"]: r["hash"] for r in rows(seeded, d413)}
    assert second.keys() == first.keys()
    assert [k for k in first if first[k] != second[k]] == ["notebook#learning-objectives"]  # only the edited one changed
    notes_fs.write_note(seeded, d413, "notebook", "## Only this\nSomething.\n")
    assert [r["key"] for r in rows(seeded, d413)] == ["notebook#only-this"]  # the rest are gone
    notes_fs.write_note(seeded, d413, "notebook", "")
    assert rows(seeded, d413) == []


def test_unchanged_note_does_not_rewrite_sections(seeded, d413):
    notes_fs.write_note(seeded, d413, "notebook", MESSY)
    stamps = [r[0] for r in seeded.execute("SELECT updated_at FROM note_chunks")]
    assert chunks.sync_file(seeded, d413["id"], "notebook", MESSY) is False
    assert [r[0] for r in seeded.execute("SELECT updated_at FROM note_chunks")] == stamps


def test_only_the_four_course_notes_become_sections(seeded, d413):
    notes_fs.write_note(seeded, d413, "overview", "# D413\n\n## What this course covers\nTelecom and wireless basics.\n")
    notes_fs.write_note(seeded, d413, "mistakes", "## Q about OSPF\nI mixed up the areas.\n")
    assert [r["key"] for r in rows(seeded, d413, "overview")] == ["overview#what-this-course-covers"]
    assert [(o["file"], o["key"]) for o in chunks.outline(seeded, d413["id"])] == [("overview", "overview#what-this-course-covers")]
    assert len(chunks.outline(seeded, d413["id"], chunks.FILES)) == 2  # mistakes only when asked for
    # PA drafts never become sections
    notes_fs.write_note(seeded, d413, "pa/task-1", "## My draft\nThe student's own work.\n")
    assert seeded.execute("SELECT COUNT(*) FROM note_chunks WHERE file LIKE 'pa%' OR text LIKE '%own work%'").fetchone()[0] == 0


def test_reindex_builds_sections_for_notes_written_outside_the_app(seeded, d413):
    p = notes_fs.note_path(d413, "notebook")
    p.write_text(MESSY, encoding="utf-8")
    assert rows(seeded, d413) == []
    notes_fs.reindex_all(seeded)  # what startup does
    assert len(rows(seeded, d413)) == 6
    notes_fs.reindex_all(seeded)  # and again changes nothing
    assert len(rows(seeded, d413)) == 6


def test_editing_notes_marks_the_profile_stale(seeded, d413):
    notes_fs.write_note(seeded, d413, "notebook", "## A\nOne thing.\n")
    seeded.execute("INSERT INTO course_brain(course_id, status, updated_at) VALUES (?, 'fresh', 'x')", (d413["id"],))
    seeded.commit()
    notes_fs.write_note(seeded, d413, "mistakes", "## M\nA slip.\n")  # mistakes don't make the profile stale
    assert seeded.execute("SELECT status FROM course_brain").fetchone()[0] == "fresh"
    notes_fs.write_note(seeded, d413, "notebook", "## A\nOne thing, changed.\n")
    assert seeded.execute("SELECT status FROM course_brain").fetchone()[0] == "stale"


def test_deleting_a_course_removes_its_sections(seeded, d413):
    notes_fs.write_note(seeded, d413, "notebook", MESSY)
    seeded.execute("DELETE FROM courses WHERE id = ?", (d413["id"],))
    seeded.commit()
    assert seeded.execute("SELECT COUNT(*) FROM note_chunks").fetchone()[0] == 0


# ---------------------------------------------------------------- cards and questions know their section

def test_cards_and_questions_link_to_a_section_and_go_stale(seeded, d413):
    notes_fs.write_note(seeded, d413, "notebook", MESSY)
    ref = chunks.ref(seeded, d413["id"], "notebook#multiplexing")
    assert ref["label"] == "Multiplexing"
    assert chunks.ref(seeded, d413["id"], "notebook#nope") is None
    cards.add(seeded, d413["id"], "TDM shares what?", "Time", source="ai-accepted", source_ref=ref)
    cards.add(seeded, d413["id"], "By hand", "No link")
    quizzes.save(seeded, d413["id"], {"kind": "mc", "prompt": "FDM shares?", "choices": "* Frequency\nTime", "explanation": ""},
                 source="ai-accepted", source_ref=ref)
    assert [(o["key"], o["cards"], o["questions"]) for o in chunks.outline(seeded, d413["id"]) if o["cards"]] == [("notebook#multiplexing", 1, 1)]
    f = chunks.freshness(seeded, d413["id"])
    assert f["cards"] == {"total": 2, "unlinked": 1, "current": 1, "stale": 0, "missing": 0}
    assert f["questions"]["current"] == 1
    notes_fs.write_note(seeded, d413, "notebook", MESSY.replace("TDM shares time", "TDM shares the channel by time slot"))
    f = chunks.freshness(seeded, d413["id"])
    assert f["cards"]["stale"] == 1 and f["cards"]["current"] == 0 and f["questions"]["stale"] == 1
    notes_fs.write_note(seeded, d413, "notebook", "## Other\nNothing about multiplexing.\n")
    assert chunks.freshness(seeded, d413["id"])["cards"]["missing"] == 1


# ---------------------------------------------------------------- the agent reads and links by section

def ctx(conn):
    tid = agent.create_thread(conn, "t")
    return agent.Ctx(conn, conn.execute("SELECT * FROM agent_threads WHERE id = ?", (tid,)).fetchone())


def call(k, tool, **args):
    return json.loads(agent.run_tool(k, ai.Call("", tool, args)))


def test_agent_reads_a_long_notebook_by_section(seeded, d413):
    big = "## Intro\nshort\n\n" + "\n\n".join(f"## Topic {i}\n" + ("fact " * 400) for i in range(12))
    notes_fs.write_note(seeded, d413, "notebook", big)
    k = ctx(seeded)
    cut = call(k, "read_note", code="D413", name="notebook")
    assert cut["truncated"] and "list_note_sections" in cut["hint"]
    out = call(k, "list_note_sections", code="D413")
    assert len(out["sections"]) == 13 and out["total_chars"] > agent.NOTE_CHARS and "error" not in out
    assert out["sections"][-1] == {"key": "notebook#topic-11", "where": "Topic 11", "chars": out["sections"][-1]["chars"],
                                   "cards": 0, "questions": 0}
    sec = call(k, "read_note_section", code="D413", key="notebook#topic-11")  # past where read_note stops
    assert sec["text"].startswith("fact fact") and sec["link"] == "/courses/D413/notes/notebook"
    assert "error" in call(k, "read_note_section", code="D413", key="notebook#nope")
    assert call(k, "get_course", code="D413")["notes"]["sections"] == 13
    assert call(k, "list_note_sections", code="D281")["sections"] == []

    # a section bigger than a normal tool result still comes back whole, as valid JSON
    notes_fs.write_note(seeded, d413, "notebook", "## Long one\n" + "\n\n".join(f"Paragraph {i}: " + "detail " * 100 for i in range(7)))
    long = call(k, "read_note_section", code="D413", key="notebook#long-one")
    assert len(long["text"]) > 4500 and long["text"].rstrip().endswith("detail") and "truncated" not in long["text"]


def test_agent_proposals_carry_the_section_through_apply(seeded, d413):
    notes_fs.write_note(seeded, d413, "notebook", MESSY)
    k = ctx(seeded)
    assert "error" in call(k, "add_flashcards", code="D413", section_key="notebook#nope", cards=[{"front": "a", "back": "b"}])
    out = call(k, "add_flashcards", code="D413", section_key="notebook#multiplexing",
               cards=[{"front": "TDM shares?", "back": "Time"}])
    assert "from “Multiplexing”" in out["summary"]
    p = seeded.execute("SELECT * FROM agent_proposals WHERE id = ?", (out["proposal_id"],)).fetchone()
    agent.apply(seeded, p, FormData([("keep", "0"), ("front_0", "TDM shares?"), ("back_0", "Time")]))
    row = seeded.execute("SELECT source_key, source_hash FROM cards").fetchone()
    assert tuple(row) == ("notebook#multiplexing", chunks.get(seeded, d413["id"], "notebook#multiplexing")["hash"])
    out = call(k, "add_questions", code="D413", section_key="notebook#multiplexing",
               questions=[{"prompt": "FDM shares?", "choices": ["Frequency", "Time"], "correct": [0]}])
    p = seeded.execute("SELECT * FROM agent_proposals WHERE id = ?", (out["proposal_id"],)).fetchone()
    agent.apply(seeded, p, FormData([("keep", "0"), ("kind_0", "mc"), ("prompt_0", "FDM shares?"),
                                     ("choices_0", "* Frequency\nTime"), ("explanation_0", "")]))
    assert seeded.execute("SELECT source_key FROM questions").fetchone()[0] == "notebook#multiplexing"
    # without a section_key it still works, unlinked
    out = call(k, "add_flashcards", code="D413", cards=[{"front": "x?", "back": "y"}])
    p = seeded.execute("SELECT * FROM agent_proposals WHERE id = ?", (out["proposal_id"],)).fetchone()
    agent.apply(seeded, p, FormData([("keep", "0"), ("front_0", "x?"), ("back_0", "y")]))
    assert seeded.execute("SELECT source_key FROM cards WHERE front = 'x?'").fetchone()[0] is None


def test_old_pending_proposals_without_a_source_still_apply(seeded, d413):
    k = ctx(seeded)
    out = call(k, "add_flashcards", code="D413", cards=[{"front": "old?", "back": "yes"}])
    seeded.execute("UPDATE agent_proposals SET payload_json = ? WHERE id = ?",
                   (json.dumps({"cards": [{"front": "old?", "back": "yes"}], "competency_id": None}), out["proposal_id"]))  # the pre-006 shape
    seeded.commit()
    p = seeded.execute("SELECT * FROM agent_proposals WHERE id = ?", (out["proposal_id"],)).fetchone()
    agent.apply(seeded, p, FormData([("keep", "0"), ("front_0", "old?"), ("back_0", "yes")]))
    assert seeded.execute("SELECT front FROM cards").fetchone()[0] == "old?"


# ---------------------------------------------------------------- AI assist links what it drafts

def test_ai_assist_links_accepted_items_to_the_chosen_section(client, monkeypatch):
    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    monkeypatch.setattr(settings, "ai_model", "m")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
    monkeypatch.setattr(ai, "_transport", httpx.MockTransport(lambda r: httpx.Response(200, json={
        "stop_reason": "end_turn", "content": [{"type": "text", "text": '[{"front": "TDM shares?", "back": "Time"}]'}]})))
    conn = db.connect()
    try:
        c = catalog.get_course(conn, "D413")
        notes_fs.ensure_repo()
        notes_fs.ensure_course_files(conn, c)
        notes_fs.write_note(conn, c, "notebook", "## Multiplexing\nTDM shares time, FDM shares frequency.\n")
        page = client.get("/courses/D413/ai?section=notebook|Multiplexing")
        assert "TDM shares time" in page.text
        r = client.post("/courses/D413/ai/run", data={"action": "cards", "text": "TDM shares time.", "section": "notebook|Multiplexing"})
        assert 'name="section_key" value="notebook#multiplexing"' in r.text and "linked to it" in r.text
        h = chunks.get(conn, c["id"], "notebook#multiplexing")["hash"]
        client.post("/courses/D413/ai/accept", data={"kind": "cards", "keep": "0", "front_0": "TDM shares?", "back_0": "Time",
                                                     "section_key": "notebook#multiplexing", "section_hash": h})
        assert tuple(conn.execute("SELECT source, source_key, source_hash FROM cards").fetchone()) == ("ai-accepted", "notebook#multiplexing", h)
        # pasted text with no section pick stays unlinked, and a bogus key is ignored rather than trusted
        r = client.post("/courses/D413/ai/run", data={"action": "cards", "text": "pasted"})
        assert 'name="section_key"' not in r.text
        client.post("/courses/D413/ai/accept", data={"kind": "cards", "keep": "0", "front_0": "p?", "back_0": "q", "section_key": "notebook#bogus"})
        assert conn.execute("SELECT source_key FROM cards WHERE front = 'p?'").fetchone()[0] is None
    finally:
        conn.close()


# ---------------------------------------------------------------- existing data survives the migration

def test_migration_006_keeps_existing_rows(data_dir, monkeypatch, tmp_path):
    old = tmp_path / "old-migrations"
    old.mkdir()
    for f in sorted(db.MIGRATIONS_DIR.glob("*.sql")):
        if f.name < "006":
            shutil.copy(f, old / f.name)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", old)
    conn = db.connect(settings.db_path)
    assert db.migrate(conn)[-1].startswith("005")
    from app import seed
    seed.seed(conn, settings.seed_file)
    c = catalog.get_course(conn, "D413")
    conn.execute("INSERT INTO competencies(course_id, ord, text, confidence) VALUES (?, 1, 'Explain the OSI model', 3)", (c["id"],))
    conn.commit()  # plain SQL: the app's own note-writing code expects the new tables, which don't exist yet in this test
    conn.execute("INSERT INTO cards(course_id, front, back, due_on, source, created_at) VALUES (?, 'SSH port?', '22', '2026-10-01', "
                 "'ai-accepted', '2026-10-01T09:00:00')", (c["id"],))
    conn.execute("INSERT INTO questions(course_id, kind, prompt, choices_json, answer_json, explanation, created_at) VALUES "
                 "(?, 'mc', 'Pick one', '[\"a\", \"b\"]', '[0]', 'x', '2026-10-01T09:00:00')", (c["id"],))
    conn.commit()
    tid = agent.create_thread(conn, "old chat")
    agent.add_memory(conn, "weak on subnetting", c["id"], tid)
    notes_fs.ensure_repo()
    legacy = notes_fs.note_path(c, "notebook")  # written the way it was before sections existed: a file, no index
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy note text\n", encoding="utf-8")
    tables = ["courses", "terms", "competencies", "cards", "questions", "agent_threads", "agent_memory", "notes_index", "search_fts"]

    def snapshot():
        return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1")] for t in tables}

    # the card/question tables get two new columns, so compare the old columns only
    def legacy_view(snap):
        return {t: [r[:{"cards": 12, "questions": 10}.get(t, len(r))] for r in rows_] for t, rows_ in snap.items()}

    before = legacy_view(snapshot())
    monkeypatch.setattr(db, "MIGRATIONS_DIR", Path(db.__file__).parent / "migrations")
    assert db.migrate(conn) == ["006_notes_brain.sql", "007_brain_drafts.sql"]  # everything added since 005, nothing else
    assert legacy_view(snapshot()) == before
    assert tuple(conn.execute("SELECT source_key, source_hash FROM cards").fetchone()) == (None, None)
    assert tuple(conn.execute("SELECT source_key, source_hash FROM questions").fetchone()) == (None, None)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    # full-text search over the old rows still works, and re-running the migrations is a no-op
    assert conn.execute("SELECT COUNT(*) FROM search_fts WHERE search_fts MATCH 'SSH'").fetchone()[0] == 1
    assert db.migrate(conn) == []
    # startup's reindex builds sections for the legacy note without touching the file
    text_before = notes_fs.note_path(c, "notebook").read_text()
    notes_fs.reindex_all(conn)
    assert [r["key"] for r in rows(conn, c)] == ["notebook#top"]
    assert notes_fs.note_path(c, "notebook").read_text() == text_before
    conn.close()


def test_template_placeholders_are_never_sections(seeded, d413):
    """The placeholder sentences in a new note aren't the student's notes, even once the file has been added to."""
    from app import competencies
    competencies.import_list(seeded, d413, ["Compare wired media"])  # adds to competencies.md, which still has its placeholder
    notes_fs.write_note(seeded, d413, "mistakes", notes_fs.read_note(d413, "mistakes")[0] + "## Q1\nI mixed up OSPF areas and ABRs.\n")
    notes_fs.write_note(seeded, d413, "notebook", notes_fs.read_note(d413, "notebook")[0] + "## Wi-Fi\nChannels 1, 6, 11 don't overlap.\n")
    got = [(r["file"], r["key"]) for r in chunks.outline(seeded, d413["id"], chunks.FILES)]
    assert got == [("notebook", "notebook#wi-fi"), ("mistakes", "mistakes#q1")]
    # every non-heading line of every template is covered, so changing a template can't quietly leak into the notes
    for name in notes_fs.NOTE_FILES:
        for line in notes_fs._template(d413, name).splitlines():
            if line.strip() and not line.startswith("#") and not line.strip().startswith("- ") and line.strip() != "-":
                assert line.strip() in chunks.BOILERPLATE, (name, line)
