"""Progress math (PLAN §4.3): SAP, term pace, projected graduation, streak. Pure functions + one DB loader."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import clock

SAP_GREEN = 0.80
SAP_MIN = 2 / 3          # WGU's 66.67 %; exactly 2/3 meets it (amber), below is red
TERM1_MIN_CU = 3
GRAD_TARGET = date(2028, 9, 30)
ROLLING_DAYS = 90
MIN_DATA_DAYS = 14
EPS = 1e-9


@dataclass
class Course:
    code: str
    cu: int
    status: str
    attempted: bool
    term_n: int | None
    passed_on: date | None = None


def sap(courses: list[Course]) -> dict:
    """Passed CU ÷ attempted CU, program-wide. Transferred CU never enter this (they aren't Course rows)."""
    attempted = sum(c.cu for c in courses if c.attempted)
    passed = sum(c.cu for c in courses if c.status == "passed")
    if not attempted:
        return {"attempted": 0, "passed": passed, "ratio": None, "color": None}
    ratio = passed / attempted
    color = "green" if ratio >= SAP_GREEN - EPS else "amber" if ratio >= SAP_MIN - EPS else "red"
    return {"attempted": attempted, "passed": passed, "ratio": ratio, "color": color}


def term1_check(courses: list[Course]) -> dict:
    passed = sum(c.cu for c in courses if c.term_n == 1 and c.status == "passed")
    return {"passed": passed, "ok": passed >= TERM1_MIN_CU, "needed": max(0, TERM1_MIN_CU - passed)}


def term_pace(start: date, end: date, target_cu: int, passed_this_term: int, today: date) -> dict:
    """rate = passed / max(days_elapsed, 7); projected = passed + rate × days_remaining."""
    days_elapsed = (today - start).days
    days_remaining = max(0, (end - today).days)
    if days_elapsed < 0:
        return {"started": False, "starts_in": -days_elapsed, "days_remaining": (end - start).days,
                "passed": passed_this_term, "target": target_cu, "projected": None, "on_pace": None}
    rate = passed_this_term / max(days_elapsed, 7)
    projected = passed_this_term + rate * days_remaining
    return {"started": True, "starts_in": 0, "days_remaining": days_remaining, "passed": passed_this_term,
            "target": target_cu, "rate_per_week": rate * 7, "projected": round(projected, 1),
            "on_pace": projected >= target_cu - EPS}


def projected_graduation(passed_dates: list[tuple[date, int]], remaining_cu: int, today: date,
                         data_start: date | None) -> dict:
    """remaining_cu / rolling-90-day rate. Needs ≥ 14 days of data."""
    if remaining_cu <= 0:
        return {"status": "done", "date": None}
    if data_start is None or (today - data_start).days < MIN_DATA_DAYS:
        return {"status": "not_enough_data", "date": None,
                "days_left": MIN_DATA_DAYS - ((today - data_start).days if data_start else 0)}
    window = min(ROLLING_DAYS, (today - data_start).days)
    since = today - timedelta(days=window)
    cu = sum(n for d, n in passed_dates if since < d <= today)
    if cu == 0:
        return {"status": "no_recent_passes", "date": None, "window": window}
    rate = cu / window
    grad = today + timedelta(days=round(remaining_cu / rate))
    return {"status": "ok", "date": grad, "window": window, "cu_per_month": rate * 30,
            "on_track": grad <= GRAD_TARGET, "target": GRAD_TARGET}


def streak(activity_days: set[date], today: date) -> int:
    """Consecutive days with a card review or study session, ending today (or yesterday if nothing yet today)."""
    day = today if today in activity_days else today - timedelta(days=1)
    n = 0
    while day in activity_days:
        n += 1
        day -= timedelta(days=1)
    return n


# ---------------------------------------------------------------- DB

def _d(v):
    return clock.parse_date(v)


def load(conn, today: date | None = None) -> dict:
    today = today or clock.today()
    rows = conn.execute("SELECT c.*, t.n AS term_n FROM courses c LEFT JOIN terms t ON t.id = c.term_id").fetchall()
    courses = [Course(r["code"], r["cu"], r["status"], bool(r["attempted"]), r["term_n"], _d(r["passed_on"])) for r in rows]
    total_remaining = sum(c.cu for c in courses)
    passed_cu = sum(c.cu for c in courses if c.status == "passed")
    transferred = conn.execute("SELECT COALESCE(SUM(cu), 0) FROM transfers").fetchone()[0]

    from . import catalog
    term = catalog.current_term(conn, today)
    pace = None
    if term:
        in_term = [c for c in courses if c.term_n == term["n"]]
        pace = term_pace(_d(term["start"]), _d(term["end"]), term["target_cu"],
                         sum(c.cu for c in in_term if c.status == "passed"), today)
        pace["n"] = term["n"]
        pace["planned"] = sum(c.cu for c in in_term)

    starts = [r[0] for r in conn.execute(
        "SELECT MIN(attempted_on) FROM courses UNION ALL SELECT MIN(substr(started_at, 1, 10)) FROM sessions "
        "UNION ALL SELECT MIN(substr(reviewed_at, 1, 10)) FROM reviews") if r[0]]
    data_start = min(_d(s) for s in starts) if starts else None
    grad = projected_graduation([(c.passed_on, c.cu) for c in courses if c.passed_on], total_remaining - passed_cu,
                                today, data_start)

    days = {_d(r[0]) for r in conn.execute(
        "SELECT DISTINCT substr(reviewed_at, 1, 10) FROM reviews UNION SELECT DISTINCT substr(started_at, 1, 10) FROM sessions")}
    return {"sap": sap(courses), "term1": term1_check(courses), "pace": pace, "grad": grad,
            "passed_cu": passed_cu, "remaining_total": total_remaining, "transferred": transferred,
            "streak": streak(days, today), "term": term}


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def minutes_between(start: str, end: str) -> float:
    return max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60)
