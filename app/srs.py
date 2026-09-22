"""SM-2 spaced repetition, exactly as PLAN §4.1.

UI grades 1 Again · 2 Hard · 3 Good · 4 Easy map to SM-2 quality 1, 3, 4, 5.
"""
from dataclasses import dataclass
from datetime import date, timedelta

GRADE_TO_Q = {1: 1, 2: 3, 3: 4, 4: 5}
GRADE_LABELS = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}
NEW_EASE = 2.5
MIN_EASE = 1.3


@dataclass(frozen=True)
class State:
    ease: float = NEW_EASE
    interval: int = 0
    reps: int = 0


def review(state: State, grade: int) -> State:
    if grade not in GRADE_TO_Q:
        raise ValueError(f"grade must be 1–4, got {grade}")
    q = GRADE_TO_Q[grade]
    if q < 3:
        reps, interval = 0, 1
    else:
        reps = state.reps + 1
        interval = 1 if reps == 1 else 6 if reps == 2 else round(state.interval * state.ease)
    ease = max(MIN_EASE, state.ease + 0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
    return State(ease=round(ease, 4), interval=interval, reps=reps)


def due_on(state: State, today: date) -> date:
    return today + timedelta(days=state.interval)


def preview(state: State) -> dict[int, int]:
    """Interval in days each grade would give; shown on the grade buttons."""
    return {g: review(state, g).interval for g in GRADE_TO_Q}
