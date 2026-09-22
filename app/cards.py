"""Flashcards: bulk-import syntax, storage, and the review loop."""
import re

from . import clock, notes_fs, srs

QA_INLINE_RE = re.compile(r"^\s*Q:\s*(.+?)\s+/?\s*A:\s*(.+)$", re.I)
Q_RE = re.compile(r"^\s*Q:\s*(.*)$", re.I)
A_RE = re.compile(r"^\s*A:\s*(.*)$", re.I)
CLOZE_RE = re.compile(r"\{\{c(\d+)::(.+?)(?:::(.+?))?\}\}")
LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def _cloze_cards(line: str) -> list[dict]:
    cards = []
    for n in sorted({m.group(1) for m in CLOZE_RE.finditer(line)}, key=int):
        def front_sub(m, n=n):
            if m.group(1) == n:
                return f"[{m.group(3) or '…'}]"
            return m.group(2)

        def back_sub(m, n=n):
            return f"**{m.group(2)}**" if m.group(1) == n else m.group(2)

        cards.append({"front": CLOZE_RE.sub(front_sub, line).strip(),
                      "back": CLOZE_RE.sub(back_sub, line).strip(), "type": "cloze"})
    return cards


def parse(text: str) -> tuple[list[dict], list[str]]:
    """Parse pasted text or a notes section into cards.

    Supports:  Q: … / A: …  (on one line or across lines; blank line ends a card)
               term :: definition
               {{c1::cloze}} and {{c1::cloze::hint}}, one card per cN
    Returns (cards, skipped_lines). Headings and blank lines are ignored, not reported."""
    cards, skipped = [], []
    cur, field = None, None
    in_code = False

    def flush():
        nonlocal cur, field
        if cur and cur["front"].strip() and cur["back"].strip():
            cards.append({"front": cur["front"].strip(), "back": cur["back"].strip(), "type": "basic"})
        elif cur:
            skipped.append(f"Q: {cur['front'].strip()} (no answer)")
        cur, field = None, None

    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            if cur:
                cur[field] += "\n" + line
            continue
        if in_code:
            if cur:
                cur[field] += "\n" + line
            continue
        bare = LIST_PREFIX_RE.sub("", line)
        m = QA_INLINE_RE.match(bare)
        if m:
            flush()
            cards.append({"front": m.group(1).strip(), "back": m.group(2).strip(), "type": "basic"})
            continue
        m = Q_RE.match(bare)
        if m:
            flush()
            cur, field = {"front": m.group(1), "back": ""}, "front"
            continue
        m = A_RE.match(bare)
        if m and cur:
            cur["back"], field = m.group(1), "back"
            continue
        if not line.strip():
            flush()
            continue
        if cur:
            cur[field] += "\n" + line
            continue
        if line.lstrip().startswith("#"):
            continue
        if CLOZE_RE.search(bare):
            cards.extend(_cloze_cards(bare))
            continue
        if "::" in bare:
            term, _, definition = bare.partition("::")
            if term.strip() and definition.strip():
                cards.append({"front": term.strip(), "back": definition.strip(), "type": "basic"})
                continue
        skipped.append(line.strip())
    flush()
    return cards, skipped


def sections(text: str) -> dict[str, str]:
    """Split a notes file into {heading: body} at `## ` headings. Text before the first one is ''."""
    out, current, buf = {}, "", []
    for line in (text or "").splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            out[current] = "\n".join(buf)
            current, buf = m.group(1), []
        else:
            buf.append(line)
    out[current] = "\n".join(buf)
    return {k: v for k, v in out.items() if k or v.strip()}


def add(conn, course_id: int, front: str, back: str, *, type_: str = "basic", competency_id=None,
        source: str = "manual") -> int:
    today = clock.today().isoformat()
    with conn:
        cur = conn.execute(
            "INSERT INTO cards(course_id, competency_id, front, back, type, due_on, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (course_id, competency_id, front.strip(), back.strip(), type_, today, source, clock.now().isoformat()))
    return cur.lastrowid


def import_cards(conn, course_id: int, parsed: list[dict], competency_id=None, source="manual") -> tuple[int, int]:
    """Insert parsed cards, skipping ones whose front already exists in this course. Returns (added, duplicates)."""
    existing = {r[0] for r in conn.execute("SELECT front FROM cards WHERE course_id = ?", (course_id,))}
    added = dup = 0
    for c in parsed:
        if c["front"] in existing:
            dup += 1
            continue
        add(conn, course_id, c["front"], c["back"], type_=c["type"], competency_id=competency_id, source=source)
        existing.add(c["front"])
        added += 1
    return added, dup


def competency_for_heading(conn, course_id: int, heading: str):
    row = conn.execute("SELECT id FROM competencies WHERE course_id = ? AND lower(text) = lower(?)",
                       (course_id, heading.strip())).fetchone()
    return row[0] if row else None


def notes_sections(course) -> list[tuple[str, str]]:
    """(file, heading) pairs for the 'import from notes' picker."""
    out = []
    for name in ("competencies", "notebook", "overview"):
        text, _ = notes_fs.read_note(course, name)
        for heading in sections(text):
            out.append((name, heading))
    return out


# ---------------------------------------------------------------- review

def due_count(conn, course_id: int | None = None) -> int:
    today = clock.today().isoformat()
    if course_id:
        return conn.execute("SELECT COUNT(*) FROM cards WHERE course_id = ? AND due_on <= ?", (course_id, today)).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM cards WHERE due_on <= ?", (today,)).fetchone()[0]


def next_due(conn, course_id: int | None = None):
    today = clock.today().isoformat()
    sql = ("SELECT cards.*, courses.code FROM cards JOIN courses ON courses.id = cards.course_id "
           "WHERE due_on <= ? {} ORDER BY due_on, cards.reps, cards.id LIMIT 1")
    if course_id:
        return conn.execute(sql.format("AND course_id = ?"), (today, course_id)).fetchone()
    return conn.execute(sql.format(""), (today,)).fetchone()


def grade(conn, card_id: int, g: int) -> srs.State:
    card = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    if not card:
        raise KeyError(card_id)
    new = srs.review(srs.State(card["ease"], card["interval"], card["reps"]), g)
    with conn:
        conn.execute("UPDATE cards SET ease = ?, interval = ?, reps = ?, due_on = ? WHERE id = ?",
                     (new.ease, new.interval, new.reps, srs.due_on(new, clock.today()).isoformat(), card_id))
        conn.execute("INSERT INTO reviews(card_id, reviewed_at, grade) VALUES (?, ?, ?)",
                     (card_id, clock.now().isoformat(), g))
    return new


def reviewed_today(conn, course_id: int | None = None) -> int:
    start = clock.today().isoformat()
    if course_id:
        return conn.execute("SELECT COUNT(*) FROM reviews r JOIN cards c ON c.id = r.card_id "
                            "WHERE c.course_id = ? AND r.reviewed_at >= ?", (course_id, start)).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM reviews WHERE reviewed_at >= ?", (start,)).fetchone()[0]
