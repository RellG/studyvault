"""Assessment prep: OA pre-assessments + readiness checklist, PA drafts/submissions, certification records."""
import difflib
import json
import re
from datetime import date, timedelta

from . import cards, catalog, clock, competencies as comp, notes_fs, quizzes

# WGU coaching-report levels → the confidence rating they suggest.
LEVELS = {"exemplary": ("Exemplary", 5), "competent": ("Competent", 4),
          "approaching": ("Approaching competence", 2), "unsatisfactory": ("Unsatisfactory", 1)}
INTEGRITY = "Submissions must be your own work — check WGU's current AI-use policy for this course."
REVISION_CYCLE_DAYS = 7
REVIEW_WINDOW_DAYS = 14
VOUCHER_STATUSES = ["none", "requested", "received", "used", "expired"]
CERT_RESULTS = ["pending", "passed", "failed"]
PA_TASK_STATUSES = ["todo", "drafting", "done"]


class AssessmentError(ValueError):
    pass


def _date(value, field, required=False):
    value = (value or "").strip()
    if not value:
        if required:
            raise AssessmentError(f"{field} is required")
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise AssessmentError(f"{field}: '{value}' isn't a date (YYYY-MM-DD)")


# ---------------------------------------------------------------- OA

def log_preassessment(conn, course, taken_on: str, score: str, passed: bool, levels: dict[int, str],
                      notes: str, apply_confidence: bool) -> int:
    taken_on = _date(taken_on, "Date", required=True)
    try:
        score_v = float(score) if str(score).strip() else None
    except ValueError:
        raise AssessmentError("Score must be a number (percent)")
    if score_v is not None and not 0 <= score_v <= 100:
        raise AssessmentError("Score is a percent, 0–100")
    levels = {int(k): v for k, v in levels.items() if v in LEVELS}
    with conn:
        cur = conn.execute("INSERT INTO preassessments(course_id, taken_on, score, passed, breakdown_json) VALUES (?, ?, ?, ?, ?)",
                           (course["id"], taken_on, score_v, int(bool(passed)),
                            json.dumps({"levels": {str(k): v for k, v in levels.items()}, "notes": notes.strip()})))
    if apply_confidence:
        for comp_id, level in levels.items():
            if conn.execute("SELECT 1 FROM competencies WHERE id = ? AND course_id = ?", (comp_id, course["id"])).fetchone():
                comp.set_confidence(conn, comp_id, LEVELS[level][1])
    if course["status"] in ("not_started", "in_progress"):
        catalog.set_status(conn, course["code"], "pre_assessed")
    return cur.lastrowid


def preassessments(conn, course_id):
    rows = conn.execute("SELECT * FROM preassessments WHERE course_id = ? ORDER BY taken_on DESC, id DESC", (course_id,)).fetchall()
    out = []
    for r in rows:
        b = json.loads(r["breakdown_json"] or '""')
        if not isinstance(b, dict):
            b = {"levels": {}, "notes": str(b)}
        out.append({**dict(r), "levels": {int(k): v for k, v in b.get("levels", {}).items()}, "notes": b.get("notes", "")})
    return out


def checklist(conn, course) -> list[dict]:
    today = clock.today()
    passed_pre = conn.execute("SELECT 1 FROM preassessments WHERE course_id = ? AND passed = 1", (course["id"],)).fetchone()
    weakest = comp.weakest(conn, course["id"])
    cutoff = (today - timedelta(days=REVIEW_WINDOW_DAYS)).isoformat()
    weak_ok = bool(weakest) and all(w["last_reviewed"] and w["last_reviewed"] >= cutoff for w in weakest)
    n_cards = conn.execute("SELECT COUNT(*) FROM cards WHERE course_id = ?", (course["id"],)).fetchone()[0]
    due = cards.due_count(conn, course["id"])
    hist = quizzes.history(conn, course["id"])
    last_pct = round(100 * hist[0]["score"] / hist[0]["total"]) if hist else None
    return [
        {"label": "Pre-assessment passed", "ok": bool(passed_pre),
         "detail": "" if passed_pre else "log a passing pre-assessment below"},
        {"label": f"Weakest competencies reviewed in the last {REVIEW_WINDOW_DAYS} days", "ok": weak_ok,
         "detail": "no competencies yet" if not weakest else ", ".join(
             w["text"][:40] for w in weakest if not (w["last_reviewed"] and w["last_reviewed"] >= cutoff))},
        {"label": "Flashcards caught up", "ok": n_cards > 0 and due == 0,
         "detail": "no cards yet" if not n_cards else (f"{due} due" if due else "")},
        {"label": f"Latest practice quiz ≥ {course['quiz_target']}%", "ok": last_pct is not None and last_pct >= course["quiz_target"],
         "detail": "no quiz yet" if last_pct is None else f"latest {last_pct}%"},
    ]


def schedule_exam(conn, course, exam_date: str, quiz_target: str | None = None) -> None:
    d = _date(exam_date, "Exam date")
    fields = {"exam_date": d}
    if quiz_target not in (None, ""):
        try:
            t = int(quiz_target)
        except ValueError:
            raise AssessmentError("Quiz target must be a whole percent")
        if not 1 <= t <= 100:
            raise AssessmentError("Quiz target must be 1–100")
        fields["quiz_target"] = t
    with conn:
        conn.execute(f"UPDATE courses SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?", (*fields.values(), course["id"]))
    if d and course["status"] not in ("passed", "scheduled"):
        catalog.set_status(conn, course["code"], "scheduled")


# ---------------------------------------------------------------- PA

