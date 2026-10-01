"""The four AI actions (spec F8). Inputs are only the notes text the user selected; outputs are drafts, never saved here."""
import json
import re

from . import ai
from .config import settings

MAX_INPUT_CHARS = 12000
MAX_CARDS = 60
MAX_QUESTIONS = 20
COUNTS = (10, 20, 30, 60)  # the "How many" choices; 0 = as many as the notes need

SYSTEM = ("You help a WGU student study from their own notes for the B.S. Cloud & Network Engineering (AWS) degree. "
          "Use only what the notes support; if the notes are thin or wrong, say so rather than inventing facts. "
          "Never write or draft performance-assessment submissions.")
# "Fill in" mode: the notes are a list of topics to learn (say, from a pre-assessment), so answers may come from the
# model's own knowledge. Those items are marked source "general" and flagged in the draft for the student to check.
SYSTEM_FILL = ("You help a WGU student study for the B.S. Cloud & Network Engineering (AWS) degree. Their notes are "
               "often just a list of topics and terms they need to learn or refresh. Where the notes say something, "
               "use it; elsewhere answer from accurate, standard knowledge at the level of the course, and correct "
               "the notes if they are wrong. Never write or draft performance-assessment submissions.")
FILL_RULES = ("Treat every bullet, line or term in the notes as a topic I need to learn. Cover EVERY topic: at least "
              "one item each, and never skip or merge topics. Where a topic has more than one thing worth knowing "
              "(a definition and a use, two standards to compare), give it more than one. "
              'For each item set "source" to "notes" if my notes state the answer, otherwise "general".')


class ActionError(ai.AIError):
    pass


