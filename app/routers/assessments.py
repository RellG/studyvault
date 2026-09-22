from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import assessments as asm
from .. import catalog, competencies as comp, notes_fs, readiness
from ..db import get_db
from ..web import render
from .notes import course_context, load

router = APIRouter()


def assessment_page(request, conn, c, status_code=200, **extra):
    c = catalog.get_course(conn, c["code"])  # fresh after status changes
    ctx = dict(**course_context(conn, c), tab="assessment", ready=readiness.for_course(conn, c["id"]),
               comps=comp.list_for(conn, c["id"]), levels=asm.LEVELS, integrity=asm.INTEGRITY, **extra)
    if c["assessment_type"] in ("OA", "cert"):
        ctx.update(checklist=asm.checklist(conn, c), pres=asm.preassessments(conn, c["id"]))
    if c["assessment_type"] == "PA":
        subs = asm.submissions(conn, c["id"])
        n_rev = sum(1 for s in subs if s["result"] == "revision")
        latest = asm.drafts(c)
        ctx.update(tasks=conn.execute("SELECT * FROM pa_tasks WHERE course_id = ? ORDER BY ord", (c["id"],)).fetchall(),
                   drafts=latest, subs=subs, n_revisions=n_rev, warning=asm.revision_warning(c, n_rev),
                   latest_text=notes_fs.read_note(c, latest[-1]["name"])[0] if latest else "",
                   task_statuses=asm.PA_TASK_STATUSES)
    if c["cert_name"] or c["assessment_type"] == "cert":
        ctx["certs"] = conn.execute("SELECT * FROM certs WHERE course_id = ?", (c["id"],)).fetchall()
        ctx.update(voucher_statuses=asm.VOUCHER_STATUSES, cert_results=asm.CERT_RESULTS)
    return render(request, "assessment/page.html", status_code=status_code, **ctx)


@router.get("/courses/{code}/assessment")
def page(request: Request, code: str, conn=Depends(get_db)):
    return assessment_page(request, conn, load(conn, code))


def _done(c, anchor=""):
    return RedirectResponse(f"/courses/{c['code']}/assessment?ok=1{anchor}", status_code=303)


@router.post("/courses/{code}/assessment/type")
async def set_type(request: Request, code: str, conn=Depends(get_db)):
    c = load(conn, code)
    form = await request.form()
    data = {k: c[k] for k in c.keys()}
    data.update(assessment_type=form.get("assessment_type") or "", term_n=c["term_n"], approved=c["approved"])
    try:
        catalog.update_course(conn, code, data)
    except catalog.CourseError as e:
        raise HTTPException(400, str(e))
    notes_fs.ensure_course_files(conn, catalog.get_course(conn, code))
    return _done(c)


@router.post("/courses/{code}/assessment/preassessment")
async def preassessment(request: Request, code: str, conn=Depends(get_db)):
    c = load(conn, code)
    form = await request.form()
    levels = {k[6:]: v for k, v in form.items() if k.startswith("level_") and k[6:].isdigit()}
    try:
        asm.log_preassessment(conn, c, form.get("taken_on", ""), form.get("score", ""), bool(form.get("passed")),
                              levels, form.get("notes", ""), bool(form.get("apply_confidence")))
    except asm.AssessmentError as e:
        return assessment_page(request, conn, c, error=str(e), status_code=400)
    return _done(c, "#pre")


@router.post("/courses/{code}/assessment/exam")
async def exam(request: Request, code: str, conn=Depends(get_db)):
    c = load(conn, code)
    form = await request.form()
    try:
        asm.schedule_exam(conn, c, form.get("exam_date", ""), form.get("quiz_target"))
    except asm.AssessmentError as e:
        return assessment_page(request, conn, c, error=str(e), status_code=400)
    return _done(c, "#exam")


@router.post("/courses/{code}/assessment/rubric")
async def rubric(request: Request, code: str, conn=Depends(get_db)):
    c = load(conn, code)
    form = await request.form()
    try:
        asm.import_rubric(conn, c, form.get("text", ""))
    except asm.AssessmentError as e:
        return assessment_page(request, conn, c, error=str(e), status_code=400)
    return _done(c, "#tasks")


