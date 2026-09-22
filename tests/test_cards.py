from datetime import date

import pytest

from app import cards, catalog, clock, competencies as comp, notes_fs


def test_parse_all_formats():
    text = """## Ports
Q: Which port does SSH use?
A: TCP 22

Q: What does DHCP do?
A: Hands out addresses.
Also gateway and DNS.

- Q: Telnet port? / A: 23
HTTPS :: TCP 443
{{c1::802.11ax}} is marketed as {{c2::Wi-Fi 6::marketing name}}.
just a sentence with no card
"""
    got, skipped = cards.parse(text)
    fronts = [c["front"] for c in got]
    assert fronts == ["Which port does SSH use?", "What does DHCP do?", "Telnet port?", "HTTPS",
                      "[…] is marketed as Wi-Fi 6.", "802.11ax is marketed as [marketing name]."]
    assert got[1]["back"] == "Hands out addresses.\nAlso gateway and DNS."
    assert got[2]["back"] == "23"
    assert got[4] == {"front": "[…] is marketed as Wi-Fi 6.", "back": "**802.11ax** is marketed as Wi-Fi 6.", "type": "cloze"}
    assert got[5]["back"] == "802.11ax is marketed as **Wi-Fi 6**."
    assert skipped == ["just a sentence with no card"]


def test_parse_question_without_answer_is_reported():
    got, skipped = cards.parse("Q: orphan\n\nQ: ok\nA: yes")
    assert [c["front"] for c in got] == ["ok"]
    assert skipped == ["Q: orphan (no answer)"]


def test_parse_keeps_code_in_answers():
    got, _ = cards.parse("Q: Show routes?\nA:\n```\nshow ip route\n```\n")
    assert got[0]["back"] == "```\nshow ip route\n```"


def test_sections():
    s = cards.sections("# Title\nintro\n## One\na\n## Two\nb\n")
    assert s == {"": "# Title\nintro", "One": "a", "Two": "b"}


@pytest.fixture
def d413(seeded, monkeypatch):
    monkeypatch.setattr(clock, "today", lambda: date(2026, 10, 5))
    notes_fs.ensure_repo()
    return catalog.get_course(seeded, "D413")


def test_import_dedupes(seeded, d413):
    parsed, _ = cards.parse("A :: 1\nB :: 2")
    assert cards.import_cards(seeded, d413["id"], parsed) == (2, 0)
    parsed, _ = cards.parse("A :: 1\nC :: 3")
    assert cards.import_cards(seeded, d413["id"], parsed) == (1, 1)


def test_review_loop(seeded, d413):
    cid = cards.add(seeded, d413["id"], "front", "back")
    assert cards.due_count(seeded) == 1 and cards.due_count(seeded, d413["id"]) == 1
    assert cards.next_due(seeded)["id"] == cid
    cards.grade(seeded, cid, 3)
    row = seeded.execute("SELECT * FROM cards WHERE id = ?", (cid,)).fetchone()
    assert (row["reps"], row["interval"], row["due_on"]) == (1, 1, "2026-10-06")
    assert cards.due_count(seeded) == 0 and cards.next_due(seeded) is None
    assert seeded.execute("SELECT grade FROM reviews WHERE card_id = ?", (cid,)).fetchone()[0] == 3


def test_import_from_notes_section_tags_competency(client, data_dir):
    client.post("/courses/D413/competencies/import", data={"text": "Explains wireless standards"})
    client.get("/courses/D413/notes/competencies")  # ensure files
    import re
    page = client.get("/courses/D413/notes/competencies?mode=edit").text
    base = re.search(r'var base = "([0-9a-f]+)"', page).group(1)
    note = "# D413\n\n## Explains wireless standards\n\nQ: 802.11ax band?\nA: 2.4, 5 and 6 GHz\n\nWPA3 :: SAE handshake\n"
    assert client.post("/courses/D413/notes/competencies", json={"content": note, "base": base}).status_code == 200
    r = client.post("/courses/D413/cards/import", data={"source": "notes", "section": "competencies|Explains wireless standards"})
    assert "Imported 2 cards" in r.text
    from app import db
    conn = db.connect()
    rows = conn.execute("SELECT c.front, k.text FROM cards c JOIN competencies k ON k.id = c.competency_id").fetchall()
    conn.close()
    assert sorted(r[0] for r in rows) == ["802.11ax band?", "WPA3"] and rows[0][1] == "Explains wireless standards"
    # same day review through the UI
    r = client.get("/review?course=D413")
    assert "2 left" in r.text
    card_id = re.search(r'action="/review/(\d+)"', r.text).group(1)
    r = client.post(f"/review/{card_id}", data={"grade": "3", "course": "D413"}, follow_redirects=True)
    assert "1 left" in r.text and "1 done today" in r.text
    assert client.post(f"/review/{card_id}", data={"grade": "9"}).status_code == 400


def test_card_pages(client):
    assert client.post("/courses/D413/cards", data={"front": "f", "back": ""}).status_code == 200  # error re-render
    r = client.post("/courses/D413/cards", data={"front": "What is RSSI?", "back": "Signal strength"}, follow_redirects=True)
    assert "Card added" in r.text and "What is RSSI?" in r.text
    import re
    cid = re.search(r'id="card-(\d+)"', r.text).group(1)
    assert client.post(f"/cards/{cid}/edit", data={"front": "RSSI?", "back": "dBm"}, follow_redirects=False).status_code == 303
    assert "RSSI?" in client.get(f"/cards/{cid}/edit").text
    assert "Review" in client.get("/").text
    assert client.post(f"/cards/{cid}/delete", follow_redirects=False).status_code == 303
    assert client.get(f"/cards/{cid}/edit").status_code == 404
