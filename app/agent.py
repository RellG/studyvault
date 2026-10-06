"""Study agent (PLAN Slice 11): a chat that looks things up across the vault with tools and proposes changes.

Read tools run as soon as the model calls them. Write tools never change anything: they store a proposal that the
chat shows as a card, and only the user's Apply (apply() below, called from routers/agent.py) carries it out.
PA drafts are out of reach: no tool reads pa/, search drops pa/ hits, and the system prompt forbids PA drafting.
"""
import html
import json
import logging
import re
import time

from . import ai, assessments, brain, cards, catalog, chunks, clock, competencies as comp, notes_fs, progress, quizzes, readiness
from . import tasks as study_tasks
from .config import settings

log = logging.getLogger("studyvault.agent")

MAX_ROUNDS = 6            # model calls per message: keeps free-tier request counts low
MAX_CALLS_PER_ROUND = 8
RESULT_CHARS = 4000       # per tool result sent back to the model
NOTE_CHARS = 8000         # read_note / get_mistakes get more room
LIST_CHARS = 24000        # list_flashcards / list_questions: a whole deck, so edits and deletes can name ids
HISTORY_MESSAGES = 20     # earlier messages resent each turn (final text only, not their lookups)
MAX_MESSAGE_CHARS = 12000  # room for a pasted list of pre-assessment topics
MAX_CARDS = 40            # per add_flashcards call; a longer list takes several calls
MAX_TASKS = 12            # per propose_tasks call
LIST_LIMIT = 150          # cards or questions per list_flashcards / list_questions call
MEMORY_TEXT = 300         # one memory
MEMORY_CHARS = 4000       # all memories sent with each request; the newest win if there are more
READABLE_NOTES = ["overview", "competencies", "notebook", "mistakes"]


class ToolError(Exception):
    """Goes back to the model as {"error": ...} so it can correct itself."""


class ApplyError(Exception):
    """Shown to the user on the proposal card."""


class Ctx:
    def __init__(self, conn, thread):
        self.conn = conn
        self.thread = thread
        self.proposals: list[int] = []


TOOLS: dict[str, dict] = {}


