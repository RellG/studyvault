from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import catalog
from ..db import get_db
from ..web import render

router = APIRouter()


def load(conn, code):
    c = catalog.get_course(conn, code)
    if not c:
        raise HTTPException(404, f"No course {code}")
    return c


@router.get("/courses/{code}/edit")
def edit_form(request: Request, code: str, conn=Depends(get_db)):
    c = load(conn, code)
    return render(request, "courses/edit.html", course=c, form=dict(c), terms=catalog.list_terms(conn),
                  statuses=catalog.STATUSES, types=catalog.ASSESSMENT_TYPES, error=None)


@router.post("/courses/{code}/edit")
async def edit(request: Request, code: str, conn=Depends(get_db)):
    c = load(conn, code)
    form = dict(await request.form())
    try:
        catalog.update_course(conn, code, form)
    except catalog.CourseError as e:
        return render(request, "courses/edit.html", course=c, form=form, terms=catalog.list_terms(conn),
                      statuses=catalog.STATUSES, types=catalog.ASSESSMENT_TYPES, error=str(e), status_code=400)
    return RedirectResponse(f"/courses/{c['code']}", status_code=303)


@router.post("/courses/{code}/status")
async def quick_status(request: Request, code: str, conn=Depends(get_db)):
    c = load(conn, code)
    form = await request.form()
    try:
        catalog.set_status(conn, code, form.get("status", ""))
    except catalog.CourseError as e:
        raise HTTPException(400, str(e))
    return RedirectResponse(request.headers.get("referer") or f"/courses/{c['code']}", status_code=303)
