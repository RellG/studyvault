"""Course export: one markdown file, or a print-friendly page (browser Print → Save as PDF)."""
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from .. import clock, competencies as comp, markdown, notes_fs, quizzes, readiness
from ..db import get_db
from ..web import render
from .notes import course_context, known_codes, load

router = APIRouter()


def build_markdown(conn, c) -> str:
    notes_fs.ensure_course_files(conn, c)
    r = readiness.for_course(conn, c["id"])
    out = [f"# {c['code']} · {c['title']}", "",
           f"*Exported {clock.now().strftime('%Y-%m-%d %H:%M')} · Term {c['term_n'] or '—'} · {c['cu']} CU · "
           f"readiness {r['score'] if r['score'] is not None else '—'}*", ""]
    comps = comp.list_for(conn, c["id"])
    if comps:
        out += ["## Competency confidence", "", "| # | Competency | Confidence | Last reviewed |", "|---|---|---|---|"]
        out += [f"| {i} | {x['text'].replace('|', '/')} | {x['confidence'] or '—'} | {x['last_reviewed'] or '—'} |"
                for i, x in enumerate(comps, 1)]
        out.append("")
    for name in ("overview", "competencies", "notebook", "mistakes"):
        text, _ = notes_fs.read_note(c, name)
        # demote headings one level so each file nests under its own section
        body = "\n".join(("#" + line) if line.startswith("#") else line for line in text.strip().splitlines())
        out += [f"## {notes_fs.NOTE_TITLES[name]}", "", body or "*(empty)*", ""]
    cards = conn.execute("SELECT front, back FROM cards WHERE course_id = ? ORDER BY id", (c["id"],)).fetchall()
    if cards:
        out += ["## Flashcards", ""]
        for k in cards:
            out += [f"**Q:** {k['front']}  ", f"**A:** {k['back']}", ""]
    qs = quizzes.bank(conn, c["id"])
    if qs:
        out += ["## Practice questions", ""]
        for i, q in enumerate(qs, 1):
            out += [f"**{i}.** {q['prompt']}", ""]
            if q["kind"] == "short":
                out += [f"*Model answer:* {json.loads(q['answer_json'])}", ""]
            else:
                ans = json.loads(q["answer_json"])
                out += [f"- {'**' if j in ans else ''}{ch}{' ✓**' if j in ans else ''}" for j, ch in enumerate(json.loads(q["choices_json"]))]
                out.append("")
            if q["explanation"]:
                out += [f"*{q['explanation']}*", ""]
    return "\n".join(out).rstrip() + "\n"


@router.get("/courses/{code}/export.md")
def export_md(code: str, conn=Depends(get_db)):
    c = load(conn, code)
    fname = f"{c['code']}-{notes_fs.slugify(c['title'])}-{clock.today().isoformat()}.md"
    return Response(build_markdown(conn, c), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/courses/{code}/export")
def export_page(request: Request, code: str, conn=Depends(get_db)):
    c = load(conn, code)
    html = markdown.render(build_markdown(conn, c), known_codes(conn))
    return render(request, "export.html", **course_context(conn, c), html=html)