@router.post("/courses/{code}/assessment/tasks/{task_id}")
async def task(request: Request, code: str, task_id: int, conn=Depends(get_db)):
    c = load(conn, code)
    form = await request.form()
    try:
        asm.set_task_status(conn, c, task_id, form.get("status", ""))
    except asm.AssessmentError as e:
        raise HTTPException(400, str(e))
    return _done(c, "#tasks")


@router.post("/courses/{code}/assessment/drafts")
async def draft(request: Request, code: str, conn=Depends(get_db)):
    c = load(conn, code)
    form = await request.form()
    try:
        asm.save_draft(conn, c, str(form.get("text", "")))
    except asm.AssessmentError as e:
        return assessment_page(request, conn, c, error=str(e), status_code=400)
    return _done(c, "#drafts")


@router.get("/courses/{code}/assessment/diff")
def diff(request: Request, code: str, a: str, b: str, conn=Depends(get_db)):
    c = load(conn, code)
    names = {d["name"] for d in asm.drafts(c)}
    if a not in names or b not in names:
        raise HTTPException(404)
    return render(request, "assessment/diff.html", **course_context(conn, c), tab="assessment",
                  table=asm.diff_html(c, a, b), a=a, b=b, drafts=asm.drafts(c))


@router.post("/courses/{code}/assessment/submissions")
async def submission(request: Request, code: str, conn=Depends(get_db)):
    c = load(conn, code)
    form = await request.form()
    try:
        asm.log_submission(conn, c, form.get("version", ""), form.get("submitted_on", ""), form.get("result", "pending"),
                           form.get("feedback", ""))
    except asm.AssessmentError as e:
        return assessment_page(request, conn, c, error=str(e), status_code=400)
    return _done(c, "#submissions")


@router.post("/courses/{code}/assessment/submissions/{sub_id}")
async def submission_update(request: Request, code: str, sub_id: int, conn=Depends(get_db)):
    c = load(conn, code)
    form = await request.form()
    try:
        asm.update_submission(conn, c, sub_id, form.get("result", ""), form.get("feedback", ""))
    except asm.AssessmentError as e:
        raise HTTPException(400, str(e))
    return _done(c, "#submissions")


# ---------------------------------------------------------------- certs

@router.get("/certs")
def certs_page(request: Request, conn=Depends(get_db), error: str | None = None):
    courses = conn.execute("SELECT code, title FROM courses ORDER BY ord").fetchall()
    return render(request, "assessment/certs.html", certs=asm.list_certs(conn), courses=courses, error=error,
                  voucher_statuses=asm.VOUCHER_STATUSES, cert_results=asm.CERT_RESULTS, earned=asm.certs_earned(conn))


@router.post("/certs")
async def cert_create(request: Request, conn=Depends(get_db)):
    form = dict(await request.form())
    try:
        asm.save_cert(conn, form)
    except asm.AssessmentError as e:
        return certs_page(request, conn, error=str(e))
    return RedirectResponse("/certs?ok=1", status_code=303)


@router.post("/certs/{cert_id}")
async def cert_update(request: Request, cert_id: int, conn=Depends(get_db)):
    form = dict(await request.form())
    if not conn.execute("SELECT 1 FROM certs WHERE id = ?", (cert_id,)).fetchone():
        raise HTTPException(404)
    if form.get("delete") == "yes":
        with conn:
            conn.execute("DELETE FROM certs WHERE id = ?", (cert_id,))
        return RedirectResponse("/certs", status_code=303)
    try:
        asm.save_cert(conn, form, cert_id)
    except asm.AssessmentError as e:
        return certs_page(request, conn, error=str(e))
    back = form.get("back") or "/certs?ok=1"
    return RedirectResponse(back if back.startswith("/") and not back.startswith("//") else "/certs", status_code=303)
