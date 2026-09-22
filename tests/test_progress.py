import re
from datetime import date, datetime, timedelta

import pytest

from app import catalog, clock, progress
from app.progress import Course


def C(cu, status="not_started", attempted=None, term=1, passed_on=None):
    return Course("X", cu, status, status != "not_started" if attempted is None else attempted, term, passed_on)


def test_attempted_vs_passed():
    s = progress.sap([C(3, "passed"), C(4, "in_progress"), C(3), C(3, "revision_needed")])
    assert (s["attempted"], s["passed"]) == (10, 3)
    # attempted sticks even if status went back to not_started
    assert progress.sap([C(3, "not_started", attempted=True)])["attempted"] == 3


def test_sap_none_without_attempts():
    assert progress.sap([C(3), C(4)]) == {"attempted": 0, "passed": 0, "ratio": None, "color": None}


@pytest.mark.parametrize("passed,attempted,color", [
    (4, 5, "green"),      # 0.80 exactly → green
    (79, 100, "amber"),   # 0.79
    (667, 1000, "amber"), # 0.667 → amber
    (2, 3, "amber"),      # exactly 2/3 meets WGU's 66.67 %
    (666, 1000, "red"),   # 0.666 → red
    (1, 2, "red"),
    (1, 1, "green"),
])
def test_sap_color_boundaries(passed, attempted, color):
    courses = [C(passed, "passed")] + ([C(attempted - passed, "in_progress")] if attempted > passed else [])
    assert progress.sap(courses)["color"] == color


def test_transferred_cu_not_in_sap(seeded):
    # E004 (3 CU transferred) lives in `transfers`, not `courses`, so SAP never sees it
    catalog.set_status(seeded, "D413", "in_progress")
    p = progress.load(seeded, date(2026, 10, 10))
    assert p["transferred"] == 3
    assert p["sap"]["attempted"] == 3 and p["sap"]["passed"] == 0 and p["sap"]["color"] == "red"
    assert p["remaining_total"] == 109


def test_term1_check():
    assert progress.term1_check([C(2, "passed")]) == {"passed": 2, "ok": False, "needed": 1}
    assert progress.term1_check([C(3, "passed"), C(3, "passed", term=2)])["ok"] is True


def test_term_pace():
    start, end = date(2026, 10, 1), date(2027, 3, 31)
    # 3 CU passed after 14 days: rate 3/14 per day, 167 days left → 3 + 35.79 = 38.8
    p = progress.term_pace(start, end, 27, 3, date(2026, 10, 15))
    assert p["projected"] == 38.8 and p["on_pace"] is True and p["days_remaining"] == 167
    # early in the term the divisor is at least 7 days
    p = progress.term_pace(start, end, 27, 3, date(2026, 10, 3))
    assert p["projected"] == round(3 + 3 / 7 * 179, 1)
    assert progress.term_pace(start, end, 27, 0, date(2026, 12, 1))["on_pace"] is False
    p = progress.term_pace(start, end, 27, 0, date(2026, 9, 22))
    assert p["started"] is False and p["starts_in"] == 9 and p["projected"] is None


def test_projected_graduation():
    today = date(2027, 1, 1)
    assert progress.projected_graduation([], 109, today, None)["status"] == "not_enough_data"
    assert progress.projected_graduation([], 109, today, today - timedelta(days=13))["status"] == "not_enough_data"
    assert progress.projected_graduation([], 109, today, today - timedelta(days=30))["status"] == "no_recent_passes"
    # 9 CU in the last 90 days → 0.1 CU/day → 100 CU remaining takes 1000 days
    passes = [(today - timedelta(days=10), 3), (today - timedelta(days=40), 3), (today - timedelta(days=80), 3),
              (today - timedelta(days=100), 4)]  # outside the window
    g = progress.projected_graduation(passes, 100, today, date(2026, 1, 1))
    assert g["status"] == "ok" and g["window"] == 90 and g["date"] == today + timedelta(days=1000)
    assert g["on_track"] is False
    # with only 30 days of data the window shrinks to 30
    g = progress.projected_graduation([(today - timedelta(days=5), 6)], 60, today, today - timedelta(days=30))
    assert g["window"] == 30 and g["date"] == today + timedelta(days=300) and g["on_track"] is True
    assert progress.projected_graduation([], 0, today, None)["status"] == "done"


def test_streak():
    t = date(2026, 10, 10)
    assert progress.streak(set(), t) == 0
    assert progress.streak({t, t - timedelta(1), t - timedelta(2)}, t) == 3
    assert progress.streak({t - timedelta(1), t - timedelta(2)}, t) == 2  # nothing yet today still counts
    assert progress.streak({t, t - timedelta(2)}, t) == 1


def test_dashboard_matches_hand_calculation(seeded, monkeypatch):
    today = date(2026, 10, 29)
    monkeypatch.setattr(clock, "today", lambda: today)
    catalog.set_status(seeded, "D413", "passed")         # 3 CU, passed_on 10-29, attempted
    catalog.set_status(seeded, "D282", "in_progress")    # 3 CU attempted
    p = progress.load(seeded, today)
    assert p["sap"]["ratio"] == 0.5 and p["sap"]["color"] == "red"
    assert p["term1"]["ok"] is True
    # 3 CU in 28 days → 3/28 per day × 153 days left + 3
    assert p["pace"]["projected"] == round(3 + 3 / 28 * 153, 1)
    assert p["grad"]["status"] == "not_enough_data"      # activity started today


def test_sessions_and_dashboard(client):
    r = client.post("/sessions/start", data={"course_code": "D413", "pomodoro": "1"}, headers={"referer": "http://x/courses/D413"},
                    follow_redirects=False)
    assert r.headers["location"] == "/courses/D413"
    page = client.get("/courses/D413").text
    assert "timer-pill" in page and "⏱ D413" in page
    assert 'id="pomo"' in client.get("/sessions").text
    client.post("/sessions/stop")
    assert "timer-pill" not in client.get("/").text
    assert client.post("/sessions/manual", data={"course_code": "D413", "day": "2026-10-05", "minutes": "90"},
                       follow_redirects=False).status_code == 303
    assert "Date and minutes are required" in client.post("/sessions/manual", data={"day": "x", "minutes": "5"}, follow_redirects=True).text
    page = client.get("/sessions").text
    assert re.search(r"D413</a>\s*<div class=\"track\">", page) and "1.5 h" in page
    dash = client.get("/").text
    assert "Term 1" in dash and "Next up" in dash and "D413" in dash and "SAP" in dash
