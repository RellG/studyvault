"""Build from notes (Slice 12, step 2): work out what a course's notes cover, and draft the course features from them.

1. analyze: one model call reads every section of the notes (chunks.py) and returns a profile: a summary, a one-line
   outline of each section, the learning objectives, competencies, and thin spots. Stored in course_brain; marked stale
   whenever the notes change.
2. build: analyze if needed, then write flashcards and practice questions section by section (a few sections per call),
   add a study plan, and save everything as a draft (brain_drafts). Nothing touches the course until the student
   reviews the draft and applies it (apply_draft).

Grounding: items come from what the notes say. The one exception is a section that is a bare list of topics (such as a
pre-assessment list); for those the model may answer from general knowledge and every such item is flagged "Check".

The provider is the same one the rest of the app uses. On Gemini's free tier (5 requests a minute) calls are spaced out
and a failed batch costs only that batch, not the whole draft. Work runs in a background thread, one job at a time,
and the page polls progress().
"""
import json
import logging
import re
import threading
import time
from datetime import timedelta

from . import ai, ai_actions, cards, catalog, chunks, clock, competencies, db, notes_fs, quizzes
from . import tasks as study_tasks
from .config import settings

log = logging.getLogger("studyvault.brain")

ANALYZE_CHARS = 90_000   # notes up to this size go to the model in one call; longer ones are outlined in batches first
BATCH_CHARS = 14_000     # sections per generation call
MIN_SECTION_CHARS = 40   # shorter than this is a stray word, not study material
MAX_COMPETENCIES = 20
MAX_TERMS = 8
MAX_TASKS = 14
MAX_TOPICS = 60
DEPTHS = {"light": 0.5, "normal": 1.0, "thorough": 1.5}
CALL_GAP_S = None        # None = 13 s between calls on Google (free tier: 5 a minute), none on Anthropic. Tests set 0.

SYSTEM = ai_actions.SYSTEM
SYSTEM_TOPICS = ai_actions.SYSTEM_FILL


class BrainError(ai.AIError):
    """User-facing: shown as-is."""


# ---------------------------------------------------------------- the notes, as the model sees them

OWN_SUMMARY_RE = re.compile(r"^From my notes · \d{4}-\d{2}-\d{2}$")  # the heading apply_draft gives the overview summary it adds


def sections(conn, course_id: int) -> list:
    """The sections a course is studied from, in reading order, without stray words or the summary a build added
    (that is the model's own text about the notes, not the notes)."""
    rows = conn.execute("SELECT * FROM note_chunks WHERE course_id = ? AND file IN ('overview', 'competencies', 'notebook') "
                        "ORDER BY CASE file WHEN 'overview' THEN 0 WHEN 'competencies' THEN 1 ELSE 2 END, ord",
                        (course_id,)).fetchall()
    return [r for r in rows if r["chars"] >= MIN_SECTION_CHARS and not OWN_SUMMARY_RE.match(r["heading"])]


def is_objectives_only(text: str) -> bool:
    """A section that only lists what the unit will teach ("At the end of this unit, you will be able to…" and short lines).
    The model is told to write nothing for these, so they aren't counted as gaps to fill."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    at = next((i for i, ln in enumerate(lines) if re.search(r"\b(will|should) be able to\b", ln, re.I)), None)
    if at is None or at > 2:
        return False
    rest = [ln for ln in lines[at + 1:] if not re.fullmatch(r"\**learning objectives:?\**", ln, re.I)]
    return bool(rest) and all(len(ln) <= 160 for ln in rest) and sum(map(len, rest)) <= 1200


def is_topic_list(text: str) -> bool:
    """A section that is mostly short bullet lines: terms to learn, not explanations (e.g. a pre-assessment list)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    bullets = [ln for ln in lines if re.match(r"^[-*•]\s*\S", ln)]
    if len(bullets) < 4 or len(bullets) < 0.6 * len(lines):
        return False
    return sum(len(b) for b in bullets) / len(bullets) < 50


def topic_terms(text: str) -> list[str]:
    out = []
    for ln in text.splitlines():
        m = re.match(r"^\s*[-*•]\s*(.+?)\s*$", ln)
        if m and len(m.group(1)) < 80:
            out.append(re.sub(r"\s+", " ", m.group(1)))
    return out[:MAX_TOPICS]


def _block(rows) -> str:
    return "\n\n".join(f'<section key="{r["key"]}" title="{chunks.where(r)}">\n{r["text"]}\n</section>' for r in rows)


def _object(text: str) -> dict:
    """The JSON object in a model reply (tolerates a code fence or a sentence around it)."""
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise BrainError("The model didn't return anything I could read. Try again.")
    try:
        data = json.loads(text[a:b + 1])
    except ValueError:
        raise BrainError("The model's reply wasn't valid JSON (it may have been cut off). Try again, or with less text.")
    if not isinstance(data, dict):
        raise BrainError("The model's reply wasn't in the expected shape. Try again.")
    return data


_last_call = 0.0


def _call(prompt: str, system: str = SYSTEM) -> str:
    """One provider call, spaced from the last so the free tier's per-minute limit isn't hit."""
    global _last_call
    gap = CALL_GAP_S if CALL_GAP_S is not None else (13.0 if settings.ai_provider == "google" else 0.0)
    wait = _last_call + gap - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    try:
        return ai.complete(prompt, max_tokens=max(settings.ai_max_output_tokens, ai.BULK_MAX_TOKENS), system=system,
                           timeout=ai.BULK_TIMEOUT_S)
    finally:
        _last_call = time.monotonic()


