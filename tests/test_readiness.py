from datetime import datetime, timedelta

import pytest

from app import catalog, clock, readiness


def test_no_data_is_none():
    r = readiness.compute([], [], None)
    assert r["score"] is None
    assert r["components"] == {"C": None, "R": None, "Q": None}


def test_all_components():
    # C = mean(4, 2)/5 = 0.6 ; R = 3 of 4 graded ≥3 = 0.75 ; Q = 8/10 = 0.8
    r = readiness.compute([4, 2], [4, 3, 3, 1], (8, 10))
    assert r["components"] == {"C": 0.6, "R": 0.75, "Q": 0.8}
    assert r["score"] in (70, 71)  # 100·(0.4·0.6 + 0.3·0.75 + 0.3·0.8) = 70.5; float rounding may go either way


@pytest.mark.parametrize("conf,grades,quiz,expected", [
    ([5], [], None, 100),                    # only C
    ([], [1, 1], None, 0),                   # only R, all lapses
    ([], [], (3, 4), 75),                    # only Q
    ([5], [], (0, 10), round(100 * 0.4 / 0.7)),  # C + Q renormalised: (0.4·1 + 0.3·0)/0.7
    ([], [3, 1], (1, 2), 50),                # R + Q: (0.3·0.5 + 0.3·0.5)/0.6
])
def test_missing_components_renormalise(conf, grades, quiz, expected):
    assert readiness.compute(conf, grades, quiz)["score"] == expected


def test_zero_total_quiz_ignored():
    assert readiness.compute([], [], (0, 0))["score"] is None


def test_for_course_uses_db(seeded, monkeypatch):
    now = datetime(2026, 10, 20, 12, 0)
    monkeypatch.setattr(clock, "now", lambda: now)
    c = catalog.get_course(seeded, "D413")
    assert readiness.for_course(seeded, c["id"])["score"] is None
    with seeded:
        seeded.execute("INSERT INTO competencies(course_id, ord, text, confidence) VALUES (?, 0, 'a', 5), (?, 1, 'b', NULL)", (c["id"], c["id"]))
        cur = seeded.execute("INSERT INTO cards(course_id, front, back, due_on, created_at) VALUES (?, 'q', 'a', '2026-10-20', 'x')", (c["id"],))
        card = cur.lastrowid
        seeded.execute("INSERT INTO reviews(card_id, reviewed_at, grade) VALUES (?, ?, 4), (?, ?, 1)",
                       (card, (now - timedelta(days=1)).isoformat(), card, (now - timedelta(days=40)).isoformat()))
        seeded.execute("INSERT INTO quiz_attempts(course_id, started_at, finished_at, score, total, question_ids_json) "
                       "VALUES (?, 'a', '2026-10-01T10:00:00', 2, 10, '[]'), (?, 'b', '2026-10-19T10:00:00', 9, 10, '[]')",
                       (c["id"], c["id"]))
    r = readiness.for_course(seeded, c["id"])
    # unrated competency ignored; 40-day-old review ignored; latest quiz used
    assert r["components"] == {"C": 1.0, "R": 1.0, "Q": 0.9}
    assert r["score"] == 97
