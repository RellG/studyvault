"""Practice quizzes: question bank, attempts, grading, and mistakes.md."""
import json
import random

from . import clock, notes_fs

KINDS = {"mc": "Multiple choice", "multi": "Multi-select", "short": "Short answer (self-graded)"}


class QuestionError(ValueError):
    pass


def build(kind: str, prompt: str, choices_text: str, short_answer: str) -> tuple[list[str], object]:
    """Validate a question form. Choices: one per line, correct ones start with '*'.
    Returns (choices, answer) where answer is a list of correct indices, or the model answer for short."""
    if kind not in KINDS:
        raise QuestionError("Pick a question type")
    if not prompt.strip():
        raise QuestionError("The question needs a prompt")
    if kind == "short":
        if not short_answer.strip():
            raise QuestionError("A short-answer question needs a model answer to compare against")
        return [], short_answer.strip()
    choices, correct = [], []
    for line in choices_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("*"):
            correct.append(len(choices))
            line = line[1:].strip()
        choices.append(line)
    if len(choices) < 2:
        raise QuestionError("Give at least two choices, one per line")
    if not correct:
        raise QuestionError("Mark the correct choice with a * at the start of its line")
    if kind == "mc" and len(correct) != 1:
        raise QuestionError("Multiple choice has exactly one correct answer; use multi-select for more")
    return choices, correct


def choices_text(q) -> str:
    choices = json.loads(q["choices_json"])
    answer = json.loads(q["answer_json"])
    return "\n".join(("* " if i in answer else "") + c for i, c in enumerate(choices)) if q["kind"] != "short" else ""


