"""Notes on disk: the .md files are the source of truth; the DB only indexes them.

Layout: notes/term-<n>/<code-lower>-<slug>/{overview,competencies,notebook,mistakes}.md (+ pa/ for PA courses).
Writes are atomic (tmp → fsync → rename). Git commits are batched: saves mark files dirty and a background
loop commits at most once a minute, so 2-second autosave doesn't produce a commit per keystroke pause.
"""
import hashlib
import logging
import os
import re
import subprocess
import threading
from pathlib import Path

from . import clock
from .config import settings

log = logging.getLogger("studyvault.notes")

GIT_ID = ["-c", "user.name=studyvault", "-c", "user.email=studyvault@localhost"]
NOTE_FILES = ["overview", "competencies", "notebook", "mistakes"]
NOTE_TITLES = {"overview": "Overview", "competencies": "Competencies", "notebook": "Notebook", "mistakes": "Mistakes"}
NAME_RE = re.compile(r"^(overview|competencies|notebook|mistakes|pa/[a-z0-9][a-z0-9._-]{0,80})$")


class NoteConflict(Exception):
    """The file changed on disk since the editor loaded it."""


class BadNoteName(ValueError):
    pass


# ---------------------------------------------------------------- git

def git(*args, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *GIT_ID, *args], cwd=cwd or settings.notes_dir,
                          capture_output=True, text=True, timeout=30)


def ensure_repo(notes_dir: Path | None = None) -> None:
    notes_dir = notes_dir or settings.notes_dir
    notes_dir.mkdir(parents=True, exist_ok=True)
    if not (notes_dir / ".git").exists():
        r = git("init", "-q", "-b", "main", cwd=notes_dir)
        if r.returncode != 0:
            log.error("git init failed: %s", r.stderr)


_dirty: dict[str, set[str]] = {}
_dirty_lock = threading.Lock()


def mark_dirty(code: str, filename: str) -> None:
    with _dirty_lock:
        _dirty.setdefault(code, set()).add(filename)


def commit(message: str) -> bool:
    """git add -A + commit in the notes repo. Failures are logged, never raised."""
    try:
        git("add", "-A")
        if not git("status", "--porcelain").stdout.strip():
            return False
        r = git("commit", "-q", "-m", message)
        if r.returncode != 0:
            log.warning("git commit failed: %s", r.stderr.strip())
            return False
        return True
    except Exception as e:  # noqa: BLE001 — a failed commit must never block a save
        log.warning("git commit error: %s", e)
        return False


def commit_pending() -> bool:
    with _dirty_lock:
        pending = {k: sorted(v) for k, v in _dirty.items()}
        _dirty.clear()
    if not pending:
        return False
    msg = "notes: " + "; ".join(f"{code} {', '.join(files)}" for code, files in sorted(pending.items()))
    return commit(msg)


# ---------------------------------------------------------------- paths

def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def course_rel_dir(course) -> str:
    term = f"term-{course['term_n']}" if course["term_n"] else "unassigned"
    return f"{term}/{course['code'].lower()}-{slugify(course['title'])}"


def course_dir(course) -> Path:
    return settings.notes_dir / course_rel_dir(course)


def check_name(name: str) -> str:
    name = name.removesuffix(".md")
    if not NAME_RE.match(name) or ".." in name:
        raise BadNoteName(name)
    return name


def note_rel(course, name: str) -> str:
    return f"{course_rel_dir(course)}/{check_name(name)}.md"


def note_path(course, name: str) -> Path:
    return settings.notes_dir / note_rel(course, name)


