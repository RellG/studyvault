"""A course's notes as sections ("chunks"), so features and the agent can work from the notes themselves.

The .md files stay the source of truth; note_chunks is derived from them and rebuilt whenever a note is indexed
(notes_fs.index_file), including at startup. A section is the text under one heading. Each has:
  key   a stable id (file + heading path), which cards and questions store to say where they came from
  hash  of the section's text (whitespace ignored), so an edited section shows its cards and questions as stale
Real notes have untidy headings (`# **Wired Data Transmission** #1`, empty `# ` lines, levels that jump), so headings
are cleaned for display and keys, and an empty heading just counts as a blank line.
"""
import hashlib
import re
from collections import Counter

from . import clock

FILES = ("overview", "competencies", "notebook", "mistakes")
BRAIN_FILES = ("overview", "competencies", "notebook")  # what a course is studied from; mistakes are feedback, not source
MAX_CHARS = 6000   # a longer section is split at paragraph breaks into parts

# The placeholder sentences notes_fs puts in a new note. They aren't the student's notes, so they never become sections.
BOILERPLATE = {
    "**Assessment type:** set it on the course's Edit page",
    "Import the competency list on the Competencies tab; a section per competency appears here.",
    "Your working notes for this course: anything that doesn't sit under one competency.",
    "Wrong quiz answers land here automatically. Fill in *Why I missed it*.",
}

# Headings of text studyvault writes into a course's notes (brain.set_overview; builds before it added a dated summary)
GENERATED_RE = re.compile(r"^(From my notes · \d{4}-\d{2}-\d{2}|Course overview \(generated\))$")
HEADING_RE = re.compile(r"^(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


def clean_heading(raw: str) -> str:
    s = re.sub(r"(?<=[\s*_])#+\s*$", "", raw or "")  # closing hashes: `## Title ##`, `# **Title**###`
    s = s.replace("*", "").strip(" _\t")
    return re.sub(r"\s+", " ", s).strip()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80]


def text_hash(text: str) -> str:
    return hashlib.sha1(" ".join((text or "").split()).encode("utf-8")).hexdigest()[:12]


def _meaningful(body: str) -> bool:
    return len(re.sub(r"[\W_]+", "", body)) >= 3  # not just "- " or "***"


def _parts(body: str) -> list[str]:
    """Cut an over-long section at blank lines (and, if one paragraph is huge, at line ends or mid-line)."""
    if len(body) <= MAX_CHARS:
        return [body]
    out, cur = [], ""
    for para in re.split(r"\n\s*\n", body):
        while len(para) > MAX_CHARS:
            cut = para.rfind("\n", 0, MAX_CHARS)
            cut = cut if cut > MAX_CHARS // 2 else MAX_CHARS
            para, rest = para[:cut], para[cut:].lstrip("\n")
            if cur:
                out.append(cur)
                cur = ""
            out.append(para)
            para = rest
        if cur and len(cur) + len(para) + 2 > MAX_CHARS:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    if cur.strip():
        out.append(cur)
    return [p.strip("\n") for p in out if p.strip()]


def split(text: str, file: str = "notebook") -> list[dict]:
    """Cut one note file into sections: [{file, key, ord, heading, breadcrumb, level, text, hash, chars}]."""
    stack: list[tuple[int, str]] = []
    blocks: list[tuple[int, str, str, str]] = []
    cur = {"level": 0, "heading": "", "crumb": "", "lines": []}
    in_fence = started = False

    def flush():
        body = "\n".join(cur["lines"]).strip("\n")
        if _meaningful(body):
            blocks.append((cur["level"], cur["heading"], cur["crumb"], body))

    for line in (text or "").replace("\r\n", "\n").split("\n"):
        if not in_fence and line.strip() in BOILERPLATE:
            continue
        if FENCE_RE.match(line):
            in_fence = not in_fence
        m = None if in_fence else HEADING_RE.match(line)
        title = clean_heading(m.group(2)) if m else ""
        if m and title:
            level = len(m.group(1))
            if not started and level == 1:  # the file's own title line ("# D413 · …: notebook"), not a section
                started = True
                continue
            started = True
            flush()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            cur = {"level": level, "heading": title, "crumb": " > ".join(h for _, h in stack), "lines": []}
        elif m:  # an empty heading line behaves like a blank line
            continue
        else:
            if line.strip():
                started = True
            cur["lines"].append(line)
    flush()

    seen: Counter = Counter()
    out = []
    for level, heading, crumb, body in blocks:
        base = f"{file}#{_slug(crumb) or 'top'}"
        seen[base] += 1
        base = base if seen[base] == 1 else f"{base}~{seen[base]}"
        for n, part in enumerate(_parts(body), 1):
            out.append({"file": file, "key": base if n == 1 else f"{base}.p{n}", "ord": len(out), "heading": heading,
                        "breadcrumb": crumb if n == 1 else f"{crumb} (part {n})", "level": level, "text": part,
                        "hash": text_hash(part), "chars": len(part)})
    return out


