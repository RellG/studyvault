"""Study timer (server-side start time, so it survives reloads) and study-hour analytics."""
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import catalog, clock, progress
from ..db import connect, get_db
from ..web import context_providers, render

router = APIRouter()
LONG_SESSION_MIN = 6 * 60


def active(conn):
    return conn.execute("SELECT s.*, c.code FROM sessions s LEFT JOIN courses c ON c.id = s.course_id "
                        "WHERE s.ended_at IS NULL ORDER BY s.id DESC LIMIT 1").fetchone()


def _timer_ctx(request):
    conn = connect()
    try:
        s = active(conn)
        if not s:
            return {"active_session": None}
        elapsed = int(progress.minutes_between(s["started_at"], clock.now().isoformat()) * 60)
        return {"active_session": {"id": s["id"], "code": s["code"], "elapsed": elapsed, "pomodoro": s["pomodoro"]}}
    finally:
        conn.close()


context_providers.append(_timer_ctx)


def stop_active(conn) -> float | None:
    s = active(conn)
    if not s:
        return None
    end = clock.now().isoformat()
    minutes = round(progress.minutes_between(s["started_at"], end), 1)
    with conn:
        conn.execute("UPDATE sessions SET ended_at = ?, minutes = ? WHERE id = ?", (end, minutes, s["id"]))
    return minutes


def _back(request, fallback="/sessions"):
    path = urlsplit(request.headers.get("referer") or "").path or fallback
    return path if path.startswith("/") and not path.startswith("//") else fallback


@router.post("/sessions/start")
def start(request: Request, course_code: str = Form(""), pomodoro: str = Form(""), conn=Depends(get_db)):
    c = catalog.get_course(conn, course_code) if course_code else None
    stop_active(conn)
    with conn:
        conn.execute("INSERT INTO sessions(course_id, started_at, pomodoro) VALUES (?, ?, ?)",
                     (c["id"] if c else None, clock.now().isoformat(), 1 if pomodoro else 0))
    return RedirectResponse(_back(request), status_code=303)


@router.post("/sessions/stop")
def stop(request: Request, conn=Depends(get_db)):
    stop_active(conn)
    return RedirectResponse(_back(request), status_code=303)


@router.post("/sessions/manual")
def manual(course_code: str = Form(""), day: str = Form(""), minutes: str = Form(""), conn=Depends(get_db)):
    c = catalog.get_course(conn, course_code) if course_code else None
    try:
        d = date.fromisoformat(day)
        m = float(minutes)
    except ValueError:
        return RedirectResponse("/sessions?error=Date+and+minutes+are+required", status_code=303)
    if not 0 < m <= 16 * 60:
        return RedirectResponse("/sessions?error=Minutes+must+be+between+1+and+960", status_code=303)
    start_at = f"{d.isoformat()}T12:00:00"
    end_at = (datetime.fromisoformat(start_at) + timedelta(minutes=m)).isoformat()
    with conn:
        conn.execute("INSERT INTO sessions(course_id, started_at, ended_at, minutes) VALUES (?, ?, ?, ?)",
                     (c["id"] if c else None, start_at, end_at, m))
    return RedirectResponse("/sessions?ok=1", status_code=303)


@router.post("/sessions/{sid}/delete")
def delete(sid: int, conn=Depends(get_db)):
    with conn:
        if not conn.execute("DELETE FROM sessions WHERE id = ?", (sid,)).rowcount:
            raise HTTPException(404)
    return RedirectResponse("/sessions", status_code=303)


def analytics(conn, today: date):
    per_course = conn.execute(
        """SELECT c.code, c.title, c.cu, t.n AS term_n, SUM(s.minutes) / 60.0 AS hours
           FROM sessions s JOIN courses c ON c.id = s.course_id LEFT JOIN terms t ON t.id = c.term_id
           WHERE s.minutes IS NOT NULL GROUP BY c.id ORDER BY hours DESC""").fetchall()
    with_hours = [r for r in per_course if r["hours"] >= 1]
    avg_per_cu = (sum(r["hours"] for r in with_hours) / sum(r["cu"] for r in with_hours)) if with_hours else None
    courses = [{**dict(r), "per_cu": r["hours"] / r["cu"],
                "slow": bool(avg_per_cu and r["hours"] >= 1 and r["hours"] / r["cu"] > 1.5 * avg_per_cu)} for r in per_course]
    weeks = []
    this_week = progress.week_start(today)
    for i in range(11, -1, -1):
        ws = this_week - timedelta(weeks=i)
        m = conn.execute("SELECT COALESCE(SUM(minutes), 0) FROM sessions WHERE minutes IS NOT NULL AND started_at >= ? AND started_at < ?",
                         (ws.isoformat(), (ws + timedelta(days=7)).isoformat())).fetchone()[0]
        weeks.append({"start": ws, "hours": m / 60})
    per_term = conn.execute(
        """SELECT t.n, SUM(s.minutes) / 60.0 AS hours FROM sessions s JOIN courses c ON c.id = s.course_id
           JOIN terms t ON t.id = c.term_id WHERE s.minutes IS NOT NULL GROUP BY t.n ORDER BY t.n""").fetchall()
    return {"courses": courses, "avg_per_cu": avg_per_cu, "weeks": weeks, "per_term": per_term,
            "max_course": max((c["hours"] for c in courses), default=0), "max_week": max((w["hours"] for w in weeks), default=0)}


@router.get("/sessions")
def sessions_page(request: Request, conn=Depends(get_db)):
    today = clock.today()
    recent = conn.execute("SELECT s.*, c.code FROM sessions s LEFT JOIN courses c ON c.id = s.course_id "
                          "WHERE s.ended_at IS NOT NULL ORDER BY s.started_at DESC LIMIT 15").fetchall()
    courses = conn.execute("SELECT code, title FROM courses WHERE status != 'passed' ORDER BY ord").fetchall()
    return render(request, "sessions.html", a=analytics(conn, today), recent=recent, courses=courses,
                  error=request.query_params.get("error"), long_min=LONG_SESSION_MIN)