def version(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- read / write

def _template(course, name: str) -> str:
    title = f"{course['code']} · {course['title']}"
    if name == "overview":
        return (f"# {title}\n\n"
                "**Assessment type:** set it on the course's Edit page\n\n"
                "## What this course covers\n\n\n## Resources I'm using\n\n- \n\n## Plan\n\n")
    if name == "competencies":
        return f"# {title}: competencies\n\nImport the competency list on the Competencies tab; a section per competency appears here.\n\n"
    if name == "notebook":
        return f"# {title}: notebook\n\nYour working notes for this course: anything that doesn't sit under one competency.\n\n"
    if name == "mistakes":
        return f"# {title}: mistakes\n\nWrong quiz answers land here automatically. Fill in *Why I missed it*.\n\n"
    return ""


def ensure_course_files(conn, course) -> None:
    d = course_dir(course)
    created = False
    for name in NOTE_FILES:
        p = d / f"{name}.md"
        if not p.exists():
            _atomic_write(p, _template(course, name))
            index_file(conn, course, name)
            created = True
    if course["assessment_type"] == "PA":
        (d / "pa").mkdir(parents=True, exist_ok=True)
    if created:
        mark_dirty(course["code"], "created")


def read_note(course, name: str) -> tuple[str, str]:
    p = note_path(course, name)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    return text, version(text)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_note(conn, course, name: str, text: str, base_version: str | None = None) -> str:
    """Save a note. If base_version is given and the file changed since, raise NoteConflict."""
    name = check_name(name)
    p = note_path(course, name)
    if base_version is not None and p.exists():
        current = version(p.read_text(encoding="utf-8"))
        if current != base_version:
            raise NoteConflict(current)
    text = text.replace("\r\n", "\n")
    _atomic_write(p, text)
    index_file(conn, course, name)
    mark_dirty(course["code"], f"{name}.md")
    return version(text)


def append_note(conn, course, name: str, text: str) -> str:
    current, _ = read_note(course, name)
    if not current:
        current = _template(course, check_name(name))
    sep = "" if current.endswith("\n\n") else ("\n" if current.endswith("\n") else "\n\n")
    return write_note(conn, course, name, current + sep + text)


def migrate_scratch_to_notebook() -> int:
    """v1.1: the Scratch tab became Notebook. Rename each course's scratch.md → notebook.md once (and its
    title line), then commit. Idempotent; skips a course that somehow already has a notebook.md."""
    moved = 0
    for src in sorted(settings.notes_dir.glob("*/*/scratch.md")):
        dst = src.with_name("notebook.md")
        if dst.exists():
            log.warning("not migrating %s: %s already exists", src, dst)
            continue
        text = re.sub(r"^(# .*): scratch[ \t]*$", r"\1: notebook", src.read_text(encoding="utf-8"), count=1, flags=re.M)
        _atomic_write(dst, text)
        src.unlink()
        moved += 1
    if moved:
        commit(f"notes: rename scratch.md -> notebook.md ({moved} course{'s' if moved != 1 else ''})")
    return moved


def list_pa_files(course) -> list[str]:
    d = course_dir(course) / "pa"
    return sorted(p.stem for p in d.glob("*.md")) if d.exists() else []


# ---------------------------------------------------------------- index

def _title(course, name: str) -> str:
    return f"{course['code']} · {NOTE_TITLES.get(name, name)}"


def index_file(conn, course, name: str) -> None:
    rel = note_rel(course, name)
    p = settings.notes_dir / rel
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    with conn:
        conn.execute("DELETE FROM search_fts WHERE kind = 'note' AND ref = ?", (rel,))
        if p.exists():
            conn.execute("INSERT INTO search_fts(kind, ref, course_id, title, body) VALUES ('note', ?, ?, ?, ?)",
                         (rel, course["id"], _title(course, name), text))
            conn.execute("INSERT INTO notes_index(path, course_id, title, updated_at) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(path) DO UPDATE SET course_id = excluded.course_id, title = excluded.title, "
                         "updated_at = excluded.updated_at", (rel, course["id"], _title(course, name), clock.now().isoformat()))
        else:
            conn.execute("DELETE FROM notes_index WHERE path = ?", (rel,))


def reindex_all(conn) -> int:
    """Rebuild the notes part of the search index from disk (picks up edits made outside the app)."""
    courses = conn.execute("SELECT c.*, t.n AS term_n FROM courses c LEFT JOIN terms t ON t.id = c.term_id").fetchall()
    with conn:
        conn.execute("DELETE FROM search_fts WHERE kind = 'note'")
        conn.execute("DELETE FROM notes_index")
    n = 0
    for c in courses:
        d = course_dir(c)
        if not d.exists():
            continue
        for p in sorted(d.rglob("*.md")):
            name = p.relative_to(d).as_posix().removesuffix(".md")
            try:
                index_file(conn, c, name)
                n += 1
            except BadNoteName:
                continue
    return n


def move_course(conn, old, new) -> None:
    """Course changed term or title: move its folder, fix the index, commit the rename."""
    old_rel, new_rel = course_rel_dir(old), course_rel_dir(new)
    src, dst = settings.notes_dir / old_rel, settings.notes_dir / new_rel
    if old_rel == new_rel or not src.exists():
        return
    if dst.exists():
        log.error("not moving %s: %s already exists", src, dst)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    try:
        src.parent.rmdir()  # drop the old term folder if now empty
    except OSError:
        pass
    with conn:
        conn.execute("UPDATE notes_index SET path = ? || substr(path, ?) WHERE path LIKE ? || '/%'",
                     (new_rel, len(old_rel) + 1, old_rel))
        conn.execute("UPDATE search_fts SET ref = ? || substr(ref, ?) WHERE kind = 'note' AND ref LIKE ? || '/%'",
                     (new_rel, len(old_rel) + 1, old_rel))
    commit(f"notes: move {new['code']} {old_rel} -> {new_rel}")
