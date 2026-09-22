import re

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from .. import cards, catalog, clock, markdown, notes_fs, readiness
from .. import competencies as comp
from ..config import settings
from ..db import get_db
from ..web import render

router = APIRouter()

IMAGE_TYPES = {  # extension → magic bytes prefix
    "png": [b"\x89PNG\r\n\x1a\n"],
    "jpg": [b"\xff\xd8\xff"],
    "jpeg": [b"\xff\xd8\xff"],
    "gif": [b"GIF87a", b"GIF89a"],
    "webp": [b"RIFF"],
}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")


def load(conn, code):
    c = catalog.get_course(conn, code)
    if not c:
        raise HTTPException(404, f"No course {code}")
    return c


def known_codes(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT code FROM courses")}


def render_md(conn, text: str) -> str:
    return markdown.render(text, known_codes(conn))


def course_context(conn, course) -> dict:
    """Shared context for every course tab."""
    return {"course": course, "pa_files": notes_fs.list_pa_files(course),
            "weakest": comp.weakest(conn, course["id"]), "anchor": comp.anchor,
            "cards_due": cards.due_count(conn, course["id"])}


@router.get("/courses/{code}")
def course_page(request: Request, code: str, conn=Depends(get_db)):
    c = load(conn, code)
    notes_fs.ensure_course_files(conn, c)
    text, _ = notes_fs.read_note(c, "overview")
    return render(request, "courses/page.html", **course_context(conn, c), tab="overview",
                  html=render_md(conn, text), ready=readiness.for_course(conn, c["id"]))


@router.get("/courses/{code}/notes/{name:path}")
def note_page(request: Request, code: str, name: str, mode: str = "view", conn=Depends(get_db)):
    if name.removesuffix(".md") == "scratch":  # old name for the Notebook tab (v1.0)
        return RedirectResponse(f"/courses/{code}/notes/notebook" + (f"?{request.url.query}" if request.url.query else ""), status_code=301)
    c = load(conn, code)
    try:
        name = notes_fs.check_name(name)
    except notes_fs.BadNoteName:
        raise HTTPException(404)
    notes_fs.ensure_course_files(conn, c)
    text, ver = notes_fs.read_note(c, name)
    tab = name if name in notes_fs.NOTE_FILES else "pa"
    return render(request, "courses/note.html", **course_context(conn, c), tab=tab, name=name, mode=mode,
                  text=text, version=ver, html=render_md(conn, text) if mode != "edit" else "",
                  title=notes_fs.NOTE_TITLES.get(name, name))


@router.post("/courses/{code}/notes/{name:path}")
async def note_save(request: Request, code: str, name: str, conn=Depends(get_db)):
    c = load(conn, code)
    body = await request.json()
    content = body.get("content")
    if not isinstance(content, str):
        raise HTTPException(400, "content missing")
    try:
        ver = notes_fs.write_note(conn, c, name, content, body.get("base"))
    except notes_fs.BadNoteName:
        raise HTTPException(404)
    except notes_fs.NoteConflict as e:
        return JSONResponse({"error": "changed elsewhere", "version": str(e)}, status_code=409)
    return {"version": ver, "saved_at": clock.now().strftime("%H:%M:%S")}


@router.post("/render")
async def render_preview(request: Request, conn=Depends(get_db)):
    body = await request.json()
    return HTMLResponse(render_md(conn, str(body.get("content", ""))))


@router.post("/courses/{code}/attachments")
async def upload(code: str, image: UploadFile = File(...), conn=Depends(get_db)):
    c = load(conn, code)
    ext = (image.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in IMAGE_TYPES:
        return JSONResponse({"error": "typeNotAllowed"}, status_code=400)
    data = await image.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        return JSONResponse({"error": "fileTooLarge"}, status_code=400)
    if not any(data.startswith(m) for m in IMAGE_TYPES[ext]):
        return JSONResponse({"error": "typeNotAllowed"}, status_code=400)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", (image.filename or "image").rsplit(".", 1)[0]).strip("-.")[:60] or "image"
    fname = f"{clock.now().strftime('%Y%m%d-%H%M%S')}-{stem}.{ext}"
    d = settings.attachments_dir / c["code"]
    d.mkdir(parents=True, exist_ok=True)
    (d / fname).write_bytes(data)
    return {"data": {"filePath": f"/attachments/{c['code']}/{fname}"}}


@router.get("/attachments/{code}/{fname}")
def attachment(code: str, fname: str):
    if not re.fullmatch(r"[A-Z]\d{3}", code) or not FILE_RE.match(fname):
        raise HTTPException(404)
    p = settings.attachments_dir / code / fname
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})
