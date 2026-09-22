import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import clock, competencies as comp, markdown, quizzes
from ..db import get_db
from ..web import render
from .notes import course_context, known_codes, load

router = APIRouter()


def _course_by_id(conn, course_id):
    return conn.execute("SELECT c.*, t.n AS term_n FROM courses c LEFT JOIN terms t ON t.id = c.term_id WHERE c.id = ?",
                        (course_id,)).fetchone()


def _attempt(conn, attempt_id):
    a = conn.execute("SELECT * FROM quiz_attempts WHERE id = ?", (attempt_id,)).fetchone()
    if not a:
        raise HTTPException(404)
    return a


def quiz_page(request, conn, c, **extra):
    return render(request, "quizzes/index.html", **course_context(conn, c), tab="quizzes",
                  questions=quizzes.bank(conn, c["id"]), history=quizzes.history(conn, c["id"]),
                  comps=comp.list_for(conn, c["id"]), kinds=quizzes.KINDS, **extra)


@router.get("/courses/{code}/quizzes")
def quiz_index(request: Request, code: str, conn=Depends(get_db)):
    return quiz_page(request, conn, load(conn, code))


def _question_form(request, conn, c, q=None, form=None, error=None, status_code=200):
    if form is None:
        form = {"kind": "mc", "prompt": "", "choices": "", "short_answer": "", "explanation": "", "competency_id": ""}
        if q:
            form = {"kind": q["kind"], "prompt": q["prompt"], "choices": quizzes.choices_text(q),
                    "short_answer": json.loads(q["answer_json"]) if q["kind"] == "short" else "",
                    "explanation": q["explanation"], "competency_id": q["competency_id"] or ""}
    return render(request, "quizzes/question.html", **course_context(conn, c), tab="quizzes", q=q, form=form,
                  error=error, comps=comp.list_for(conn, c["id"]), kinds=quizzes.KINDS, status_code=status_code)


@router.get("/courses/{code}/quizzes/questions/new")
def question_new(request: Request, code: str, conn=Depends(get_db)):
    return _question_form(request, conn, load(conn, code))


@router.post("/courses/{code}/quizzes/questions/new")
async def question_create(request: Request, code: str, conn=Depends(get_db)):
    c = load(conn, code)
    form = dict(await request.form())
    try:
        quizzes.save(conn, c["id"], form)
    except quizzes.QuestionError as e:
        return _question_form(request, conn, c, form=form, error=str(e), status_code=400)
    if form.get("another"):
        return RedirectResponse(f"/courses/{c['code']}/quizzes/questions/new?saved=1", status_code=303)
    return RedirectResponse(f"/courses/{c['code']}/quizzes?saved=1", status_code=303)


def _question(conn, c, qid):
    q = conn.execute("SELECT * FROM questions WHERE id = ? AND course_id = ?", (qid, c["id"])).fetchone()
    if not q:
        raise HTTPException(404)
    return q


@router.get("/courses/{code}/quizzes/questions/{qid}")
def question_edit_form(request: Request, code: str, qid: int, conn=Depends(get_db)):
    c = load(conn, code)
    return _question_form(request, conn, c, _question(conn, c, qid))


@router.post("/courses/{code}/quizzes/questions/{qid}")
async def question_update(request: Request, code: str, qid: int, conn=Depends(get_db)):
    c = load(conn, code)
    q = _question(conn, c, qid)
    form = dict(await request.form())
    if form.get("delete") == "yes":
        with conn:
            conn.execute("DELETE FROM questions WHERE id = ?", (qid,))
        return RedirectResponse(f"/courses/{c['code']}/quizzes", status_code=303)
    try:
        quizzes.save(conn, c["id"], form, question_id=qid)
    except quizzes.QuestionError as e:
        return _question_form(request, conn, c, q, form=form, error=str(e), status_code=400)
    return RedirectResponse(f"/courses/{c['code']}/quizzes?saved=1", status_code=303)


@router.post("/courses/{code}/quizzes/start")
async def quiz_start(request: Request, code: str, conn=Depends(get_db)):
    c = load(conn, code)
    form = await request.form()
    try:
        count = max(1, min(200, int(form.get("count") or 10)))
        minutes = int(form.get("minutes") or 0) if form.get("timed") else None
        comp_id = int(form["competency_id"]) if form.get("competency_id") else None
        attempt_id = quizzes.start(conn, c["id"], count, comp_id, shuffle=bool(form.get("shuffle")),
                                   time_limit_min=minutes if minutes and minutes > 0 else None)
    except (ValueError, quizzes.QuestionError) as e:
        return quiz_page(request, conn, c, error=str(e))
    return RedirectResponse(f"/quizzes/{attempt_id}", status_code=303)


@router.get("/quizzes/{attempt_id}")
def attempt_page(request: Request, attempt_id: int, conn=Depends(get_db)):
    a = _attempt(conn, attempt_id)
    c = _course_by_id(conn, a["course_id"])
    qs = quizzes.attempt_questions(conn, a)
    codes = known_codes(conn)
    rendered = {q["id"]: markdown.render(q["prompt"], codes) for q in qs}
    choices = {q["id"]: json.loads(q["choices_json"]) for q in qs}
    answers = {r["question_id"]: r for r in conn.execute("SELECT * FROM quiz_answers WHERE attempt_id = ?", (attempt_id,))}
    ctx = dict(**course_context(conn, c), tab="quizzes", attempt=a, questions=qs, prompts=rendered, choices=choices,
               answers=answers, loads=json.loads)
    if a["finished_at"]:
        return render(request, "quizzes/result.html", **ctx,
                      explanations={q["id"]: markdown.render(q["explanation"], codes) for q in qs})
    if answers:
        return render(request, "quizzes/selfgrade.html", **ctx)
    remaining = None
    if a["time_limit_min"]:
        elapsed = (clock.now() - datetime.fromisoformat(a["started_at"])).total_seconds()
        remaining = max(0, int(a["time_limit_min"] * 60 - elapsed))
    return render(request, "quizzes/run.html", **ctx, remaining=remaining)


@router.post("/quizzes/{attempt_id}/submit")
async def attempt_submit(request: Request, attempt_id: int, conn=Depends(get_db)):
    a = _attempt(conn, attempt_id)
    form = await request.form()
    answers = {}
    for q in quizzes.attempt_questions(conn, a):
        if q["kind"] == "short":
            answers[q["id"]] = [str(form.get(f"q{q['id']}", ""))]
        else:
            answers[q["id"]] = [v for v in form.getlist(f"q{q['id']}") if str(v).isdigit()]
    try:
        quizzes.submit(conn, attempt_id, answers)
    except quizzes.QuestionError:
        pass  # double submit: just show where it stands
    return RedirectResponse(f"/quizzes/{attempt_id}", status_code=303)


@router.post("/quizzes/{attempt_id}/selfgrade")
async def attempt_selfgrade(request: Request, attempt_id: int, conn=Depends(get_db)):
    _attempt(conn, attempt_id)
    form = await request.form()
    marks = {int(k[1:]): v == "right" for k, v in form.items() if k.startswith("q") and k[1:].isdigit() and v in ("right", "wrong")}
    try:
        quizzes.self_grade(conn, attempt_id, marks)
    except quizzes.QuestionError:
        pass
    return RedirectResponse(f"/quizzes/{attempt_id}", status_code=303)
