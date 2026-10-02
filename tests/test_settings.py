import re


def test_default_look(client):
    html = client.get("/").text
    assert 'data-theme="notebook"' in html and 'data-mode="auto"' in html  # Notebook is the default look
    assert "/static/themes.css" in html and "/static/vendor/fonts/fonts.css" in html


def test_pick_theme_and_mode(client):
    page = client.get("/settings").text
    assert all(name in page for name in ["Notebook", "Sage", "Paper"]) and "Night Ops" not in page
    r = client.post("/settings", data={"theme": "sage", "mode": "dark"}, follow_redirects=True)
    assert r.status_code == 200 and "Saved" in r.text
    html = client.get("/courses/D413").text
    assert 'data-theme="sage"' in html and 'data-mode="dark"' in html
    assert "hljs-github-dark.min.css" in html and "hljs-github.min.css" not in html  # code colours follow the forced mode
    assert client.post("/settings", data={"theme": "bogus", "mode": "dark"}).status_code == 400
    assert client.post("/settings", data={"theme": "paper", "mode": "sepia"}).status_code == 400


def test_login_page_uses_saved_theme(client):
    client.post("/settings", data={"theme": "paper", "mode": "light"})
    client.post("/logout")
    html = client.get("/login").text
    assert 'data-theme="paper"' in html and 'data-mode="light"' in html


def test_write_button_on_notes(client):
    for url in ["/courses/D413/notes/notebook", "/courses/D413"]:
        html = client.get(url).text
        assert re.search(r'class="button small primary write"[^>]*>\s*<svg', html) and "Write</a>" in html
        assert ">Edit overview<" not in html


def test_vendored_fonts_served(client):
    css = client.get("/static/vendor/fonts/fonts.css").text
    files = re.findall(r"url\(([^)]+)\)", css)
    assert len(files) == 5
    for f in files:
        r = client.get(f"/static/vendor/fonts/{f}")
        assert r.status_code == 200 and r.content[:4] == b"wOF2"


def test_removed_theme_falls_back_to_notebook(client):
    from app import db
    conn = db.connect()
    with conn:
        conn.execute("INSERT INTO meta(key, value) VALUES ('theme', 'nightops') ON CONFLICT(key) DO UPDATE SET value = excluded.value")
    conn.close()
    assert 'data-theme="notebook"' in client.get("/").text
    assert client.post("/settings", data={"theme": "nightops", "mode": "dark"}).status_code == 400


def test_note_tabs_keep_the_chosen_mode(client):
    """A note's ?mode=edit is the editor, not light/dark: it must not replace data-mode on <html>."""
    client.post("/settings", data={"theme": "notebook", "mode": "dark"})
    for url in ("/courses/D413/notes/notebook", "/courses/D413/notes/notebook?mode=edit", "/courses/D413"):
        assert 'data-mode="dark"' in client.get(url).text, url