def where(c) -> str:
    return c["breadcrumb"] or "(top of file)"


# ---------------------------------------------------------------- database

def generated(heading: str) -> bool:
    """A section studyvault wrote into the notes itself (the course overview, or a build's summary before that). It's about
    the notes, not part of them, so it never counts as notes."""
    return bool(GENERATED_RE.match(heading or ""))


def _own(rows) -> dict:
    """{key: hash} of the student's own sections."""
    return {r["key"]: r["hash"] for r in rows if not generated(r["heading"])}


def sync_file(conn, course_id: int, file: str, text: str) -> bool:
    """Make note_chunks match this file's text. Returns True if anything changed (and marks the course's profile stale)."""
    if file not in FILES:
        return False
    new = split(text, file)
    rows = conn.execute("SELECT key, hash, ord, breadcrumb, heading FROM note_chunks WHERE course_id = ? AND file = ?",
                        (course_id, file)).fetchall()
    if {r["key"]: (r["hash"], r["ord"], r["breadcrumb"]) for r in rows} == {c["key"]: (c["hash"], c["ord"], c["breadcrumb"]) for c in new}:
        return False
    notes_changed = _own(rows) != _own(new)
    now = clock.now().isoformat()
    with conn:
        keys = [c["key"] for c in new]
        conn.execute(f"DELETE FROM note_chunks WHERE course_id = ? AND file = ?"
                     f"{' AND key NOT IN (' + ','.join('?' * len(keys)) + ')' if keys else ''}", (course_id, file, *keys))
        for c in new:
            conn.execute(
                "INSERT INTO note_chunks(course_id, file, key, ord, heading, breadcrumb, level, text, hash, chars, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(course_id, file, key) DO UPDATE SET "
                "ord = excluded.ord, heading = excluded.heading, breadcrumb = excluded.breadcrumb, level = excluded.level, "
                "text = excluded.text, hash = excluded.hash, chars = excluded.chars, updated_at = excluded.updated_at",
                (course_id, file, c["key"], c["ord"], c["heading"], c["breadcrumb"], c["level"], c["text"], c["hash"],
                 c["chars"], now))
        if file in BRAIN_FILES and notes_changed:  # not when only studyvault's own overview section changed
            conn.execute("UPDATE course_brain SET status = 'stale', updated_at = ? WHERE course_id = ? AND status = 'fresh'",
                         (now, course_id))
    return True


def get(conn, course_id: int, key: str):
    return conn.execute("SELECT * FROM note_chunks WHERE course_id = ? AND key = ?", (course_id, str(key or "").strip())).fetchone()


def find(conn, course_id: int, file: str, heading: str):
    """The section under this heading in this file, or None if there's no such heading or it appears more than once."""
    want = clean_heading(heading)
    if not want:
        return None
    rows = conn.execute("SELECT * FROM note_chunks WHERE course_id = ? AND file = ? AND heading = ? COLLATE NOCASE ORDER BY ord",
                        (course_id, file, want)).fetchall()
    return rows[0] if len(rows) == 1 else None


