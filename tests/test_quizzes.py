import json
import random
import re

import pytest

from app import catalog, notes_fs, quizzes, readiness


def test_build_validates():
    assert quizzes.build("mc", "Port?", "21\n* 22\n23", "") == (["21", "22", "23"], [1])
    assert quizzes.build("multi", "L2?", "* ARP\nIP\n*  STP ", "") == (["ARP", "IP", "STP"], [0, 2])
    assert quizzes.build("short", "Explain OSPF", "", " link state ") == ([], "link state")
    for args in [("mc", "x", "a\nb", ""), ("mc", "x", "*a\n*b", ""), ("mc", "x", "*a", ""),
                 ("short", "x", "", ""), ("mc", " ", "*a\nb", ""), ("nope", "x", "*a\nb", "")]:
        with pytest.raises(quizzes.QuestionError):
            quizzes.build(*args)


@pytest.fixture
def course(seeded):
    notes_fs.ensure_repo()
    return catalog.get_course(seeded, "D413")


def make_bank(conn, course):
    ids = []
    for i in range(10):
        ids.append(quizzes.save(conn, course["id"], {"kind": "mc", "prompt": f"Q{i}: which is right?",
                                                      "choices": "wrong\n* right", "explanation": f"because {i}"}))
    ids.append(quizzes.save(conn, course["id"], {"kind": "multi", "prompt": "Pick both", "choices": "* a\n* b\nc"}))
    ids.append(quizzes.save(conn, course["id"], {"kind": "short", "prompt": "What is SSID?", "short_answer": "network name"}))
    return ids


def test_ten_question_quiz_scores_and_appends_misses(seeded, course):
    make_bank(seeded, course)
    aid = quizzes.start(seeded, course["id"], 10, shuffle=False)  # the ten mc questions, in order
    attempt = seeded.execute("SELECT * FROM quiz_attempts WHERE id = ?", (aid,)).fetchone()
    qs = quizzes.attempt_questions(seeded, attempt)
    assert len(qs) == 10 and attempt["total"] == 10
    answers, expected_wrong = {}, 0
    for n, q in enumerate(qs):
        right = json.loads(q["answer_json"])
        if q["kind"] == "short":
            answers[q["id"]] = ["no idea"]
        elif n < 3:  # get the first three wrong
            answers[q["id"]] = [str(i) for i in range(len(json.loads(q["choices_json"]))) if i not in right][:1]
            expected_wrong += 1
        else:
            answers[q["id"]] = [str(i) for i in right]
    pending = quizzes.submit(seeded, aid, answers)
    shorts = [q for q in qs if q["kind"] == "short"]
    assert pending == bool(shorts)
    if shorts:
        quizzes.self_grade(seeded, aid, {shorts[0]["id"]: False})
        expected_wrong += 1
    attempt = seeded.execute("SELECT * FROM quiz_attempts WHERE id = ?", (aid,)).fetchone()
    assert attempt["finished_at"] and attempt["score"] == 10 - expected_wrong
    text, _ = notes_fs.read_note(course, "mistakes")
    assert text.count(f"Quiz #{aid} · miss") == expected_wrong
    assert "**Why I missed it:** " in text and "**Correct:** right" in text and "**My answer:** wrong" in text
    # latest quiz feeds readiness
    assert readiness.for_course(seeded, course["id"])["components"]["Q"] == attempt["score"] / 10


def test_multi_needs_exact_set(seeded, course):
    ids = make_bank(seeded, course)
    q = seeded.execute("SELECT * FROM questions WHERE id = ?", (ids[10],)).fetchone()
    assert quizzes.is_correct(q, ["0", "1"]) is True
    assert quizzes.is_correct(q, ["0"]) is False
    assert quizzes.is_correct(q, ["0", "1", "2"]) is False


def test_double_submit_rejected(seeded, course):
    make_bank(seeded, course)
    aid = quizzes.start(seeded, course["id"], 2, shuffle=False)
    quizzes.submit(seeded, aid, {})
    with pytest.raises(quizzes.QuestionError):
        quizzes.submit(seeded, aid, {})


def test_competency_filter_and_empty_bank(seeded, course):
    with pytest.raises(quizzes.QuestionError):
        quizzes.start(seeded, course["id"], 10)
    with seeded:
        cid = seeded.execute("INSERT INTO competencies(course_id, ord, text) VALUES (?, 0, 'RF')", (course["id"],)).lastrowid
    quizzes.save(seeded, course["id"], {"kind": "mc", "prompt": "tagged", "choices": "*a\nb", "competency_id": str(cid)})
    quizzes.save(seeded, course["id"], {"kind": "mc", "prompt": "untagged", "choices": "*a\nb"})
    aid = quizzes.start(seeded, course["id"], 10, competency_id=cid)
    assert seeded.execute("SELECT total FROM quiz_attempts WHERE id = ?", (aid,)).fetchone()[0] == 1


def test_quiz_flow_via_ui(client):
    for i in range(3):
        r = client.post("/courses/D413/quizzes/questions/new",
                        data={"kind": "mc", "prompt": f"Which band {i}?", "choices": "* 5 GHz\n900 MHz", "explanation": "Wi-Fi"},
                        follow_redirects=False)
        assert r.status_code == 303
    r = client.post("/courses/D413/quizzes/questions/new", data={"kind": "mc", "prompt": "bad", "choices": "a\nb"})
    assert r.status_code == 400 and "Mark the correct choice" in r.text
    r = client.post("/courses/D413/quizzes/start", data={"count": "3", "shuffle": "1"}, follow_redirects=True)
    assert "Submit answers" in r.text
    aid = re.search(r'action="/quizzes/(\d+)/submit"', r.text).group(1)
    qids = re.findall(r'name="q(\d+)" value="0"', r.text)
    data = {f"q{qids[0]}": "0", f"q{qids[1]}": "1"}  # right, wrong, blank
    r = client.post(f"/quizzes/{aid}/submit", data=data, follow_redirects=True)
    assert "1/3" in r.text and "2 misses added to mistakes.md" in r.text
    assert client.get("/courses/D413/notes/mistakes").text.count("<strong>Why I missed it:</strong>") == 2
    assert f"/quizzes/{aid}" in client.get("/courses/D413/quizzes").text
    # search links to the question editor
    hit = re.search(r'href="(/courses/D413/quizzes/questions/\d+)"', client.get("/search?q=band").text).group(1)
    assert client.get(hit).status_code == 200


def test_short_answer_self_grading(seeded, course):
    make_bank(seeded, course)
    aid = quizzes.start(seeded, course["id"], 12, shuffle=True, rng=random.Random(7))
    attempt = seeded.execute("SELECT * FROM quiz_attempts WHERE id = ?", (aid,)).fetchone()
    short = next(q for q in quizzes.attempt_questions(seeded, attempt) if q["kind"] == "short")
    assert quizzes.submit(seeded, aid, {short["id"]: ["the network name"]}) is True
    assert seeded.execute("SELECT finished_at FROM quiz_attempts WHERE id = ?", (aid,)).fetchone()[0] is None
    with pytest.raises(quizzes.QuestionError):
        quizzes.self_grade(seeded, aid, {})
    quizzes.self_grade(seeded, aid, {short["id"]: True})
    row = seeded.execute("SELECT score, finished_at FROM quiz_attempts WHERE id = ?", (aid,)).fetchone()
    assert row["finished_at"] and row["score"] == 1  # only the short one was answered
