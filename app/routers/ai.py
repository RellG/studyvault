"""AI assist pages. Hidden entirely (404) unless AI_PROVIDER, AI_MODEL and the key are set.
Everything generated is a draft: nothing is saved until the user accepts it, and only the rows they keep."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import ai, ai_actions, assessments, cards, clock, competencies as comp, markdown, notes_fs, quizzes
from ..db import get_db
from ..web import render
from .notes import course_context, known_codes, load

router = APIRouter()

ACTIONS = {"cards": "Notes → flashcards", "questions": "Notes → practice questions",
           "explain": "Explain differently", "gap": "Gap check"}


def _require_ai():
    if not ai.enabled():
        raise HTTPException(404)


def _page(request, conn, c, text="", section="", error=None, status_code=200):
    return render(request, "ai/index.html", **course_context(conn, c), tab="ai", sections=cards.notes_sections(c),
                  comps=comp.list_for(conn, c["id"]), text=text, section=section, error=error, actions=ACTIONS,
                  provider=ai.describe(), integrity=assessments.INTEGRITY, max_chars=ai_actions.MAX_INPUT_CHARS,
                  status_code=status_code)


@router.get("/courses/{code}/ai")
def ai_page(request: Request, code: str, section: str = "", conn=Depends(get_db)):
    _require_ai()
    c = load(conn, code)
    text = ""
    if section:
        name, _, heading = section.partition("|")
        if name in ("overview", "competencies", "notebook"):  # never PA drafts or mistakes
            note, _ = notes_fs.read_note(c, name)
            body = cards.sections(note).get(heading, "")
            text = (f"## {heading}\n" if heading else "") + body.strip()
    return _page(request, conn, c, text=text, section=section)


@router.post("/courses/{code}/ai/run")
async def ai_run(request: Request, code: str, conn=Depends(get_db)):
    _require_ai()
    c = load(conn, code)
    form = await request.form()
    action, text = form.get("action", ""), str(form.get("text", ""))
    if action not in ACTIONS:
        raise HTTPException(400)
    competency = ""
    if form.get("competency_id"):
        row = conn.execute("SELECT text FROM competencies WHERE id = ? AND course_id = ?",
                           (form["competency_id"], c["id"])).fetchone()
        competency = row["text"] if row else ""
    try:
        if action == "cards":
            result = ai_actions.flashcards(text)
        elif action == "questions":
            result = ai_actions.questions(text)
        elif action == "explain":
            result = ai_actions.explain(text)
        else:
            result = ai_actions.gap_check(competency, text)
    except ai.AIError as e:
        return _page(request, conn, c, text=text, section=form.get("section", ""), error=str(e), status_code=502)
    rendered = markdown.render(result, known_codes(conn)) if isinstance(result, str) else None
    return render(request, "ai/draft.html", **course_context(conn, c), tab="ai", action=action, label=ACTIONS[action],
                  result=result, rendered=rendered, comps=comp.list_for(conn, c["id"]),
                  competency_id=form.get("competency_id", ""), competency=competency)


def _comp_id(conn, c, value):
    if not value:
        return None
    row = conn.execute("SELECT id FROM competencies WHERE id = ? AND course_id = ?", (value, c["id"])).fetchone()
    return row[0] if row else None


@router.post("/courses/{code}/ai/accept")
async def ai_accept(request: Request, code: str, conn=Depends(get_db)):
    _require_ai()
    c = load(conn, code)
    form = await request.form()
    kind = form.get("kind")
    comp_id = _comp_id(conn, c, form.get("competency_id"))
    kept = 0
    if kind == "cards":
        for i in form.getlist("keep"):
            front, back = str(form.get(f"front_{i}", "")).strip(), str(form.get(f"back_{i}", "")).strip()
            if front and back:
                cards.add(conn, c["id"], front, back, competency_id=comp_id, source="ai-accepted")
                kept += 1
        return RedirectResponse(f"/courses/{c['code']}/cards?added={kept}", status_code=303)
    if kind == "questions":
        for i in form.getlist("keep"):
            try:
                quizzes.save(conn, c["id"], {"kind": form.get(f"kind_{i}", "mc"), "prompt": form.get(f"prompt_{i}", ""),
                                             "choices": form.get(f"choices_{i}", ""), "explanation": form.get(f"explanation_{i}", ""),
                                             "competency_id": str(comp_id or "")}, source="ai-accepted")
                kept += 1
            except quizzes.QuestionError:
                continue  # an edit broke it (e.g. no * left); skip rather than lose the rest
        return RedirectResponse(f"/courses/{c['code']}/quizzes?saved={kept}", status_code=303)
    if kind == "text":
        text = str(form.get("text", "")).strip()
        if text:
            title = form.get("title") or "AI note"
            notes_fs.append_note(conn, c, "notebook", f"## {title} · {clock.today().isoformat()}\n\n{text}\n")
        return RedirectResponse(f"/courses/{c['code']}/notes/notebook", status_code=303)
    raise HTTPException(400)
