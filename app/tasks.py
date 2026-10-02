"""Study tasks: plan real work, and close it from real evidence.

A task is a title with an optional course, competency, due date, estimate and priority (1 high, 2 normal, 3 low).
How it gets done is its done_rule:
  manual          only the student's Done (or an Apply they approve)
  cards_reviewed  closed by cards.grade once the course's (competency's) due cards are cleared, with at least one
                  review made today after the task was created
  quiz_finished   closed by quizzes.finish when a quiz in the course finishes after the task was created
Generated content never counts: adding cards, questions or notes (by hand or from an Ask proposal) is not a review or
a quiz attempt, so it closes nothing. `evidence` (JSON) records what closed a task.
"""
import json
import logging
from datetime import timedelta

from . import clock

log = logging.getLogger("studyvault.tasks")

KINDS = {"study": "Study", "review_cards": "Review cards", "quiz": "Quiz", "read": "Read", "write": "Write", "other": "Other"}
STATUSES = {"todo": "To do", "doing": "In progress", "blocked": "Blocked", "done": "Done", "cancelled": "Cancelled"}
OPEN = ("todo", "doing", "blocked")
RULES = {"manual": "you mark it done", "cards_reviewed": "you clear this course's due cards",
         "quiz_finished": "you finish a quiz in this course"}
PRIORITIES = {1: "High", 2: "Normal", 3: "Low"}
TITLE_CHARS = 200
NOTE_CHARS = 200

_SELECT = ("SELECT t.*, c.code, k.text AS comp_text FROM study_tasks t LEFT JOIN courses c ON c.id = t.course_id "
           "LEFT JOIN competencies k ON k.id = t.competency_id")
# Overdue and today first, then priority, then the soonest date (undated last), then oldest.
_ORDER = " ORDER BY (t.due_on IS NOT NULL AND t.due_on <= ?) DESC, t.priority, t.due_on IS NULL, t.due_on, t.id"


class TaskError(ValueError):
    pass


def _date(value):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return clock.parse_date(value).isoformat()
    except ValueError:
        raise TaskError(f"“{value}” isn't a date (YYYY-MM-DD).")


def _int(value, field, lo, hi):
    if value in (None, ""):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise TaskError(f"{field} must be a whole number.")
    if not lo <= n <= hi:
        raise TaskError(f"{field} must be {lo}-{hi}.")
    return n


def _course_id(conn, value):
    if value in (None, "", 0, "0"):
        return None
    if not str(value).isdigit() or not conn.execute("SELECT 1 FROM courses WHERE id = ?", (int(value),)).fetchone():
        raise TaskError("That course no longer exists.")
    return int(value)


def _comp(conn, course_id, value):
    """Returns (course_id, competency_id); a competency pulls its course in, and must belong to the one given."""
    if value in (None, "", 0, "0"):
        return course_id, None
    row = conn.execute("SELECT course_id FROM competencies WHERE id = ?", (int(value),)).fetchone() if str(value).isdigit() else None
    if not row or (course_id and row[0] != course_id):
        raise TaskError("That competency isn't in this course.")
    return row[0], int(value)


def _choice(value, allowed, field):
    value = str(value or "").strip()
    if value not in allowed:
        raise TaskError(f"{field} must be one of: {', '.join(allowed)}.")
    return value


def check(conn, title, *, course_id=None, competency_id=None, note="", kind="study", due_on=None, est_minutes=None,
          priority=2, done_rule="manual") -> dict:
    """Validate and clean the fields of a new task; raises TaskError. Used by insert and by Ask's proposals."""
    title = " ".join(str(title or "").split())[:TITLE_CHARS]
    if not title:
        raise TaskError("A task needs a title.")
    course_id, competency_id = _comp(conn, _course_id(conn, course_id), competency_id)
    return {"title": title, "course_id": course_id, "competency_id": competency_id,
            "note": " ".join(str(note or "").split())[:NOTE_CHARS], "kind": _choice(kind, KINDS, "kind"),
            "due_on": _date(due_on), "est_minutes": _int(est_minutes, "Minutes", 1, 1440),
            "priority": _int(priority, "Priority", 1, 3) or 2, "done_rule": _choice(done_rule, RULES, "done_rule")}


def insert(conn, title, *, source="manual", **fields) -> int:
    """Insert a task without committing (callers wrap it in `with conn:`)."""
    v = check(conn, title, **fields)
    now = clock.now().isoformat()
    cur = conn.execute(
        "INSERT INTO study_tasks(course_id, competency_id, note, title, kind, due_on, est_minutes, priority, source, "
        "done_rule, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (v["course_id"], v["competency_id"], v["note"], v["title"], v["kind"], v["due_on"], v["est_minutes"],
         v["priority"], str(source or "manual"), v["done_rule"], now, now))
    return cur.lastrowid


def add(conn, title, **fields) -> int:
    with conn:
        return insert(conn, title, **fields)


