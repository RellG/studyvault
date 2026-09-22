"""Competency lists: paste-import, confidence ratings, and their sections in competencies.md."""
import re

from . import clock, notes_fs

BULLET_RE = re.compile(r"^\s*(?:[-*•·▪◦‣–]+|\(?\d{1,2}[.)]|\(?[a-zA-Z][.)])\s+")


def parse(text: str) -> list[str]:
    """One competency per line; strips bullets and list numbering, drops blanks and duplicates."""
    out, seen = [], set()
    for line in (text or "").splitlines():
        line = BULLET_RE.sub("", line).strip()
        line = re.sub(r"\s+", " ", line)
        if line and line.lower() not in seen:
            seen.add(line.lower())
            out.append(line)
    return out


def list_for(conn, course_id):
    return conn.execute("SELECT * FROM competencies WHERE course_id = ? ORDER BY ord", (course_id,)).fetchall()


def import_list(conn, course, lines: list[str]) -> dict:
    """Replace the course's competencies with `lines`. Rows whose text is unchanged keep their rating."""
    existing = {r["text"].lower(): r for r in list_for(conn, course["id"])}
    kept = added = 0
    wanted = {t.lower() for t in lines}
    with conn:
        for r in existing.values():
            if r["text"].lower() not in wanted:
                conn.execute("DELETE FROM competencies WHERE id = ?", (r["id"],))
        for i, text in enumerate(lines):
            r = existing.get(text.lower())
            if r:
                conn.execute("UPDATE competencies SET ord = ?, text = ? WHERE id = ?", (i, text, r["id"]))
                kept += 1
            else:
                conn.execute("INSERT INTO competencies(course_id, ord, text) VALUES (?, ?, ?)", (course["id"], i, text))
                added += 1
    removed = len(existing) - kept
    sections = add_sections(conn, course, lines)
    return {"kept": kept, "added": added, "removed": removed, "sections": sections}


def add_sections(conn, course, lines: list[str]) -> int:
    """Append a `## <competency>` section to competencies.md for each competency that doesn't have one.
    Never deletes or rewrites existing notes."""
    notes_fs.ensure_course_files(conn, course)
    text, _ = notes_fs.read_note(course, "competencies")
    have = {notes_fs.slugify(m.group(1)) for m in re.finditer(r"^##\s+(.+?)\s*$", text, re.M)}
    missing = [t for t in lines if notes_fs.slugify(t) not in have]
    if missing:
        notes_fs.append_note(conn, course, "competencies", "\n".join(f"## {t}\n\n" for t in missing))
    return len(missing)


def set_confidence(conn, comp_id: int, value: int | None):
    if value is not None and not 1 <= value <= 5:
        raise ValueError("confidence must be 1–5")
    with conn:
        conn.execute("UPDATE competencies SET confidence = ?, last_reviewed = ? WHERE id = ?",
                     (value, clock.today().isoformat(), comp_id))


def mark_reviewed(conn, comp_id: int):
    with conn:
        conn.execute("UPDATE competencies SET last_reviewed = ? WHERE id = ?", (clock.today().isoformat(), comp_id))


def weakest(conn, course_id, n=3):
    """Unrated first, then lowest confidence, then least recently reviewed."""
    return conn.execute(
        "SELECT * FROM competencies WHERE course_id = ? ORDER BY confidence IS NOT NULL, confidence, "
        "last_reviewed IS NOT NULL, last_reviewed, ord LIMIT ?", (course_id, n)).fetchall()


def anchor(text: str) -> str:
    return notes_fs.slugify(text) or "section"
