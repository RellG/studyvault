import re
from datetime import date

import pytest

from app import assessments as asm
from app import catalog, clock, competencies as comp, notes_fs, readiness


@pytest.fixture
def today(monkeypatch):
    monkeypatch.setattr(clock, "today", lambda: date(2026, 10, 12))
    return date(2026, 10, 12)


@pytest.fixture
def d413(seeded, today):
    notes_fs.ensure_repo()
    c = catalog.get_course(seeded, "D413")
    comp.import_list(seeded, c, ["Wireless standards", "RF basics", "Cellular"])
    return catalog.get_course(seeded, "D413")


def test_preassessment_sets_confidence_status_and_changes_readiness(seeded, d413):
    assert readiness.for_course(seeded, d413["id"])["score"] is None
    ids = [r["id"] for r in comp.list_for(seeded, d413["id"])]
    asm.log_preassessment(seeded, d413, "2026-10-10", "72.5", False,
                          {ids[0]: "competent", ids[1]: "approaching", ids[2]: "bogus"}, "RF math", True)
    rows = {r["id"]: r for r in comp.list_for(seeded, d413["id"])}
    assert rows[ids[0]]["confidence"] == 4 and rows[ids[1]]["confidence"] == 2 and rows[ids[2]]["confidence"] is None
    assert readiness.for_course(seeded, d413["id"])["score"] == 60  # mean(4, 2)/5
    c = catalog.get_course(seeded, "D413")
    assert c["status"] == "pre_assessed" and c["attempted"] == 1
    pre = asm.preassessments(seeded, d413["id"])[0]
    assert pre["score"] == 72.5 and pre["levels"] == {ids[0]: "competent", ids[1]: "approaching"} and pre["notes"] == "RF math"


def test_preassessment_validation(seeded, d413):
    for args in [("", "50"), ("2026-10-10", "abc"), ("2026-10-10", "150")]:
        with pytest.raises(asm.AssessmentError):
            asm.log_preassessment(seeded, d413, args[0], args[1], False, {}, "", False)


def test_checklist(seeded, d413):
    items = {i["label"][:12]: i["ok"] for i in asm.checklist(seeded, d413)}
    assert not any(items.values())
    asm.log_preassessment(seeded, d413, "2026-10-10", "85", True, {}, "", False)
    for r in comp.list_for(seeded, d413["id"]):
        comp.set_confidence(seeded, r["id"], 3)  # marks reviewed today
    with seeded:
        seeded.execute("INSERT INTO cards(course_id, front, back, due_on, created_at) VALUES (?, 'f', 'b', '2026-10-20', 'x')", (d413["id"],))
        seeded.execute("INSERT INTO quiz_attempts(course_id, started_at, finished_at, score, total, question_ids_json) "
                       "VALUES (?, 'a', '2026-10-11T09:00', 8, 10, '[]')", (d413["id"],))
    assert all(i["ok"] for i in asm.checklist(seeded, d413))


def test_schedule_exam(seeded, d413):
    asm.schedule_exam(seeded, d413, "2026-10-16", "85")
    c = catalog.get_course(seeded, "D413")
    assert (c["exam_date"], c["quiz_target"], c["status"]) == ("2026-10-16", 85, "scheduled")
    with pytest.raises(asm.AssessmentError):
        asm.schedule_exam(seeded, c, "soon")


@pytest.fixture
def d339(seeded, today):
    notes_fs.ensure_repo()
    c = catalog.get_course(seeded, "D339")
    data = dict(c)
    data.update(assessment_type="PA", term_n=c["term_n"])
    catalog.update_course(seeded, "D339", data)
    return catalog.get_course(seeded, "D339")


def test_pa_drafts_and_diff(seeded, d339):
    asm.import_rubric(seeded, d339, "- Audience analysis\n- Clear structure")
    tasks = seeded.execute("SELECT * FROM pa_tasks WHERE course_id = ? ORDER BY ord", (d339["id"],)).fetchall()
    assert [t["rubric_text"] for t in tasks] == ["Audience analysis", "Clear structure"]
    asm.set_task_status(seeded, d339, tasks[0]["id"], "done")
    with pytest.raises(asm.AssessmentError):
        asm.set_task_status(seeded, d339, tasks[0]["id"], "nope")
    assert asm.save_draft(seeded, d339, "Intro\nBody one\n")["version"] == "v1"
    assert asm.save_draft(seeded, d339, "Intro\nBody two\nConclusion\n")["version"] == "v2"
    d = asm.drafts(d339)
    assert [x["name"] for x in d] == ["pa/draft-v1-2026-10-12", "pa/draft-v2-2026-10-12"]
    html = asm.diff_html(d339, d[0]["name"], d[1]["name"])
    assert "Conclusion" in html and "diff_add" in html
    assert notes_fs.read_note(d339, d[0]["name"])[0] == "Intro\nBody one\n"  # earlier versions untouched


