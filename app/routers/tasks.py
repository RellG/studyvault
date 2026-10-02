"""Study tasks: the global /tasks page, the per-course Tasks tab, and plain-form actions that redirect back."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import catalog, clock, competencies as comp, tasks
from ..db import get_db
from ..web import render
from .notes import course_context, load

router = APIRouter()

BLANK = {"title": "", "kind": "study", "due_on": "", "est_minutes": "", "priority": "2", "done_rule": "manual",
         "competency_id": "", "course": ""}


def _back(form, fallback="/tasks") -> str:
    """Where to go after an action: the page it came from (a local path only)."""
    path = str(form.get("back") or "")
    ok = path.startswith("/") and not path.startswith("//") and not any(ch == "\\" or ord(ch) < 32 for ch in path)
    return path if ok else fallback  # browsers drop tabs and newlines, so a "/" + tab + "/x" back would become "//x"


def _get(conn, task_id):
    t = tasks.get(conn, task_id)
    if not t:
        raise HTTPException(404)
    return t


def _lists(conn, course=None):
    today = clock.today()
    open_rows = tasks.decorate(tasks.list_tasks(conn, course["id"] if course else None, tasks.OPEN), today)
    return {"due_now": [t for t in open_rows if t["due_now"]],
            "later": [t for t in open_rows if t["due_on"] and not t["due_now"]],
            "someday": [t for t in open_rows if not t["due_on"]],
            "closed": tasks.decorate(tasks.closed(conn, course["id"] if course else None), today)}


def _ctx(conn, course=None, **extra):
    return {"form": dict(BLANK, course=course["code"] if course else ""), "error": None, **_lists(conn, course), **extra}


def _page(request, conn, course=None, status_code=200, **extra):
    courses = conn.execute("SELECT id, code, title FROM courses ORDER BY status = 'passed', ord, code").fetchall()
    if course:
        return render(request, "tasks/course.html", **course_context(conn, course), tab="tasks", courses=courses,
                      comps=comp.list_for(conn, course["id"]), **_ctx(conn, course, **extra), status_code=status_code)
    return render(request, "tasks/index.html", courses=courses, comps=[], **_ctx(conn, **extra), status_code=status_code)


@router.get("/tasks")
def tasks_page(request: Request, conn=Depends(get_db)):
    return _page(request, conn)


@router.get("/courses/{code}/tasks")
def course_tasks(request: Request, code: str, conn=Depends(get_db)):
    return _page(request, conn, load(conn, code))


@router.post("/tasks")
async def task_create(request: Request, conn=Depends(get_db)):
    form = dict(await request.form())
    c = catalog.get_course(conn, form.get("course", "")) if form.get("course") else None
    try:
        tasks.add(conn, form.get("title", ""), course_id=c["id"] if c else None,
                  competency_id=form.get("competency_id") or None, kind=form.get("kind", "study"),
                  due_on=form.get("due_on"), est_minutes=form.get("est_minutes"), priority=form.get("priority") or 2,
                  done_rule=form.get("done_rule", "manual"))
    except tasks.TaskError as e:
        on_course = c if c and str(form.get("back", "")).startswith("/courses/") else None
        return _page(request, conn, on_course, status_code=400, error=str(e), form={**BLANK, **form})
    return RedirectResponse(_back(form), status_code=303)


@router.get("/tasks/{task_id}/edit")
def task_edit_form(request: Request, task_id: int, conn=Depends(get_db)):
    t = _get(conn, task_id)
    return render(request, "tasks/edit.html", t=t, error=None, back=request.query_params.get("back", ""), **_edit_ctx(conn, t))


def _edit_ctx(conn, t):
    courses = conn.execute("SELECT id, code, title FROM courses ORDER BY status = 'passed', ord, code").fetchall()
    return {"courses": courses, "comps": comp.list_for(conn, t["course_id"]) if t["course_id"] else []}


@router.post("/tasks/{task_id}/edit")
async def task_edit(request: Request, task_id: int, conn=Depends(get_db)):
    t = _get(conn, task_id)
    form = dict(await request.form())
    course_id = form.get("course_id") or None
    same_course = str(t["course_id"] or "") == str(course_id or "")  # a competency belongs to the old course only
    try:
        tasks.update(conn, task_id, title=form.get("title", ""), course_id=course_id,
                     competency_id=(form.get("competency_id") or None) if same_course else None, note=form.get("note", ""),
                     kind=form.get("kind", t["kind"]), due_on=form.get("due_on"), est_minutes=form.get("est_minutes"),
                     priority=form.get("priority") or 2, done_rule=form.get("done_rule", t["done_rule"]),
                     status=form.get("status", t["status"]))
    except tasks.TaskError as e:
        return render(request, "tasks/edit.html", t={**dict(t), **form}, error=str(e), back=form.get("back", ""),
                      status_code=400, **_edit_ctx(conn, t))
    return RedirectResponse(_back(form), status_code=303)


def _act(fn):
    async def route(request: Request, task_id: int, conn=Depends(get_db)):
        _get(conn, task_id)
        form = await request.form()
        try:
            fn(conn, task_id, form)
        except tasks.TaskError as e:
            raise HTTPException(400, str(e))
        return RedirectResponse(_back(form), status_code=303)
    return route


router.post("/tasks/{task_id}/done")(_act(lambda conn, i, f: tasks.complete(conn, i)))
router.post("/tasks/{task_id}/cancel")(_act(lambda conn, i, f: tasks.set_status(conn, i, "cancelled")))
router.post("/tasks/{task_id}/reopen")(_act(lambda conn, i, f: tasks.set_status(conn, i, "todo")))
router.post("/tasks/{task_id}/status")(_act(lambda conn, i, f: tasks.set_status(conn, i, f.get("status", ""))))
router.post("/tasks/{task_id}/postpone")(_act(lambda conn, i, f: tasks.postpone(conn, i, f.get("due_on"), f.get("days"))))
