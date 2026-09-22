"""Jinja2 setup and the render() helper every router uses."""
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from . import clock
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


templates.env.filters["date"] = _fmt_date
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS

# Extra per-request context providers registered by routers (e.g. the running study timer).
context_providers = []


def render(request: Request, template: str, status_code: int = 200, **ctx):
    base = {"today": clock.today(), "ai_enabled": settings.ai_enabled, "authed": bool(request.session.get("auth"))}
    if base["authed"]:
        for provider in context_providers:
            base.update(provider(request))
    base.update(ctx)
    return templates.TemplateResponse(request, template, base, status_code=status_code)
