from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import catalog, readiness
from ..db import get_db
from ..web import render

router = APIRouter()


@router.get("/terms")
def term_list(request: Request, conn=Depends(get_db)):
    terms = conn.execute(
        """SELECT t.*, COUNT(c.id) AS n_courses, COALESCE(SUM(c.cu), 0) AS planned_cu,
                  COALESCE(SUM(CASE WHEN c.status = 'passed' THEN c.cu END), 0) AS passed_cu
           FROM terms t LEFT JOIN courses c ON c.term_id = t.id GROUP BY t.id ORDER BY t.n""").fetchall()
    unassigned = catalog.term_courses(conn, None)
    current = catalog.current_term(conn)
    return render(request, "terms/list.html", terms=terms, unassigned=unassigned, current=current)


@router.get("/terms/{n}")
def term_detail(request: Request, n: int, conn=Depends(get_db), error: str | None = None):
    term = catalog.get_term(conn, n)
    if not term:
        raise HTTPException(404)
    courses = catalog.term_courses(conn, term["id"])
    others = conn.execute(
        "SELECT c.code, c.title, c.cu, t.n AS term_n FROM courses c LEFT JOIN terms t ON t.id = c.term_id "
        "WHERE c.term_id IS NOT ? AND c.status != 'passed' ORDER BY t.n, c.ord", (term["id"],)).fetchall()
    ready = {c["code"]: readiness.for_course(conn, c["id"]) for c in courses}
    stats = {
        "planned": sum(c["cu"] for c in courses),
        "passed": sum(c["cu"] for c in courses if c["status"] == "passed"),
    }
    return render(request, "terms/detail.html", term=term, courses=courses, others=others, stats=stats,
                  tl=catalog.timeline(term, courses), ready=ready, error=error,
                  terms=catalog.list_terms(conn))


@router.post("/terms/{n}/add")
async def term_add(request: Request, n: int, conn=Depends(get_db)):
    form = dict(await request.form())
    try:
        if form.get("mode") == "new":
            catalog.add_course(conn, {**form, "term_n": n})
        else:
            catalog.move_to_term(conn, form.get("code", ""), n)
    except catalog.CourseError as e:
        return term_detail(request, n, conn, error=str(e))
    return RedirectResponse(f"/terms/{n}", status_code=303)