def get(conn, task_id):
    return conn.execute(_SELECT + " WHERE t.id = ?", (task_id,)).fetchone()


EDITABLE = ("title", "course_id", "competency_id", "note", "kind", "due_on", "est_minutes", "priority", "done_rule")


def update(conn, task_id, **fields) -> None:
    """Change the given fields (only keys passed are touched). status goes through set_status."""
    t = get(conn, task_id)
    if not t:
        raise TaskError("That task no longer exists.")
    unknown = set(fields) - set(EDITABLE) - {"status"}
    if unknown:
        raise TaskError(f"Can't change {', '.join(sorted(unknown))}.")
    new = {k: fields[k] for k in EDITABLE if k in fields}
    if "course_id" in new and "competency_id" not in new:
        new["competency_id"] = t["competency_id"] if t["competency_id"] and _course_id(conn, new["course_id"]) == t["course_id"] else None
    sets = {}
    if "title" in new:
        sets["title"] = " ".join(str(new["title"] or "").split())[:TITLE_CHARS]
        if not sets["title"]:
            raise TaskError("A task needs a title.")
    if "course_id" in new or "competency_id" in new:
        sets["course_id"], sets["competency_id"] = _comp(conn, _course_id(conn, new.get("course_id", t["course_id"])),
                                                         new.get("competency_id", t["competency_id"]))
    if "note" in new:
        sets["note"] = " ".join(str(new["note"] or "").split())[:NOTE_CHARS]
    if "kind" in new:
        sets["kind"] = _choice(new["kind"], KINDS, "kind")
    if "due_on" in new:
        sets["due_on"] = _date(new["due_on"])
    if "est_minutes" in new:
        sets["est_minutes"] = _int(new["est_minutes"], "Minutes", 1, 1440)
    if "priority" in new:
        sets["priority"] = _int(new["priority"], "Priority", 1, 3) or 2
    if "done_rule" in new:
        sets["done_rule"] = _choice(new["done_rule"], RULES, "done_rule")
    with conn:
        if sets:
            conn.execute(f"UPDATE study_tasks SET {', '.join(k + ' = ?' for k in sets)}, updated_at = ? WHERE id = ?",
                         (*sets.values(), clock.now().isoformat(), task_id))
        if "status" in fields:
            set_status(conn, task_id, fields["status"])


def set_status(conn, task_id, status, evidence=None) -> bool:
    """Move a task to `status`. 'done' records done_at and evidence (default: the student marked it); leaving
    done/cancelled clears them. Returns False when nothing changed."""
    status = _choice(status, STATUSES, "status")
    t = conn.execute("SELECT status FROM study_tasks WHERE id = ?", (task_id,)).fetchone()
    if not t:
        raise TaskError("That task no longer exists.")
    if t["status"] == status:
        return False
    now = clock.now().isoformat()
    done = status == "done"
    ev = json.dumps(evidence or {"rule": "manual", "by": "you"}) if done else None
    with conn:
        conn.execute("UPDATE study_tasks SET status = ?, done_at = ?, evidence = ?, updated_at = ? WHERE id = ?",
                     (status, now if done else None, ev, now, task_id))
    return True


def complete(conn, task_id, evidence=None) -> bool:
    """Mark an open task done. A task that is already done or cancelled is left alone."""
    t = conn.execute("SELECT status FROM study_tasks WHERE id = ?", (task_id,)).fetchone()
    if not t or t["status"] not in OPEN:
        return False
    return set_status(conn, task_id, "done", evidence)


def postpone(conn, task_id, due_on=None, days=None) -> None:
    """New due date: a given date, or `days` from today."""
    if days:
        due_on = (clock.today() + timedelta(days=int(days))).isoformat()
    if not _date(due_on):
        raise TaskError("Pick a date to postpone to.")
    update(conn, task_id, due_on=due_on)


def list_tasks(conn, course_id=None, statuses=None, limit=None):
    sql, params = _SELECT, []
    where = []
    if course_id:
        where.append("t.course_id = ?")
        params.append(course_id)
    if statuses:
        where.append(f"t.status IN ({','.join('?' * len(statuses))})")
        params += list(statuses)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += _ORDER + (f" LIMIT {int(limit)}" if limit else "")
    return conn.execute(sql, [*params, clock.today().isoformat()]).fetchall()


def open_count(conn, course_id=None) -> int:
    sql, params = "SELECT COUNT(*) FROM study_tasks WHERE status IN ('todo','doing','blocked')", []
    if course_id:
        sql += " AND course_id = ?"
        params.append(course_id)
    return conn.execute(sql, params).fetchone()[0]


def next_tasks(conn, limit=3):
    """What to do next: open tasks you can act on (not blocked), overdue/today first, then priority."""
    today = clock.today().isoformat()
    return conn.execute(_SELECT + " WHERE t.status IN ('todo','doing')" + _ORDER + " LIMIT ?", (today, limit)).fetchall()


