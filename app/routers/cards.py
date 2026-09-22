from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import cards, competencies as comp, markdown, notes_fs, srs
from ..db import connect, get_db
from ..web import context_providers, render
from .notes import course_context, known_codes, load

router = APIRouter()


def _due_badge(request):
    conn = connect()
    try:
        return {"due_total": cards.due_count(conn)}
    finally:
        conn.close()


context_providers.append(_due_badge)


def _comp_id(conn, course_id, value):
    if not value:
        return None
    row = conn.execute("SELECT id FROM competencies WHERE id = ? AND course_id = ?", (int(value), course_id)).fetchone()
    return row[0] if row else None


def cards_page(request, conn, c, **extra):
    rows = conn.execute(
        "SELECT cards.*, competencies.text AS comp_text FROM cards LEFT JOIN competencies ON competencies.id = cards.competency_id "
        "WHERE cards.course_id = ? ORDER BY cards.id DESC", (c["id"],)).fetchall()
    return render(request, "cards/list.html", **course_context(conn, c), tab="cards", cards=rows,
                  comps=comp.list_for(conn, c["id"]), due=cards.due_count(conn, c["id"]),
                  sections=cards.notes_sections(c), **extra)


@router.get("/courses/{code}/cards")
def cards_list(request: Request, code: str, conn=Depends(get_db)):
    return cards_page(request, conn, load(conn, code))


@router.post("/courses/{code}/cards")
def card_create(request: Request, code: str, front: str = Form(""), back: str = Form(""),
                competency_id: str = Form(""), conn=Depends(get_db)):
    c = load(conn, code)
    if not front.strip() or not back.strip():
        return cards_page(request, conn, c, error="A card needs both a front and a back.", draft={"front": front, "back": back})
    cards.add(conn, c["id"], front, back, competency_id=_comp_id(conn, c["id"], competency_id))
    return RedirectResponse(f"/courses/{c['code']}/cards?added=1", status_code=303)


@router.post("/courses/{code}/cards/import")
def card_import(request: Request, code: str, text: str = Form(""), source: str = Form("paste"),
                section: str = Form(""), competency_id: str = Form(""), conn=Depends(get_db)):
    c = load(conn, code)
    comp_id = _comp_id(conn, c["id"], competency_id)
    if source == "notes":
        name, _, heading = section.partition("|")
        try:
            note, _ = notes_fs.read_note(c, name)
        except notes_fs.BadNoteName:
            raise HTTPException(400)
        body = cards.sections(note).get(heading)
        if body is None:
            return cards_page(request, conn, c, error=f"Couldn't find the section “{heading}” any more.")
        text = body
        comp_id = comp_id or cards.competency_for_heading(conn, c["id"], heading)
    parsed, skipped = cards.parse(text)
    added, dup = cards.import_cards(conn, c["id"], parsed, comp_id)
    return cards_page(request, conn, c, imported={"added": added, "dup": dup, "skipped": skipped},
                      pasted=text if source == "paste" and not added else "")


@router.get("/cards/{card_id}/edit")
def card_edit_form(request: Request, card_id: int, conn=Depends(get_db)):
    card = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    if not card:
        raise HTTPException(404)
    c = conn.execute("SELECT c.*, t.n AS term_n FROM courses c LEFT JOIN terms t ON t.id = c.term_id WHERE c.id = ?",
                     (card["course_id"],)).fetchone()
    return render(request, "cards/edit.html", **course_context(conn, c), tab="cards", card=card,
                  comps=comp.list_for(conn, c["id"]))


@router.post("/cards/{card_id}/edit")
def card_edit(card_id: int, front: str = Form(""), back: str = Form(""), competency_id: str = Form(""),
              conn=Depends(get_db)):
    card = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    if not card:
        raise HTTPException(404)
    if not front.strip() or not back.strip():
        raise HTTPException(400, "A card needs both a front and a back.")
    with conn:
        conn.execute("UPDATE cards SET front = ?, back = ?, competency_id = ? WHERE id = ?",
                     (front.strip(), back.strip(), _comp_id(conn, card["course_id"], competency_id), card_id))
    code = conn.execute("SELECT code FROM courses WHERE id = ?", (card["course_id"],)).fetchone()[0]
    return RedirectResponse(f"/courses/{code}/cards#card-{card_id}", status_code=303)


@router.post("/cards/{card_id}/delete")
def card_delete(card_id: int, conn=Depends(get_db)):
    card = conn.execute("SELECT course_id FROM cards WHERE id = ?", (card_id,)).fetchone()
    if not card:
        raise HTTPException(404)
    code = conn.execute("SELECT code FROM courses WHERE id = ?", (card["course_id"],)).fetchone()[0]
    with conn:
        conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
    return RedirectResponse(f"/courses/{code}/cards", status_code=303)


# ---------------------------------------------------------------- review

@router.get("/review")
def review_page(request: Request, course: str = "", conn=Depends(get_db)):
    c = load(conn, course) if course else None
    cid = c["id"] if c else None
    card = cards.next_due(conn, cid)
    ctx = {"scope": c, "remaining": cards.due_count(conn, cid), "done": cards.reviewed_today(conn, cid)}
    if card:
        codes = known_codes(conn)
        state = srs.State(card["ease"], card["interval"], card["reps"])
        ctx.update(card=card, front=markdown.render(card["front"], codes), back=markdown.render(card["back"], codes),
                   preview=srs.preview(state), labels=srs.GRADE_LABELS)
    return render(request, "cards/review.html", **ctx)


@router.post("/review/{card_id}")
def review_grade(card_id: int, grade: int = Form(...), course: str = Form(""), conn=Depends(get_db)):
    try:
        cards.grade(conn, card_id, grade)
    except KeyError:
        raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return RedirectResponse(f"/review?course={course}" if course else "/review", status_code=303)
