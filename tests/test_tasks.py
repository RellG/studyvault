"""Study tasks: migration, service, routes, Today, evidence rules, and the Ask tools."""
import json
import sqlite3
from datetime import timedelta

import httpx
import pytest
from starlette.datastructures import FormData

from app import agent, ai, cards, catalog, clock, competencies, db, quizzes, tasks
from app.config import settings


def course(conn, code="D413"):
    return catalog.get_course(conn, code)


def day(n=0):
    return (clock.today() + timedelta(days=n)).isoformat()


def status(conn, tid):
    return conn.execute("SELECT status FROM study_tasks WHERE id = ?", (tid,)).fetchone()[0]


def evidence(conn, tid):
    return json.loads(conn.execute("SELECT evidence FROM study_tasks WHERE id = ?", (tid,)).fetchone()[0])


# ---------------------------------------------------------------- migration

def test_migration_applies_and_is_additive(conn):
    assert "005_study_tasks.sql" in {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
    cols = {r[1] for r in conn.execute("PRAGMA table_info(study_tasks)")}
    assert {"course_id", "competency_id", "note", "title", "kind", "status", "due_on", "est_minutes", "priority",
            "source", "done_rule", "evidence", "created_at", "updated_at", "done_at"} <= cols
    assert db.migrate(conn) == []  # nothing left to apply
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO study_tasks(title, kind, created_at, updated_at) VALUES ('x', 'bogus', 'a', 'a')")


def test_course_delete_cascades_and_competency_delete_nulls(seeded):
    c = course(seeded)
    competencies.import_list(seeded, c, ["Explain OSI"])
    comp_id = competencies.list_for(seeded, c["id"])[0]["id"]
    tid = tasks.add(seeded, "Read chapter 1", course_id=c["id"], competency_id=comp_id)
    with seeded:
        seeded.execute("DELETE FROM competencies WHERE id = ?", (comp_id,))
    assert seeded.execute("SELECT competency_id FROM study_tasks WHERE id = ?", (tid,)).fetchone()[0] is None
    with seeded:
        seeded.execute("DELETE FROM courses WHERE id = ?", (c["id"],))
    assert seeded.execute("SELECT COUNT(*) FROM study_tasks").fetchone()[0] == 0


# ---------------------------------------------------------------- service

def test_add_validates(seeded):
    c = course(seeded)
    with pytest.raises(tasks.TaskError):
        tasks.add(seeded, "   ")
    with pytest.raises(tasks.TaskError):
        tasks.add(seeded, "x", kind="nap")
    with pytest.raises(tasks.TaskError):
        tasks.add(seeded, "x", due_on="next week")
    with pytest.raises(tasks.TaskError):
        tasks.add(seeded, "x", priority=9)
    competencies.import_list(seeded, c, ["A competency"])
    comp_id = competencies.list_for(seeded, c["id"])[0]["id"]
    with pytest.raises(tasks.TaskError):  # a competency of D413 can't sit under D282
        tasks.add(seeded, "x", course_id=course(seeded, "D282")["id"], competency_id=comp_id)
    tid = tasks.add(seeded, "  Review   OSI  ", competency_id=comp_id, est_minutes="25")  # a competency pulls its course in
    t = tasks.get(seeded, tid)
    assert (t["title"], t["course_id"], t["est_minutes"], t["status"], t["priority"]) == ("Review OSI", c["id"], 25, "todo", 2)


def test_status_changes_and_evidence(seeded):
    tid = tasks.add(seeded, "Write summary", kind="write")
    assert tasks.set_status(seeded, tid, "doing") and not tasks.set_status(seeded, tid, "doing")
    assert tasks.complete(seeded, tid)
    t = tasks.get(seeded, tid)
    assert t["status"] == "done" and t["done_at"] and evidence(seeded, tid) == {"rule": "manual", "by": "you"}
    assert not tasks.complete(seeded, tid)  # already closed
    tasks.set_status(seeded, tid, "todo")  # reopening clears the evidence
    t = tasks.get(seeded, tid)
    assert t["done_at"] is None and t["evidence"] is None
    tasks.postpone(seeded, tid, days=1)
    assert tasks.get(seeded, tid)["due_on"] == day(1)
    tasks.postpone(seeded, tid, due_on=day(7))
    assert tasks.get(seeded, tid)["due_on"] == day(7)
    with pytest.raises(tasks.TaskError):
        tasks.postpone(seeded, tid)


def test_next_tasks_order(seeded):
    low_overdue = tasks.add(seeded, "overdue low", due_on=day(-2), priority=3)
    high_today = tasks.add(seeded, "today high", due_on=day(0), priority=1)
    high_later = tasks.add(seeded, "later high", due_on=day(5), priority=1)
    undated = tasks.add(seeded, "undated", priority=2)
    blocked = tasks.add(seeded, "blocked", due_on=day(-1), priority=1)
    tasks.set_status(seeded, blocked, "blocked")
    done = tasks.add(seeded, "done", due_on=day(-1), priority=1)
    tasks.complete(seeded, done)
    order = [t["title"] for t in tasks.next_tasks(seeded, 10)]
    assert order == ["today high", "overdue low", "later high", "undated"]
    assert [t["id"] for t in tasks.next_tasks(seeded, 2)] == [high_today, low_overdue]
    assert {high_later, undated} <= {t["id"] for t in tasks.list_tasks(seeded, statuses=tasks.OPEN)}
    d = {x["id"]: x for x in tasks.decorate(tasks.list_tasks(seeded, statuses=tasks.OPEN))}
    assert d[low_overdue]["due_label"] == "overdue 2 days" and d[low_overdue]["overdue"]
    assert d[high_today]["due_label"] == "due today" and d[high_later]["why"] == "high priority"


# ---------------------------------------------------------------- routes

def test_task_pages_and_crud(client):
    assert "Nothing open" in client.get("/tasks").text
    r = client.post("/tasks", data={"title": "Review OSPF", "course": "D413", "kind": "review_cards", "due_on": day(0),
                                    "est_minutes": "20", "priority": "1", "done_rule": "cards_reviewed", "back": "/tasks"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/tasks"
    page = client.get("/tasks").text
    assert "Review OSPF" in page and "due today" in page and "20 min" in page and "high priority" in page
    assert "Done when you clear this course" in page and "Due now" in page
    conn = db.connect()
    try:
        tid = conn.execute("SELECT id FROM study_tasks").fetchone()[0]
        assert "Review OSPF" in client.get("/courses/D413/tasks").text
        assert 'class="active" aria-current="page">Tasks' in client.get("/courses/D413/tasks").text
        assert "Review OSPF" not in client.get("/courses/D282/tasks").text
        # edit, including the status
        assert client.get(f"/tasks/{tid}/edit").status_code == 200
        r = client.post(f"/tasks/{tid}/edit", data={"title": "Review OSPF areas", "course_id": str(course(conn)["id"]),
                                                    "status": "doing", "kind": "study", "due_on": day(3), "est_minutes": "",
                                                    "priority": "2", "done_rule": "manual", "note": "notebook"},
                        follow_redirects=False)
        assert r.status_code == 303
        t = tasks.get(conn, tid)
        assert (t["title"], t["status"], t["due_on"], t["est_minutes"], t["done_rule"], t["note"]) == \
            ("Review OSPF areas", "doing", day(3), None, "manual", "notebook")
        assert "Coming up" in client.get("/tasks").text
        # postpone, done, reopen, cancel
        client.post(f"/tasks/{tid}/postpone", data={"days": "1", "back": "/tasks"})
        assert tasks.get(conn, tid)["due_on"] == day(1)
        client.post(f"/tasks/{tid}/postpone", data={"due_on": day(9)})
        assert tasks.get(conn, tid)["due_on"] == day(9)
        client.post(f"/tasks/{tid}/done", data={"back": "/"})
        assert status(conn, tid) == "done" and "Done: marked by you" in client.get("/tasks").text
        client.post(f"/tasks/{tid}/reopen")
        assert status(conn, tid) == "todo"
        client.post(f"/tasks/{tid}/cancel")
        assert status(conn, tid) == "cancelled" and "Done and cancelled (1)" in client.get("/tasks").text
        assert client.post("/tasks/9999/done").status_code == 404
        assert client.get("/tasks/9999/edit").status_code == 404
    finally:
        conn.close()


def test_create_errors_keep_what_was_typed(client):
    r = client.post("/tasks", data={"title": "  ", "course": "D413", "back": "/courses/D413/tasks"})
    assert r.status_code == 400 and "needs a title" in r.text and 'aria-label="Course sections"' in r.text
    r = client.post("/tasks", data={"title": "Keep me", "due_on": "tomorrow-ish", "back": "/tasks"})
    assert r.status_code == 400 and "isn&#39;t a date" in r.text and 'value="Keep me"' in r.text
    conn = db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM study_tasks").fetchone()[0] == 0
    finally:
        conn.close()


def test_back_is_a_local_path_only(client):
    for bad in ("//evil.example", "https://evil.example", ""):
        r = client.post("/tasks", data={"title": "x", "back": bad}, follow_redirects=False)
        assert r.headers["location"] == "/tasks", bad


def test_course_tab_has_competency_picker_and_badge(client):
    conn = db.connect()
    try:
        competencies.import_list(conn, course(conn), ["Explain the OSI model"])
        comp_id = competencies.list_for(conn, course(conn)["id"])[0]["id"]
        client.post("/tasks", data={"title": "OSI drill", "course": "D413", "competency_id": str(comp_id),
                                    "back": "/courses/D413/tasks"})
        assert tasks.get(conn, 1)["competency_id"] == comp_id
    finally:
        conn.close()
    page = client.get("/courses/D413/tasks").text
    assert "Explain the OSI model" in page and 'title="1 open"' in page


# ---------------------------------------------------------------- Today

def test_today_shows_resume_cards_and_next_tasks(client):
    conn = db.connect()
    try:
        cards.add(conn, course(conn)["id"], "front", "back")
        for title, due, pri in [("Overdue read", day(-3), 2), ("Today write", day(0), 1), ("Next week", day(6), 1),
                                ("Later still", day(20), 1)]:
            tasks.add(conn, title, course_id=course(conn)["id"], due_on=due, priority=pri)
        done = tasks.add(conn, "Already done", due_on=day(-9), priority=1)
        tasks.complete(conn, done)
    finally:
        conn.close()
    page = client.get("/").text
    assert "Do next" in page and "Review 1" in page and "Resume" not in page  # nothing opened yet
    shown = sorted((page.index(t), t) for t in ("Today write", "Overdue read", "Next week", "Later still", "Already done") if t in page)
    assert [t for _, t in shown] == ["Today write", "Overdue read", "Next week"]  # the next three, in order
    assert 'action="/tasks/' in page and "overdue 3 days" in page and "All tasks (4 open)" in page
    client.get("/courses/D413/notes/notebook")
    page = client.get("/").text
    assert "Resume" in page and 'href="/courses/D413/notes/notebook"' in page
    client.get("/courses/D413/notes/mistakes?mode=edit")
    assert 'href="/courses/D413/notes/mistakes"' in client.get("/").text


def test_today_with_nothing_planned(client):
    page = client.get("/").text
    assert "Do next" in page and "No open tasks" in page and "none due" in page


def test_done_from_today_returns_to_today(client):
    conn = db.connect()
    try:
        tid = tasks.add(conn, "Quick win", due_on=day(0))
    finally:
        conn.close()
    r = client.post(f"/tasks/{tid}/done", data={"back": "/"}, follow_redirects=False)
    assert r.headers["location"] == "/"
    assert "Quick win" not in client.get("/").text


# ---------------------------------------------------------------- evidence: cards

def make_cards(conn, n=2, code="D413", competency_id=None):
    c = course(conn, code)
    return [cards.add(conn, c["id"], f"Q{i}", f"A{i}", competency_id=competency_id) for i in range(n)]


def test_cards_reviewed_closes_when_the_due_cards_are_cleared(seeded):
    c = course(seeded)
    ids = make_cards(seeded, 2)
    tid = tasks.add(seeded, "Clear D413 cards", course_id=c["id"], kind="review_cards", done_rule="cards_reviewed")
    other = tasks.add(seeded, "Other course", course_id=course(seeded, "D282")["id"], done_rule="cards_reviewed")
    manual = tasks.add(seeded, "Manual one", course_id=c["id"])
    cards.grade(seeded, ids[0], 3)
    assert status(seeded, tid) == "todo"  # one card is still due
    cards.grade(seeded, ids[1], 3)
    assert status(seeded, tid) == "done" and status(seeded, other) == "todo" and status(seeded, manual) == "todo"
    ev = evidence(seeded, tid)
    assert ev["rule"] == "cards_reviewed" and ev["reviews"] == 2 and ev["day"] == day(0)
    assert "reviewed 2 cards" in tasks.evidence_text(tasks.get(seeded, tid))


def test_cards_reviewed_ignores_reviews_before_the_task_and_generated_cards(seeded):
    c = course(seeded)
    ids = make_cards(seeded, 1)
    tid = tasks.add(seeded, "Later task", course_id=c["id"], done_rule="cards_reviewed")
    with seeded:  # pretend the task was made after the review below
        seeded.execute("UPDATE study_tasks SET created_at = ? WHERE id = ?", ((clock.now() + timedelta(hours=1)).isoformat(), tid))
    cards.grade(seeded, ids[0], 3)
    assert status(seeded, tid) == "todo"
    # new cards (hand-made, imported or AI-accepted) are not reviews: nothing closes
    tid2 = tasks.add(seeded, "Generated cards", course_id=c["id"], done_rule="cards_reviewed")
    cards.add(seeded, c["id"], "gen front", "gen back", source="ai-accepted")
    cards.import_cards(seeded, c["id"], [{"front": "imp", "back": "x", "type": "basic"}])
    assert status(seeded, tid2) == "todo"


def test_cards_reviewed_by_competency(seeded):
    c = course(seeded)
    competencies.import_list(seeded, c, ["One", "Two"])
    one, two = [r["id"] for r in competencies.list_for(seeded, c["id"])]
    a = make_cards(seeded, 1, competency_id=one)[0]
    b = make_cards(seeded, 1, competency_id=two)[0]
    tid = tasks.add(seeded, "Cards for Two", competency_id=two, done_rule="cards_reviewed")
    cards.grade(seeded, a, 3)
    assert status(seeded, tid) == "todo"  # a card from another competency
    cards.grade(seeded, b, 3)
    assert status(seeded, tid) == "done"


# ---------------------------------------------------------------- evidence: quizzes

def make_quiz(conn, code="D413", n=2, competency_id=None):
    c = course(conn, code)
    for i in range(n):
        quizzes.save(conn, c["id"], {"kind": "mc", "prompt": f"Question {i}?", "choices": "* right\nwrong",
                                     "competency_id": str(competency_id or "")})
    return c


def take(conn, c, count=2):
    attempt = quizzes.start(conn, c["id"], count)
    qs = quizzes.attempt_questions(conn, conn.execute("SELECT * FROM quiz_attempts WHERE id = ?", (attempt,)).fetchone())
    quizzes.submit(conn, attempt, {q["id"]: ["0"] for q in qs})
    return attempt


def test_quiz_finished_closes_the_course_task(seeded):
    c = make_quiz(seeded)
    tid = tasks.add(seeded, "Take a D413 quiz", course_id=c["id"], kind="quiz", done_rule="quiz_finished")
    other = tasks.add(seeded, "D282 quiz", course_id=course(seeded, "D282")["id"], done_rule="quiz_finished")
    manual = tasks.add(seeded, "Manual", course_id=c["id"])
    attempt = take(seeded, c)
    assert status(seeded, tid) == "done" and status(seeded, other) == "todo" and status(seeded, manual) == "todo"
    ev = evidence(seeded, tid)
    assert ev == {"rule": "quiz_finished", "attempt_id": attempt, "score": 2, "total": 2}
    assert "finished quiz" in tasks.evidence_text(tasks.get(seeded, tid))


def test_quiz_before_the_task_and_new_questions_do_not_count(seeded):
    c = make_quiz(seeded)
    take(seeded, c)
    tid = tasks.add(seeded, "After the fact", course_id=c["id"], done_rule="quiz_finished")
    assert status(seeded, tid) == "todo"
    # adding questions (hand-made or generated) is not taking a quiz
    quizzes.save(seeded, c["id"], {"kind": "mc", "prompt": "More?", "choices": "* a\nb"}, source="ai-accepted")
    assert status(seeded, tid) == "todo"


def test_finish_claimed_once_closes_once(seeded):
    c = make_quiz(seeded)
    attempt = take(seeded, c)
    tid = tasks.add(seeded, "Again", course_id=c["id"], done_rule="quiz_finished")
    quizzes.finish(seeded, attempt)  # already finished: claims nothing, so it closes nothing
    assert status(seeded, tid) == "todo"


def test_quiz_task_for_a_competency_needs_that_competency(seeded):
    c = course(seeded)
    competencies.import_list(seeded, c, ["One", "Two"])
    one, two = [r["id"] for r in competencies.list_for(seeded, c["id"])]
    make_quiz(seeded, competency_id=one)
    tid = tasks.add(seeded, "Quiz on Two", competency_id=two, done_rule="quiz_finished")
    take(seeded, c)
    assert status(seeded, tid) == "todo"
    quizzes.save(seeded, c["id"], {"kind": "mc", "prompt": "Two?", "choices": "* a\nb", "competency_id": str(two)})
    attempt = quizzes.start(seeded, c["id"], 1, two)
    q = quizzes.attempt_questions(seeded, seeded.execute("SELECT * FROM quiz_attempts WHERE id = ?", (attempt,)).fetchone())[0]
    quizzes.submit(seeded, attempt, {q["id"]: ["0"]})
    assert status(seeded, tid) == "done"


def test_manual_tasks_never_close_themselves(seeded):
    c = make_quiz(seeded)
    ids = make_cards(seeded, 1)
    tid = tasks.add(seeded, "Mine to close", course_id=c["id"], kind="quiz")
    cards.grade(seeded, ids[0], 3)
    take(seeded, c)
    assert status(seeded, tid) == "todo"


# ---------------------------------------------------------------- Ask tools


class Api:
    def __init__(self):
        self.bodies, self.replies = [], []

    def __call__(self, request):
        self.bodies.append(json.loads(request.content))
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


@pytest.fixture
def api(monkeypatch):
    a = Api()
    monkeypatch.setattr(ai, "_transport", httpx.MockTransport(a))
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    monkeypatch.setattr(settings, "ai_model", "model-from-env")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
    return a


def a_calls(*calls):
    return httpx.Response(200, json={"stop_reason": "tool_use", "content": [
        {"type": "tool_use", "id": f"tu{i}", "name": n, "input": args} for i, (n, args) in enumerate(calls)]})


def a_text(text):
    return httpx.Response(200, json={"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]})


@pytest.fixture
def tid(seeded):
    return agent.create_thread(seeded, "task chat")


def run(conn, thread_id, name, **args):
    k = agent.Ctx(conn, conn.execute("SELECT * FROM agent_threads WHERE id = ?", (thread_id,)).fetchone())
    return json.loads(agent.run_tool(k, ai.Call("", name, args)))


def proposal(conn, pid):
    return conn.execute("SELECT * FROM agent_proposals WHERE id = ?", (pid,)).fetchone()


def test_list_tasks_tool(seeded, tid):
    assert run(seeded, tid, "list_tasks")["tasks"] == []
    c = course(seeded)
    done = tasks.add(seeded, "Finished thing", course_id=c["id"])
    tasks.complete(seeded, done)
    tasks.add(seeded, "Open thing", course_id=c["id"], due_on=day(-1), done_rule="cards_reviewed")
    out = run(seeded, tid, "list_tasks", code="D413")
    assert [t["title"] for t in out["tasks"]] == ["Open thing"]
    t = out["tasks"][0]
    assert t["done_rule"] == "cards_reviewed" and t["when"] == "overdue 1 day" and t["course"] == "D413"
    allt = run(seeded, tid, "list_tasks", status="done")["tasks"]
    assert allt[0]["title"] == "Finished thing" and allt[0]["evidence"].startswith("Done: marked by you")
    assert "error" in run(seeded, tid, "list_tasks", status="sleeping")
    assert "error" in run(seeded, tid, "list_tasks", code="X999")


def test_propose_tasks_creates_nothing_until_apply(seeded, tid):
    out = run(seeded, tid, "propose_tasks", tasks=[
        {"title": "Review OSI notes", "code": "D413", "due_on": day(1), "est_minutes": 25, "priority": 1},
        {"title": "Clear cards", "code": "D413", "kind": "review_cards", "done_rule": "cards_reviewed"},
        {"title": "", "code": "D413"}, "junk"])
    assert "proposal_id" in out and seeded.execute("SELECT COUNT(*) FROM study_tasks").fetchone()[0] == 0
    p = proposal(seeded, out["proposal_id"])
    assert p["kind"] == "tasks" and p["status"] == "pending" and p["course_id"] == course(seeded)["id"]
    assert len(json.loads(p["payload_json"])["tasks"]) == 2
    result, link = agent.apply(seeded, p, FormData([("keep", "0"), ("keep", "1"), ("title_0", "Review OSI notes (edited)"),
                                                    ("due_0", day(2)), ("minutes_0", "30"), ("priority_0", "2")]))
    assert result == "Added 2 tasks." and link == "/courses/D413/tasks"
    rows = seeded.execute("SELECT * FROM study_tasks ORDER BY id").fetchall()
    assert [r["title"] for r in rows] == ["Review OSI notes (edited)", "Clear cards"]
    assert (rows[0]["due_on"], rows[0]["est_minutes"], rows[0]["priority"], rows[0]["source"]) == (day(2), 30, 2, f"proposal:{p['id']}")
    assert rows[1]["done_rule"] == "cards_reviewed" and rows[1]["status"] == "todo"
    assert proposal(seeded, p["id"])["status"] == "applied"
    with pytest.raises(agent.ApplyError):  # applying twice adds nothing
        agent.apply(seeded, proposal(seeded, p["id"]), FormData([("keep", "0")]))
    assert seeded.execute("SELECT COUNT(*) FROM study_tasks").fetchone()[0] == 2


def test_propose_tasks_guards(seeded, tid):
    assert "past" in run(seeded, tid, "propose_tasks", tasks=[{"title": "x", "due_on": day(-1)}])["error"]
    assert "kind" in run(seeded, tid, "propose_tasks", tasks=[{"title": "x", "kind": "nap"}])["error"]
    assert "No course" in run(seeded, tid, "propose_tasks", tasks=[{"title": "x", "code": "X999"}])["error"]
    assert "No usable" in run(seeded, tid, "propose_tasks", tasks=[{"title": ""}])["error"]
    many = [{"title": f"t{i}"} for i in range(agent.MAX_TASKS + 5)]
    out = run(seeded, tid, "propose_tasks", tasks=many)
    assert len(json.loads(proposal(seeded, out["proposal_id"])["payload_json"])["tasks"]) == agent.MAX_TASKS
    p = proposal(seeded, out["proposal_id"])
    with pytest.raises(agent.ApplyError):
        agent.apply(seeded, p, FormData([]))  # nothing ticked
    assert seeded.execute("SELECT COUNT(*) FROM study_tasks").fetchone()[0] == 0


def test_task_update_proposal_and_apply(seeded, tid):
    t1 = tasks.add(seeded, "Read ch. 2", course_id=course(seeded)["id"], due_on=day(1))
    out = run(seeded, tid, "propose_task_update", task_id=t1, due_on=day(4), priority=1, status="doing")
    p = proposal(seeded, out["proposal_id"])
    assert p["kind"] == "task_update" and tasks.get(seeded, t1)["due_on"] == day(1)  # nothing yet
    agent.apply(seeded, p, FormData([("due_on", day(5)), ("priority", "1"), ("status", "doing"), ("title", "Read ch. 2 and 3")]))
    t = tasks.get(seeded, t1)
    assert (t["due_on"], t["priority"], t["status"], t["title"]) == (day(5), 1, "doing", "Read ch. 2 and 3")
    assert "Nothing to change" in run(seeded, tid, "propose_task_update", task_id=t1, priority=1)["error"]
    assert "No task" in run(seeded, tid, "propose_task_update", task_id=999, priority=1)["error"]
    assert "past" in run(seeded, tid, "propose_task_update", task_id=t1, due_on=day(-2))["error"]


def test_agent_cannot_complete_tasks_that_need_evidence(seeded, tid):
    c = course(seeded)
    auto = tasks.add(seeded, "Clear cards", course_id=c["id"], done_rule="cards_reviewed")
    quiz = tasks.add(seeded, "Take a quiz", course_id=c["id"], done_rule="quiz_finished")
    manual = tasks.add(seeded, "Write the summary", course_id=c["id"])
    for t in (auto, quiz):
        assert "can't mark it done" in run(seeded, tid, "propose_task_update", task_id=t, status="done")["error"]
    # a manual task can be proposed as done, but only the student's Apply closes it, and the evidence says so
    out = run(seeded, tid, "propose_task_update", task_id=manual, status="done")
    assert status(seeded, manual) == "todo"
    p = proposal(seeded, out["proposal_id"])
    agent.apply(seeded, p, FormData([("status", "done")]))
    assert status(seeded, manual) == "done"
    assert evidence(seeded, manual) == {"rule": "manual", "by": "you", "via": f"proposal:{p['id']}"}
    # an edited form can't sneak a done through either: the rule is checked again at Apply
    out = run(seeded, tid, "propose_task_update", task_id=auto, priority=1)
    with pytest.raises(agent.ApplyError):
        agent.apply(seeded, proposal(seeded, out["proposal_id"]), FormData([("status", "done"), ("priority", "1")]))
    assert status(seeded, auto) == "todo" and tasks.get(seeded, auto)["priority"] == 2
    # a closed task can't be proposed for changes
    assert "already done" in run(seeded, tid, "propose_task_update", task_id=manual, priority=1)["error"]


def test_generated_content_never_completes_a_task(seeded, tid):
    """An applied card/question proposal adds material; it is not a review or a quiz attempt."""
    c = course(seeded)
    auto = tasks.add(seeded, "Clear cards", course_id=c["id"], done_rule="cards_reviewed")
    quiz = tasks.add(seeded, "Quiz me", course_id=c["id"], done_rule="quiz_finished")
    out = run(seeded, tid, "add_flashcards", code="D413", cards=[{"front": "Port?", "back": "22"}])
    agent.apply(seeded, proposal(seeded, out["proposal_id"]), FormData([("keep", "0"), ("front_0", "Port?"), ("back_0", "22")]))
    out = run(seeded, tid, "add_questions", code="D413",
              questions=[{"prompt": "Port?", "choices": ["22", "80"], "correct": [0]}])
    agent.apply(seeded, proposal(seeded, out["proposal_id"]), FormData([
        ("keep", "0"), ("prompt_0", "Port?"), ("choices_0", "* 22\n80"), ("kind_0", "mc")]))
    assert status(seeded, auto) == "todo" and status(seeded, quiz) == "todo"


def test_ask_flow_with_tasks(client, api):
    api.replies = [a_calls(("propose_tasks", {"tasks": [{"title": "Review the OSI model", "code": "D413", "due_on": day(1),
                                                         "est_minutes": 20, "done_rule": "cards_reviewed"}]})),
                   a_text("I've proposed a task.")]
    page = client.post("/ask/new", data={"text": "Plan tomorrow", "course": "D413"})
    assert page.status_code == 200 and "Add kept tasks" in page.text and "Review the OSI model" in page.text
    assert "closes itself when you clear this course" in page.text
    conn = db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM study_tasks").fetchone()[0] == 0
        pid = conn.execute("SELECT id FROM agent_proposals").fetchone()[0]
        r = client.post(f"/ask/proposals/{pid}/apply", data={"keep": "0", "title_0": "Review the OSI model",
                                                             "due_0": day(1), "minutes_0": "20", "priority_0": "2"})
        assert r.status_code == 200 and "Added 1 task." in r.text
        t = conn.execute("SELECT * FROM study_tasks").fetchone()
        assert t["source"] == f"proposal:{pid}" and t["done_rule"] == "cards_reviewed"
    finally:
        conn.close()
    assert "Review the OSI model" in client.get("/tasks").text
    # a task-change card renders with its current values
    conn = db.connect()
    try:
        tid = conn.execute("SELECT id FROM study_tasks").fetchone()[0]
    finally:
        conn.close()
    api.replies = [a_calls(("propose_task_update", {"task_id": tid, "due_on": day(3)})), a_text("Moved it.")]
    page = client.post("/ask/1/send", data={"text": "push it back"})
    assert "Apply change" in page.text and "Review the OSI model" in page.text and "Done isn't offered" in page.text


def test_system_prompt_states_the_evidence_rule(seeded, tid):
    prompt = agent.system_prompt(seeded, seeded.execute("SELECT * FROM agent_threads").fetchone())
    assert "never say work or a task is done without evidence" in prompt.lower()
    assert "list_tasks" in {t["name"] for t in agent.schemas()} and "propose_tasks" in agent.TOOLS
