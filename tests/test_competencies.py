from app import catalog, competencies as comp, notes_fs


def test_parse_strips_bullets_numbers_and_dupes():
    text = """
    • Explains wireless standards
    - Configures VLANs
    1. Troubleshoots   RF interference
    2) Explains wireless standards

    a. Describes 5G core
    4064.1.1 : Keeps WGU numbering
    """
    assert comp.parse(text) == [
        "Explains wireless standards", "Configures VLANs", "Troubleshoots RF interference",
        "Describes 5G core", "4064.1.1 : Keeps WGU numbering",
    ]


def test_import_creates_rows_and_sections(seeded):
    notes_fs.ensure_repo()
    c = catalog.get_course(seeded, "D413")
    r = comp.import_list(seeded, c, ["Explains wireless standards", "Configures VLANs"])
    assert r == {"kept": 0, "added": 2, "removed": 0, "sections": 2}
    text, _ = notes_fs.read_note(c, "competencies")
    assert "## Explains wireless standards\n" in text and "## Configures VLANs\n" in text


def test_reimport_keeps_ratings_and_notes(seeded):
    notes_fs.ensure_repo()
    c = catalog.get_course(seeded, "D413")
    comp.import_list(seeded, c, ["A thing", "B thing"])
    a = comp.list_for(seeded, c["id"])[0]
    comp.set_confidence(seeded, a["id"], 4)
    notes_fs.append_note(seeded, c, "competencies", "my precious notes under B\n")
    r = comp.import_list(seeded, c, ["C thing", "A thing"])
    assert r == {"kept": 1, "added": 1, "removed": 1, "sections": 1}
    rows = comp.list_for(seeded, c["id"])
    assert [x["text"] for x in rows] == ["C thing", "A thing"]
    assert rows[1]["confidence"] == 4 and rows[1]["id"] == a["id"]
    text, _ = notes_fs.read_note(c, "competencies")
    assert "my precious notes under B" in text and "## B thing" in text  # never deletes notes
    assert text.count("## A thing") == 1


def test_weakest_order(seeded):
    c = catalog.get_course(seeded, "D413")
    with seeded:
        for i, (t, conf, rev) in enumerate([("high", 5, "2026-10-01"), ("unrated", None, None), ("low-old", 2, "2026-09-01"),
                                            ("low-new", 2, "2026-10-10"), ("mid", 3, "2026-10-01")]):
            seeded.execute("INSERT INTO competencies(course_id, ord, text, confidence, last_reviewed) VALUES (?, ?, ?, ?, ?)",
                           (c["id"], i, t, conf, rev))
    assert [r["text"] for r in comp.weakest(seeded, c["id"])] == ["unrated", "low-old", "low-new"]


def test_competency_pages(client):
    r = client.post("/courses/D413/competencies/import", data={"text": "One\nTwo\nThree"})
    assert r.status_code == 200 and "3 new" in r.text
    r = client.post("/courses/D413/competencies/import", data={"text": "One\nFour"})
    assert "Yes, replace" in r.text and "2 will be removed" in r.text
    r = client.post("/courses/D413/competencies/import", data={"text": "One\nFour", "confirm": "yes"})
    assert "1 kept" in r.text
    page = client.get("/courses/D413/competencies").text
    import re
    cid = re.search(r'id="comp-(\d+)"', page).group(1)
    r = client.post(f"/courses/D413/competencies/{cid}/confidence", data={"value": "3"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and 'hx-swap-oob="true"' in r.text and "60" in r.text  # readiness = 3/5
    assert client.post(f"/courses/D413/competencies/{cid}/confidence", data={"value": "9"}).status_code == 400
    assert "Weakest:" in client.get("/courses/D413").text