def _clip(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise ActionError("Pick or paste some notes first.")
    if len(text) > MAX_INPUT_CHARS:
        raise ActionError(f"That's {len(text):,} characters; trim it to {MAX_INPUT_CHARS:,} or less (one section at a time works best).")
    return text


# One JSON object with no nested objects (strings may hold braces). Used to rescue a reply cut off mid-list.
_OBJECT = re.compile(r'\{(?:[^{}"]|"(?:\\.|[^"\\])*")*\}')


def _json_items(text: str) -> tuple[list, bool]:
    """Pull the JSON array out of a model reply (tolerates code fences and a sentence around it). A reply cut off at
    the output limit keeps its complete items. Returns (items, cut_off)."""
    m = re.search(r"\[.*\]", text, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
        except ValueError:
            data = None
        if isinstance(data, list):
            return data, False
    start = text.find("[")
    if start < 0:
        raise ActionError("The model didn't return a list I could read. Try again.")
    items = []
    for obj in _OBJECT.finditer(text, start):
        try:
            items.append(json.loads(obj.group(0)))
        except ValueError:
            continue
    if not items:
        raise ActionError("The model's list wasn't valid JSON. Try again.")
    return items, True


def _bulk(prompt: str, system: str) -> str:
    return ai.complete(prompt, max_tokens=max(settings.ai_max_output_tokens, ai.BULK_MAX_TOKENS), system=system,
                       timeout=ai.BULK_TIMEOUT_S)


def _for(course: str) -> str:
    return f" for my course {course}" if course else ""


def _cut_note(cut_off: bool, n: int, what: str) -> str | None:
    if not cut_off:
        return None
    return (f"The model's reply was cut off, so these {n} {what} may not cover everything you sent. "
            "Accept these, then run it again on the part of your notes that's missing.")


def flashcards(notes: str, n: int = 0, fill: bool = False, course: str = "") -> tuple[list[dict], str | None]:
    """Draft cards from the notes: up to `n`, or as many as the notes need (0). `fill` = the notes are a list of topics,
    answer them from general knowledge where the notes don't. Returns (cards, a note for the user or None)."""
    notes = _clip(notes)
    limit = min(n or MAX_CARDS, MAX_CARDS)
    how_many = f"up to {limit} flashcards" if n else f"as many flashcards as the notes need (at most {limit})"
    if fill:
        prompt = (f"Write {how_many}{_for(course)} from the notes below. {FILL_RULES}\n"
                  "Favour things worth memorising: ports, commands, definitions, AWS services, standards, numbers. "
                  "Answers short.\n"
                  'Reply with only a JSON array like [{"front": "...", "back": "...", "source": "notes" or "general"}].')
    else:
        prompt = (f"Write {how_many}{_for(course)} from the notes below. Favour things worth memorising: ports, "
                  "commands, definitions, AWS services, standards, numbers. One fact per card; answers short.\n"
                  'Reply with only a JSON array like [{"front": "...", "back": "..."}].')
    items, cut_off = _json_items(_bulk(f"{prompt}\n\n<notes>\n{notes}\n</notes>", SYSTEM_FILL if fill else SYSTEM))
    out = []
    for item in items:
        if isinstance(item, dict) and str(item.get("front", "")).strip() and str(item.get("back", "")).strip():
            card = {"front": str(item["front"]).strip(), "back": str(item["back"]).strip()}
            if fill:
                card["general"] = str(item.get("source", "")).strip().lower() != "notes"
            out.append(card)
    if not out:
        raise ActionError("The model returned no usable cards.")
    out = out[:limit]
    return out, _cut_note(cut_off, len(out), "cards")


def questions(notes: str, n: int = 0, fill: bool = False, course: str = "") -> tuple[list[dict], str | None]:
    """Draft practice questions: up to `n` (0 = 10), at most MAX_QUESTIONS. `fill` as for flashcards()."""
    notes = _clip(notes)
    limit = min(n or 10, MAX_QUESTIONS)
    grounding = f". Spread the questions across the topics. {FILL_RULES}\n" if fill else " grounded in the notes.\n"
    source = ', "source": "notes" or "general"' if fill else ""
    prompt = (f"Write up to {limit} practice exam questions{_for(course)} from the notes below, in the style of a WGU "
              "objective assessment. Mix single-answer and select-all-that-apply. Each needs 4 choices, the correct "
              f"choice indexes (0-based), and a one-to-two sentence explanation{grounding}"
              'Reply with only a JSON array like [{"kind": "mc" or "multi", "prompt": "...", "choices": ["..."], '
              f'"correct": [0], "explanation": "..."{source}}}].')
    items, cut_off = _json_items(_bulk(f"{prompt}\n\n<notes>\n{notes}\n</notes>", SYSTEM_FILL if fill else SYSTEM))
    out = []
    for q in items:
        if not isinstance(q, dict):
            continue
        choices = [str(c).strip() for c in q.get("choices") or [] if str(c).strip()]
        try:
            correct = sorted({int(i) for i in q.get("correct") or []})
        except (TypeError, ValueError):
            continue
        prompt = str(q.get("prompt", "")).strip()
        if not prompt or len(choices) < 2 or not correct or any(not 0 <= i < len(choices) for i in correct):
            continue
        kind = "multi" if len(correct) > 1 or q.get("kind") == "multi" else "mc"
        out.append({"kind": kind, "prompt": prompt, "choices": choices, "correct": correct,
                    "explanation": str(q.get("explanation", "")).strip(),
                    "general": fill and str(q.get("source", "")).strip().lower() != "notes",
                    # editable text form: correct choices start with '*', same as the manual question form
                    "choices_text": "\n".join(("* " if i in correct else "") + c for i, c in enumerate(choices))})
    if not out:
        raise ActionError("The model returned no usable questions.")
    out = out[:limit]
    return out, _cut_note(cut_off, len(out), "questions")


def explain(passage: str) -> str:
    passage = _clip(passage)
    return ai.complete(
        "Explain this passage from my notes a different way: plain language first, then a small worked "
        "example if it helps (for subnetting or other calculations, go step by step). Markdown, under 350 words.\n\n"
        f"<passage>\n{passage}\n</passage>", system=SYSTEM)


def gap_check(competency: str, notes: str) -> str:
    notes = _clip(notes)
    if not competency.strip():
        raise ActionError("Pick the competency to check against.")
    return ai.complete(
        "Here is a course competency statement and my notes for it. List what the competency expects that my "
        "notes don't cover yet, or cover too thinly, as a short markdown checklist (`- [ ] ...`), most important "
        "first. Then one line on what the notes already cover well. Don't write the missing content itself.\n\n"
        f"<competency>\n{competency.strip()}\n</competency>\n\n<notes>\n{notes}\n</notes>", system=SYSTEM)
