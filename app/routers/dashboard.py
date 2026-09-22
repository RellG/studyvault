from fastapi import APIRouter, Depends, Request

from .. import assessments, cards, catalog, clock, progress, readiness
from ..db import get_db
from ..web import render

router = APIRouter()

ACTIVE = ("in_progress", "pre_assessed", "scheduled", "revision_needed")


def next_up(conn, term):
    """The course to work on now: an active one in the current term, else the next not-passed one."""
    if not term:
        return None
    courses = [c for c in catalog.term_courses(conn, term["id"]) if c["status"] != "passed"]
    active = [c for c in courses if c["status"] in ACTIVE]
    c = (active or courses or [None])[0]
    if not c:
        return None
    return {"course": c, "ready": readiness.for_course(conn, c["id"]), "due": cards.due_count(conn, c["id"])}


def upcoming_exams(conn, today):
    t = today.isoformat()
    rows = [dict(r, kind="course") for r in conn.execute(
        "SELECT code, title, exam_date FROM courses WHERE exam_date >= ? AND status != 'passed'", (t,))]
    rows += [dict(r, kind="cert") for r in conn.execute(
        "SELECT certs.name AS title, courses.code, certs.exam_date FROM certs LEFT JOIN courses ON courses.id = certs.course_id "
        "WHERE certs.exam_date >= ? AND certs.result = 'pending'", (t,))]
    for r in rows:
        r["days"] = (clock.parse_date(r["exam_date"]) - today).days
    return sorted(rows, key=lambda r: r["exam_date"])[:6]


@router.get("/")
def dashboard(request: Request, conn=Depends(get_db)):
    today = clock.today()
    p = progress.load(conn, today)
    program = conn.execute("SELECT * FROM program").fetchone()
    return render(request, "dashboard.html", program=program, p=p, next=next_up(conn, p["term"]),
                  exams=upcoming_exams(conn, today), due_all=cards.due_count(conn),
                  certs_earned=assessments.certs_earned(conn), grad_target=progress.GRAD_TARGET)
