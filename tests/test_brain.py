"""Build from notes (Slice 12, step 2): analysis, the draft, applying it, the background job, the pages and the agent hooks.
The model is faked: it reads the section keys out of each prompt and answers in the format the real one is asked for."""
import json
import re
import time

import httpx
import pytest
from starlette.datastructures import FormData

from app import agent, ai, brain, cards, catalog, chunks, competencies, db, notes_fs, quizzes
from app.config import settings

NOTES = """# D413 · Telecomm: notebook

#### PreAssessment

- Bandwidth
- Attenuation
- Jitter buffers
- CIDR notation
- Multiplexing

## Wired transmission
Ethernet, fiber and coax differ in medium, reach and cost. Fiber carries light and resists interference, coax is shielded copper,
and twisted pair is cheap but limited to about 100 metres per segment.

## Wireless
Wi-Fi 2.4 GHz channels 1, 6 and 11 do not overlap. 5 GHz offers more channels but a shorter range, and walls absorb it faster.
Bluetooth is short range and low power.
"""


def reply(obj):
    text = obj if isinstance(obj, str) else json.dumps(obj)
    return httpx.Response(200, json={"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]})


class Fake:
    """The provider. Knobs: `fail_parts` (generation calls, 1-based, that return an error), `extra` (added to each reply)."""
    def __init__(self):
        self.prompts, self.gen_calls = [], 0
        self.fail_parts, self.fail_all, self.bogus = set(), None, False

    def __call__(self, request):
        prompt = json.loads(request.content)["messages"][0]["content"]
        self.prompts.append(prompt)
        keys = re.findall(r'<section key="([^"]+)"', prompt)
        if prompt.startswith("Analyze my notes"):
            return reply(self.analysis(keys))
        if prompt.startswith("Outline these sections"):
            return reply({"outline": self.analysis(keys)["outline"]})
        if prompt.startswith("Here is an outline"):
            a = self.analysis(re.findall(r"^- (\S+):", prompt, re.M))
            return reply({k: a[k] for k in ("summary", "objectives", "competencies", "thin_spots")})
        if prompt.startswith("Write flashcards"):
            self.gen_calls += 1
            if self.fail_all or self.gen_calls in self.fail_parts:
                return httpx.Response(400, json={"error": {"message": self.fail_all or "bad request"}})
            return reply(self.generation(keys))
        raise AssertionError("unexpected prompt: " + prompt[:60])

    def analysis(self, keys):
        outline = [{"key": k, "title": k.split("#")[1].replace("-", " ").title(), "summary": f"What {k} says.",
                    "terms": ["a", "b"], "importance": 3 if "wired" in k else 2} for k in keys]
        if self.bogus:
            outline = [{"key": "notebook#not-real", "title": "x", "summary": "x"}] + outline[:1]  # one real, one invented, rest missing
        return {"summary": "These notes cover transmission media.", "outline": outline,
                "objectives": ["Define telecomm"], "thin_spots": [{"key": keys[-1], "why": "Only a few lines"}, {"key": "nope", "why": "x"}],
                "competencies": [{"text": "Compare wired media", "keys": [k for k in keys if "wired" in k] + ["nope"]},
                                 {"text": "Choose Wi-Fi channels", "keys": [k for k in keys if "wireless" in k]},
                                 {"text": "compare wired media", "keys": []}]}  # a duplicate, differently cased

    def generation(self, keys):
        cards_, qs = [], []
        for k in keys:
            cards_ += [{"section": k, "front": f"What about {k}?", "back": "The answer.", "source": "notes"},
                       {"section": k, "front": f"Invented fact for {k}?", "back": "From nowhere.", "source": "general"},
                       {"section": "bogus#key", "front": "Orphan?", "back": "x", "source": "notes"}]
            qs += [{"section": k, "kind": "mc", "prompt": f"Which is true of {k}?", "choices": ["A", "B", "C", "D"], "correct": [1],
                    "explanation": "Because.", "source": "notes"},
                   {"section": k, "kind": "mc", "prompt": f"Broken question about {k}", "choices": ["A"], "correct": [5], "source": "notes"}]
        if "preassessment" in " ".join(keys):  # a topic list: general-knowledge answers are allowed, flagged
            cards_.append({"section": "notebook#preassessment", "front": "What is bandwidth?", "back": "Capacity.", "source": "general"})
        return {"cards": cards_, "questions": qs}


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(ai, "_transport", httpx.MockTransport(f))
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    monkeypatch.setattr(brain, "CALL_GAP_S", 0)
    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    monkeypatch.setattr(settings, "ai_model", "m")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
    return f


@pytest.fixture
def d413(seeded):
    notes_fs.ensure_repo()
    c = catalog.get_course(seeded, "D413")
    notes_fs.ensure_course_files(seeded, c)
    notes_fs.write_note(seeded, c, "notebook", NOTES)
    return c


# ---------------------------------------------------------------- reading the notes

def test_topic_lists_are_told_apart_from_explanations():
    assert brain.is_topic_list("- Bandwidth\n- Attenuation\n- Jitter\n- CIDR\n- OSPF")
    assert not brain.is_topic_list("- OSPF is a link-state routing protocol that floods LSAs to every router in an area and "
                                   "then runs Dijkstra\n- BGP is a path-vector protocol between autonomous systems and is used on the internet")
    assert not brain.is_topic_list("Just a paragraph about OSPF.\nAnother line.")
    assert brain.topic_terms("- Bandwidth\n  - Attenuation\n* Jitter buffers\nnot a bullet") == ["Bandwidth", "Attenuation", "Jitter buffers"]


def test_sections_skip_stray_words_and_template_text(seeded, d413):
    notes_fs.write_note(seeded, d413, "overview", "# D413\n\n**Assessment type:** set it on the course's Edit page\n\n## Stray\nok\n")
    notes_fs.write_note(seeded, d413, "mistakes", "## M\nA slip that is long enough to count as a section on its own.\n")
    keys = [r["key"] for r in brain.sections(seeded, d413["id"])]
    assert keys == ["notebook#preassessment", "notebook#wired-transmission", "notebook#wireless"]  # no overview bits, no mistakes


# ---------------------------------------------------------------- analysis

def test_analyze_stores_a_checked_profile(seeded, d413, fake):
    prof = brain.analyze(seeded, d413)
    assert prof["summary"].startswith("These notes cover")
    assert [o["key"] for o in prof["outline"]] == ["notebook#preassessment", "notebook#wired-transmission", "notebook#wireless"]
    assert [c["text"] for c in prof["competencies"]] == ["Compare wired media", "Choose Wi-Fi channels"]  # duplicate dropped
    assert prof["competencies"][0]["keys"] == ["notebook#wired-transmission"]  # the invented key is dropped
    assert prof["thin_spots"] == [{"key": "notebook#wireless", "why": "Only a few lines"}]
    assert prof["topic_lists"] == [{"key": "notebook#preassessment", "where": "PreAssessment",
                                    "terms": ["Bandwidth", "Attenuation", "Jitter buffers", "CIDR notation", "Multiplexing"]}]
    got = brain.get_profile(seeded, d413["id"])
    assert got["status"] == "fresh" and got["profile"] == prof
    notes_fs.write_note(seeded, d413, "notebook", NOTES + "\n## New\nSomething else that was added later to the notes.\n")
    assert brain.get_profile(seeded, d413["id"])["status"] == "stale"
    assert len(fake.prompts) == 1 and "<section" in fake.prompts[0]


def test_analyze_fills_in_sections_the_model_skipped_or_invented(seeded, d413, fake):
    fake.bogus = True
    prof = brain.analyze(seeded, d413)
    assert [o["key"] for o in prof["outline"]] == ["notebook#preassessment", "notebook#wired-transmission", "notebook#wireless"]
    assert prof["outline"][2]["title"] == "Wireless" and prof["outline"][2]["importance"] == 2  # filled from the notes


def test_long_notes_are_outlined_in_batches_first(seeded, d413, fake, monkeypatch):
    monkeypatch.setattr(brain, "ANALYZE_CHARS", 300)
    prof = brain.analyze(seeded, d413)
    kinds = [p.split(" ")[0] for p in fake.prompts]
    assert kinds.count("Outline") >= 2 and kinds[-1] == "Here" and "Analyze" not in kinds
    assert [o["key"] for o in prof["outline"]] == ["notebook#preassessment", "notebook#wired-transmission", "notebook#wireless"]
    assert prof["competencies"]


def test_analyze_needs_notes_and_a_working_provider(seeded, fake):
    c = catalog.get_course(seeded, "D281")
    with pytest.raises(brain.BrainError, match="write some notes"):
        brain.analyze(seeded, c)


# ---------------------------------------------------------------- the draft

def test_build_drafts_from_the_notes_only(seeded, d413, fake):
    competencies.import_list(seeded, d413, ["Compare wired media"])  # already there: not offered again
    cards.add(seeded, d413["id"], "What about notebook#wireless?", "Already have it")
    draft_id = brain.build(seeded, d413)
    d = seeded.execute("SELECT * FROM brain_drafts WHERE id = ?", (draft_id,)).fetchone()
    p = json.loads(d["payload_json"])
    assert [c["text"] for c in p["competencies"]] == ["Choose Wi-Fi channels"]
    fronts = [c["front"] for c in p["cards"]]
    assert "What about notebook#wired-transmission?" in fronts and "What about notebook#wireless?" not in fronts  # not twice
    assert not any(f in fronts for f in ("Invented fact for notebook#wired-transmission?", "Invented fact for notebook#wireless?", "Orphan?"))
    # ^ answers from general knowledge are dropped from sections that explain things, and so are cards for unknown sections
    topic = [c for c in p["cards"] if c["section"] == "notebook#preassessment"]
    assert {c["front"] for c in topic} == {"What about notebook#preassessment?", "Invented fact for notebook#preassessment?", "What is bandwidth?"}
    assert {c["general"] for c in topic} == {False, True}  # a topic list may be answered from general knowledge, and is flagged
    assert [q["prompt"] for q in p["questions"] if q["section"] == "notebook#wireless"] == ["Which is true of notebook#wireless?"]
    assert all(q["choices_text"].count("* ") == 1 for q in p["questions"])
    assert p["hashes"].keys() == {"notebook#preassessment", "notebook#wired-transmission", "notebook#wireless"} and p["overview"]
    assert d["status"] == "pending" and d["notes_hash"] == chunks.notes_hash(seeded, d413["id"])
    # nothing was added to the course yet
    assert seeded.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1 and seeded.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 0
    # one analysis call, then generation; and a later build reuses the fresh analysis
    assert [p_.split(" ")[0] for p_ in fake.prompts] == ["Analyze", "Write"]
    brain.build(seeded, d413)
    assert [p_.split(" ")[0] for p_ in fake.prompts] == ["Analyze", "Write", "Write"]
    assert seeded.execute("SELECT COUNT(*) FROM brain_drafts WHERE status = 'pending'").fetchone()[0] == 1  # the older one was replaced


def test_generation_prompt_enforces_the_notes(seeded, d413, fake):
    brain.build(seeded, d413)
    gen = fake.prompts[-1]
    assert 'must be "notes"' in gen and "a list of topics to learn" in gen  # per-section rules
    assert "only lists learning objectives" in gen  # no cards made out of a bare objectives list
    assert gen.index("notebook#wired-transmission") < gen.index("notebook#wireless")


def test_a_failed_part_costs_only_that_part(seeded, d413, fake, monkeypatch):
    monkeypatch.setattr(brain, "BATCH_CHARS", 100)  # one section per call
    fake.fail_parts = {2}
    p = json.loads(seeded.execute("SELECT payload_json FROM brain_drafts WHERE id = ?", (brain.build(seeded, d413),)).fetchone()[0])
    assert fake.gen_calls == 3 and len(p["warnings"]) == 1 and "Part 2" in p["warnings"][0]
    assert {c["section"] for c in p["cards"]} == {"notebook#preassessment", "notebook#wireless"}


def test_everything_failing_is_an_error_not_an_empty_draft(seeded, d413, fake, monkeypatch):
    monkeypatch.setattr(brain, "BATCH_CHARS", 100)
    fake.fail_all = "nope"
    with pytest.raises(brain.BrainError, match="Nothing could be drafted"):
        brain.build(seeded, d413)
    assert seeded.execute("SELECT COUNT(*) FROM brain_drafts").fetchone()[0] == 0


def test_study_plan_is_spread_up_to_the_exam(d413):
    prof = {"outline": [{"key": f"notebook#s{i}", "title": f"S{i}", "summary": "", "importance": 3 if i % 2 else 1, "terms": []}
                        for i in range(5)], "thin_spots": [{"key": "notebook#s1", "why": "short"}]}
    from datetime import date
    course = {"code": "D413", "exam_date": "2026-10-25", "target": None, "due": None}
    tasks = brain.plan_tasks(course, prof, today=date(2026, 10, 5))
    assert [t["kind"] for t in tasks] == ["read"] * 5 + ["review_cards", "quiz"]
    assert [t["done_rule"] for t in tasks][-2:] == ["cards_reviewed", "quiz_finished"]
    dates = [t["due_on"] for t in tasks]
    assert dates == sorted(dates) and dates[0] > "2026-10-05" and dates[-1] == "2026-10-24" and max(dates) <= "2026-10-25"
    assert tasks[1]["priority"] == 1 and tasks[0]["priority"] == 2 and tasks[1]["note"]
    undated = brain.plan_tasks({"code": "D413", "exam_date": None, "target": None, "due": None}, prof, today=date(2026, 10, 5))
    assert {t["due_on"] for t in undated} == {None}
    many = {"outline": [{"key": f"k{i}", "title": f"T{i}", "summary": "", "importance": 2, "terms": []} for i in range(40)], "thin_spots": []}
    assert len(brain.plan_tasks({"code": "D413", "exam_date": None, "target": None, "due": None}, many)) == brain.MAX_TASKS


# ---------------------------------------------------------------- applying it

def tick(**kw):
    out = []
    for name, vals in kw.items():
        out += [(name, str(v)) for v in vals]
    return out


def test_apply_adds_only_what_is_ticked_and_links_it(seeded, d413, fake):
    draft = seeded.execute("SELECT * FROM brain_drafts WHERE id = ?", (brain.build(seeded, d413),)).fetchone()
    p = json.loads(draft["payload_json"])
    i_wired = next(i for i, c in enumerate(p["cards"]) if c["section"] == "notebook#wired-transmission" and not c["general"])
    q_wired = next(i for i, q in enumerate(p["questions"]) if q["section"] == "notebook#wired-transmission")
    form = FormData(tick(comp=[0, 1], card=[i_wired], question=[q_wired], task=[0, len(p["tasks"]) - 1]) + [
        (f"front_{i_wired}", "Which media carry light?"), (f"back_{i_wired}", "Fiber"), ("overview", "1"),
        ("title_0", "My own title"), ("due_0", "2026-10-20")])
    before = notes_fs.read_note(d413, "notebook")[0]
    result = brain.apply_draft(seeded, d413, draft, form)
    assert result == "Added 2 competencies, 1 card, 1 question, 2 tasks, an overview summary."
    assert [r["text"] for r in competencies.list_for(seeded, d413["id"])] == ["Compare wired media", "Choose Wi-Fi channels"]
    card = seeded.execute("SELECT * FROM cards").fetchone()
    assert (card["front"], card["back"], card["source"], card["source_key"]) == ("Which media carry light?", "Fiber", "ai-accepted", "notebook#wired-transmission")
    assert card["source_hash"] == p["hashes"]["notebook#wired-transmission"]
    wired_comp = seeded.execute("SELECT id FROM competencies WHERE text = 'Compare wired media'").fetchone()[0]
    assert card["competency_id"] == wired_comp  # linked through the section the competency came from
    q = seeded.execute("SELECT * FROM questions").fetchone()
    assert q["source_key"] == "notebook#wired-transmission" and q["competency_id"] == wired_comp
    tasks = seeded.execute("SELECT title, due_on, done_rule, source FROM study_tasks ORDER BY id").fetchall()
    assert [t["title"] for t in tasks] == ["My own title", f"Take a D413 practice quiz"] and tasks[0]["due_on"] == "2026-10-20"
    assert tasks[1]["done_rule"] == "quiz_finished" and tasks[0]["source"] == f"brain:{draft['id']}"
    overview = notes_fs.read_note(d413, "overview")[0]
    assert "## From my notes ·" in overview and "These notes cover" in overview
    assert notes_fs.read_note(d413, "notebook")[0] == before  # the student's own notes are untouched
    assert seeded.execute("SELECT status FROM brain_drafts").fetchone()[0] == "applied"
    with pytest.raises(brain.ApplyError, match="already"):
        brain.apply_draft(seeded, d413, seeded.execute("SELECT * FROM brain_drafts").fetchone(), form)


def test_apply_with_nothing_ticked_changes_nothing(seeded, d413, fake):
    draft = seeded.execute("SELECT * FROM brain_drafts WHERE id = ?", (brain.build(seeded, d413),)).fetchone()
    with pytest.raises(brain.ApplyError, match="Nothing was ticked"):
        brain.apply_draft(seeded, d413, draft, FormData([]))
    assert seeded.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
    assert seeded.execute("SELECT status FROM brain_drafts").fetchone()[0] == "pending"
    brain.dismiss(seeded, draft)
    assert seeded.execute("SELECT status FROM brain_drafts").fetchone()[0] == "dismissed"


def test_items_go_stale_if_the_notes_changed_while_the_draft_waited(seeded, d413, fake):
    draft = seeded.execute("SELECT * FROM brain_drafts WHERE id = ?", (brain.build(seeded, d413),)).fetchone()
    notes_fs.write_note(seeded, d413, "notebook", NOTES.replace("Wi-Fi 2.4 GHz channels 1, 6 and 11", "Wi-Fi 2.4 GHz channels 1, 5 and 9"))
    p = json.loads(draft["payload_json"])
    i = next(i for i, c in enumerate(p["cards"]) if c["section"] == "notebook#wireless")
    brain.apply_draft(seeded, d413, draft, FormData([("card", str(i))]))
    assert chunks.freshness(seeded, d413["id"])["cards"] == {"total": 1, "unlinked": 0, "current": 0, "stale": 1, "missing": 0}


# ---------------------------------------------------------------- background job

def wait_done(code, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        p = brain.progress(code)
        if p and p["done"]:
            return p
        time.sleep(0.05)
    raise AssertionError("job didn't finish")


@pytest.fixture(autouse=True)
def _no_old_job():
    brain._job = {}
    yield
    brain._job = {}


def test_job_runs_in_the_background_and_leaves_a_draft(client, fake):
    conn = db.connect()
    try:
        c = catalog.get_course(conn, "D413")
        notes_fs.ensure_repo()
        notes_fs.ensure_course_files(conn, c)
        notes_fs.write_note(conn, c, "notebook", NOTES)
        state = brain.start(c, "build", "light")
        assert brain.busy() == "D413"
        with pytest.raises(brain.BrainError, match="Already working on D413"):
            brain.start(c, "build")
        done = wait_done("D413")
        assert done["error"] is None and done["draft_id"] and brain.busy() is None
        assert brain.pending_draft(conn, c["id"])["id"] == done["draft_id"] and state["steps"]
    finally:
        conn.close()


def test_job_errors_are_reported_not_raised(client, fake):
    conn = db.connect()
    try:
        c = catalog.get_course(conn, "D413")
        notes_fs.ensure_repo()
        notes_fs.ensure_course_files(conn, c)
        notes_fs.write_note(conn, c, "notebook", NOTES)
        fake.fail_all = "boom"
        brain.start(c, "build")
        done = wait_done("D413")
        assert "Nothing could be drafted" in done["error"] and done["draft_id"] is None
        assert brain.pending_draft(conn, c["id"]) is None
        brain.start(c, "analyze")  # a finished job doesn't block the next one
        assert wait_done("D413")["error"] is None and brain.get_profile(conn, c["id"])
    finally:
        conn.close()


def test_job_needs_ai_on(seeded, d413, monkeypatch):
    monkeypatch.setattr(settings, "ai_provider", "")
    with pytest.raises(brain.BrainError, match="AI features are off"):
        brain.start(d413, "build")


# ---------------------------------------------------------------- pages

def test_pages_end_to_end(client, fake):
    conn = db.connect()
    try:
        c = catalog.get_course(conn, "D413")
        notes_fs.ensure_repo()
        notes_fs.ensure_course_files(conn, c)
        assert "No notes to work from yet" in client.get("/courses/D413/brain").text
        notes_fs.write_note(conn, c, "notebook", NOTES)
        page = client.get("/courses/D413/brain").text
        assert "From notes" in page and "Wired transmission" in page and "Not analyzed" in page and "Build from notes" in page
        assert "From your notes" in client.get("/courses/D413").text and "Build from notes" in client.get("/courses/D413").text
        r = client.post("/courses/D413/brain/run", data={"action": "build", "depth": "normal"}, follow_redirects=False)
        assert r.status_code == 303
        assert client.post("/courses/D413/brain/run", data={"action": "wat"}).status_code == 400
        wait_done("D413")
        page = client.get("/courses/D413/brain").text
        assert "Review draft" in page and "These notes cover transmission media." in page and "core" in page and "thin" in page
        draft = brain.pending_draft(conn, c["id"])
        d = client.get(f"/courses/D413/brain/draft/{draft['id']}")
        assert d.status_code == 200 and "Add ticked items" in d.text and "What about notebook#wireless?" in d.text and ">Check</span>" in d.text
        assert client.get(f"/courses/D413/brain/draft/{draft['id'] + 99}").status_code == 404
        r = client.post(f"/courses/D413/brain/draft/{draft['id']}/apply", data={"card": "0", "comp": "0"}, follow_redirects=False)
        assert r.status_code == 303 and "applied=Added" in r.headers["location"]
        assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1
        page = client.get(r.headers["location"]).text
        assert "Added 1 competency, 1 card." in page
        # the sections table now shows coverage, and applying twice is refused
        assert client.post(f"/courses/D413/brain/draft/{draft['id']}/apply", data={"card": "1"}, follow_redirects=True).text.count("already handled") == 1
        assert client.get("/courses/D413/brain/progress").json()["done"] is True
    finally:
        conn.close()


def test_pages_without_ai(client, monkeypatch):
    monkeypatch.setattr(settings, "ai_provider", "")
    conn = db.connect()
    try:
        c = catalog.get_course(conn, "D413")
        notes_fs.ensure_repo()
        notes_fs.ensure_course_files(conn, c)
        notes_fs.write_note(conn, c, "notebook", NOTES)
        page = client.get("/courses/D413/brain").text
        assert "Wired transmission" in page and "AI is off" in page and "Build from notes</button>" not in page
        r = client.post("/courses/D413/brain/run", data={"action": "build"})
        assert r.status_code == 409 and "AI features are off" in r.text
    finally:
        conn.close()


# ---------------------------------------------------------------- the agent

def ctx(conn, course_code=None):
    cid = catalog.get_course(conn, course_code)["id"] if course_code else None
    tid = agent.create_thread(conn, "t", cid)
    return agent.Ctx(conn, conn.execute("SELECT * FROM agent_threads WHERE id = ?", (tid,)).fetchone())


def call(k, tool, **args):
    return json.loads(agent.run_tool(k, ai.Call("", tool, args)))


def test_agent_knows_what_the_notes_cover(seeded, d413, fake):
    k = ctx(seeded, "D413")
    out = call(k, "get_course_brain", code="D413")
    assert out["analyzed"] is False and "propose_build_from_notes" in out["note"]
    assert "What this course's notes cover" not in agent.system_prompt(seeded, k.thread)
    brain.analyze(seeded, d413)
    out = call(k, "get_course_brain", code="D413")
    assert out["analyzed"] and out["up_to_date"] and out["summary"].startswith("These notes cover")
    assert [s["key"] for s in out["sections"]][1] == "notebook#wired-transmission" and out["sections"][2]["thin"] == "Only a few lines"
    assert out["topic_lists"][0]["terms"][0] == "Bandwidth"
    assert call(k, "get_course_brain", code="D281") == {"analyzed": False, "note": "No notes written in this course yet."}
    system = agent.system_prompt(seeded, k.thread)  # a course chat starts out knowing the layout of the notes
    assert "What this course's notes cover" in system and "notebook#wired-transmission" in system and "propose_build_from_notes" in system
    assert "What this course's notes cover" not in agent.system_prompt(seeded, ctx(seeded).thread)  # not in general chats


def test_agent_offers_a_build_and_apply_starts_it(seeded, d413, fake, monkeypatch):
    k = ctx(seeded)
    assert "error" in call(k, "propose_build_from_notes", code="D281")  # no notes
    out = call(k, "propose_build_from_notes", code="D413", depth="thorough")
    assert out["summary"] == "build D413 from its notes (thorough)"
    started = []
    monkeypatch.setattr(brain, "start", lambda course, kind, depth: started.append((course["code"], kind, depth)))
    p = seeded.execute("SELECT * FROM agent_proposals WHERE id = ?", (out["proposal_id"],)).fetchone()
    result, link = agent.apply(seeded, p, FormData([("depth", "light")]))
    assert started == [("D413", "build", "light")] and link == "/courses/D413/brain" and "Started building D413" in result
    assert agent.TOOLS["propose_build_from_notes"]["kind"] == "write" and agent.TOOLS["get_course_brain"]["kind"] == "read"


def test_agent_apply_reports_a_busy_job(seeded, d413, monkeypatch):
    out = call(ctx(seeded), "propose_build_from_notes", code="D413")
    p = seeded.execute("SELECT * FROM agent_proposals WHERE id = ?", (out["proposal_id"],)).fetchone()

    def busy(*a):
        raise brain.BrainError("Already working on D282.")
    monkeypatch.setattr(brain, "start", busy)
    with pytest.raises(agent.ApplyError, match="Already working"):
        agent.apply(seeded, p, FormData([]))
    assert seeded.execute("SELECT status FROM agent_proposals").fetchone()[0] == "pending"  # still there to try again
