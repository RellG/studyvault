from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import appearance
from ..db import get_db
from ..web import render

router = APIRouter()


@router.get("/settings")
def settings_page(request: Request):
    return render(request, "settings.html", themes=appearance.THEMES, modes=appearance.MODES)


@router.post("/settings")
def settings_save(theme: str = Form(...), mode: str = Form("auto"), conn=Depends(get_db)):
    try:
        appearance.save(conn, theme, mode)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return RedirectResponse("/settings?saved=1", status_code=303)