def closed(conn, course_id=None, limit=20):
    """Recently done or cancelled tasks, newest first."""
    sql, params = _SELECT + " WHERE t.status IN ('done','cancelled')", []
    if course_id:
        sql += " AND t.course_id = ?"
        params.append(course_id)
    return conn.execute(sql + " ORDER BY t.updated_at DESC, t.id DESC LIMIT ?", (*params, limit)).fetchall()


# ---------------------------------------------------------------- presentation

def evidence_text(t) -> str:
    try:
        ev = json.loads(t["evidence"] or "null")
    except ValueError:
        ev = None
    if not ev:
        return ""
    rule = ev.get("rule")
    if rule == "cards_reviewed":
        n = ev.get("reviews", 0)
        return f"Done: reviewed {n} card{'' if n == 1 else 's'} on {ev.get('day', '')}"
    if rule == "quiz_finished":
        score = f" ({ev['score']}/{ev['total']})" if ev.get("total") else ""
        return f"Done: finished quiz #{ev.get('attempt_id')}{score}"
    return "Done: marked by you" + (" (applied from Ask)" if ev.get("via") else "")


def decorate(rows, today=None):
    """Rows as dicts plus how to word the due date: `due_label`, `overdue`, `due_now`, and `why` for Today."""
    today = today or clock.today()
    out = []
    for t in rows:
        d = dict(t)
        due = clock.parse_date(t["due_on"])
        days = (due - today).days if due else None
        d["overdue"] = days is not None and days < 0
        d["due_now"] = days is not None and days <= 0
        d["due_label"] = ("" if days is None else f"overdue {-days} day{'' if days == -1 else 's'}" if days < 0
                          else "due today" if days == 0 else "due tomorrow" if days == 1 else f"due {due.strftime('%b %-d')}")
        d["why"] = d["due_label"] if d["due_now"] else ("high priority" if t["priority"] == 1 else d["due_label"])
        d["evidence_text"] = evidence_text(t)
        out.append(d)
    return out


# ---------------------------------------------------------------- evidence hooks

def _scope(course_id, competency_id):
    sql, params = "", []
    if course_id:
        sql += " AND k.course_id = ?"
        params.append(course_id)
    if competency_id:
        sql += " AND k.competency_id = ?"
        params.append(competency_id)
    return sql, params


def on_card_reviewed(conn, card) -> None:
    """Called by cards.grade after a real review was stored. Closes cards_reviewed tasks whose cards are cleared."""
    try:
        today = clock.today().isoformat()
        tasks = conn.execute("SELECT * FROM study_tasks WHERE done_rule = 'cards_reviewed' AND status IN ('todo','doing','blocked') "
                             "AND (course_id IS NULL OR course_id = ?) AND (competency_id IS NULL OR competency_id = ?)",
                             (card["course_id"], card["competency_id"])).fetchall()
        for t in tasks:
            scope, params = _scope(t["course_id"], t["competency_id"])
            since = max(t["created_at"], today)  # reviewed today, and after the task existed
            n = conn.execute("SELECT COUNT(*) FROM reviews r JOIN cards k ON k.id = r.card_id WHERE r.reviewed_at >= ?" + scope,
                             (since, *params)).fetchone()[0]
            left = conn.execute("SELECT COUNT(*) FROM cards k WHERE k.due_on <= ?" + scope, (today, *params)).fetchone()[0]
            if n and not left:
                complete(conn, t["id"], {"rule": "cards_reviewed", "reviews": n, "day": today})
    except Exception:  # noqa: BLE001 — grading a card must never fail because of task bookkeeping
        log.exception("task auto-complete after a card review failed")


def on_quiz_finished(conn, attempt_id) -> None:
    """Called by quizzes.finish when it claimed the finish. Closes quiz_finished tasks for that course."""
    try:
        a = conn.execute("SELECT * FROM quiz_attempts WHERE id = ?", (attempt_id,)).fetchone()
        if not a or not a["finished_at"]:
            return
        tasks = conn.execute("SELECT * FROM study_tasks WHERE done_rule = 'quiz_finished' AND status IN ('todo','doing','blocked') "
                             "AND (course_id IS NULL OR course_id = ?) AND created_at <= ?",
                             (a["course_id"], a["finished_at"])).fetchall()
        ids = json.loads(a["question_ids_json"] or "[]")
        for t in tasks:
            if t["competency_id"] and not (ids and conn.execute(
                    f"SELECT 1 FROM questions WHERE competency_id = ? AND id IN ({','.join('?' * len(ids))})",
                    (t["competency_id"], *ids)).fetchone()):
                continue  # a quiz on other topics doesn't count for a competency-specific task
            complete(conn, t["id"], {"rule": "quiz_finished", "attempt_id": a["id"], "score": a["score"], "total": a["total"]})
    except Exception:  # noqa: BLE001
        log.exception("task auto-complete after a quiz failed")