def save(conn, course_id, form: dict, question_id=None, source="manual") -> int:
    choices, answer = build(form.get("kind", ""), form.get("prompt", ""), form.get("choices", ""), form.get("short_answer", ""))
    comp = form.get("competency_id") or None
    vals = (form["kind"], form["prompt"].strip(), json.dumps(choices), json.dumps(answer),
            (form.get("explanation") or "").strip(), int(comp) if comp else None)
    with conn:
        if question_id:
            conn.execute("UPDATE questions SET kind = ?, prompt = ?, choices_json = ?, answer_json = ?, explanation = ?, "
                         "competency_id = ? WHERE id = ? AND course_id = ?", (*vals, question_id, course_id))
            return question_id
        cur = conn.execute("INSERT INTO questions(kind, prompt, choices_json, answer_json, explanation, competency_id, "
                           "course_id, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (*vals, course_id, source, clock.now().isoformat()))
        return cur.lastrowid


def bank(conn, course_id, competency_id=None):
    sql = ("SELECT q.*, k.text AS comp_text FROM questions q LEFT JOIN competencies k ON k.id = q.competency_id "
           "WHERE q.course_id = ?")
    params = [course_id]
    if competency_id:
        sql += " AND q.competency_id = ?"
        params.append(competency_id)
    return conn.execute(sql + " ORDER BY q.id", params).fetchall()


def start(conn, course_id, count: int, competency_id=None, shuffle=True, time_limit_min=None, rng=random) -> int:
    ids = [q["id"] for q in bank(conn, course_id, competency_id)]
    if not ids:
        raise QuestionError("No questions match. Add some to the bank first.")
    if shuffle:
        rng.shuffle(ids)
    ids = ids[:max(1, count)]
    with conn:
        cur = conn.execute("INSERT INTO quiz_attempts(course_id, started_at, total, time_limit_min, question_ids_json) "
                           "VALUES (?, ?, ?, ?, ?)",
                           (course_id, clock.now().isoformat(), len(ids), time_limit_min or None, json.dumps(ids)))
    return cur.lastrowid


def attempt_questions(conn, attempt):
    ids = json.loads(attempt["question_ids_json"])
    rows = {r["id"]: r for r in conn.execute(
        f"SELECT * FROM questions WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
    return [rows[i] for i in ids if i in rows]


def is_correct(q, given) -> bool | None:
    """True/False for mc and multi; None for short (needs self-grading)."""
    answer = json.loads(q["answer_json"])
    if q["kind"] == "short":
        return None
    return sorted(int(g) for g in given) == sorted(answer)


def submit(conn, attempt_id: int, answers: dict[int, list]) -> bool:
    """Record answers. Returns True if some short answers still need self-grading."""
    attempt = conn.execute("SELECT * FROM quiz_attempts WHERE id = ?", (attempt_id,)).fetchone()
    if attempt["finished_at"] or conn.execute("SELECT 1 FROM quiz_answers WHERE attempt_id = ?", (attempt_id,)).fetchone():
        raise QuestionError("This quiz was already submitted")
    pending = False
    with conn:
        for q in attempt_questions(conn, attempt):
            given = answers.get(q["id"], [])
            ok = is_correct(q, given)
            if ok is None:
                pending = True
            conn.execute("INSERT INTO quiz_answers(attempt_id, question_id, given_json, correct) VALUES (?, ?, ?, ?)",
                         (attempt_id, q["id"], json.dumps(given), None if ok is None else int(ok)))
    if not pending:
        finish(conn, attempt_id)
    return pending


def self_grade(conn, attempt_id: int, marks: dict[int, bool]) -> None:
    with conn:
        for qid, ok in marks.items():
            conn.execute("UPDATE quiz_answers SET correct = ? WHERE attempt_id = ? AND question_id = ? AND correct IS NULL",
                         (int(ok), attempt_id, qid))
    if conn.execute("SELECT 1 FROM quiz_answers WHERE attempt_id = ? AND correct IS NULL", (attempt_id,)).fetchone():
        raise QuestionError("Mark every short answer right or wrong")
    finish(conn, attempt_id)


def _fmt_given(q, given) -> str:
    if q["kind"] == "short":
        return given[0] if given and str(given[0]).strip() else "(blank)"
    choices = json.loads(q["choices_json"])
    return "; ".join(choices[int(i)] for i in given if 0 <= int(i) < len(choices)) or "(no answer)"


def _fmt_answer(q) -> str:
    answer = json.loads(q["answer_json"])
    if q["kind"] == "short":
        return answer
    choices = json.loads(q["choices_json"])
    return "; ".join(choices[i] for i in answer)


def finish(conn, attempt_id: int) -> None:
    attempt = conn.execute("SELECT * FROM quiz_attempts WHERE id = ?", (attempt_id,)).fetchone()
    rows = conn.execute("SELECT * FROM quiz_answers WHERE attempt_id = ?", (attempt_id,)).fetchall()
    score = sum(1 for r in rows if r["correct"])
    with conn:
        conn.execute("UPDATE quiz_attempts SET score = ?, finished_at = ? WHERE id = ?",
                     (score, clock.now().isoformat(), attempt_id))
    append_mistakes(conn, attempt, rows)


def append_mistakes(conn, attempt, answer_rows) -> int:
    wrong = [r for r in answer_rows if r["correct"] == 0]
    if not wrong:
        return 0
    course = conn.execute("SELECT c.*, t.n AS term_n FROM courses c LEFT JOIN terms t ON t.id = c.term_id WHERE c.id = ?",
                          (attempt["course_id"],)).fetchone()
    qs = {q["id"]: q for q in attempt_questions(conn, attempt)}
    comp_text = {r["id"]: r["text"] for r in conn.execute("SELECT id, text FROM competencies WHERE course_id = ?", (course["id"],))}
    day = clock.today().isoformat()
    blocks = []
    for n, r in enumerate(wrong, 1):
        q = qs.get(r["question_id"])
        if not q:
            continue
        given = json.loads(r["given_json"])
        lines = [f"### {day} · Quiz #{attempt['id']} · miss {n} of {len(wrong)}", ""]
        if q["competency_id"] in comp_text:
            lines += [f"*Competency:* {comp_text[q['competency_id']]}", ""]
        lines += [f"**Question:** {q['prompt']}", "",
                  f"**My answer:** {_fmt_given(q, given)}  ",
                  f"**Correct:** {_fmt_answer(q)}  ",
                  f"**Explanation:** {q['explanation'] or '—'}", "",
                  "**Why I missed it:** ", ""]
        blocks.append("\n".join(lines))
    notes_fs.append_note(conn, course, "mistakes", "\n".join(blocks))
    return len(blocks)


def history(conn, course_id):
    return conn.execute("SELECT * FROM quiz_attempts WHERE course_id = ? AND finished_at IS NOT NULL "
                        "ORDER BY finished_at DESC", (course_id,)).fetchall()
