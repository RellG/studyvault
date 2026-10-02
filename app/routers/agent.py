"""Ask: the study agent's chat pages (PLAN Slice 11). Hidden entirely (404) unless AI is configured, like Slice 9.
Plain forms and redirects: a reply can take a while, and a full page load keeps phone and laptop simple."""
import json
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .. import agent, ai, catalog, markdown, tasks
from ..db import get_db
from ..web import render
from .notes import known_codes

router = APIRouter()

QUICK = ["What should I study today?", "Plan my week.", "Quiz me on my weakest competency.",
         "What do my mistakes have in common?", "Am I ready for the exam?"]
FILL = ["Explain this differently: ", "Gap-check this competency against my notes: ",
        "From my pre-assessment, I need to learn these (make flashcards for every one, and remember my weak areas):\n"]
KIND_LABELS = {"cards": "Flashcards", "questions": "Practice questions", "notebook": "Notebook section",
               "confidence": "Competency confidence", "exam": "Exam date", "quiz": "Practice quiz", "study": "Study time",
               "tasks": "Study tasks", "task_update": "Task change", "delete_cards": "Delete flashcards",
               "edit_cards": "Edit flashcards", "delete_questions": "Delete practice questions"}


def _require_ai():
    if not ai.enabled():
        raise HTTPException(404)


def _thread(conn, tid):
    t = conn.execute("SELECT t.*, c.code FROM agent_threads t LEFT JOIN courses c ON c.id = t.course_id WHERE t.id = ?",
                     (tid,)).fetchone()
    if not t:
        raise HTTPException(404)
    return t


def _proposal(conn, pid):
    p = conn.execute("SELECT p.*, c.code FROM agent_proposals p LEFT JOIN courses c ON c.id = p.course_id WHERE p.id = ?",
                     (pid,)).fetchone()
    if not p:
        raise HTTPException(404)
    return p


def _courses(conn):
    return conn.execute("SELECT id, code, title FROM courses ORDER BY status = 'passed', ord, code").fetchall()


@router.get("/ask")
def ask_home(request: Request, course: str = "", conn=Depends(get_db)):
    _require_ai()
    c = catalog.get_course(conn, course) if course else None
    threads = conn.execute("SELECT t.*, c.code, (SELECT COUNT(*) FROM agent_messages m WHERE m.thread_id = t.id) AS n "
                           "FROM agent_threads t LEFT JOIN courses c ON c.id = t.course_id "
                           "ORDER BY t.updated_at DESC LIMIT 40").fetchall()
    n_memory = conn.execute("SELECT COUNT(*) FROM agent_memory").fetchone()[0]
    return render(request, "agent/index.html", threads=threads, courses=_courses(conn), scope=c, quick=QUICK, fill=FILL,
                  provider=ai.describe(), error=request.query_params.get("error"), n_memory=n_memory)


@router.get("/ask/memory")
def memory_page(request: Request, conn=Depends(get_db)):
    _require_ai()
    return render(request, "agent/memory.html", memories=agent.memories(conn), courses=_courses(conn),
                  limit=agent.MEMORY_TEXT, error=request.query_params.get("error"))


@router.post("/ask/memory")
def memory_add(text: str = Form(""), course: str = Form(""), conn=Depends(get_db)):
    _require_ai()
    c = catalog.get_course(conn, course) if course else None
    try:
        agent.add_memory(conn, text, c["id"] if c else None)
    except ValueError as e:
        return RedirectResponse(f"/ask/memory?{urlencode({'error': str(e)})}", status_code=303)
    return RedirectResponse("/ask/memory", status_code=303)


@router.post("/ask/memory/{mid}/delete")
def memory_delete(mid: int, conn=Depends(get_db)):
    _require_ai()
    agent.delete_memory(conn, mid)
    return RedirectResponse("/ask/memory", status_code=303)


