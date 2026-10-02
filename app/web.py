"""Jinja2 setup and the render() helper every router uses."""
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from . import appearance, clock, tasks
from .config import settings

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

STATUS_LABELS = {
    "not_started": "Not started",
    "in_progress": "In progress",
    "pre_assessed": "Pre-assessed",
    "scheduled": "Scheduled",
    "passed": "Passed",
    "revision_needed": "Revision needed",
}


def _fmt_date(value, fmt="%b %-d, %Y"):
    d = clock.parse_date(value)
    return d.strftime(fmt) if d else "—"


# Which top-bar item a URL belongs to (the first prefix that matches wins), so the nav can mark the current section.
NAV_SECTIONS = [("/terms", "courses"), ("/courses", "courses"), ("/quizzes", "courses"), ("/cards", "courses"),
                ("/review", "review"), ("/tasks", "tasks"), ("/ask/memory", "more"), ("/ask", "ask"), ("/search", "search"),
                ("/sessions", "more"), ("/certs", "more"), ("/settings", "more")]


def nav_section(path: str) -> str:
    if path == "/":
        return "today"
    return next((key for prefix, key in NAV_SECTIONS if path == prefix or path.startswith(prefix + "/")), "")


templates.env.filters["date"] = _fmt_date
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS
templates.env.globals.update(TASK_KINDS=tasks.KINDS, TASK_STATUSES=tasks.STATUSES, TASK_RULES=tasks.RULES,
                             TASK_PRIORITIES=tasks.PRIORITIES)

# Extra per-request context providers registered by routers (e.g. the running study timer).
context_providers = []


def render(request: Request, template: str, status_code: int = 200, **ctx):
    base = {"today": clock.today(), "ai_enabled": settings.ai_enabled, "authed": bool(request.session.get("auth")),
            "nav_active": nav_section(request.url.path), **appearance.get()}
    if base["authed"]:
        for provider in context_providers:
            base.update(provider(request))
    base.update(ctx)
    return templates.TemplateResponse(request, template, base, status_code=status_code)