def import_rubric(conn, course, text: str) -> int:
    lines = comp.parse(text)
    if not lines:
        raise AssessmentError("Paste the rubric rows, one per line")
    with conn:
        conn.execute("DELETE FROM pa_tasks WHERE course_id = ?", (course["id"],))
        for i, line in enumerate(lines):
            conn.execute("INSERT INTO pa_tasks(course_id, ord, rubric_text) VALUES (?, ?, ?)", (course["id"], i, line))
    return len(lines)


def set_task_status(conn, course, task_id: int, status: str) -> None:
    if status not in PA_TASK_STATUSES:
        raise AssessmentError("Unknown task status")
    with conn:
        conn.execute("UPDATE pa_tasks SET status = ? WHERE id = ? AND course_id = ?", (status, task_id, course["id"]))


DRAFT_RE = re.compile(r"^draft-v(\d+)-(\d{4}-\d{2}-\d{2})$")


def drafts(course) -> list[dict]:
    out = []
    for name in notes_fs.list_pa_files(course):
        m = DRAFT_RE.match(name)
        if m:
            out.append({"name": f"pa/{name}", "version": f"v{m.group(1)}", "n": int(m.group(1)), "date": m.group(2)})
    return sorted(out, key=lambda d: d["n"])


def save_draft(conn, course, text: str) -> dict:
    if not text.strip():
        raise AssessmentError("The draft is empty")
    existing = drafts(course)
    n = (existing[-1]["n"] + 1) if existing else 1
    name = f"pa/draft-v{n}-{clock.today().isoformat()}"
    notes_fs.write_note(conn, course, name, text)
    return {"name": name, "version": f"v{n}"}


def diff_html(course, a: str, b: str) -> str:
    ta, _ = notes_fs.read_note(course, a)
    tb, _ = notes_fs.read_note(course, b)
    return difflib.HtmlDiff(wrapcolumn=60).make_table(ta.splitlines(), tb.splitlines(), a.removeprefix("pa/"),
                                                     b.removeprefix("pa/"), context=True, numlines=2)


def log_submission(conn, course, version: str, submitted_on: str, result: str, feedback: str) -> None:
    if result not in ("pending", "passed", "revision"):
        raise AssessmentError("Unknown result")
    if not version.strip():
        raise AssessmentError("Which version did you submit?")
    d = _date(submitted_on, "Submitted on", required=True)
    with conn:
        conn.execute("INSERT INTO pa_submissions(course_id, version, submitted_on, result, feedback) VALUES (?, ?, ?, ?, ?)",
                     (course["id"], version.strip(), d, result, feedback.strip()))
    _sync_pa_status(conn, course, result)


def update_submission(conn, course, sub_id: int, result: str, feedback: str) -> None:
    if result not in ("pending", "passed", "revision"):
        raise AssessmentError("Unknown result")
    with conn:
        conn.execute("UPDATE pa_submissions SET result = ?, feedback = ? WHERE id = ? AND course_id = ?",
                     (result, feedback.strip(), sub_id, course["id"]))
    _sync_pa_status(conn, course, result)


def _sync_pa_status(conn, course, result):
    fresh = catalog.get_course(conn, course["code"])
    if result == "revision" and fresh["status"] != "passed":
        catalog.set_status(conn, course["code"], "revision_needed")
    elif result == "passed":
        catalog.set_status(conn, course["code"], "passed")
    elif result == "pending" and fresh["status"] in ("not_started", "in_progress", "revision_needed"):
        catalog.set_status(conn, course["code"], "scheduled")


def submissions(conn, course_id):
    return conn.execute("SELECT * FROM pa_submissions WHERE course_id = ? ORDER BY submitted_on DESC, id DESC", (course_id,)).fetchall()


def revision_warning(course, n_revisions: int, today: date | None = None) -> dict | None:
    """PLAN §5 Slice 7: warn when today + 7 days × (revisions so far + 1) passes the term end."""
    if not course["term_end"]:
        return None
    today = today or clock.today()
    projected = today + timedelta(days=REVISION_CYCLE_DAYS * (n_revisions + 1))
    end = date.fromisoformat(course["term_end"])
    if projected > end:
        return {"projected": projected.isoformat(), "term_end": end.isoformat(), "cycles": n_revisions + 1}
    return None


# ---------------------------------------------------------------- certs

def list_certs(conn):
    return conn.execute("SELECT certs.*, courses.code FROM certs LEFT JOIN courses ON courses.id = certs.course_id "
                        "ORDER BY certs.result = 'passed' DESC, certs.exam_date IS NULL, certs.exam_date, courses.ord, certs.name").fetchall()


def save_cert(conn, form: dict, cert_id: int | None = None) -> int:
    name = (form.get("name") or "").strip()
    if not name:
        raise AssessmentError("Certification name is required")
    vs = form.get("voucher_status") or "none"
    res = form.get("result") or "pending"
    if vs not in VOUCHER_STATUSES or res not in CERT_RESULTS:
        raise AssessmentError("Unknown voucher status or result")
    course_id = None
    if form.get("course_code"):
        c = catalog.get_course(conn, form["course_code"])
        course_id = c["id"] if c else None
    vals = {"name": name, "course_id": course_id, "voucher_status": vs,
            "voucher_expires": _date(form.get("voucher_expires"), "Voucher expires"),
            "exam_date": _date(form.get("exam_date"), "Exam date"), "result": res,
            "cert_id": (form.get("cert_id") or "").strip() or None,
            "expires_on": _date(form.get("expires_on"), "Cert expires")}
    with conn:
        if cert_id:
            conn.execute(f"UPDATE certs SET {', '.join(k + ' = ?' for k in vals)} WHERE id = ?", (*vals.values(), cert_id))
            return cert_id
        cur = conn.execute(f"INSERT INTO certs({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})", tuple(vals.values()))
        return cur.lastrowid


def certs_earned(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM certs WHERE result = 'passed'").fetchone()[0]