def ref(conn, course_id: int, key) -> dict | None:
    """What a card or question stores about its source: {key, hash, label}. None if the section doesn't exist."""
    c = get(conn, course_id, key) if key else None
    return {"key": c["key"], "hash": c["hash"], "label": where(c)} if c else None


def notes_hash(conn, course_id: int) -> str:
    rows = [r for r in conn.execute("SELECT file, key, hash, heading FROM note_chunks WHERE course_id = ? AND file IN "
                                    "('overview','competencies','notebook') ORDER BY file, ord", (course_id,)) if not generated(r["heading"])]
    return hashlib.sha1("\n".join(f"{r['file']}|{r['key']}|{r['hash']}" for r in rows).encode()).hexdigest()[:12]


def outline(conn, course_id: int, files=BRAIN_FILES) -> list[dict]:
    """Every section in order, with how many cards and questions were made from it."""
    files = [f for f in files if f in FILES]
    if not files:
        return []
    rows = conn.execute(
        f"""SELECT n.file, n.key, n.ord, n.heading, n.breadcrumb, n.level, n.chars, n.hash,
                   (SELECT COUNT(*) FROM cards k WHERE k.course_id = n.course_id AND k.source_key = n.key) AS cards,
                   (SELECT COUNT(*) FROM questions q WHERE q.course_id = n.course_id AND q.source_key = n.key) AS questions,
                   (SELECT COUNT(*) FROM cards k WHERE k.course_id = n.course_id AND k.source_key = n.key
                                                   AND k.source_hash IS NOT n.hash) AS stale_cards,
                   (SELECT COUNT(*) FROM questions q WHERE q.course_id = n.course_id AND q.source_key = n.key
                                                       AND q.source_hash IS NOT n.hash) AS stale_questions
            FROM note_chunks n WHERE n.course_id = ? AND n.file IN ({','.join('?' * len(files))})
            ORDER BY CASE n.file WHEN 'overview' THEN 0 WHEN 'competencies' THEN 1 WHEN 'notebook' THEN 2 ELSE 3 END, n.ord""",
        (course_id, *files)).fetchall()
    return [dict(r) for r in rows]


def freshness(conn, course_id: int) -> dict:
    """For cards and questions that came from a section: how many still match it (current), how many were made from
    text that has since changed (stale), and how many point at a section that is gone (missing). `unlinked` = made by
    hand or before sections existed."""
    out = {}
    for table in ("cards", "questions"):
        r = conn.execute(
            f"""SELECT COUNT(*) AS total,
                       SUM(t.source_key IS NULL) AS unlinked,
                       SUM(t.source_key IS NOT NULL AND n.id IS NOT NULL AND n.hash = t.source_hash) AS current,
                       SUM(t.source_key IS NOT NULL AND n.id IS NOT NULL AND n.hash IS NOT t.source_hash) AS stale,
                       SUM(t.source_key IS NOT NULL AND n.id IS NULL) AS missing
                FROM {table} t LEFT JOIN note_chunks n ON n.course_id = t.course_id AND n.key = t.source_key
                WHERE t.course_id = ?""", (course_id,)).fetchone()
        out[table] = {k: r[k] or 0 for k in ("total", "unlinked", "current", "stale", "missing")}
    return out


def states(conn, table: str, course_id: int) -> dict:
    """{item id: ('current' | 'stale' | 'missing', where)} for the cards or questions of a course that came from a section."""
    if table not in ("cards", "questions"):
        raise ValueError(table)
    rows = conn.execute(
        f"""SELECT t.id, n.breadcrumb, CASE WHEN n.id IS NULL THEN 'missing' WHEN n.hash IS NOT t.source_hash THEN 'stale'
                                            ELSE 'current' END AS state
            FROM {table} t LEFT JOIN note_chunks n ON n.course_id = t.course_id AND n.key = t.source_key
            WHERE t.course_id = ? AND t.source_key IS NOT NULL""", (course_id,)).fetchall()
    return {r["id"]: (r["state"], (r["breadcrumb"] or "(top of file)") if r["state"] != "missing" else "") for r in rows}
