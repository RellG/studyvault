"""Single-user password login with a signed session cookie."""
import asyncio
import hmac

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from .config import settings
from .web import render

router = APIRouter()

PUBLIC_PREFIXES = ("/login", "/health", "/static/")


def is_public(path: str) -> bool:
    return path == "/health" or path == "/login" or path.startswith("/static/")


async def require_login(request: Request, call_next):
    if is_public(request.url.path) or request.session.get("auth"):
        return await call_next(request)
    if request.headers.get("HX-Request"):
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    if request.method == "GET":
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    return Response("Not logged in", status_code=401)


def _safe_next(nxt: str | None) -> str:
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else "/"


@router.get("/login")
def login_form(request: Request, next: str = "/"):
    return render(request, "login.html", next=_safe_next(next), error=None)


@router.post("/login")
async def login(request: Request, password: str = Form(""), next: str = Form("/")):
    if settings.password and hmac.compare_digest(password.encode(), settings.password.encode()):
        request.session.clear()
        request.session["auth"] = True
        return RedirectResponse(_safe_next(next), status_code=303)
    await asyncio.sleep(1)  # slow down guessing
    return render(request, "login.html", next=_safe_next(next), error="Wrong password.", status_code=401)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
