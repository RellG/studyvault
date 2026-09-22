"""Readiness score (PLAN §4.2): 0.4·confidence + 0.3·card recall + 0.3·latest quiz, renormalised over what exists."""
from datetime import timedelta

from . import clock

WEIGHTS = {"C": 0.4, "R": 0.3, "Q": 0.3}
LABELS = {"C": "Competency confidence", "R": "Flashcard recall (30 days)", "Q": "Latest practice quiz"}


def compute(confidences, grades, latest_quiz):
    """confidences: 1–5 ratings (unrated excluded); grades: 1–4 review grades from the last 30 days;
    latest_quiz: (score, total) or None. Returns {"score": 0–100 or None, "components": {...}}."""
    comps = {"C": None, "R": None, "Q": None}
    if confidences:
        comps["C"] = sum(confidences) / len(confidences) / 5
    if grades:
        comps["R"] = sum(1 for g in grades if g >= 3) / len(grades)
    if latest_quiz and latest_quiz[1]:
        comps["Q"] = latest_quiz[0] / latest_quiz[1]
    present = {k: v for k, v in comps.items() if v is not None}
    if not present:
        return {"score": None, "components": comps}
    wsum = sum(WEIGHTS[k] for k in present)
    score = 100 * sum(WEIGHTS[k] * v for k, v in present.items()) / wsum
    return {"score": round(score), "components": comps}


def for_course(conn, course_id: int):
    conf = [r[0] for r in conn.execute(
        "SELECT confidence FROM competencies WHERE course_id = ? AND confidence IS NOT NULL", (course_id,))]
    since = (clock.now() - timedelta(days=30)).isoformat()
    grades = [r[0] for r in conn.execute(
        "SELECT r.grade FROM reviews r JOIN cards c ON c.id = r.card_id WHERE c.course_id = ? AND r.reviewed_at >= ?",
        (course_id, since))]
    q = conn.execute(
        "SELECT score, total FROM quiz_attempts WHERE course_id = ? AND finished_at IS NOT NULL "
        "ORDER BY finished_at DESC LIMIT 1", (course_id,)).fetchone()
    result = compute(conf, grades, tuple(q) if q else None)
    result["weights"] = WEIGHTS
    result["labels"] = LABELS
    return result
