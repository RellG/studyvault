"""The four AI actions (spec F8). Inputs are only the notes text the user selected; outputs are drafts, never saved here."""
import json
import re

from . import ai

MAX_INPUT_CHARS = 12000

SYSTEM = ("You help a WGU student study from their own notes for the B.S. Cloud & Network Engineering (AWS) degree. "
          "Use only what the notes support; if the notes are thin or wrong, say so rather than inventing facts. "
          "Never write or draft performance-assessment submissions.")


class ActionError(ai.AIError):
    pass


def _clip(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise ActionError("Pick or paste some notes first.")
    if len(text) > MAX_INPUT_CHARS:
        raise ActionError(f"That's {len(text):,} characters; trim it to {MAX_INPUT_CHARS:,} or less (one section at a time works best).")
    return text


def _json_array(text: str) -> list:
    """Pull the first JSON array out of a model reply (tolerates code fences and a sentence around it)."""
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ActionError("The model didn't return a list I could read. Try again.")
    try:
        data = json.loads(m.group(0))
    except ValueError:
        raise ActionError("The model's list wasn't valid JSON. Try again.")
    if not isinstance(data, list):
        raise ActionError("The model didn't return a list.")
    return data


def flashcards(notes: str, n: int = 10) -> list[dict]:
    notes = _clip(notes)
    reply = ai.complete(
        f"Write up to {n} flashcards from the notes below. Favour things worth memorising: ports, commands, "
        "definitions, AWS services, standards, numbers. One fact per card; answers short.\n"
        'Reply with only a JSON array like [{"front": "...", "back": "..."}].\n\n'
        f"<notes>\n{notes}\n</notes>", system=SYSTEM)
    out = []
    for item in _json_array(reply):
        if isinstance(item, dict) and str(item.get("front", "")).strip() and str(item.get("back", "")).strip():
            out.append({"front": str(item["front"]).strip(), "back": str(item["back"]).strip()})
    if not out:
        raise ActionError("The model returned no usable cards.")
    return out[:n]


def questions(notes: str, n: int = 5) -> list[dict]:
    notes = _clip(notes)
    reply = ai.complete(
        f"Write up to {n} practice exam questions from the notes below, in the style of a WGU objective "
        "assessment. Mix single-answer and select-all-that-apply. Each needs 4 choices, the correct choice "
        "indexes (0-based), and a one-to-two sentence explanation grounded in the notes.\n"
        'Reply with only a JSON array like [{"kind": "mc" or "multi", "prompt": "...", "choices": ["..."], '
        '"correct": [0], "explanation": "..."}].\n\n'
        f"<notes>\n{notes}\n</notes>", system=SYSTEM)
    out = []
    for q in _json_array(reply):
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
                    # editable text form: correct choices start with '*', same as the manual question form
                    "choices_text": "\n".join(("* " if i in correct else "") + c for i, c in enumerate(choices))})
    if not out:
        raise ActionError("The model returned no usable questions.")
    return out[:n]


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