def tool(name, kind, label, description, properties=None, required=()):
    def deco(fn):
        TOOLS[name] = {"kind": kind, "label": label, "fn": fn, "schema": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": list(required)}}}
        return fn
    return deco


CODE = {"type": "string", "description": "Course code, e.g. D413"}
COMP = {"type": "integer", "description": "Competency id from list_competencies (optional)"}


def _course(conn, code):
    c = catalog.get_course(conn, str(code).strip()) if code else None
    if not c:
        raise ToolError(f"No course “{code}”. Call list_courses for the codes.")
    return c


def _comp_id(conn, course, value):
    if value in (None, "", 0, "0"):
        return None
    try:
        cid = int(value)
    except (TypeError, ValueError):
        raise ToolError("competency_id must be a number from list_competencies.")
    if not conn.execute("SELECT 1 FROM competencies WHERE id = ? AND course_id = ?", (cid, course["id"])).fetchone():
        raise ToolError(f"Competency {cid} isn't in {course['code']}. Call list_competencies.")
    return cid


def _pick(row, *keys):
    return {k: row[k] for k in keys}


# ---------------------------------------------------------------- read tools

@tool("get_overview", "read", "progress overview",
      "Degree progress at a glance: today's date, current term and pace, SAP completion rate, CU passed and remaining, "
      "projected graduation, study streak, cards due today, the course to work on next, and upcoming exams.")
def get_overview(ctx):
    from .routers.dashboard import next_up, upcoming_exams
    conn, today = ctx.conn, clock.today()
    p = progress.load(conn, today)
    nu = next_up(conn, p["term"])
    program = conn.execute("SELECT name, total_cu FROM program").fetchone()
    return {"today": today.isoformat(), "weekday": today.strftime("%A"),
            "program": dict(program) if program else None,
            "current_term": dict(p["term"]) if p["term"] else None, "term_pace": p["pace"],
            "sap": p["sap"], "term1_check": p["term1"], "cu_passed": p["passed_cu"], "cu_in_plan": p["remaining_total"],
            "cu_transferred": p["transferred"], "projected_graduation": p["grad"],
            "graduation_target": progress.GRAD_TARGET, "study_streak_days": p["streak"],
            "cards_due_today": cards.due_count(conn),
            "next_up": {**_pick(nu["course"], "code", "title", "status"), "readiness": nu["ready"]["score"],
                        "cards_due": nu["due"]} if nu else None,
            "upcoming_exams": upcoming_exams(conn, today)}


@tool("list_courses", "read", "course list",
      "Every course in the plan with term, status, CU, assessment type (OA/PA/cert), start/due/target dates, exam "
      "date and readiness (0-100). Optionally only one term.",
      {"term": {"type": "integer", "description": "Term number (optional)"}})
def list_courses(ctx, term=None):
    conn = ctx.conn
    sql = "SELECT c.*, t.n AS term_n FROM courses c LEFT JOIN terms t ON t.id = c.term_id"
    rows = conn.execute(sql + (" WHERE t.n = ?" if term else "") + " ORDER BY t.n IS NULL, t.n, c.ord, c.code",
                        (int(term),) if term else ()).fetchall()
    terms = [_pick(t, "n", "start", "end", "target_cu") for t in catalog.list_terms(conn)]
    return {"terms": terms, "courses": [
        {**_pick(r, "code", "title", "cu", "term_n", "status", "assessment_type", "start", "due", "target",
                 "exam_date", "cert_name"),
         "readiness": readiness.for_course(conn, r["id"])["score"] if r["status"] != "passed" else None}
        for r in rows]}


@tool("get_course", "read", "course",
      "One course in detail: dates and status, readiness score and its parts, weakest competencies, the exam-prep "
      "checklist, pre-assessment results, card and question-bank counts, and PA task progress (status only).",
      {"code": CODE}, ["code"])
def get_course(ctx, code):
    conn = ctx.conn
    c = _course(conn, code)
    r = readiness.for_course(conn, c["id"])
    secs = chunks.outline(conn, c["id"])
    out = {"course": _pick(c, "code", "title", "cu", "term_n", "status", "assessment_type", "start", "due", "target",
                           "exam_date", "quiz_target", "cert_name", "passed_on"),
           "readiness": {"score": r["score"], "parts_percent": {r["labels"][k]: None if v is None else round(100 * v)
                                                                for k, v in r["components"].items()}},
           "weakest_competencies": [_pick(w, "id", "text", "confidence", "last_reviewed")
                                    for w in comp.weakest(conn, c["id"])],
           "exam_prep_checklist": assessments.checklist(conn, c),
           "preassessments": [{k: p[k] for k in ("taken_on", "score", "passed")}
                              for p in assessments.preassessments(conn, c["id"])[:5]],
           "cards": {"total": conn.execute("SELECT COUNT(*) FROM cards WHERE course_id = ?", (c["id"],)).fetchone()[0],
                     "due": cards.due_count(conn, c["id"])},
           "question_bank": len(quizzes.bank(conn, c["id"])),
           "notes": {"sections": len(secs), "chars": sum(x["chars"] for x in secs)}}
    if c["assessment_type"] == "PA":
        out["pa_tasks"] = {row[0]: row[1] for row in conn.execute(
            "SELECT status, COUNT(*) FROM pa_tasks WHERE course_id = ? GROUP BY status", (c["id"],))}
    return out


@tool("search_notes", "read", "search",
      "Full-text search across notes, flashcards and practice questions. Returns where each hit is and a snippet.",
      {"query": {"type": "string", "description": "Words to search for"}, "code": CODE}, ["query"])
def search_notes(ctx, query, code=None):
    from .routers.search import search
    if code:
        _course(ctx.conn, code)
    hits = [h for h in search(ctx.conn, str(query), code=code or None, limit=20) if "/notes/pa/" not in h["url"]]
    clean = lambda s: html.unescape(re.sub(r"</?mark>", "", s))  # noqa: E731
    return {"results": [{"where": h["label"], "kind": h["kind"], "link": h["url"], "snippet": clean(h["snippet"])}
                        for h in hits[:12]]} if hits else {"results": [], "note": "No matches."}


@tool("read_note", "read", "notes",
      "Read one of a course's note files: overview, competencies, notebook or mistakes. Give `section` (a ## heading) "
      "to read just that part. Long files are cut off: for the whole thing use list_note_sections, then read_note_section.",
      {"code": CODE, "name": {"type": "string", "enum": READABLE_NOTES},
       "section": {"type": "string", "description": "A ## heading in that file (optional)"}}, ["code", "name"])
def read_note(ctx, code, name, section=None):
    c = _course(ctx.conn, code)
    if name not in READABLE_NOTES:
        raise ToolError("name must be overview, competencies, notebook or mistakes. PA drafts are off limits.")
    text, _ = notes_fs.read_note(c, name)
    secs = cards.sections(text)
    if section:
        match = next((h for h in secs if h.lower() == str(section).strip().lower()), None)
        if match is None:
            raise ToolError(f"No section “{section}”. Sections: {', '.join(h for h in secs if h) or 'none'}")
        text = f"## {match}\n{secs[match]}"
    out = {"link": f"/courses/{c['code']}/notes/{name}", "text": text.strip() or "(empty)"}
    if len(text) > NOTE_CHARS:
        out.update(text=text[:NOTE_CHARS], truncated=True, sections=[h for h in secs if h],
                   hint="Cut off. Use list_note_sections and read_note_section to read all of it.")
    return out


@tool("list_note_sections", "read", "note sections",
      "A course's notes cut into sections, each with a `key`, where it sits (heading path), its size, and how many "
      "flashcards and questions were made from it. Shows everything the notes cover, which read_note can't for long "
      "files. Then read a section with read_note_section. Default: overview, competencies and notebook.",
      {"code": CODE, "name": {"type": "string", "enum": READABLE_NOTES, "description": "One file only (optional)"}},
      ["code"])
def list_note_sections(ctx, code, name=None):
    c = _course(ctx.conn, code)
    if name and name not in READABLE_NOTES:
        raise ToolError("name must be overview, competencies, notebook or mistakes.")
    rows = chunks.outline(ctx.conn, c["id"], (name,) if name else chunks.BRAIN_FILES)
    if not rows:
        return {"sections": [], "note": "No notes written in this course yet."}
    return {"sections": [{"key": r["key"], "where": r["breadcrumb"] or "(top of file)", "chars": r["chars"],
                          "cards": r["cards"], "questions": r["questions"]} for r in rows],
            "total_chars": sum(r["chars"] for r in rows),
            "made_from_sections": chunks.freshness(ctx.conn, c["id"])}


@tool("read_note_section", "read", "note section",
      "Read one section of a course's notes in full, by its `key` from list_note_sections.",
      {"code": CODE, "key": {"type": "string", "description": "A key from list_note_sections"}}, ["code", "key"])
def read_note_section(ctx, code, key):
    c = _course(ctx.conn, code)
    sec = chunks.get(ctx.conn, c["id"], key)
    if not sec:
        raise ToolError(f"No section “{key}” in {c['code']}. Call list_note_sections for the keys.")
    return {"key": sec["key"], "where": chunks.where(sec), "link": f"/courses/{c['code']}/notes/{sec['file']}",
            "text": sec["text"]}


@tool("get_course_brain", "read", "what the notes cover",
      "What a course's notes cover, worked out from the notes themselves: a summary, the learning objectives, every section "
      "with a one-line summary, its importance, how many cards and questions were made from it, thin spots, and any lists "
      "of topics to learn. Says if the notes haven't been analyzed yet or have changed since.",
      {"code": CODE}, ["code"])
def get_course_brain(ctx, code):
    c = _course(ctx.conn, code)
    i = brain.info(ctx.conn, c)
    if not i["sections"]:
        return {"analyzed": False, "note": "No notes written in this course yet."}
    if not i["profile"]:
        return {"analyzed": False, "sections": i["sections"], "note": "The notes haven't been analyzed yet. Use "
                "list_note_sections and read_note_section to read them, or offer propose_build_from_notes."}
    prof = i["profile"]
    counts = {o["key"]: o for o in chunks.outline(ctx.conn, c["id"])}
    thin = {t["key"]: t["why"] for t in prof["thin_spots"]}
    return {"analyzed": True, "up_to_date": i["state"] == "fresh", "analyzed_on": i["analyzed_at"][:10],
            "summary": prof["summary"], "objectives": prof["objectives"], "competencies": [x["text"] for x in prof["competencies"]],
            "sections": [{**_pick(o, "key", "title", "summary", "importance"), "cards": counts.get(o["key"], {}).get("cards", 0),
                          "questions": counts.get(o["key"], {}).get("questions", 0), "thin": thin.get(o["key"]),
                          "out_of_date": counts.get(o["key"], {}).get("stale_cards", 0) + counts.get(o["key"], {}).get("stale_questions", 0)}
                         for o in prof["outline"]],
            "coverage": {k: v for k, v in brain.coverage(ctx.conn, c["id"]).items() if k not in ("uncovered", "stale", "objectives")},
            "topic_lists": [{"where": t["where"], "terms": t["terms"][:40]} for t in prof["topic_lists"]]}


@tool("list_competencies", "read", "competencies",
      "A course's competencies with id, confidence (1-5, null = unrated), last reviewed date, and how many cards "
      "and questions are linked to each.", {"code": CODE}, ["code"])
def list_competencies(ctx, code):
    conn = ctx.conn
    c = _course(conn, code)
    n_cards = dict(conn.execute("SELECT competency_id, COUNT(*) FROM cards WHERE course_id = ? GROUP BY competency_id", (c["id"],)).fetchall())
    n_qs = dict(conn.execute("SELECT competency_id, COUNT(*) FROM questions WHERE course_id = ? GROUP BY competency_id", (c["id"],)).fetchall())
    rows = comp.list_for(conn, c["id"])
    if not rows:
        return {"competencies": [], "note": "No competencies imported yet (Competencies tab)."}
    return {"competencies": [{**_pick(r, "id", "text", "confidence", "last_reviewed"),
                              "cards": n_cards.get(r["id"], 0), "questions": n_qs.get(r["id"], 0)} for r in rows]}


@tool("get_card_stats", "read", "flashcards",
      "Flashcard counts per course (total, due today, reviewed today, AI-made) and the hardest cards "
      "(most often graded Again). Optionally one course.", {"code": CODE})
def get_card_stats(ctx, code=None):
    conn, today = ctx.conn, clock.today().isoformat()
    where, params = ("WHERE c.code = ?", [_course(conn, code)["code"]]) if code else ("", [])
    per = conn.execute(f"""SELECT c.id, c.code, COUNT(*) AS total, SUM(k.due_on <= ?) AS due,
                                  SUM(k.source = 'ai-accepted') AS ai_made
                           FROM cards k JOIN courses c ON c.id = k.course_id {where} GROUP BY c.id ORDER BY c.ord""",
                       [today, *params]).fetchall()
    hard = conn.execute(f"""SELECT c.code, k.front, k.back, SUM(r.grade = 1) AS again, COUNT(r.id) AS reviews
                            FROM reviews r JOIN cards k ON k.id = r.card_id JOIN courses c ON c.id = k.course_id {where}
                            GROUP BY k.id HAVING again > 0 ORDER BY again DESC, reviews DESC LIMIT 8""", params).fetchall()
    return {"courses": [{**_pick(r, "code", "total", "due", "ai_made"), "reviewed_today": cards.reviewed_today(conn, r["id"])}
                        for r in per] or "No cards yet.",
            "hardest_cards": [dict(r) for r in hard]}


@tool("get_quiz_history", "read", "quiz history",
      "A course's finished practice quizzes (newest first, with percent), its quiz target, question-bank size, and "
      "the questions missed most often.", {"code": CODE}, ["code"])
def get_quiz_history(ctx, code):
    conn = ctx.conn
    c = _course(conn, code)
    missed = conn.execute("""SELECT q.id, q.prompt, SUM(a.correct = 0) AS missed, COUNT(*) AS answered
                             FROM quiz_answers a JOIN questions q ON q.id = a.question_id
                             JOIN quiz_attempts t ON t.id = a.attempt_id WHERE t.course_id = ?
                             GROUP BY q.id HAVING missed > 0 ORDER BY missed DESC LIMIT 8""", (c["id"],)).fetchall()
    return {"quiz_target_percent": c["quiz_target"], "question_bank": len(quizzes.bank(conn, c["id"])),
            "attempts": [{"finished": a["finished_at"][:16], "score": a["score"], "total": a["total"],
                          "percent": round(100 * a["score"] / a["total"]) if a["total"] else None}
                         for a in quizzes.history(conn, c["id"])[:10]],
            "most_missed": [dict(r) for r in missed]}


@tool("get_mistakes", "read", "mistake log",
      "The course's mistake log (wrong quiz answers, with the student's own 'why I missed it' notes). Newest last.",
      {"code": CODE}, ["code"])
def get_mistakes(ctx, code):
    c = _course(ctx.conn, code)
    text, _ = notes_fs.read_note(c, "mistakes")
    return {"link": f"/courses/{c['code']}/notes/mistakes",
            "text": ("…" + text[-NOTE_CHARS:]) if len(text) > NOTE_CHARS else (text.strip() or "(empty)")}


@tool("get_study_time", "read", "study time",
      "Study hours per course (and per CU), hours per week for the last 12 weeks, and whether a study timer is running.")
def get_study_time(ctx):
    from .routers.sessions import active, analytics
    conn = ctx.conn
    a = analytics(conn, clock.today())
    running = active(conn)
    return {"hours_by_course": [{"code": r["code"], "hours": round(r["hours"], 1), "hours_per_cu": round(r["per_cu"], 1)}
                                for r in a["courses"]],
            "average_hours_per_cu": round(a["avg_per_cu"], 1) if a["avg_per_cu"] else None,
            "hours_by_week": [{"week_of": w["start"].isoformat(), "hours": round(w["hours"], 1)} for w in a["weeks"]],
            "timer_running": {"course": running["code"], "since": running["started_at"]} if running else None}


@tool("list_certs", "read", "certs",
      "Certifications: course, voucher status and expiry, exam date, result, expiry.")
def list_certs(ctx):
    return {"certs": [_pick(r, "name", "code", "voucher_status", "voucher_expires", "exam_date", "result", "expires_on")
                      for r in assessments.list_certs(ctx.conn)] or "None yet."}


@tool("list_tasks", "read", "tasks",
      "The student's study tasks (their plan): id, course, title, type, status, due date, minutes, priority, and how it "
      "gets done (done_rule: manual, cards_reviewed or quiz_finished). A finished task shows its evidence. Defaults to "
      "open tasks; optionally one course and/or a status.",
      {"code": CODE, "status": {"type": "string", "enum": ["open", "all", *study_tasks.STATUSES],
                                "description": "open (default), all, or one status"}})
def list_tasks(ctx, code=None, status="open"):
    conn = ctx.conn
    c = _course(conn, code) if code else None
    status = str(status or "open")
    if status not in ("open", "all", *study_tasks.STATUSES):
        raise ToolError("status must be open, all, todo, doing, blocked, done or cancelled.")
    statuses = study_tasks.OPEN if status == "open" else None if status == "all" else (status,)
    rows = study_tasks.decorate(study_tasks.list_tasks(conn, c["id"] if c else None, statuses, limit=40))
    if not rows:
        return {"tasks": [], "note": "No tasks."}
    return {"tasks": [{**_pick(t, "id", "title", "kind", "status", "due_on", "est_minutes", "priority", "done_rule"),
                       "course": t["code"], "when": t["due_label"] or None, "evidence": t["evidence_text"] or None}
                      for t in rows]}


# ---------------------------------------------------------------- write tools: proposals only

PROPOSED = "Shown to the student as a card with an Apply button. Nothing is saved unless they press Apply."


def _propose(ctx, kind, course, payload, summary):
    with ctx.conn:
        cur = ctx.conn.execute("INSERT INTO agent_proposals(thread_id, kind, course_id, payload_json, created_at) "
                               "VALUES (?, ?, ?, ?, ?)", (ctx.thread["id"], kind, course["id"] if course else None,
                                                          json.dumps(payload), clock.now().isoformat()))
    ctx.proposals.append(cur.lastrowid)
    return {"proposal_id": cur.lastrowid, "summary": summary, "status": PROPOSED}


def _text(value, field, limit):
    s = str(value or "").strip()
    if not s:
        raise ToolError(f"{field} is empty.")
    return s[:limit]


SECTION = {"type": "string", "description": "Key from list_note_sections of the notes section these come from "
                                           "(optional, but set it when they come from one section)"}


def _section(conn, course, key):
    """{key, hash, label} for a notes section, or None if no key was given."""
    if not key:
        return None
    r = chunks.ref(conn, course["id"], key)
    if not r:
        raise ToolError(f"No section “{key}” in {course['code']}. Call list_note_sections for the keys.")
    return r


@tool("add_flashcards", "write", "proposed flashcards",
      "Propose flashcards for a course (the student reviews, edits and applies them). One fact per card, short answers. "
      f"Up to {MAX_CARDS} cards per call; for more, call it again in the same step.",
      {"code": CODE, "competency_id": COMP, "section_key": SECTION,
       "cards": {"type": "array", "description": f"Up to {MAX_CARDS} cards", "items": {"type": "object", "properties": {
           "front": {"type": "string"}, "back": {"type": "string"}}, "required": ["front", "back"]}}},
      ["code", "cards"])
def add_flashcards(ctx, code, cards, competency_id=None, section_key=None):
    c = _course(ctx.conn, code)
    src = _section(ctx.conn, c, section_key)
    items = [{"front": str(x.get("front", "")).strip()[:1000], "back": str(x.get("back", "")).strip()[:2000]}
             for x in (cards if isinstance(cards, list) else []) if isinstance(x, dict)]
    items = [x for x in items if x["front"] and x["back"]][:MAX_CARDS]
    if not items:
        raise ToolError("No usable cards: each needs a front and a back.")
    return _propose(ctx, "cards", c, {"cards": items, "competency_id": _comp_id(ctx.conn, c, competency_id), "source": src},
                    f"{len(items)} flashcards for {c['code']}" + (f" from “{src['label']}”" if src else ""))


@tool("add_questions", "write", "proposed questions",
      "Propose practice questions for a course's question bank, WGU objective-assessment style. Each has 2-6 "
      "choices and the 0-based indexes of the correct ones (more than one = select all that apply).",
      {"code": CODE, "competency_id": COMP, "section_key": SECTION,
       "questions": {"type": "array", "description": "Up to 20 questions", "items": {"type": "object", "properties": {
           "prompt": {"type": "string"}, "choices": {"type": "array", "items": {"type": "string"}},
           "correct": {"type": "array", "items": {"type": "integer"}}, "explanation": {"type": "string"}},
           "required": ["prompt", "choices", "correct"]}}},
      ["code", "questions"])
def add_questions(ctx, code, questions, competency_id=None, section_key=None):
    c = _course(ctx.conn, code)
    src = _section(ctx.conn, c, section_key)
    out = []
    for q in questions if isinstance(questions, list) else []:
        if not isinstance(q, dict):
            continue
        choices = [str(x).strip() for x in q.get("choices") or [] if str(x).strip()][:6]
        try:
            correct = sorted({int(i) for i in q.get("correct") or []})
        except (TypeError, ValueError):
            continue
        prompt = str(q.get("prompt", "")).strip()
        if not prompt or len(choices) < 2 or not correct or any(not 0 <= i < len(choices) for i in correct):
            continue
        out.append({"kind": "multi" if len(correct) > 1 else "mc", "prompt": prompt[:2000],
                    "explanation": str(q.get("explanation", "")).strip()[:2000],
                    "choices_text": "\n".join(("* " if i in correct else "") + ch for i, ch in enumerate(choices))})
    if not out:
        raise ToolError("No usable questions: each needs a prompt, 2+ choices and valid correct indexes.")
    return _propose(ctx, "questions", c, {"questions": out[:20], "competency_id": _comp_id(ctx.conn, c, competency_id),
                                          "source": src},
                    f"{len(out[:20])} practice questions for {c['code']}" + (f" from “{src['label']}”" if src else ""))


@tool("list_flashcards", "read", "flashcard list",
      "A course's flashcards with their ids (front, back, competency, next due date, AI-made or not), oldest first. "
      "Use it before editing or deleting cards. Optional: a competency, or words to match in front/back.",
      {"code": CODE, "competency_id": COMP, "query": {"type": "string", "description": "Words to match (optional)"}},
      ["code"])
def list_flashcards(ctx, code, competency_id=None, query=None):
    c = _course(ctx.conn, code)
    sql, params = "SELECT * FROM cards WHERE course_id = ?", [c["id"]]
    if competency_id:
        sql, params = sql + " AND competency_id = ?", params + [_comp_id(ctx.conn, c, competency_id)]
    if query:
        sql, params = sql + " AND (front LIKE ? OR back LIKE ?)", params + [f"%{query}%"] * 2
    rows = ctx.conn.execute(sql + " ORDER BY id LIMIT ?", (*params, LIST_LIMIT + 1)).fetchall()
    out = [{"id": r["id"], "front": r["front"][:160], "back": r["back"][:160], "competency_id": r["competency_id"],
            "due_on": r["due_on"], "ai_made": r["source"] == "ai-accepted"} for r in rows[:LIST_LIMIT]]
    return {"cards": out or "No cards match.", "more": len(rows) > LIST_LIMIT}


@tool("list_questions", "read", "question bank",
      "A course's practice-question bank with ids (prompt, kind, competency). Use it before deleting questions.",
      {"code": CODE, "competency_id": COMP}, ["code"])
def list_questions(ctx, code, competency_id=None):
    c = _course(ctx.conn, code)
    rows = quizzes.bank(ctx.conn, c["id"], _comp_id(ctx.conn, c, competency_id) if competency_id else None)
    out = [{"id": q["id"], "kind": q["kind"], "prompt": q["prompt"][:200], "competency_id": q["competency_id"]}
           for q in rows[:LIST_LIMIT]]
    return {"questions": out or "The bank is empty.", "more": len(rows) > LIST_LIMIT}


def _pick_rows(conn, table, course, ids, all_):
    """The rows of a course a delete/edit tool may touch: the given ids that belong to it, or all of them."""
    if all_:
        return conn.execute(f"SELECT * FROM {table} WHERE course_id = ? ORDER BY id", (course["id"],)).fetchall()
    try:
        ids = sorted({int(i) for i in ids or []})
    except (TypeError, ValueError):
        raise ToolError("ids must be numbers from list_flashcards / list_questions.")
    if not ids:
        raise ToolError("Give the ids to change (from list_flashcards / list_questions), or all=true.")
    rows = conn.execute(f"SELECT * FROM {table} WHERE course_id = ? AND id IN ({','.join('?' * len(ids))}) ORDER BY id",
                        (course["id"], *ids)).fetchall()
    if not rows:
        raise ToolError(f"None of those ids are in {course['code']}. Look them up with list_flashcards / list_questions.")
    return rows


IDS = {"type": "array", "items": {"type": "integer"}, "description": "Ids from the list tool"}
ALL = {"type": "boolean", "description": "true = every one in the course (only when the student asked for all)"}


@tool("delete_flashcards", "write", "proposed card deletion",
      "Propose deleting flashcards (with their review history) from a course: given ids, or all=true for the whole "
      "deck. The student sees the list, can untick any, and confirms; nothing is deleted before that.",
      {"code": CODE, "card_ids": IDS, "all": ALL}, ["code"])
def delete_flashcards(ctx, code, card_ids=None, all=False):  # noqa: A002 — the tool's argument name
    c = _course(ctx.conn, code)
    rows = _pick_rows(ctx.conn, "cards", c, card_ids, all)
    items = [{"id": r["id"], "front": r["front"][:300], "back": r["back"][:300]} for r in rows]
    return _propose(ctx, "delete_cards", c, {"cards": items}, f"delete {len(items)} flashcards from {c['code']}")


@tool("edit_flashcards", "write", "proposed card edits",
      "Propose fixing existing flashcards (wording, a wrong answer, splitting hairs): give each card's id and its new "
      f"front and/or back. Up to {MAX_CARDS} per call. Review history is kept.",
      {"code": CODE, "edits": {"type": "array", "items": {"type": "object", "properties": {
          "card_id": {"type": "integer"}, "front": {"type": "string"}, "back": {"type": "string"}},
          "required": ["card_id"]}}}, ["code", "edits"])
def edit_flashcards(ctx, code, edits):
    c = _course(ctx.conn, code)
    edits = [e for e in (edits if isinstance(edits, list) else []) if isinstance(e, dict)][:MAX_CARDS]
    rows = {r["id"]: r for r in _pick_rows(ctx.conn, "cards", c, [e.get("card_id") for e in edits], False)}
    items = []
    for e in edits:
        r = rows.get(int(e.get("card_id") or 0))
        if not r:
            continue
        front = str(e.get("front") or r["front"]).strip()[:1000]
        back = str(e.get("back") or r["back"]).strip()[:2000]
        if (front, back) != (r["front"], r["back"]):
            items.append({"id": r["id"], "old_front": r["front"], "old_back": r["back"], "front": front, "back": back})
    if not items:
        raise ToolError("Nothing to change: the new text matches the cards as they are.")
    return _propose(ctx, "edit_cards", c, {"cards": items}, f"edit {len(items)} flashcards in {c['code']}")


@tool("delete_questions", "write", "proposed question deletion",
      "Propose deleting practice questions from a course's bank: given ids, or all=true. Past quiz scores stay. "
      "The student confirms first.",
      {"code": CODE, "question_ids": IDS, "all": ALL}, ["code"])
def delete_questions(ctx, code, question_ids=None, all=False):  # noqa: A002
    c = _course(ctx.conn, code)
    rows = _pick_rows(ctx.conn, "questions", c, question_ids, all)
    items = [{"id": r["id"], "prompt": r["prompt"][:300]} for r in rows]
    return _propose(ctx, "delete_questions", c, {"questions": items}, f"delete {len(items)} questions from {c['code']}")


@tool("append_to_notebook", "write", "proposed notebook section",
      "Propose a new dated section at the end of a course notebook (an explanation, summary, study plan or checklist). "
      "Never used for PA work. Existing notes are never changed.",
      {"code": CODE, "title": {"type": "string", "description": "Section heading"},
       "markdown": {"type": "string", "description": "The section body in markdown"}}, ["code", "title", "markdown"])
def append_to_notebook(ctx, code, title, markdown):
    c = _course(ctx.conn, code)
    title = _text(title, "title", 120).lstrip("#").strip()
    return _propose(ctx, "notebook", c, {"title": title, "text": _text(markdown, "markdown", 20000)},
                    f"notebook section “{title}” in {c['code']}")


@tool("set_confidence", "write", "proposed confidence rating",
      "Propose a confidence rating (1 = lost, 5 = could teach it) for one competency, and/or marking it reviewed today.",
      {"code": CODE, "competency_id": {"type": "integer", "description": "From list_competencies"},
       "confidence": {"type": "integer", "description": "1-5 (optional)"},
       "mark_reviewed": {"type": "boolean", "description": "Mark reviewed today"}}, ["code", "competency_id"])
def set_confidence(ctx, code, competency_id, confidence=None, mark_reviewed=False):
    c = _course(ctx.conn, code)
    cid = _comp_id(ctx.conn, c, competency_id)
    if cid is None:
        raise ToolError("competency_id is required.")
    if confidence is not None:
        try:
            confidence = int(confidence)
        except (TypeError, ValueError):
            raise ToolError("confidence must be 1-5.")
        if not 1 <= confidence <= 5:
            raise ToolError("confidence must be 1-5.")
    elif not mark_reviewed:
        raise ToolError("Give a confidence, or mark_reviewed: true.")
    text = ctx.conn.execute("SELECT text FROM competencies WHERE id = ?", (cid,)).fetchone()[0]
    return _propose(ctx, "confidence", c, {"competency_id": cid, "competency": text, "confidence": confidence,
                                           "mark_reviewed": bool(mark_reviewed)}, f"confidence for “{text[:60]}”")


@tool("schedule_exam", "write", "proposed exam date",
      "Propose setting a course's objective-assessment/exam date (YYYY-MM-DD) and optionally its practice-quiz target %.",
      {"code": CODE, "date": {"type": "string", "description": "YYYY-MM-DD"},
       "quiz_target": {"type": "integer", "description": "Percent, 1-100 (optional)"}}, ["code", "date"])
def schedule_exam(ctx, code, date, quiz_target=None):
    c = _course(ctx.conn, code)
    d = clock.parse_date(str(date))
    if not d:
        raise ToolError("date must be YYYY-MM-DD.")
    if quiz_target is not None and not (isinstance(quiz_target, (int, float)) and 1 <= int(quiz_target) <= 100):
        raise ToolError("quiz_target must be 1-100.")
    return _propose(ctx, "exam", c, {"date": d.isoformat(), "quiz_target": int(quiz_target) if quiz_target else None},
                    f"{c['code']} exam on {d.isoformat()}")


@tool("start_quiz", "write", "proposed quiz",
      "Offer the student a practice quiz from a course's question bank (they press Start to take it in the app).",
      {"code": CODE, "count": {"type": "integer", "description": "Number of questions, 1-50 (default 10)"},
       "competency_id": COMP}, ["code"])
def start_quiz(ctx, code, count=10, competency_id=None):
    c = _course(ctx.conn, code)
    cid = _comp_id(ctx.conn, c, competency_id)
    available = len(quizzes.bank(ctx.conn, c["id"], cid))
    if not available:
        raise ToolError("No questions in that bank yet. Propose some with add_questions first.")
    try:
        count = max(1, min(50, int(count or 10), available))
    except (TypeError, ValueError):
        count = min(10, available)
    return _propose(ctx, "quiz", c, {"count": count, "competency_id": cid}, f"{count}-question {c['code']} quiz")


@tool("log_study_time", "write", "proposed study log",
      "Propose logging study time the student did away from the timer.",
      {"date": {"type": "string", "description": "YYYY-MM-DD"}, "minutes": {"type": "integer", "description": "1-960"},
       "code": CODE}, ["date", "minutes"])
def log_study_time(ctx, date, minutes, code=None):
    c = _course(ctx.conn, code) if code else None
    d = clock.parse_date(str(date))
    if not d or d > clock.today():
        raise ToolError("date must be YYYY-MM-DD and not in the future.")
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        raise ToolError("minutes must be a number.")
    if not 0 < m <= 960:
        raise ToolError("minutes must be 1-960.")
    return _propose(ctx, "study", c, {"date": d.isoformat(), "minutes": m},
                    f"{m} min of study{' on ' + c['code'] if c else ''} on {d.isoformat()}")


def _future_date(value, field="due_on"):
    d = clock.parse_date(str(value)) if value else None
    if value and not d:
        raise ToolError(f"{field} must be YYYY-MM-DD.")
    if d and d < clock.today():
        raise ToolError(f"{field} is in the past (today is {clock.today().isoformat()}).")
    return d.isoformat() if d else None


@tool("propose_tasks", "write", "proposed tasks",
      "Propose study tasks for the student's plan (they edit and Apply them). Small and concrete: one sitting each, "
      "with a due date and minutes where you can. done_rule: manual (default; only the student marks it done), "
      "cards_reviewed (the app closes it once the student has cleared that course's due cards) or quiz_finished (the "
      f"app closes it when they finish a quiz). Up to {MAX_TASKS} per call.",
      {"tasks": {"type": "array", "description": f"Up to {MAX_TASKS} tasks", "items": {"type": "object", "properties": {
          "title": {"type": "string"}, "code": CODE, "competency_id": COMP,
          "kind": {"type": "string", "enum": list(study_tasks.KINDS)},
          "due_on": {"type": "string", "description": "YYYY-MM-DD, today or later"},
          "est_minutes": {"type": "integer"}, "priority": {"type": "integer", "description": "1 high, 2 normal, 3 low"},
          "done_rule": {"type": "string", "enum": list(study_tasks.RULES)},
          "note": {"type": "string", "description": "A note name or short pointer (optional)"}},
          "required": ["title"]}}},
      ["tasks"])
def propose_tasks(ctx, tasks):
    conn, items = ctx.conn, []
    for x in (tasks if isinstance(tasks, list) else [])[:MAX_TASKS]:
        if not isinstance(x, dict) or not str(x.get("title") or "").strip():
            continue
        c = _course(conn, x["code"]) if x.get("code") else None
        try:
            v = study_tasks.check(conn, x["title"], course_id=c["id"] if c else None,
                                  competency_id=_comp_id(conn, c, x.get("competency_id")) if c else None,
                                  note=x.get("note"), kind=x.get("kind") or "study", due_on=_future_date(x.get("due_on")),
                                  est_minutes=x.get("est_minutes"), priority=x.get("priority") or 2,
                                  done_rule=x.get("done_rule") or "manual")
        except study_tasks.TaskError as e:
            raise ToolError(f"“{str(x['title'])[:40]}”: {e}")
        items.append({**v, "code": c["code"] if c else None})
    if not items:
        raise ToolError("No usable tasks: each needs a title.")
    courses = {i["course_id"] for i in items}
    one = conn.execute("SELECT * FROM courses WHERE id = ?", (courses.pop(),)).fetchone() if len(courses) == 1 and None not in courses else None
    return _propose(ctx, "tasks", one, {"tasks": items}, f"{len(items)} study task{'s' * (len(items) != 1)}")


@tool("propose_task_update", "write", "proposed task change",
      "Propose changing one open study task: its status, due date, priority or title (get the id from list_tasks). "
      "You can't mark a task done if its done_rule is cards_reviewed or quiz_finished: the app closes those from the "
      "student's real reviews and quizzes. Only propose done for a manual task when the student says they finished it.",
      {"task_id": {"type": "integer", "description": "From list_tasks"},
       "status": {"type": "string", "enum": list(study_tasks.STATUSES)},
       "due_on": {"type": "string", "description": "YYYY-MM-DD, today or later"},
       "priority": {"type": "integer", "description": "1 high, 2 normal, 3 low"}, "title": {"type": "string"}},
      ["task_id"])
def propose_task_update(ctx, task_id, status=None, due_on=None, priority=None, title=None):
    conn = ctx.conn
    try:
        t = study_tasks.get(conn, int(task_id))
    except (TypeError, ValueError):
        raise ToolError("task_id must be a number from list_tasks.")
    if not t:
        raise ToolError(f"No task {task_id}. Call list_tasks for the ids.")
    if t["status"] not in study_tasks.OPEN:
        raise ToolError(f"Task {t['id']} is already {t['status']}; it can't be changed from here.")
    changes = {}
    if status is not None:
        if status not in study_tasks.STATUSES:
            raise ToolError("status must be todo, doing, blocked, done or cancelled.")
        if status == "done" and t["done_rule"] != "manual":
            raise ToolError(f"Task {t['id']} closes itself from the student's real reviews or quizzes (done_rule "
                            f"{t['done_rule']}); I can't mark it done. Leave it open.")
        changes["status"] = status
    if due_on:
        changes["due_on"] = _future_date(due_on)
    if priority is not None:
        if str(priority) not in ("1", "2", "3"):
            raise ToolError("priority must be 1 (high), 2 (normal) or 3 (low).")
        changes["priority"] = int(priority)
    if title:
        changes["title"] = _text(title, "title", study_tasks.TITLE_CHARS)
    changes = {k: v for k, v in changes.items() if v is not None and v != t[k]}
    if not changes:
        raise ToolError("Nothing to change: give a different status, due_on, priority or title.")
    course = conn.execute("SELECT * FROM courses WHERE id = ?", (t["course_id"],)).fetchone() if t["course_id"] else None
    return _propose(ctx, "task_update", course, {"task_id": t["id"], "title": t["title"], "changes": changes},
                    f"change task “{t['title'][:60]}”")


BUILD_MODES = {"all": "build {code} from its notes ({depth})", "fill": "fill the uncovered sections of {code} ({depth})",
               "refresh": "check out-of-date {code} cards and questions", "overview": "write the {code} course overview"}


@tool("propose_build_from_notes", "write", "proposed build from notes",
      "Offer to set a course up from its own notes: it reads every section, then drafts competencies, flashcards, practice "
      "questions, a study plan and an overview summary, all from what the notes say. It runs in the background and takes a "
      "few minutes (the student's free AI tier is rate-limited); the student reviews the draft on the course's From notes "
      "page before anything is added. Use this when asked to set up, populate or fill in a course from its notes, instead "
      "of writing all of it yourself. Optional depth: light, normal or thorough. mode 'fill' writes cards and questions only "
      "for sections that have none yet; mode 'refresh' checks the cards and questions made from sections the student has "
      "edited since (get_course_brain shows both) and suggests keeping, updating or deleting each. mode 'overview' writes the "
      "course overview at the top of its Overview page, from the notes, or from its competency list if there are no notes.",
      {"code": CODE, "depth": {"type": "string", "enum": list(brain.DEPTHS), "description": "How many cards and questions"},
       "mode": {"type": "string", "enum": list(BUILD_MODES), "description": "all (the default), fill, refresh or overview"}},
      ["code"])
def propose_build_from_notes(ctx, code, depth="normal", mode="all"):
    c = _course(ctx.conn, code)
    mode = mode if mode in BUILD_MODES else "all"
    if mode == "overview":
        if not brain.sections(ctx.conn, c["id"]) and not comp.list_for(ctx.conn, c["id"]):
            raise ToolError(f"{c['code']} has no notes and no competency list yet, so there's nothing to write an overview from.")
        return _propose(ctx, "brain_build", c, {"depth": "normal", "mode": mode}, BUILD_MODES[mode].format(code=c["code"], depth=""))
    if not brain.sections(ctx.conn, c["id"]):
        raise ToolError(f"{c['code']} has no notes to build from yet. Ask the student to write some in the Notebook.")
    depth = depth if depth in brain.DEPTHS else "normal"
    cov = brain.coverage(ctx.conn, c["id"])
    if mode == "fill" and not cov["uncovered"]:
        raise ToolError("Every section already has cards or questions; there's nothing to fill.")
    if mode == "refresh" and not cov["stale"]:
        raise ToolError("Nothing is out of date: every card and question made from the notes still matches them.")
    return _propose(ctx, "brain_build", c, {"depth": depth, "mode": mode}, BUILD_MODES[mode].format(code=c["code"], depth=depth))


# ---------------------------------------------------------------- memory: saved at once (it's the agent's own notes
# about the student, not their data), shown in the chat and on /ask/memory, where the student can delete any of it

def add_memory(conn, text, course_id=None, thread_id=None) -> int:
    text = re.sub(r"\s+", " ", str(text or "")).strip()[:MEMORY_TEXT]
    if not text:
        raise ValueError("The memory is empty.")
    with conn:
        cur = conn.execute("INSERT INTO agent_memory(course_id, text, thread_id, created_at) VALUES (?, ?, ?, ?)",
                           (course_id, text, thread_id, clock.now().isoformat()))
    return cur.lastrowid


def memories(conn) -> list:
    return conn.execute("SELECT m.*, c.code FROM agent_memory m LEFT JOIN courses c ON c.id = m.course_id "
                        "ORDER BY m.id DESC").fetchall()


def delete_memory(conn, mid) -> bool:
    with conn:
        return conn.execute("DELETE FROM agent_memory WHERE id = ?", (mid,)).rowcount > 0


@tool("remember", "memory", "saved to memory",
      "Save one short, lasting fact about the student to your memory, which you see at the start of every chat: weak "
      "topics from a pre-assessment, goals, deadlines they mention, how they like to study, what they've mastered. "
      "One fact per call, one sentence. Not for things already stored in the vault (cards, dates, notes).",
      {"text": {"type": "string", "description": "The fact, one sentence"},
       "code": {"type": "string", "description": "Course code if it's about one course (optional)"}}, ["text"])
def remember(ctx, text, code=None):
    c = _course(ctx.conn, code) if code else None
    try:
        mid = add_memory(ctx.conn, text, c["id"] if c else None, ctx.thread["id"])
    except ValueError as e:
        raise ToolError(str(e))
    return {"memory_id": mid, "saved": True}


@tool("forget", "memory", "removed from memory",
      "Delete one memory that is wrong or no longer true (e.g. a weak topic the student has now mastered).",
      {"memory_id": {"type": "integer", "description": "The id shown in your memory list"}}, ["memory_id"])
def forget(ctx, memory_id):
    try:
        mid = int(memory_id)
    except (TypeError, ValueError):
        raise ToolError("memory_id must be a number.")
    if not delete_memory(ctx.conn, mid):
        raise ToolError(f"No memory {mid}.")
    return {"deleted": mid}


def _memory_block(conn) -> str:
    lines, used = [], 0
    for m in memories(conn):
        line = f"- [{m['id']}]{' ' + m['code'] if m['code'] else ''} ({m['created_at'][:10]}) {m['text']}"
        if used + len(line) > MEMORY_CHARS:
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return "\n\nYour memory is empty so far."
    return ("\n\nWhat you remember about the student (newest first; [id] is for forget). Use it, but trust the vault "
            "if they disagree:\n" + "\n".join(lines))


# ---------------------------------------------------------------- the loop

def schemas() -> list[dict]:
    return [t["schema"] for t in TOOLS.values()]


def run_tool(ctx, call: ai.Call) -> str:
    t = TOOLS.get(call.name)
    if not t:
        result = {"error": f"There is no action “{call.name}”."}
    else:
        props = t["schema"]["parameters"]["properties"]
        args = {k: v for k, v in (call.args if isinstance(call.args, dict) else {}).items() if k in props and v is not None}
        missing = [k for k in t["schema"]["parameters"]["required"] if k not in args]
        try:
            if missing:
                raise ToolError(f"Missing {', '.join(missing)}.")
            result = t["fn"](ctx, **args)
        except (ToolError, ValueError) as e:
            result = {"error": str(e)}
        except Exception:  # a bug in a tool must not take the chat down
            log.exception("agent tool %s failed", call.name)
            result = {"error": "That action failed inside studyvault."}
    text = json.dumps(result, default=str, ensure_ascii=False)
    limit = (NOTE_CHARS + 500 if call.name in ("read_note", "get_mistakes", "read_note_section") else
             LIST_CHARS if call.name in ("list_flashcards", "list_questions", "list_note_sections", "get_course_brain") else RESULT_CHARS)
    return text if len(text) <= limit else text[:limit] + "…(truncated)"


def _use_label(call: ai.Call) -> str:
    t = TOOLS.get(call.name)
    label = t["label"] if t else call.name
    args = call.args if isinstance(call.args, dict) else {}
    if call.name == "search_notes" and args.get("query"):
        label += f" “{str(args['query'])[:40]}”"
    if call.name == "read_note" and args.get("name"):
        label = str(args["name"])
    said = re.sub(r"\s+", " ", str(args.get("text") or "")).strip()
    if call.name == "remember" and said:
        label += f" “{said[:80]}”"
    return f"{str(args['code']).upper()} {label}" if args.get("code") else label


SYSTEM = """You are the study agent inside studyvault, a private study notebook for one WGU student in the B.S. Cloud & Network Engineering (AWS) program. Talk to the student as "you". Today is {today} ({weekday}).{scope}

How to work:
- Facts about the student's own courses, notes, flashcards, quizzes, progress, dates and schedule come only from your tools. Look them up; never guess them. If a lookup is empty, say so.
- Requests are rate-limited, so gather what you need in as few steps as possible: call several tools at once rather than one per step, and don't repeat a lookup you already have.
- The student's notes are the source for their course material. A long notebook is cut off by read_note: list_note_sections shows every section with a key, and read_note_section reads one in full. When you write flashcards or questions from the notes, read the section first, base them on what it says, and pass its `section_key` so each item is linked to where it came from. Where you add anything the notes don't state, say so; if the notes don't cover something the student asks about, say that and offer to propose a notebook section.
- To set a course up from its notes (competencies, cards, questions, a study plan), use propose_build_from_notes: it reads all the notes and drafts everything for the student to review, which you can't do within your lookup limit. get_course_brain tells you what the notes cover, what's still missing and which items are out of date; offer mode fill for sections with no cards or questions, mode refresh for out-of-date items, and mode overview to write the course overview.
- For subject knowledge (networking, AWS, Linux, security, IT) answer from what you know, and say when it differs from the student's notes.
- Changes (adding, editing or deleting flashcards; adding or deleting questions; notebook sections, confidence ratings, exam dates, quizzes, study time, study tasks) are proposals: calling a write action shows the student a card with an Apply button just below your reply, and nothing is saved until they press it. Say you've proposed it; never say it was saved or added. To edit or delete, look the items up first (list_flashcards / list_questions) and pass their ids; use all=true only when the student asks for everything. Never say you can't edit or delete cards or questions.
- When the student gives you a list (topics to learn, terms from a pre-assessment, missed questions), cover EVERY item: at least one flashcard per term, more where a term has two things worth knowing. Fill in answers from your own knowledge where their notes have none. A long list takes several add_flashcards calls in the same step, so make them all at once. Then say how many cards you proposed and name any item you left out and why.
- You have a memory that carries across chats (below). Use `remember` for lasting facts worth knowing next time: weak topics and scores from a pre-assessment, goals, exam plans, how they like to study. Save a pre-assessment's weak topics as a few short memories, not one per term. Use `forget` when a memory turns out wrong or stale. Mention briefly what you saved.
- Study tasks are the student's plan, not a record of work done. Look at list_tasks before planning; propose small, concrete tasks (one sitting, a date, minutes) rather than a long wish list. A task with done_rule cards_reviewed or quiz_finished is closed by the app when the student really reviews cards or finishes a quiz: you can't mark it done, and adding cards, questions or notes never completes anything. Never say work or a task is done without evidence from your tools (list_tasks shows it); propose "done" only for a manual task, and only when the student says they finished it.
- Performance assessments (PAs) must be the student's own work. Never draft, outline, rewrite or grade PA submissions or task answers, even if asked; you cannot see PA drafts. You may explain the underlying concepts.
- To quiz the student in the chat: ask one question at a time, wait for the answer, then give brief feedback and the next question.
- Be concise and practical. Markdown is rendered. Refer to courses by code; writing [[D413]] links to that course. Use dates like "Mon Oct 5"."""


def system_prompt(conn, thread) -> str:
    scope = ""
    if thread["course_id"]:
        c = conn.execute("SELECT code, title FROM courses WHERE id = ?", (thread["course_id"],)).fetchone()
        if c:
            scope = (f"\nThis chat was opened from course {c['code']} ({c['title']}): assume questions are about it "
                     "unless the student says otherwise." + brain.prompt_block(conn, thread["course_id"]))
    today = clock.today()
    return SYSTEM.format(today=today.isoformat(), weekday=today.strftime("%A"), scope=scope) + _memory_block(conn)


def _history(conn, thread_id) -> list[dict]:
    rows = conn.execute("SELECT id, role, text FROM agent_messages WHERE thread_id = ? ORDER BY id DESC LIMIT ?",
                        (thread_id, HISTORY_MESSAGES)).fetchall()[::-1]
    msgs = []
    for r in rows:
        text = r["text"]
        if r["role"] == "assistant":
            props = conn.execute("SELECT id, kind, status FROM agent_proposals WHERE message_id = ?", (r["id"],)).fetchall()
            text += "".join(f"\n[proposal {p['id']} ({p['kind']}): {p['status']}]" for p in props)
            if not text.strip():
                continue  # a turn that failed
        if msgs and msgs[-1]["role"] == r["role"]:
            msgs[-1]["text"] += "\n\n" + text
        else:
            msgs.append({"role": r["role"], "text": text})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


def _add_message(conn, thread_id, role, text, meta=None) -> int:
    now = clock.now().isoformat()
    with conn:
        cur = conn.execute("INSERT INTO agent_messages(thread_id, role, text, meta_json, created_at) VALUES (?, ?, ?, ?, ?)",
                           (thread_id, role, text, json.dumps(meta or {}), now))
        conn.execute("UPDATE agent_threads SET updated_at = ? WHERE id = ?", (now, thread_id))
    return cur.lastrowid


def create_thread(conn, first_message: str, course_id=None) -> int:
    title = re.sub(r"\s+", " ", first_message).strip()
    title = title if len(title) <= 60 else title[:59].rstrip() + "…"
    now = clock.now().isoformat()
    with conn:
        cur = conn.execute("INSERT INTO agent_threads(title, course_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                           (title or "New chat", course_id, now, now))
    return cur.lastrowid


# What each running reply has done so far, for the "Thinking…" line in the chat (GET /ask/{id}/progress).
# In memory: the app is one process, and a reply only lives as long as its request.
_progress: dict[int, dict] = {}


def reply_progress(thread_id: int) -> dict | None:
    p = _progress.get(thread_id)
    return {"seconds": int(time.time() - p["started"]), "steps": list(p["steps"]), "round": p["round"]} if p else None


def reply(conn, thread_id: int, text: str) -> int:
    """Store the user's message, run the model with tools until it answers, store and return the assistant message id.
    AI failures are stored on the assistant message (meta.error) rather than raised."""
    text = (text or "").strip()[:MAX_MESSAGE_CHARS]
    if not text:
        raise ValueError("Type a message first.")
    thread = conn.execute("SELECT * FROM agent_threads WHERE id = ?", (thread_id,)).fetchone()
    _add_message(conn, thread_id, "user", text)
    ctx = Ctx(conn, thread)
    msgs = _history(conn, thread_id)
    used, out, error = [], "", None
    state = _progress[thread_id] = {"started": time.time(), "steps": [], "round": 0}
    try:
        for n in range(MAX_ROUNDS):
            state["round"] = n + 1
            # a turn that writes 40+ cards needs far more than a chat answer, and takes longer
            turn = ai.chat(system_prompt(conn, thread), msgs, schemas(),
                           max(settings.ai_max_output_tokens, ai.BULK_MAX_TOKENS), ai.BULK_TIMEOUT_S)
            if not turn.calls:
                out = turn.text
                break
            msgs.append({"role": "assistant", "turn": turn})
            results = []
            for i, call in enumerate(turn.calls):
                if i < MAX_CALLS_PER_ROUND:
                    used.append(_use_label(call))
                    state["steps"].append(used[-1])
                    results.append((call, run_tool(ctx, call)))
                else:  # every call still needs an answer
                    results.append((call, json.dumps({"error": "Too many actions at once; this one was skipped."})))
            msgs.append({"role": "tool", "results": results})
        else:
            out = (turn.text + "\n\n" if turn.text else "") + (
                f"*I hit the limit of {MAX_ROUNDS} lookups for one message. Say “continue” and I'll carry on.*")
        if turn.truncated:
            out += "\n\n*(cut off at the output limit)*"
    except ai.AIError as e:
        error = str(e)
    finally:
        _progress.pop(thread_id, None)
    meta = {"tools": list(dict.fromkeys(used))}
    if error:
        meta["error"] = error
    mid = _add_message(conn, thread_id, "assistant", out, meta)
    if ctx.proposals:
        with conn:
            conn.execute(f"UPDATE agent_proposals SET message_id = ? WHERE id IN ({','.join('?' * len(ctx.proposals))})",
                         (mid, *ctx.proposals))
    return mid


# ---------------------------------------------------------------- Apply

def _course_by_id(conn, course_id):
    row = conn.execute("SELECT code FROM courses WHERE id = ?", (course_id,)).fetchone() if course_id else None
    return catalog.get_course(conn, row[0]) if row else None


def _still_comp(conn, course, cid):
    if cid and not conn.execute("SELECT 1 FROM competencies WHERE id = ? AND course_id = ?", (cid, course["id"])).fetchone():
        return None
    return cid


def apply(conn, p, form) -> tuple[str, str | None]:
    """Carry out a pending proposal with the user's edits from `form`. Returns (what happened, link to it)."""
    if p["status"] != "pending":
        raise ApplyError("This was already handled.")
    payload = json.loads(p["payload_json"])
    c = _course_by_id(conn, p["course_id"])
    if p["course_id"] and not c:
        raise ApplyError("That course no longer exists.")
    kind, today = p["kind"], clock.today().isoformat()
    base = f"/courses/{c['code']}" if c else ""

    if kind == "cards":
        cid, added = _still_comp(conn, c, payload.get("competency_id")), 0
        for i in form.getlist("keep"):
            front, back = str(form.get(f"front_{i}", "")).strip(), str(form.get(f"back_{i}", "")).strip()
            if front and back:
                cards.add(conn, c["id"], front, back, competency_id=cid, source="ai-accepted", source_ref=payload.get("source"))
                added += 1
        if not added:
            raise ApplyError("Keep at least one card, or press Dismiss.")
        result, link = f"Added {added} card{'s' * (added != 1)} to {c['code']}.", f"{base}/cards"
    elif kind == "questions":
        cid, saved = _still_comp(conn, c, payload.get("competency_id")), 0
        for i in form.getlist("keep"):
            try:
                quizzes.save(conn, c["id"], {"kind": form.get(f"kind_{i}", "mc"), "prompt": form.get(f"prompt_{i}", ""),
                                             "choices": form.get(f"choices_{i}", ""),
                                             "explanation": form.get(f"explanation_{i}", ""),
                                             "competency_id": str(cid or "")}, source="ai-accepted",
                             source_ref=payload.get("source"))
                saved += 1
            except quizzes.QuestionError:
                continue  # an edit broke it; keep the rest
        if not saved:
            raise ApplyError("No question could be saved: each needs a prompt and a choice marked with *.")
        result, link = f"Added {saved} question{'s' * (saved != 1)} to the {c['code']} bank.", f"{base}/quizzes"
    elif kind == "notebook":
        title = str(form.get("title") or payload["title"]).strip()
        text = str(form.get("text") or "").strip()
        if not text:
            raise ApplyError("The section is empty.")
        notes_fs.append_note(conn, c, "notebook", f"## {title} · {today}\n\n{text}\n")
        result, link = f"Added “{title}” to the {c['code']} notebook.", f"{base}/notes/notebook"
    elif kind == "confidence":
        cid = _still_comp(conn, c, payload["competency_id"])
        if not cid:
            raise ApplyError("That competency no longer exists.")
        value = str(form.get("confidence", "")).strip()
        if value:
            comp.set_confidence(conn, cid, int(value))
            result = f"Confidence set to {value}/5."
        elif form.get("mark_reviewed"):
            comp.mark_reviewed(conn, cid)
            result = "Marked reviewed today."
        else:
            raise ApplyError("Pick a confidence or tick Mark reviewed.")
        link = f"{base}/competencies"
    elif kind == "exam":
        try:
            assessments.schedule_exam(conn, c, str(form.get("date", "")), str(form.get("quiz_target", "")))
        except assessments.AssessmentError as e:
            raise ApplyError(str(e))
        result, link = f"{c['code']} exam set for {form.get('date')}.", f"{base}/assessment"
    elif kind == "brain_build":
        try:
            mode = payload.get("mode") or "all"
            if mode != "all" and brain.pending_draft(conn, c["id"]):
                raise ApplyError("A draft is waiting on the From notes page. Add or dismiss it first, so it isn't replaced.")
            brain.start(c, "build" if mode == "all" else mode, str(form.get("depth") or payload.get("depth") or "normal"))
        except ai.AIError as e:
            raise ApplyError(str(e))
        result = (f"Started {'building' if mode == 'all' else 'working on'} {c['code']} from its notes. It takes a few minutes; "
                  "the draft will be waiting on its From notes page.")
        link = f"{base}/brain"
    elif kind == "quiz":
        try:
            count = max(1, min(50, int(form.get("count") or payload["count"])))
            attempt = quizzes.start(conn, c["id"], count, _still_comp(conn, c, payload.get("competency_id")))
        except (ValueError, quizzes.QuestionError) as e:
            raise ApplyError(str(e))
        result, link = f"Started a {count}-question quiz.", f"/quizzes/{attempt}"
    elif kind == "study":
        from .routers.sessions import add_manual
        try:
            add_manual(conn, c, str(form.get("date", "")), str(form.get("minutes", "")))
        except ValueError as e:
            raise ApplyError(str(e))
        result, link = f"Logged {form.get('minutes')} min.", "/sessions"
    elif kind == "tasks":
        made, items = 0, payload["tasks"]
        try:
            with conn:  # all or none
                for i in form.getlist("keep"):
                    if not str(i).isdigit() or int(i) >= len(items):
                        continue
                    it, i = items[int(i)], int(i)
                    cid = it["competency_id"] if conn.execute("SELECT 1 FROM competencies WHERE id = ?", (it["competency_id"],)).fetchone() else None
                    study_tasks.insert(conn, form.get(f"title_{i}") or it["title"], course_id=it["course_id"], competency_id=cid,
                                       note=it["note"], kind=it["kind"], due_on=form.get(f"due_{i}", it["due_on"]),
                                       est_minutes=form.get(f"minutes_{i}", it["est_minutes"]),
                                       priority=form.get(f"priority_{i}") or it["priority"], done_rule=it["done_rule"],
                                       source=f"proposal:{p['id']}")
                    made += 1
        except study_tasks.TaskError as e:
            raise ApplyError(str(e))
        if not made:
            raise ApplyError("Keep at least one task, or press Dismiss.")
        result, link = f"Added {made} task{'s' * (made != 1)}.", f"{base}/tasks" if c else "/tasks"
    elif kind == "task_update":
        t = study_tasks.get(conn, payload["task_id"])
        if not t or t["status"] not in study_tasks.OPEN:
            raise ApplyError("That task is gone or already closed.")
        want = lambda k: (form.get(k) if form.get(k) is not None else payload["changes"].get(k)) or None  # noqa: E731
        status = want("status")
        if status == "done" and t["done_rule"] != "manual":
            raise ApplyError(f"This task closes itself when {study_tasks.RULES[t['done_rule']]}; it can't be marked done here.")
        fields = {k: v for k, v in {"title": want("title"), "due_on": want("due_on"), "priority": want("priority")}.items()
                  if v and str(v) != str(t[k])}
        try:
            study_tasks.update(conn, t["id"], **fields)
            if status == "done":
                study_tasks.complete(conn, t["id"], {"rule": "manual", "by": "you", "via": f"proposal:{p['id']}"})
            elif status:
                study_tasks.set_status(conn, t["id"], status)
        except study_tasks.TaskError as e:
            raise ApplyError(str(e))
        result, link = f"Updated “{t['title'][:60]}”.", f"{base}/tasks" if c else "/tasks"
    elif kind in ("delete_cards", "delete_questions"):
        table, noun = ("cards", "card") if kind == "delete_cards" else ("questions", "question")
        ids = [int(i) for i in form.getlist("keep") if str(i).isdigit()]
        if not ids:
            raise ApplyError(f"Tick at least one {noun} to delete, or press Dismiss.")
        with conn:  # only rows still in this course; reviews go with their cards (ON DELETE CASCADE)
            gone = conn.execute(f"DELETE FROM {table} WHERE course_id = ? AND id IN ({','.join('?' * len(ids))})",
                                (c["id"], *ids)).rowcount
        result = f"Deleted {gone} {noun}{'s' * (gone != 1)} from {c['code']}."
        link = f"{base}/cards" if table == "cards" else f"{base}/quizzes"
    elif kind == "edit_cards":
        changed = 0
        with conn:
            for i in form.getlist("keep"):
                if not str(i).isdigit() or int(i) >= len(payload["cards"]):
                    continue
                front, back = str(form.get(f"front_{i}", "")).strip(), str(form.get(f"back_{i}", "")).strip()
                if front and back:
                    changed += conn.execute("UPDATE cards SET front = ?, back = ? WHERE id = ? AND course_id = ?",
                                            (front, back, payload["cards"][int(i)]["id"], c["id"])).rowcount
        if not changed:
            raise ApplyError("No card was changed: keep at least one, with a front and a back (it may have been deleted).")
        result, link = f"Updated {changed} card{'s' * (changed != 1)} in {c['code']}.", f"{base}/cards"
    else:
        raise ApplyError("Unknown proposal.")
    with conn:
        conn.execute("UPDATE agent_proposals SET status = 'applied', result = ?, link = ? WHERE id = ?", (result, link or '', p["id"]))
    return result, link


def dismiss(conn, p) -> None:
    if p["status"] == "pending":
        with conn:
            conn.execute("UPDATE agent_proposals SET status = 'dismissed' WHERE id = ?", (p["id"],))