def _str(value, limit) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


# ---------------------------------------------------------------- analysis

OUTLINE_FORMAT = ('"outline": [{"key": "<the section key>", "title": "a short title", "summary": "one sentence on what the '
                  'section says", "terms": ["up to 8 key terms"], "importance": 1 to 3, 3 = core exam material}] '
                  "with one entry per section, in order")


def _analysis_prompt(course, rows) -> str:
    return (f"Analyze my notes for {course['code']} {course['title']}. They are cut into sections, each with a key. "
            "Reply with ONLY a JSON object:\n"
            '{"summary": "3-5 sentences: what the notes cover and how the course fits together",\n'
            f" {OUTLINE_FORMAT},\n"
            ' "objectives": ["the learning objectives the notes state, close to verbatim"],\n'
            ' "competencies": [{"text": "something a student should be able to do after this course, one sentence", '
            '"keys": ["keys of the sections it comes from"]}] (4 to 10),\n'
            ' "thin_spots": [{"key": "...", "why": "what is missing or too brief in that section"}]}\n'
            "Use only what the notes support; do not add topics they don't contain.\n\n" + _block(rows))


def _batches(rows, limit: int) -> list[list]:
    out, cur, size = [], [], 0
    for r in rows:
        if cur and size + r["chars"] > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(r)
        size += r["chars"]
    if cur:
        out.append(cur)
    return out


def _clean_profile(raw: dict, rows) -> dict:
    """Keep only what refers to real sections; every section ends up in the outline."""
    by_key = {r["key"]: r for r in rows}
    given = {}
    for o in raw.get("outline") or []:
        if isinstance(o, dict) and o.get("key") in by_key and o["key"] not in given:
            try:
                imp = min(3, max(1, int(o.get("importance") or 2)))
            except (TypeError, ValueError):
                imp = 2
            given[o["key"]] = {"key": o["key"], "title": _str(o.get("title"), 120) or chunks.where(by_key[o["key"]]),
                               "summary": _str(o.get("summary"), 300), "importance": imp,
                               "terms": [t for t in (_str(x, 60) for x in (o.get("terms") or [])[:MAX_TERMS]) if t]}
    outline = [given.get(r["key"]) or {"key": r["key"], "title": chunks.where(r), "summary": "", "importance": 2, "terms": []}
               for r in rows]
    comps, seen = [], set()
    for c in raw.get("competencies") or []:
        text = _str(c.get("text") if isinstance(c, dict) else c, 300)
        if text and text.lower() not in seen and len(comps) < MAX_COMPETENCIES:
            seen.add(text.lower())
            keys = [k for k in (c.get("keys") or [] if isinstance(c, dict) else []) if k in by_key]
            comps.append({"text": text, "keys": keys})
    thin = [{"key": t["key"], "why": _str(t.get("why"), 200)} for t in raw.get("thin_spots") or []
            if isinstance(t, dict) and t.get("key") in by_key]
    weak = []
    for r in rows:
        if is_topic_list(r["text"]):
            weak.append({"key": r["key"], "where": chunks.where(r), "terms": topic_terms(r["text"])})
    return {"summary": _str(raw.get("summary"), 1200), "outline": outline,
            "objectives": [o for o in (_str(x, 300) for x in (raw.get("objectives") or [])[:20]) if o],
            "competencies": comps, "thin_spots": thin, "topic_lists": weak}


