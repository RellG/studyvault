import html
import re

from fastapi import APIRouter, Depends, Request

from .. import catalog
from ..db import get_db
from ..web import render

router = APIRouter()

START, END = "\x02", "\x03"


def fts_query(q: str) -> str | None:
    """Turn free text into a safe FTS5 query: every word must match; the last one as a prefix."""
    words = re.findall(r"\w+", q, flags=re.UNICODE)
    if not words:
        return None
    parts = [f'"{w}"' for w in words]
    parts[-1] += "*"
    return " ".join(parts)


def _snippet(raw: str) -> str:
    return html.escape(raw).replace(START, "<mark>").replace(END, "</mark>")


def search(conn, q: str, term_n: int | None = None, code: str | None = None, limit: int = 50):
    query = fts_query(q)
    if not query:
        return []
    where, params = ["search_fts MATCH ?"], [query]
    if code:
        where.append("c.code = ?")
        params.append(code.upper())
    elif term_n:
        where.append("t.n = ?")
        params.append(term_n)
    rows = conn.execute(
        f"""SELECT f.kind, f.ref, f.title, c.code, c.title AS course_title, t.n AS term_n,
                   snippet(search_fts, 4, '{START}', '{END}', '…', 14) AS snip,
                   snippet(search_fts, 3, '{START}', '{END}', '…', 14) AS tsnip
            FROM search_fts f
            LEFT JOIN courses c ON c.id = f.course_id
            LEFT JOIN terms t ON t.id = c.term_id
            WHERE {' AND '.join(where)}
            ORDER BY bm25(search_fts, 0, 0, 0, 3.0, 1.0) LIMIT ?""", (*params, limit)).fetchall()
    hits = []
    for r in rows:
        if r["kind"] == "note":
            name = r["ref"].split("/", 2)[-1].removesuffix(".md")
            url = f"/courses/{r['code']}/notes/{name}"
            label = r["title"]
        elif r["kind"] == "card":
            url = f"/courses/{r['code']}/cards#card-{r['ref']}"
            label = f"{r['code']} · Flashcard"
        else:
            url = f"/courses/{r['code']}/quizzes/questions/{r['ref']}"
            label = f"{r['code']} · Quiz question"
        hits.append({"kind": r["kind"], "url": url, "label": label, "term_n": r["term_n"],
                     "title": _snippet(r["tsnip"] or ""), "snippet": _snippet(r["snip"] or "")})
    return hits


@router.get("/search")
def search_page(request: Request, q: str = "", term: str = "", course: str = "", conn=Depends(get_db)):
    term_n = int(term) if term.isdigit() else None
    hits = search(conn, q, term_n, course or None) if q.strip() else []
    courses = conn.execute("SELECT code, title FROM courses ORDER BY code").fetchall()
    return render(request, "search.html", q=q, term=term_n, course=course.upper(), hits=hits,
                  terms=catalog.list_terms(conn), courses=courses)