@router.post("/ask/new")
def ask_new(text: str = Form(""), quick: str = Form(""), course: str = Form(""), start: str = Form(""),
            conn=Depends(get_db)):
    _require_ai()
    message = (quick or text).strip()
    c = catalog.get_course(conn, course) if course else None
    if not message:
        return RedirectResponse(f"/ask?course={c['code'] if c else ''}", status_code=303)
    tid = agent.create_thread(conn, message, c["id"] if c else None)
    if start:  # the chat page's script sends the first message itself, so it can show progress while it waits
        return JSONResponse({"id": tid})
    agent.reply(conn, tid, message)
    return RedirectResponse(f"/ask/{tid}#latest", status_code=303)


@router.get("/ask/{tid}")
def ask_thread(request: Request, tid: int, conn=Depends(get_db)):
    _require_ai()
    t = _thread(conn, tid)
    codes = known_codes(conn)
    props = {}
    for p in conn.execute("SELECT p.*, c.code FROM agent_proposals p LEFT JOIN courses c ON c.id = p.course_id "
                          "WHERE p.thread_id = ? ORDER BY p.id", (tid,)):
        props.setdefault(p["message_id"], []).append({**dict(p), "payload": json.loads(p["payload_json"]),
                                                      "label": KIND_LABELS.get(p["kind"], p["kind"])})
    messages = []
    for m in conn.execute("SELECT * FROM agent_messages WHERE thread_id = ? ORDER BY id", (tid,)):
        meta = json.loads(m["meta_json"] or "{}")
        messages.append({**dict(m), "meta": meta, "proposals": props.get(m["id"], []),
                         "html": markdown.render(m["text"], codes) if m["role"] == "assistant" and m["text"] else None})
    comps = {}
    for p in (x for ps in props.values() for x in ps if x["kind"] == "confidence"):
        comps[p["id"]] = conn.execute("SELECT confidence FROM competencies WHERE id = ?",
                                      (p["payload"]["competency_id"],)).fetchone()
    current_task = {p["id"]: tasks.get(conn, p["payload"]["task_id"])
                    for ps in props.values() for p in ps if p["kind"] == "task_update"}
    return render(request, "agent/thread.html", thread=t, messages=messages, quick=QUICK, fill=FILL,
                  provider=ai.describe(), error=request.query_params.get("error"), current_conf=comps,
                  current_task=current_task)


@router.get("/ask/{tid}/progress")
def ask_progress(tid: int):
    """Polled by the chat while a reply is on its way: how long, and what it has looked at so far."""
    _require_ai()
    return agent.reply_progress(tid) or {"done": True}


@router.post("/ask/{tid}/send")
def ask_send(tid: int, text: str = Form(""), quick: str = Form(""), conn=Depends(get_db)):
    _require_ai()
    _thread(conn, tid)
    message = (quick or text).strip()
    if message:
        agent.reply(conn, tid, message)
    return RedirectResponse(f"/ask/{tid}#latest", status_code=303)


@router.post("/ask/{tid}/delete")
def ask_delete(tid: int, conn=Depends(get_db)):
    _require_ai()
    _thread(conn, tid)
    with conn:
        conn.execute("DELETE FROM agent_threads WHERE id = ?", (tid,))
    return RedirectResponse("/ask", status_code=303)


@router.post("/ask/proposals/{pid}/apply")
async def proposal_apply(request: Request, pid: int, conn=Depends(get_db)):
    _require_ai()
    p = _proposal(conn, pid)
    form = await request.form()
    try:
        _, link = agent.apply(conn, p, form)
    except (agent.ApplyError, ValueError) as e:
        return RedirectResponse(f"/ask/{p['thread_id']}?{urlencode({'error': str(e)})}#p{pid}", status_code=303)
    if p["kind"] == "quiz":  # the point of this one is to go and take it
        return RedirectResponse(link, status_code=303)
    return RedirectResponse(f"/ask/{p['thread_id']}#p{pid}", status_code=303)


@router.post("/ask/proposals/{pid}/dismiss")
def proposal_dismiss(pid: int, conn=Depends(get_db)):
    _require_ai()
    p = _proposal(conn, pid)
    agent.dismiss(conn, p)
    return RedirectResponse(f"/ask/{p['thread_id']}#p{pid}", status_code=303)