def analyze(conn, course, state: dict | None = None) -> dict:
    """Read all the course's notes and store the profile. Raises BrainError (or ai.AIError) with a message to show."""
    rows = sections(conn, course["id"])
    if not rows:
        raise BrainError("There's nothing to analyze yet: write some notes first (the Notebook tab).")
    used_hash = chunks.notes_hash(conn, course["id"])
    total = sum(r["chars"] for r in rows)
    _step(state, f"Reading {len(rows)} sections ({total:,} characters)…")
    if total <= ANALYZE_CHARS:
        raw = _object(_call(_analysis_prompt(course, rows)))
    else:  # too long for one call: outline in batches, then summarize the outline
        outline = []
        parts = _batches(rows, ANALYZE_CHARS // 3)
        for i, part in enumerate(parts, 1):
            _step(state, f"Outlining the notes: part {i} of {len(parts)}…")
            got = _object(_call(f"Outline these sections of my notes for {course['code']} {course['title']}. Reply with ONLY a "
                                "JSON object: {" + OUTLINE_FORMAT + "}\n\n" + _block(part)))
            outline += [o for o in got.get("outline") or [] if isinstance(o, dict)]
        _step(state, "Summarizing the course…")
        digest = "\n".join(f"- {o.get('key')}: {o.get('title')}: {o.get('summary')}" for o in outline)
        raw = _object(_call(f"Here is an outline of my notes for {course['code']} {course['title']}, one line per section. "
                            'Reply with ONLY a JSON object: {"summary": "3-5 sentences on what the notes cover", '
                            '"objectives": ["learning objectives the notes state"], "competencies": [{"text": "...", "keys": '
                            '["section keys"]}] (4 to 10), "thin_spots": [{"key": "...", "why": "..."}]}\n\n' + digest))
        raw["outline"] = outline
    profile = _clean_profile(raw, rows)
    save_profile(conn, course["id"], profile, used_hash)
    return profile


def save_profile(conn, course_id: int, profile: dict, used_hash: str) -> None:
    now = clock.now().isoformat()
    state = "fresh" if chunks.notes_hash(conn, course_id) == used_hash else "stale"  # edited while it was running
    with conn:
        conn.execute("INSERT INTO course_brain(course_id, notes_hash, profile_json, status, analyzed_at, updated_at) "
                     "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(course_id) DO UPDATE SET notes_hash = excluded.notes_hash, "
                     "profile_json = excluded.profile_json, status = excluded.status, analyzed_at = excluded.analyzed_at, "
                     "updated_at = excluded.updated_at", (course_id, used_hash, json.dumps(profile), state, now, now))


def get_profile(conn, course_id: int) -> dict | None:
    """{"profile", "status" ('fresh' | 'stale'), "analyzed_at"} or None if the notes were never analyzed."""
    r = conn.execute("SELECT * FROM course_brain WHERE course_id = ? AND status != 'none'", (course_id,)).fetchone()
    if not r:
        return None
    return {"profile": json.loads(r["profile_json"]), "status": r["status"], "analyzed_at": r["analyzed_at"]}


def coverage(conn, course_id: int) -> dict:
    """Which usable sections have cards or questions made from them, and how many items have gone out of date.
    `uncovered` / `stale` are section keys in reading order; `missing` counts items whose section is gone. Sections that only
    list learning objectives are left out of the count (`objectives`): there's nothing in them to make cards from."""
    usable = {r["key"]: r for r in sections(conn, course_id)}
    every = chunks.outline(conn, course_id)
    objectives = [r["key"] for r in every if r["key"] in usable and not r["cards"] and not r["questions"]
                  and is_objectives_only(usable[r["key"]]["text"])]
    rows = [r for r in every if r["key"] in usable and r["key"] not in objectives]
    fresh = chunks.freshness(conn, course_id)
    return {"sections": len(rows), "covered": sum(1 for r in rows if r["cards"] or r["questions"]), "objectives": objectives,
            "uncovered": [r["key"] for r in rows if not r["cards"] and not r["questions"]],
            "stale": [r["key"] for r in every if r["stale_cards"] or r["stale_questions"]],  # even a section now too short to build from
            "stale_cards": fresh["cards"]["stale"], "stale_questions": fresh["questions"]["stale"],
            "missing_cards": fresh["cards"]["missing"], "missing_questions": fresh["questions"]["missing"]}


def info(conn, course) -> dict:
    """What a course page needs: how many sections, and the profile state."""
    rows = sections(conn, course["id"])
    p = get_profile(conn, course["id"])
    return {"sections": len(rows), "chars": sum(r["chars"] for r in rows), "profile": p["profile"] if p else None,
            "state": p["status"] if p else "none", "analyzed_at": p["analyzed_at"] if p else None}


def prompt_block(conn, course_id: int, limit: int = 3500) -> str:
    """What the notes cover, for the agent's system prompt in a course chat. '' if never analyzed."""
    p = get_profile(conn, course_id)
    if not p:
        return ""
    prof = p["profile"]
    counts = {o["key"]: o for o in chunks.outline(conn, course_id)}
    lines = []
    for o in prof["outline"]:
        c = counts.get(o["key"], {})
        lines.append(f"- {o['key']} · {o['title']}: {o['summary']} ({c.get('cards', 0)} cards, {c.get('questions', 0)} questions)")
    head = (f"\n\nWhat this course's notes cover (analyzed {p['analyzed_at'][:10]}"
            f"{'; the notes have changed since' if p['status'] == 'stale' else ''}). Use it to know where things are, and "
            f"read a section before relying on it:\n{prof['summary']}\n")
    out = head + "\n".join(lines)
    return out if len(out) <= limit else out[:limit].rsplit("\n", 1)[0] + "\n- …"


# ---------------------------------------------------------------- the draft

def _targets(r, depth: float, topics: bool) -> tuple[int, int]:
    """(cards, questions) to ask for from one section."""
    if topics:
        return min(len(topic_terms(r["text"])) + 2, 40), max(2, round(3 * depth))
    n = max(2, min(14, round(r["chars"] / 350 * depth)))
    return n, max(1, min(4, round(r["chars"] / 900 * depth)))


def _gen_prompt(course, rows, depth: float) -> tuple[str, str]:
    notes, topics = [], False
    for r in rows:
        t = is_topic_list(r["text"])
        topics |= t
        nc, nq = _targets(r, depth, t)
        mode = ("a list of topics to learn: write at least one card per topic, answering from accurate standard knowledge "
                'where the notes give no answer, and set "source" to "general" for those') if t else \
               ('use ONLY what this section says; "source" must be "notes"; write fewer items if it is thin; write nothing '
                'for a section that only lists learning objectives, links or headings')
        notes.append(f'- {r["key"]}: about {nc} cards and {nq} questions; {mode}')
    prompt = (f"Write flashcards and practice questions for {course['code']} {course['title']} from the sections below, in the "
              "style of a WGU objective assessment. One fact per card, short answers; favour definitions, ports, commands, "
              "numbers, comparisons. Questions: single-answer or select-all-that-apply, 4 choices, the correct choice indexes "
              "(0-based), a one-to-two sentence explanation. Targets per section:\n" + "\n".join(notes) +
              '\nReply with ONLY a JSON object: {"cards": [{"section": "<key>", "front": "...", "back": "...", "source": '
              '"notes"}], "questions": [{"section": "<key>", "kind": "mc" or "multi", "prompt": "...", "choices": ["..."], '
              '"correct": [0], "explanation": "...", "source": "notes"}]}\n\n' + _block(rows))
    return prompt, SYSTEM_TOPICS if topics else SYSTEM


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", str(text).lower()).strip()


def _generate(course, rows, depth, have_fronts, have_prompts, warnings) -> tuple[list, list]:
    prompt, system = _gen_prompt(course, rows, depth)
    raw = _object(_call(prompt, system))
    by_key = {r["key"]: r for r in rows}
    topic = {r["key"]: is_topic_list(r["text"]) for r in rows}
    out_cards, out_qs = [], []
    for c in raw.get("cards") or []:
        if not isinstance(c, dict) or c.get("section") not in by_key:
            continue
        front, back = _str(c.get("front"), 1000), _str(c.get("back"), 2000)
        general = str(c.get("source", "")).strip().lower() != "notes"
        if not front or not back or (general and not topic[c["section"]]) or _norm(front) in have_fronts:
            continue
        have_fronts.add(_norm(front))
        out_cards.append({"section": c["section"], "label": chunks.where(by_key[c["section"]]), "front": front,
                          "back": back, "general": general})
    for q in raw.get("questions") or []:
        if not isinstance(q, dict) or q.get("section") not in by_key:
            continue
        clean = ai_actions.clean_question(q, fill=True)
        if not clean or _norm(clean["prompt"]) in have_prompts or (clean["general"] and not topic[q["section"]]):
            continue
        have_prompts.add(_norm(clean["prompt"]))
        out_qs.append({"section": q["section"], "label": chunks.where(by_key[q["section"]]), "kind": clean["kind"],
                       "prompt": clean["prompt"], "choices_text": clean["choices_text"], "explanation": clean["explanation"],
                       "general": clean["general"]})
    return out_cards, out_qs


def plan_tasks(course, profile: dict, today=None) -> list[dict]:
    """A study plan from the outline, no model needed: read each core section, then clear the cards, then a quiz. Dated
    evenly up to the exam date (or the course's target / due date) when there is one."""
    today = today or clock.today()
    end = next((d for d in (clock.parse_date(course[k]) for k in ("exam_date", "target", "due")) if d and d >= today), None)
    secs = sorted(profile["outline"], key=lambda o: -o["importance"])[:MAX_TASKS - 2]
    secs = [o for o in profile["outline"] if o in secs]
    span = max((end - today).days, 0) if end else 0

    def when(frac):
        return (today + timedelta(days=round(span * frac))).isoformat() if end else None

    thin = {t["key"] for t in profile.get("thin_spots", [])}
    tasks = []
    for i, o in enumerate(secs):
        tasks.append({"title": f"Review notes: {o['title']}"[:200], "kind": "read", "done_rule": "manual", "key": o["key"],
                      "due_on": when(0.7 * (i + 1) / len(secs)), "est_minutes": 20, "priority": 1 if o["importance"] == 3 else 2,
                      "note": ("Notes look thin here: add detail." if o["key"] in thin else "")})
    tasks.append({"title": f"Clear {course['code']} flashcards", "kind": "review_cards", "done_rule": "cards_reviewed",
                  "due_on": when(0.85), "est_minutes": 25, "priority": 2, "note": "", "key": ""})
    tasks.append({"title": f"Take a {course['code']} practice quiz", "kind": "quiz", "done_rule": "quiz_finished",
                  "due_on": when(0.95), "est_minutes": 30, "priority": 2, "note": "", "key": ""})
    return tasks


def overview_text(profile: dict) -> str:
    lines = [profile["summary"], ""]
    if profile["objectives"]:
        lines += ["**Learning objectives**", ""] + [f"- {o}" for o in profile["objectives"]] + [""]
    lines += ["**Sections**", ""]
    lines += [f"- **{o['title']}**: {o['summary']}" for o in profile["outline"] if o["summary"]]
    return "\n".join(lines).strip()


def build(conn, course, depth: str = "normal", state: dict | None = None, scope: str = "all") -> int:
    """Analyze if needed, write the cards and questions, and save the draft. Returns its id.
    scope 'uncovered' writes only for sections with no cards or questions yet, and leaves out the competencies, study
    plan and overview (those came with the first build); it reuses any analysis, even an old one, instead of a new call."""
    factor = DEPTHS.get(depth, 1.0)
    got = get_profile(conn, course["id"])
    if scope == "uncovered":
        want = set(coverage(conn, course["id"])["uncovered"])
        rows = [r for r in sections(conn, course["id"]) if r["key"] in want]
        if not rows:
            raise BrainError("Every section already has cards or questions. Nothing to fill in.")
        profile = got["profile"] if got else None
        _step(state, f"Writing for {len(rows)} section{'s' if len(rows) != 1 else ''} with no cards or questions yet…")
    else:
        if got and got["status"] == "fresh":
            profile = got["profile"]
            _step(state, "Using the analysis of your notes from earlier.")
        else:
            profile = analyze(conn, course, state)
        rows = sections(conn, course["id"])
    if not rows:
        raise BrainError("There's nothing to build from yet: write some notes first.")
    used_hash = chunks.notes_hash(conn, course["id"])
    have_fronts = {_norm(r[0]) for r in conn.execute("SELECT front FROM cards WHERE course_id = ?", (course["id"],))}
    have_prompts = {_norm(r[0]) for r in conn.execute("SELECT prompt FROM questions WHERE course_id = ?", (course["id"],))}
    all_cards, all_qs, warnings = [], [], []
    parts = _batches(rows, BATCH_CHARS)
    for i, part in enumerate(parts, 1):
        _step(state, f"Writing cards and questions: part {i} of {len(parts)}…", i - 1, len(parts))
        try:
            c, q = _generate(course, part, factor, have_fronts, have_prompts, warnings)
            all_cards += c
            all_qs += q
        except ai.AIError as e:
            names = ", ".join(chunks.where(r) for r in part[:3]) + ("…" if len(part) > 3 else "")
            warnings.append(f"Part {i} ({names}) failed: {e}")
            log.warning("brain build %s part %d failed: %s", course["code"], i, e)
            if "usage limit" in str(e) or "rejected the API key" in str(e):
                break  # every later call would fail the same way
    if not all_cards and not all_qs:
        if scope == "uncovered" and not warnings:
            raise BrainError("The model found nothing in those sections to make cards or questions from (they may only "
                             "list links or headings). Add detail to them and try again.")
        raise BrainError("Nothing could be drafted. " + (warnings[0] if warnings else "The model returned no usable items."))
    if scope == "uncovered":
        payload = {"summary": f"Cards and questions for {len(rows)} section{'s' if len(rows) != 1 else ''} that had none.",
                   "scope": scope, "depth": depth, "competencies": [], "cards": all_cards, "questions": all_qs, "tasks": [],
                   "overview": "", "hashes": {r["key"]: r["hash"] for r in rows}, "warnings": warnings}
    else:
        existing = {r["text"].lower() for r in competencies.list_for(conn, course["id"])}
        payload = {"summary": profile["summary"], "scope": "all", "depth": depth,
                   "competencies": [c for c in profile["competencies"] if c["text"].lower() not in existing],
                   "cards": all_cards, "questions": all_qs, "tasks": plan_tasks(course, profile),
                   "overview": overview_text(profile),
                   "hashes": {r["key"]: r["hash"] for r in rows}, "warnings": warnings}
    return _save_draft(conn, course["id"], payload, used_hash)


def _save_draft(conn, course_id: int, payload: dict, used_hash: str) -> int:
    """Store a draft, replacing any older one still waiting for review."""
    now = clock.now().isoformat()
    with conn:
        conn.execute("UPDATE brain_drafts SET status = 'dismissed' WHERE course_id = ? AND status = 'pending'", (course_id,))
        cur = conn.execute("INSERT INTO brain_drafts(course_id, payload_json, notes_hash, created_at) VALUES (?, ?, ?, ?)",
                           (course_id, json.dumps(payload), used_hash, now))
    return cur.lastrowid


# ---------------------------------------------------------------- refresh: items made from a section that has since changed

REFRESH_ITEMS = 40   # existing items checked per call
ACTIONS = ("keep", "update", "remove")


def stale_items(conn, course_id: int) -> dict:
    """{section key: {"section": row, "cards": [...], "questions": [...]}} for items whose section's text has changed since
    they were made, sections in reading order. Items whose section is gone aren't here: there's nothing to compare them with."""
    out = {}
    for table in ("cards", "questions"):
        for t in conn.execute(f"SELECT t.* FROM {table} t JOIN note_chunks n ON n.course_id = t.course_id AND n.key = t.source_key "
                              f"WHERE t.course_id = ? AND n.hash IS NOT t.source_hash ORDER BY t.id", (course_id,)):
            out.setdefault(t["source_key"], {"cards": [], "questions": []})[table].append(t)
    order = {r["key"]: i for i, r in enumerate(chunks.outline(conn, course_id, chunks.FILES))}
    for key in out:
        out[key]["section"] = chunks.get(conn, course_id, key)
    return dict(sorted(out.items(), key=lambda kv: order.get(kv[0], len(order))))


def _item_line(table, t) -> str:
    if table == "cards":
        return f'card {t["id"]}: front: {t["front"]} | back: {t["back"]}'
    choices = json.loads(t["choices_json"])
    answer = json.loads(t["answer_json"])
    if t["kind"] == "short":
        return f'question {t["id"]} (short answer): {t["prompt"]} | model answer: {answer}'
    marked = " ".join(f'{"*" if i in answer else ""}{chr(65 + i)}) {c}' for i, c in enumerate(choices))
    return f'question {t["id"]} ({t["kind"]}): {t["prompt"]} | choices (* = correct): {marked} | explanation: {t["explanation"]}'


def _refresh_prompt(course, groups) -> str:
    blocks, topics = [], False
    for key, g in groups:
        items = [_item_line("cards", t) for t in g["cards"]] + [_item_line("questions", t) for t in g["questions"]]
        kind = ' kind="topic list"' if is_topic_list(g["section"]["text"]) else ""
        topics |= bool(kind)
        blocks.append(f'<section key="{key}" title="{chunks.where(g["section"])}"{kind}>\n{g["section"]["text"]}\n</section>\n'
                      f'<items section="{key}">\n' + "\n".join(items) + "\n</items>")
    # a topic list (e.g. a pre-assessment list) was answered from general knowledge on purpose; judged against its own
    # text, nearly every item would look unsupported, so it's judged on whether its topic is still listed
    topic_rule = ('A section marked kind="topic list" is a list of topics to learn, and its items answer those topics from '
                  "general knowledge on purpose: keep such an item while its topic is still in the list, update it only if the "
                  "list now names or scopes the topic differently, and remove it only if its topic was taken out of the list.\n"
                  if topics else "")
    return (f"My notes for {course['code']} {course['title']} have changed. Each section below is its CURRENT text, followed by "
            "flashcards and practice questions I made from an earlier version of it. Decide for every item:\n"
            '"keep": still correct and still supported by the section as it is now;\n'
            '"update": the section still covers it but a detail changed or the item is now wrong: rewrite it from the current text;\n'
            '"remove": the section no longer covers it.\n' + topic_rule +
            'Reply with ONLY a JSON object: {"cards": [{"id": <id>, "action": "keep" | "update" | "remove", "front": "...", '
            '"back": "...", "why": "a few words"}], "questions": [{"id": <id>, "action": "...", "kind": "mc" or "multi", '
            '"prompt": "...", "choices": ["..."], "correct": [0], "explanation": "...", "why": "..."}]}\n'
            'Give the new text (front/back, or the question fields) only for "update". Except in a topic list, use only what the current text says.\n\n'
            + "\n\n".join(blocks))


def _refresh_batches(groups) -> list[list]:
    """Sections with their stale items, packed so one call holds about BATCH_CHARS of notes and at most REFRESH_ITEMS items.
    A section with more items than that is split across calls, its text sent with each part."""
    flat = []
    for key, g in groups.items():
        items = [("cards", t) for t in g["cards"]] + [("questions", t) for t in g["questions"]]
        for i in range(0, len(items), REFRESH_ITEMS):
            part = items[i:i + REFRESH_ITEMS]
            flat.append((key, {"section": g["section"], "cards": [t for k, t in part if k == "cards"],
                               "questions": [t for k, t in part if k == "questions"]}))
    out, cur, size, n = [], [], 0, 0
    for key, g in flat:
        k = len(g["cards"]) + len(g["questions"])
        if cur and (size + g["section"]["chars"] > BATCH_CHARS or n + k > REFRESH_ITEMS):
            out.append(cur)
            cur, size, n = [], 0, 0
        cur.append((key, g))
        size += g["section"]["chars"]
        n += k
    if cur:
        out.append(cur)
    return out


def _check(course, batch) -> tuple[list, list]:
    """One call: the model's verdict on each item of this batch, as draft entries. Items it skipped come back as 'check'."""
    topics = any(is_topic_list(g["section"]["text"]) for _, g in batch)
    raw = _object(_call(_refresh_prompt(course, batch), SYSTEM_TOPICS if topics else SYSTEM))
    said = {"cards": {}, "questions": {}}
    for table in said:
        for x in raw.get(table) or []:
            if isinstance(x, dict) and str(x.get("id", "")).isdigit():
                said[table].setdefault(int(x["id"]), x)
    out_cards, out_qs = [], []
    for key, g in batch:
        label, now = chunks.where(g["section"]), g["section"]["hash"]
        for t in g["cards"]:
            x = said["cards"].get(t["id"], {})
            act = x.get("action") if x.get("action") in ACTIONS else "check"
            front, back = _str(x.get("front"), 1000), _str(x.get("back"), 2000)
            if act == "update" and not (front and back):
                act = "check"
            out_cards.append({"id": t["id"], "section": key, "label": label, "now_hash": now, "action": act,
                              "why": _str(x.get("why"), 200) if act != "check" else "", "old_front": t["front"],
                              "old_back": t["back"], "front": front if act == "update" else t["front"],
                              "back": back if act == "update" else t["back"]})
        for t in g["questions"]:
            x = said["questions"].get(t["id"], {})
            act = x.get("action") if x.get("action") in ACTIONS else "check"
            clean = ai_actions.clean_question(x) if act == "update" and t["kind"] != "short" else None
            if act == "update" and not clean:
                act = "check"
            out_qs.append({"id": t["id"], "section": key, "label": label, "now_hash": now, "action": act,
                           "why": _str(x.get("why"), 200) if act != "check" else "", "short": t["kind"] == "short",
                           "old_prompt": t["prompt"], "old_choices_text": quizzes.choices_text(t),
                           "kind": clean["kind"] if clean else t["kind"], "prompt": clean["prompt"] if clean else t["prompt"],
                           "choices_text": clean["choices_text"] if clean else quizzes.choices_text(t),
                           "explanation": clean["explanation"] if clean else t["explanation"]})
    return out_cards, out_qs


def refresh(conn, course, state: dict | None = None) -> int:
    """Check every out-of-date card and question against its section as it is now, and save the verdicts as a draft:
    keep (just mark it current), update (new text, review history kept) or remove. Returns the draft id."""
    groups = stale_items(conn, course["id"])
    if not groups:
        raise BrainError("Nothing is out of date: every card and question made from your notes matches them.")
    n = sum(len(g["cards"]) + len(g["questions"]) for g in groups.values())
    used_hash = chunks.notes_hash(conn, course["id"])
    batches = _refresh_batches(groups)
    up_cards, up_qs, warnings = [], [], []
    for i, batch in enumerate(batches, 1):
        _step(state, f"Checking {n} out-of-date items against your notes: part {i} of {len(batches)}…", i - 1, len(batches))
        try:
            c, q = _check(course, batch)
            up_cards += c
            up_qs += q
        except ai.AIError as e:
            names = ", ".join(chunks.where(g["section"]) for _, g in batch[:3]) + ("…" if len(batch) > 3 else "")
            warnings.append(f"Part {i} ({names}) failed: {e}")
            log.warning("brain refresh %s part %d failed: %s", course["code"], i, e)
            if "usage limit" in str(e) or "rejected the API key" in str(e):
                break
    if not up_cards and not up_qs:
        raise BrainError("Nothing could be checked. " + (warnings[0] if warnings else "The model returned nothing usable."))
    payload = {"summary": f"{len(up_cards) + len(up_qs)} out-of-date item{'s' if len(up_cards) + len(up_qs) != 1 else ''} "
                          f"checked against your notes as they are now.",
               "scope": "refresh", "competencies": [], "cards": [], "questions": [], "tasks": [], "overview": "",
               "hashes": {k: g["section"]["hash"] for k, g in groups.items()}, "warnings": warnings,
               "updates": {"cards": up_cards, "questions": up_qs}}
    return _save_draft(conn, course["id"], payload, used_hash)


def pending_draft(conn, course_id: int):
    return conn.execute("SELECT * FROM brain_drafts WHERE course_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
                        (course_id,)).fetchone()


# ---------------------------------------------------------------- background job (one at a time)

KINDS = ("analyze", "build", "fill", "refresh")
_job: dict = {}
_job_lock = threading.Lock()


def _step(state, text, done=None, of=None):
    if state is not None:
        state["step"] = text
        state["steps"].append(text)
        if of:
            state["done_parts"], state["parts"] = done, of


def start(course, kind: str = "build", depth: str = "normal") -> dict:
    """Begin a job in the background: kind = analyze | build | fill (build for uncovered sections only) | refresh.
    Raises BrainError if a job is already running."""
    global _job
    if kind not in KINDS:
        raise ValueError(kind)
    if not ai.enabled():
        raise BrainError("AI features are off. Set AI_PROVIDER, AI_MODEL and the matching API key in .env.")
    with _job_lock:
        if _job and not _job["done"]:
            raise BrainError(f"Already working on {_job['code']} ({_job['step']}). The free tier handles one job at a time.")
        _job = {"code": course["code"], "kind": kind, "depth": depth, "started": time.time(), "step": "Starting…",
                "steps": [], "done": False, "error": None, "draft_id": None, "done_parts": 0, "parts": 0, "seen": False}
        state = _job
    threading.Thread(target=_run, args=(state, course["code"], kind, depth), daemon=True, name=f"brain-{course['code']}").start()
    return state


def _run(state, code, kind, depth):
    conn = db.connect()
    try:
        course = catalog.get_course(conn, code)
        if kind == "analyze":
            analyze(conn, course, state)
        elif kind == "refresh":
            state["draft_id"] = refresh(conn, course, state)
        else:
            state["draft_id"] = build(conn, course, depth, state, scope="uncovered" if kind == "fill" else "all")
    except ai.AIError as e:
        state["error"] = str(e)
    except Exception:  # noqa: BLE001 — a bug must end the job with a message, never hang the page
        log.exception("brain job for %s failed", code)
        state["error"] = "Something went wrong inside studyvault. Nothing was saved; check the app log."
    finally:
        state["done"] = True
        conn.close()


def progress(code: str) -> dict | None:
    """The current or most recent job for this course, as plain data for the page (None if there isn't one)."""
    if not _job or _job["code"] != code:
        return None
    return {k: _job[k] for k in ("kind", "step", "done", "error", "draft_id", "done_parts", "parts")} | {
        "seconds": int(time.time() - _job["started"]), "steps": list(_job["steps"][-5:])}


def busy() -> str | None:
    """Course code of the running job, if any."""
    return _job["code"] if _job and not _job["done"] else None


def clear_finished(code: str) -> None:
    global _job
    with _job_lock:
        if _job and _job["code"] == code and _job["done"]:
            _job = {}


# ---------------------------------------------------------------- apply

class ApplyError(Exception):
    pass


def apply_draft(conn, course, draft, form) -> str:
    """Create what the student ticked. `form` has the ticked indexes (`comp`, `card`, `question`, `task`) and their edits."""
    if draft["status"] != "pending":
        raise ApplyError("This draft was already handled.")
    p = json.loads(draft["payload_json"])
    ticked = lambda name, n: [int(i) for i in form.getlist(name) if str(i).isdigit() and int(i) < n]  # noqa: E731
    made = {"competencies": 0, "cards": 0, "questions": 0, "tasks": 0, "overview": 0, "updated": 0, "confirmed": 0, "removed": 0}

    picked = ticked("comp", len(p["competencies"]))
    if picked:
        have = [r["text"] for r in competencies.list_for(conn, course["id"])]
        new = [t for t in (_str(form.get(f"comptext_{i}") or p["competencies"][i]["text"], 300) for i in picked)
               if t.lower() not in {h.lower() for h in have}]
        competencies.import_list(conn, course, have + new)
        made["competencies"] = len(new)
    # section key → competency id, from the profile's own competency → sections links
    comp_ids = {r["text"].lower(): r["id"] for r in competencies.list_for(conn, course["id"])}
    prof = (get_profile(conn, course["id"]) or {}).get("profile") or {}
    by_key = {}
    for c in prof.get("competencies", []):
        cid = comp_ids.get(c["text"].lower())
        for k in c["keys"]:
            if cid:
                by_key.setdefault(k, cid)

    def ref(key):
        sec = chunks.get(conn, course["id"], key)
        return {"key": sec["key"], "hash": p["hashes"].get(key) or sec["hash"]} if sec else None

    have_fronts = {_norm(r[0]) for r in conn.execute("SELECT front FROM cards WHERE course_id = ?", (course["id"],))}
    for i in ticked("card", len(p["cards"])):
        c = p["cards"][i]
        front, back = _str(form.get(f"front_{i}") or c["front"], 1000), _str(form.get(f"back_{i}") or c["back"], 2000)
        if front and back and _norm(front) not in have_fronts:
            have_fronts.add(_norm(front))
            cards.add(conn, course["id"], front, back, competency_id=by_key.get(c["section"]), source="ai-accepted",
                      source_ref=ref(c["section"]))
            made["cards"] += 1
    for i in ticked("question", len(p["questions"])):
        q = p["questions"][i]
        try:
            quizzes.save(conn, course["id"], {"kind": form.get(f"kind_{i}") or q["kind"], "prompt": form.get(f"prompt_{i}") or q["prompt"],
                                              "choices": form.get(f"choices_{i}") or q["choices_text"],
                                              "explanation": form.get(f"explanation_{i}") or q["explanation"],
                                              "competency_id": str(by_key.get(q["section"]) or "")},
                         source="ai-accepted", source_ref=ref(q["section"]))
            made["questions"] += 1
        except quizzes.QuestionError:
            continue  # an edit broke it (no * left); keep the rest
    try:
        with conn:
            for i in ticked("task", len(p["tasks"])):
                t = p["tasks"][i]
                study_tasks.insert(conn, form.get(f"title_{i}") or t["title"], course_id=course["id"],
                                   competency_id=by_key.get(t["key"]), note=t["note"], kind=t["kind"],
                                   due_on=form.get(f"due_{i}") or t["due_on"], est_minutes=t["est_minutes"],
                                   priority=t["priority"], done_rule=t["done_rule"], source=f"brain:{draft['id']}")
                made["tasks"] += 1
    except study_tasks.TaskError as e:
        raise ApplyError(str(e))
    if form.get("overview"):
        text = str(form.get("overview_text") or p["overview"]).strip()
        if text:
            notes_fs.append_note(conn, course, "overview", f"## From my notes · {clock.today().isoformat()}\n\n{text}\n")
            made["overview"] = 1
    _apply_updates(conn, course, p.get("updates") or {}, form, ticked, made)
    if not any(made.values()):
        raise ApplyError("Nothing was ticked. Tick what you want, or press Dismiss.")
    words = {"competencies": ("competency", "competencies"), "cards": ("card", "cards"), "questions": ("question", "questions"),
             "tasks": ("task", "tasks")}
    parts = [f"{n} {words[name][n != 1]}" for name, n in made.items() if n and name in words]
    if made["overview"]:
        parts.append("an overview summary")
    result = " ".join(s for s in (
        ("Added " + ", ".join(parts) + ".") if parts else "",
        f"Updated {made['updated']}." if made["updated"] else "",
        f"Marked {made['confirmed']} still correct." if made["confirmed"] else "",
        f"Deleted {made['removed']}." if made["removed"] else "") if s)
    with conn:
        conn.execute("UPDATE brain_drafts SET status = 'applied', result = ? WHERE id = ?", (result, draft["id"]))
    return result


def _apply_updates(conn, course, updates, form, ticked, made) -> None:
    """A refresh draft: for each ticked item, `act_card_{i}` / `act_q_{i}` says keep (mark it current), update (the edited
    text, review history kept) or remove. The item must still exist, in this course, from the same section. It is marked
    with the hash the check was made against, so if the section changed again since, it still shows as out of date."""
    for i in ticked("upd_card", len(updates.get("cards", []))):
        u = updates["cards"][i]
        row = conn.execute("SELECT id FROM cards WHERE id = ? AND course_id = ? AND source_key = ?",
                           (u["id"], course["id"], u["section"])).fetchone()
        act = form.get(f"act_card_{i}") or u["action"]
        if not row or act not in ACTIONS:
            continue
        with conn:
            if act == "remove":
                conn.execute("DELETE FROM cards WHERE id = ?", (u["id"],))
            elif act == "update":
                front = _str(form.get(f"ufront_{i}") or u["front"], 1000)
                back = _str(form.get(f"uback_{i}") or u["back"], 2000)
                if not front or not back:
                    continue
                conn.execute("UPDATE cards SET front = ?, back = ?, source_hash = ? WHERE id = ?", (front, back, u["now_hash"], u["id"]))
            else:
                conn.execute("UPDATE cards SET source_hash = ? WHERE id = ?", (u["now_hash"], u["id"]))
        made[{"remove": "removed", "update": "updated", "keep": "confirmed"}[act]] += 1
    for i in ticked("upd_q", len(updates.get("questions", []))):
        u = updates["questions"][i]
        row = conn.execute("SELECT * FROM questions WHERE id = ? AND course_id = ? AND source_key = ?",
                           (u["id"], course["id"], u["section"])).fetchone()
        act = form.get(f"act_q_{i}") or u["action"]
        if not row or act not in ACTIONS or (act == "update" and row["kind"] == "short"):
            continue
        if act == "remove":
            with conn:
                conn.execute("DELETE FROM questions WHERE id = ?", (u["id"],))
        elif act == "update":
            try:
                quizzes.save(conn, course["id"], {"kind": form.get(f"ukind_{i}") or u["kind"],
                                                  "prompt": form.get(f"uprompt_{i}") or u["prompt"],
                                                  "choices": form.get(f"uchoices_{i}") or u["choices_text"],
                                                  "explanation": form.get(f"uexpl_{i}") or u["explanation"],
                                                  "competency_id": str(row["competency_id"] or "")}, question_id=u["id"])
            except quizzes.QuestionError:
                continue  # an edit broke it; leave the question as it was
            with conn:
                conn.execute("UPDATE questions SET source_hash = ? WHERE id = ?", (u["now_hash"], u["id"]))
        else:
            with conn:
                conn.execute("UPDATE questions SET source_hash = ? WHERE id = ?", (u["now_hash"], u["id"]))
        made[{"remove": "removed", "update": "updated", "keep": "confirmed"}[act]] += 1


def dismiss(conn, draft) -> None:
    if draft["status"] == "pending":
        with conn:
            conn.execute("UPDATE brain_drafts SET status = 'dismissed' WHERE id = ?", (draft["id"],))
