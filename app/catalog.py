"""Terms and courses: lookups and the rules for editing a course."""
import re
from datetime import date

from . import clock, notes_fs

STATUSES = ["not_started", "in_progress", "pre_assessed", "scheduled", "passed", "revision_needed"]
ASSESSMENT_TYPES = ["OA", "PA", "cert"]
CODE_RE = re.compile(r"^[A-Z]\d{3}$")


class CourseError(ValueError):
    pass


def get_course(conn, code: str):
    return conn.execute(
        """SELECT c.*, t.n AS term_n, t.start AS term_start, t.end AS term_end
           FROM courses c LEFT JOIN terms t ON t.id = c.term_id WHERE c.code = ?""", (code.upper(),)).fetchone()


def get_term(conn, n: int):
    return conn.execute("SELECT * FROM terms WHERE n = ?", (n,)).fetchone()


def list_terms(conn):
    return conn.execute("SELECT * FROM terms ORDER BY n").fetchall()


def term_courses(conn, term_id):
    return conn.execute(
        "SELECT c.*, t.n AS term_n FROM courses c LEFT JOIN terms t ON t.id = c.term_id "
        "WHERE c.term_id IS ? ORDER BY c.ord, c.code", (term_id,)).fetchall()


def current_term(conn, today: date | None = None):
    """The term containing today, else the next one to start, else the last one."""
    today = (today or clock.today()).isoformat()
    t = conn.execute("SELECT * FROM terms WHERE start <= ? AND end >= ? ORDER BY n LIMIT 1", (today, today)).fetchone()
    return t or conn.execute("SELECT * FROM terms WHERE start > ? ORDER BY n LIMIT 1", (today,)).fetchone() \
        or conn.execute("SELECT * FROM terms ORDER BY n DESC LIMIT 1").fetchone()


def _clean_date(value, field):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise CourseError(f"{field}: '{value}' isn't a date (YYYY-MM-DD)")


def _clean_cu(value):
    try:
        cu = int(value)
    except (TypeError, ValueError):
        raise CourseError("CU must be a whole number")
    if not 1 <= cu <= 12:
        raise CourseError("CU must be between 1 and 12")
    return cu


def _term_id(conn, term_n):
    if term_n in (None, "", "none"):
        return None
    t = get_term(conn, int(term_n))
    if not t:
        raise CourseError(f"No term {term_n}")
    return t["id"]


def update_course(conn, code: str, form: dict) -> None:
    """Apply an edit form. Enforces: attempted sticks once status leaves not_started; passed_on follows status."""
    c = get_course(conn, code)
    if not c:
        raise CourseError(f"No course {code}")
    status = form.get("status", c["status"])
    if status not in STATUSES:
        raise CourseError(f"Unknown status {status}")
    atype = form.get("assessment_type") or None
    if atype and atype not in ASSESSMENT_TYPES:
        raise CourseError(f"Unknown assessment type {atype}")
    title = (form.get("title") or c["title"]).strip()
    fields = {
        "title": title,
        "cu": _clean_cu(form.get("cu", c["cu"])),
        "term_id": _term_id(conn, form.get("term_n", c["term_n"])),
        "status": status,
        "assessment_type": atype,
        "start": _clean_date(form.get("start"), "Start"),
        "due": _clean_date(form.get("due"), "Mentor due date"),
        "target": _clean_date(form.get("target"), "Target"),
        "approved": 1 if form.get("approved") in ("1", "on", True, 1) else 0,
        "cert_name": (form.get("cert_name") or "").strip() or None,
        "passed_on": _clean_date(form.get("passed_on"), "Passed on"),
        "exam_date": _clean_date(form.get("exam_date"), "Exam date"),
    }
    today = clock.today().isoformat()
    if status == "passed":
        fields["passed_on"] = fields["passed_on"] or c["passed_on"] or today
    else:
        fields["passed_on"] = None
    if status != "not_started" and not c["attempted"]:
        fields["attempted"] = 1
        fields["attempted_on"] = today
    old_term_n = c["term_n"]
    with conn:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE courses SET {sets} WHERE id = ?", (*fields.values(), c["id"]))
    new = get_course(conn, code)
    if old_term_n != new["term_n"] or c["title"] != new["title"]:
        notes_fs.move_course(conn, c, new)


def set_status(conn, code: str, status: str) -> None:
    c = get_course(conn, code)
    form = {k: c[k] for k in c.keys()}
    form.update(status=status, term_n=c["term_n"], approved=c["approved"])
    update_course(conn, code, form)


def add_course(conn, form: dict) -> str:
    code = (form.get("code") or "").strip().upper()
    if not CODE_RE.match(code):
        raise CourseError("Course code looks like D413: one letter, three digits")
    if get_course(conn, code):
        raise CourseError(f"{code} already exists; move it instead")
    title = (form.get("title") or "").strip()
    if not title:
        raise CourseError("Title is required")
    cu = _clean_cu(form.get("cu"))
    term_id = _term_id(conn, form.get("term_n"))
    ord_ = conn.execute("SELECT COALESCE(MAX(ord), 0) + 1 FROM courses").fetchone()[0]
    with conn:
        conn.execute("INSERT INTO courses(code, title, cu, term_id, ord, target) VALUES (?, ?, ?, ?, ?, ?)",
                     (code, title, cu, term_id, ord_, _clean_date(form.get("target"), "Target")))
    return code


def move_to_term(conn, code: str, term_n) -> None:
    c = get_course(conn, code)
    if not c:
        raise CourseError(f"No course {code}")
    form = {k: c[k] for k in c.keys()}
    form.update(term_n=term_n, approved=c["approved"])
    update_course(conn, code, form)


def timeline(term, courses, today: date | None = None):
    """Bars for the term timeline, positioned as percentages of the term span."""
    t0, t1 = clock.parse_date(term["start"]), clock.parse_date(term["end"])
    span = max((t1 - t0).days, 1)

    def pct(d):
        return max(0.0, min(100.0, (d - t0).days / span * 100))

    rows, cursor = [], t0
    for c in courses:
        start, due, target = (clock.parse_date(c[k]) for k in ("start", "due", "target"))
        planned = not (start and due)
        s = start or cursor
        e = due or target
        row = {"code": c["code"], "status": c["status"], "planned": planned, "bar": None, "target": None,
               "label": f"{c['code']}: {c['title']}"}
        if e and e >= s:
            row["bar"] = (pct(s), max(pct(e) - pct(s), 1.0))
            cursor = target or e  # chain planned courses from the previous target: plans are made by target date
        if target:
            row["target"] = pct(target)
        rows.append(row)
    today = today or clock.today()
    today_pct = pct(today) if t0 <= today <= t1 else None
    return {"rows": rows, "today": today_pct}