def test_pa_submissions_status_and_warning(seeded, d339, today):
    asm.log_submission(seeded, d339, "v1", "2026-10-12", "revision", "Needs sources")
    assert catalog.get_course(seeded, "D339")["status"] == "revision_needed"
    c = catalog.get_course(seeded, "D339")
    # term 1 ends 2027-03-31: far away, no warning
    assert asm.revision_warning(c, 1, today) is None
    # 3 revisions so far, 4 more weeks from 2027-03-10 → 2027-04-07 > term end
    w = asm.revision_warning(c, 3, date(2027, 3, 10))
    assert w == {"projected": "2027-04-07", "term_end": "2027-03-31", "cycles": 4}
    assert asm.revision_warning(c, 0, date(2027, 3, 24)) is None       # 3/31 is not after 3/31
    assert asm.revision_warning(c, 0, date(2027, 3, 25)) is not None
    sub = asm.submissions(seeded, d339["id"])[0]
    asm.update_submission(seeded, d339, sub["id"], "passed", "Great")
    c = catalog.get_course(seeded, "D339")
    assert c["status"] == "passed" and c["passed_on"] == "2026-10-12"


def test_certs(seeded, today):
    certs = asm.list_certs(seeded)
    assert len(certs) == 7 and all(c["code"] for c in certs)  # example seed: in-program certs only
    ccp = next(c for c in certs if c["code"] == "D282")
    asm.save_cert(seeded, {"name": ccp["name"], "course_code": "D282", "voucher_status": "received",
                           "voucher_expires": "2027-01-31", "exam_date": "2026-11-05", "result": "passed", "cert_id": "ABC123"}, ccp["id"])
    assert asm.certs_earned(seeded) == 1
    new = asm.save_cert(seeded, {"name": "Terraform Associate", "voucher_status": "none", "result": "pending"})
    assert seeded.execute("SELECT course_id FROM certs WHERE id = ?", (new,)).fetchone()[0] is None
    with pytest.raises(asm.AssessmentError):
        asm.save_cert(seeded, {"name": ""})


def test_assessment_pages(client):
    # OA on D413
    assert client.post("/courses/D413/assessment/type", data={"assessment_type": "OA"}, follow_redirects=False).status_code == 303
    client.post("/courses/D413/competencies/import", data={"text": "A\nB"})
    page = client.get("/courses/D413/assessment").text
    assert "Readiness checklist" in page and "Log a pre-assessment" in page
    lvl = re.findall(r'name="level_(\d+)"', page)
    r = client.post("/courses/D413/assessment/preassessment",
                    data={"taken_on": "2026-10-10", "score": "80", "passed": "1", f"level_{lvl[0]}": "exemplary",
                          "apply_confidence": "1"}, follow_redirects=True)
    assert "Exemplary" in r.text and "Pre-assessed" in r.text
    r = client.post("/courses/D413/assessment/exam", data={"exam_date": "2026-10-16", "quiz_target": "80"}, follow_redirects=True)
    assert "Scheduled" in r.text and "Exam Oct 16, 2026" in r.text
    # PA on D339
    client.post("/courses/D339/assessment/type", data={"assessment_type": "PA"})
    page = client.get("/courses/D339/assessment").text
    assert asm.INTEGRITY.replace("'", "&#39;") in page and "PA workspace" in page
    client.post("/courses/D339/assessment/drafts", data={"text": "one"})
    client.post("/courses/D339/assessment/drafts", data={"text": "one\ntwo"})
    page = client.get("/courses/D339/assessment").text
    names = re.findall(r'<option value="(pa/draft-v\d-[\d-]+)"', page)
    assert client.get(f"/courses/D339/assessment/diff?a={names[0]}&b={names[1]}").status_code == 200
    assert client.get("/courses/D339/assessment/diff?a=pa/x&b=pa/y").status_code == 404
    # cert on D282 + certs page
    client.post("/courses/D282/assessment/type", data={"assessment_type": "cert"})
    page = client.get("/courses/D282/assessment").text
    assert "AWS Certified Cloud Practitioner" in page and "Readiness checklist" in page
    r = client.get("/certs")
    assert r.status_code == 200 and "LPI Linux Essentials" in r.text and "0 earned" in r.text
