from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import competencies as comp
from .. import readiness
from ..db import get_db
from ..web import render
from .notes import course_context, load

router = APIRouter()


def page(request, conn, c, **extra):
    return render(request, "courses/competencies.html", **course_context(conn, c), tab="competencies",
                  comps=comp.list_for(conn, c["id"]), ready=readiness.for_course(conn, c["id"]),
                  **extra)


@router.get("/courses/{code}/competencies")
def competencies_page(request: Request, code: str, conn=Depends(get_db)):
    return page(request, conn, load(conn, code))


@router.post("/courses/{code}/competencies/import")
def competencies_import(request: Request, code: str, text: str = Form(""), confirm: str = Form(""),
                        conn=Depends(get_db)):
    c = load(conn, code)
    lines = comp.parse(text)
    if not lines:
        return page(request, conn, c, error="Paste at least one competency, one per line.", pasted=text)
    existing = comp.list_for(conn, c["id"])
    if existing and confirm != "yes":
        keep = len({r["text"].lower() for r in existing} & {t.lower() for t in lines})
        return page(request, conn, c, pasted=text, confirm_replace={
            "existing": len(existing), "new": len(lines), "keep": keep, "lose": len(existing) - keep})
    result = comp.import_list(conn, c, lines)
    return page(request, conn, c, imported=result)


@router.post("/courses/{code}/competencies/{comp_id}/confidence")
def competency_confidence(request: Request, code: str, comp_id: int, value: str = Form(""), conn=Depends(get_db)):
    c = load(conn, code)
    row = conn.execute("SELECT * FROM competencies WHERE id = ? AND course_id = ?", (comp_id, c["id"])).fetchone()
    if not row:
        raise HTTPException(404)
    if value == "reviewed":
        comp.mark_reviewed(conn, comp_id)
    else:
        try:
            comp.set_confidence(conn, comp_id, int(value) if value else None)
        except ValueError:
            raise HTTPException(400, "confidence must be 1–5")
    if request.headers.get("HX-Request"):
        row = conn.execute("SELECT * FROM competencies WHERE id = ?", (comp_id,)).fetchone()
        return render(request, "courses/_competency_row.html", course=c, r=row, anchor=comp.anchor,
                      ready=readiness.for_course(conn, c["id"]))
    return RedirectResponse(f"/courses/{c['code']}/competencies#comp-{comp_id}", status_code=303)
