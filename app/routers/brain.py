"""Build from notes: see what a course's notes cover, run the analysis / build in the background, review the draft."""
import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .. import ai, brain, chunks
from ..db import get_db
from ..web import render
from .notes import course_context, load

router = APIRouter()


def _rows(conn, c, profile):
    """Every section with its size and coverage, plus what the analysis said about it."""
    said = {o["key"]: o for o in (profile or {}).get("outline", [])}
    thin = {t["key"]: t["why"] for t in (profile or {}).get("thin_spots", [])}
    usable = {r["key"] for r in brain.sections(conn, c["id"])}
    objectives = set(brain.coverage(conn, c["id"])["objectives"])
    out = []
    for r in chunks.outline(conn, c["id"]):
        if r["key"] not in usable:
            continue
        o = said.get(r["key"], {})
        out.append({**r, "where": r["breadcrumb"] or "(top of file)", "title": o.get("title"), "summary": o.get("summary", ""),
                    "importance": o.get("importance"), "thin": thin.get(r["key"]), "stale": r["stale_cards"] + r["stale_questions"],
                    "objectives": r["key"] in objectives})
    return out


def _page(request, conn, c, error=None, status_code=200):
    info = brain.info(conn, c)
    job = brain.progress(c["code"])
    return render(request, "brain/index.html", **course_context(conn, c), tab="brain", info=info, rows=_rows(conn, c, info["profile"]),
                  job=job, running=bool(job and not job["done"]), other=brain.busy() if brain.busy() != c["code"] else None,
                  draft=brain.pending_draft(conn, c["id"]), error=error or (job["error"] if job and job["done"] else None),
                  depths=brain.DEPTHS, cov=brain.coverage(conn, c["id"]), status_code=status_code)


@router.get("/courses/{code}/brain")
def brain_page(request: Request, code: str, conn=Depends(get_db)):
    return _page(request, conn, load(conn, code))


@router.post("/courses/{code}/brain/run")
async def brain_run(request: Request, code: str, conn=Depends(get_db)):
    c = load(conn, code)
    form = await request.form()
    action = form.get("action")
    if action not in brain.KINDS:
        raise HTTPException(400)
    if action in ("fill", "refresh") and brain.pending_draft(conn, c["id"]):
        return _page(request, conn, c, error="A draft is waiting for review. Add or dismiss it first, so it isn't replaced.",
                     status_code=409)
    depth = form.get("depth") if form.get("depth") in brain.DEPTHS else "normal"
    try:
        brain.start(c, action, depth)
    except ai.AIError as e:
        return _page(request, conn, c, error=str(e), status_code=409)
    return RedirectResponse(f"/courses/{c['code']}/brain", status_code=303)


@router.get("/courses/{code}/brain/progress")
def brain_progress(code: str, conn=Depends(get_db)):
    c = load(conn, code)
    return JSONResponse(brain.progress(c["code"]) or {"done": True, "error": None, "step": "", "steps": []})


def _draft(conn, c, draft_id):
    d = conn.execute("SELECT * FROM brain_drafts WHERE id = ? AND course_id = ?", (draft_id, c["id"])).fetchone()
    if not d:
        raise HTTPException(404)
    return d


def _groups(items, order):
    """[(section label, [(index, item), …])] in reading order; the index is the item's place in the draft (its form name)."""
    by = {}
    for i, it in enumerate(items):
        by.setdefault(it["section"], []).append((i, it))
    keys = sorted(by, key=lambda k: order.index(k) if k in order else len(order))
    return [{"label": by[k][0][1]["label"], "key": k, "items": by[k]} for k in keys]


@router.get("/courses/{code}/brain/draft/{draft_id}")
def draft_page(request: Request, code: str, draft_id: int, error: str = "", conn=Depends(get_db)):
    c = load(conn, code)
    d = _draft(conn, c, draft_id)
    p = json.loads(d["payload_json"])
    order = [r["key"] for r in chunks.outline(conn, c["id"], chunks.FILES)]
    up = p.get("updates") or {}
    return render(request, "brain/draft.html", **course_context(conn, c), tab="brain", draft=d, p=p,
                  card_groups=_groups(p["cards"], order), question_groups=_groups(p["questions"], order),
                  update_card_groups=_groups(up.get("cards", []), order), update_q_groups=_groups(up.get("questions", []), order),
                  changed=d["notes_hash"] != chunks.notes_hash(conn, c["id"]), error=error)


@router.post("/courses/{code}/brain/draft/{draft_id}/apply")
async def draft_apply(request: Request, code: str, draft_id: int, conn=Depends(get_db)):
    c = load(conn, code)
    d = _draft(conn, c, draft_id)
    form = await request.form()
    try:
        result = brain.apply_draft(conn, c, d, form)
    except brain.ApplyError as e:
        return RedirectResponse(f"/courses/{c['code']}/brain/draft/{d['id']}?error={quote(str(e))}", status_code=303)
    return RedirectResponse(f"/courses/{c['code']}/brain?applied={quote(result)}", status_code=303)


@router.post("/courses/{code}/brain/draft/{draft_id}/dismiss")
def draft_dismiss(code: str, draft_id: int, conn=Depends(get_db)):
    c = load(conn, code)
    brain.dismiss(conn, _draft(conn, c, draft_id))
    return RedirectResponse(f"/courses/{c['code']}/brain", status_code=303)
