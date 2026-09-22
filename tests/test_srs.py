from datetime import date

import pytest

from app import srs
from app.srs import State


def run(grades, start=State()):
    s = start
    for g in grades:
        s = srs.review(s, g)
    return s


def test_first_second_nth_intervals():
    s1 = srs.review(State(), 3)              # Good, q=4: ease unchanged (0.1 - 1·0.1 = 0)
    assert s1 == State(2.5, 1, 1)
    s2 = srs.review(s1, 3)
    assert s2 == State(2.5, 6, 2)
    s3 = srs.review(s2, 3)
    assert s3 == State(2.5, 15, 3)           # round(6 × 2.5)
    s4 = srs.review(s3, 3)
    assert s4 == State(2.5, 38, 4)           # round(15 × 2.5) = 37.5 → 38


def test_nth_interval_uses_ease_before_update():
    s = srs.review(State(2.5, 6, 2), 4)      # Easy, q=5
    assert s.interval == 15 and s.ease == 2.6


def test_ease_changes_by_grade():
    assert srs.review(State(), 4).ease == 2.6    # Easy  +0.10
    assert srs.review(State(), 3).ease == 2.5    # Good   0
    assert srs.review(State(), 2).ease == 2.36   # Hard  −0.14
    assert srs.review(State(), 1).ease == 1.96   # Again −0.54


def test_hard_still_counts_as_recall():
    s = srs.review(State(2.5, 6, 2), 2)
    assert s.reps == 3 and s.interval == 15


def test_lapse_resets():
    s = srs.review(State(2.5, 40, 5), 1)
    assert s.reps == 0 and s.interval == 1
    assert srs.review(s, 3).interval == 1   # relearning starts over: 1, 6, …
    assert srs.review(srs.review(s, 3), 3).interval == 6


def test_ease_floor():
    s = run([1, 1, 1, 1, 1])
    assert s.ease == 1.3
    assert srs.review(State(1.35, 10, 3), 1).ease == 1.3


def test_due_on_and_preview():
    s = srs.review(State(), 3)
    assert srs.due_on(s, date(2026, 10, 1)) == date(2026, 10, 2)
    assert srs.preview(State(2.5, 6, 2)) == {1: 1, 2: 15, 3: 15, 4: 15}
    assert srs.preview(State()) == {1: 1, 2: 1, 3: 1, 4: 1}


def test_bad_grade():
    with pytest.raises(ValueError):
        srs.review(State(), 5)
