from datetime import date

import pytest

from app import catalog, clock, notes_fs


@pytest.fixture
def fixed_today(monkeypatch):
    monkeypatch.setattr(clock, "today", lambda: date(2026, 10, 20))
    return date(2026, 10, 20)


def test_status_marks_attempted_and_it_sticks(seeded, fixed_today):
    catalog.set_status(seeded, "D413", "in_progress")
    c = catalog.get_course(seeded, "D413")
    assert c["attempted"] == 1 and c["attempted_on"] == "2026-10-20"
    catalog.set_status(seeded, "D413", "not_started")
    c = catalog.get_course(seeded, "D413")
    assert c["attempted"] == 1  # stays attempted


def test_passed_sets_and_clears_passed_on(seeded, fixed_today):
    catalog.set_status(seeded, "D413", "passed")
    assert catalog.get_course(seeded, "D413")["passed_on"] == "2026-10-20"
    catalog.set_status(seeded, "D413", "revision_needed")
    assert catalog.get_course(seeded, "D413")["passed_on"] is None


def test_edit_keeps_explicit_passed_on(seeded, fixed_today):
    c = dict(catalog.get_course(seeded, "D413"))
    c.update(status="passed", passed_on="2026-10-15")
    catalog.update_course(seeded, "D413", c)
    assert catalog.get_course(seeded, "D413")["passed_on"] == "2026-10-15"


def test_edit_validates(seeded):
    c = dict(catalog.get_course(seeded, "D413"))
    with pytest.raises(catalog.CourseError):
        catalog.update_course(seeded, "D413", {**c, "due": "not-a-date"})
    with pytest.raises(catalog.CourseError):
        catalog.update_course(seeded, "D413", {**c, "cu": "0"})
    with pytest.raises(catalog.CourseError):
        catalog.update_course(seeded, "D413", {**c, "status": "bogus"})


def test_move_course_between_terms(seeded):
    catalog.move_to_term(seeded, "D315", 1)
    assert catalog.get_course(seeded, "D315")["term_n"] == 1
    cu = seeded.execute("SELECT SUM(cu) FROM courses c JOIN terms t ON t.id = c.term_id WHERE t.n = 1").fetchone()[0]
    assert cu == 30


def test_add_course(seeded):
    assert catalog.add_course(seeded, {"code": "c123", "title": "New Thing", "cu": "3", "term_n": 2}) == "C123"
    assert catalog.get_course(seeded, "C123")["term_n"] == 2
    with pytest.raises(catalog.CourseError):
        catalog.add_course(seeded, {"code": "D413", "title": "Dup", "cu": "3"})
    with pytest.raises(catalog.CourseError):
        catalog.add_course(seeded, {"code": "XYZ", "title": "Bad", "cu": "3"})


def test_move_moves_notes_folder(seeded, data_dir):
    notes_fs.ensure_repo()
    old = catalog.get_course(seeded, "D316")
    d = notes_fs.course_dir(old)
    d.mkdir(parents=True)
    (d / "notebook.md").write_text("hello", encoding="utf-8")
    rel = notes_fs.course_rel_dir(old) + "/notebook.md"
    seeded.execute("INSERT INTO notes_index(path, course_id, title, updated_at) VALUES (?, ?, 'notebook', 'x')", (rel, old["id"]))
    seeded.commit()
    catalog.move_to_term(seeded, "D316", 2)
    new = catalog.get_course(seeded, "D316")
    assert not d.exists()
    assert (notes_fs.course_dir(new) / "notebook.md").read_text(encoding="utf-8") == "hello"
    assert seeded.execute("SELECT path FROM notes_index").fetchone()[0] == "term-2/d316-it-foundations/notebook.md"


def test_current_term(seeded):
    assert catalog.current_term(seeded, date(2026, 9, 22))["n"] == 1  # before term 1: next to start
    assert catalog.current_term(seeded, date(2027, 5, 1))["n"] == 2
    assert catalog.current_term(seeded, date(2029, 1, 1))["n"] == 4


def test_timeline_positions(seeded):
    term = catalog.get_term(seeded, 1)
    tl = catalog.timeline(term, catalog.term_courses(seeded, term["id"]), date(2026, 10, 1))
    d413 = tl["rows"][0]
    assert d413["code"] == "D413" and not d413["planned"] and d413["bar"][0] > 0
    assert tl["rows"][4]["planned"]  # D316 has only a target
    assert tl["today"] == 0.0


def test_term_and_course_pages(client):
    r = client.get("/terms/1")
    assert r.status_code == 200 and "D413" in r.text and "Timeline" in r.text
    r = client.post("/courses/D413/edit", data={"title": "Telecomm and Wireless Communications", "cu": "3", "term_n": "1",
                                                "status": "passed", "assessment_type": "OA"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.get("/terms")
    assert "3 of 27 CU target passed" in r.text
    r = client.post("/courses/D413/edit", data={"title": "x", "cu": "3", "term_n": "1", "status": "passed", "start": "13/10/2026"})
    assert r.status_code == 400 and "isn&#39;t a date" in r.text
    assert client.get("/courses/D413").status_code == 200
    assert client.get("/courses/ZZZZ").status_code == 404
    r = client.post("/terms/1/add", data={"mode": "move", "code": "D315"}, follow_redirects=False)
    assert r.status_code == 303
