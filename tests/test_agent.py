"""Study agent (PLAN Slice 11): tool-calling adapters, the loop, guardrails, proposals and the Ask pages."""
import json

import httpx
import pytest
from starlette.datastructures import FormData

from app import agent, ai, catalog, competencies, db, notes_fs
from app.config import settings


class Api:
    """Fake provider: replies in order, the last one repeats. Captures request bodies."""
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
    return a


def use(monkeypatch, provider="anthropic"):
    monkeypatch.setattr(settings, "ai_provider", provider)
    monkeypatch.setattr(settings, "ai_model", "model-from-env")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test" if provider == "anthropic" else "")
    monkeypatch.setattr(settings, "google_api_key", "g-test" if provider == "google" else "")


def a_calls(*calls):
    return httpx.Response(200, json={"stop_reason": "tool_use", "content": [
        {"type": "tool_use", "id": f"tu{i}", "name": n, "input": args} for i, (n, args) in enumerate(calls)]})


def a_text(text):
    return httpx.Response(200, json={"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]})


def g_call(name, args):
    return httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": [
        {"functionCall": {"name": name, "args": args, "id": "call_1"}, "thoughtSignature": "SIG"}]}}]})


def g_text(text):
    return httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}]})


@pytest.fixture
def tid(seeded):
    return agent.create_thread(seeded, "test chat")


def ctx(conn, tid):
    return agent.Ctx(conn, conn.execute("SELECT * FROM agent_threads WHERE id = ?", (tid,)).fetchone())


def message(conn, mid):
    m = conn.execute("SELECT * FROM agent_messages WHERE id = ?", (mid,)).fetchone()
    return m["text"], json.loads(m["meta_json"])


# ---------------------------------------------------------------- adapters

def test_google_round_trip_replays_thought_signature(seeded, tid, api, monkeypatch):
    use(monkeypatch, "google")
    api.replies = [g_call("get_course", {"code": "D413"}), g_text("D413 has no readiness score yet.")]
    mid = agent.reply(seeded, tid, "How is D413 going?")
    first, second = api.bodies
    decls = {d["name"]: d for d in first["tools"][0]["functionDeclarations"]}
    assert set(decls) == set(agent.TOOLS) and "parameters" not in decls["get_overview"]
    assert "Today is" in first["systemInstruction"]["parts"][0]["text"]
    assert second["contents"][-2] == {"role": "model", "parts": [
        {"functionCall": {"name": "get_course", "args": {"code": "D413"}, "id": "call_1"}, "thoughtSignature": "SIG"}]}
    resp = second["contents"][-1]["parts"][0]["functionResponse"]
    assert resp["id"] == "call_1" and resp["name"] == "get_course" and "Telecomm" in resp["response"]["result"]
    assert message(seeded, mid) == ("D413 has no readiness score yet.", {"tools": ["D413 course"]})


def test_anthropic_round_trip(seeded, tid, api, monkeypatch):
    use(monkeypatch)
    api.replies = [a_calls(("search_notes", {"query": "subnet"})), a_text("Nothing on subnets yet.")]
    agent.reply(seeded, tid, "What do my notes say about subnets?")
    first, second = api.bodies
    assert {t["name"] for t in first["tools"]} == set(agent.TOOLS)
    assert all(t["input_schema"]["type"] == "object" for t in first["tools"])
    assert second["messages"][-2]["role"] == "assistant" and second["messages"][-2]["content"][0]["type"] == "tool_use"
    result = second["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "tu0" and "No matches" in result["content"]


def test_later_turns_resend_only_final_text(seeded, tid, api, monkeypatch):
    use(monkeypatch)
    api.replies = [a_calls(("get_overview", {})), a_text("You're on pace.")]
    agent.reply(seeded, tid, "Am I on pace?")
    api.replies = [a_text("Sure.")]
    agent.reply(seeded, tid, "Thanks")
    assert api.bodies[-1]["messages"] == [{"role": "user", "content": "Am I on pace?"},
                                          {"role": "assistant", "content": "You're on pace."},
                                          {"role": "user", "content": "Thanks"}]


# ---------------------------------------------------------------- loop and guardrails

def test_step_cap(seeded, tid, api, monkeypatch):
    use(monkeypatch)
    api.replies = [a_calls(("get_overview", {}))]  # never stops calling
    text, meta = message(seeded, agent.reply(seeded, tid, "loop forever"))
    assert len(api.bodies) == agent.MAX_ROUNDS and "limit" in text and meta["tools"] == ["progress overview"]


def test_bad_calls_go_back_to_the_model(seeded, tid, api, monkeypatch):
    use(monkeypatch)
    api.replies = [a_calls(("read_note", {"code": "D413", "name": "pa/task1"}), ("delete_everything", {}),
                           ("get_course", {}), ("get_course", {"code": "Z999"})), a_text("ok")]
    agent.reply(seeded, tid, "try things")
    results = [r["content"] for r in api.bodies[1]["messages"][-1]["content"]]
    assert "off limits" in results[0] and "no action" in results[1]
    assert "Missing code" in results[2] and "No course" in results[3]


def test_pa_drafts_never_reach_the_model(seeded, tid):
    with seeded:
        seeded.execute("UPDATE courses SET assessment_type = 'PA' WHERE code = 'D339'")
    c = catalog.get_course(seeded, "D339")
    notes_fs.write_note(seeded, c, "pa/task1", "zebracorn draft answer")
    notes_fs.write_note(seeded, c, "notebook", "zebracorn concept notes")
    out = json.loads(agent.run_tool(ctx(seeded, tid), ai.Call("", "search_notes", {"query": "zebracorn"})))
    assert [r["link"] for r in out["results"]] == ["/courses/D339/notes/notebook"]


def test_ai_error_is_stored_and_the_message_kept(seeded, tid, api, monkeypatch):
    use(monkeypatch)
    api.replies = [httpx.Response(429, json={})]
    text, meta = message(seeded, agent.reply(seeded, tid, "first try"))
    assert text == "" and "busy" in meta["error"]
    api.replies = [a_text("ok")]
    agent.reply(seeded, tid, "second try")
    assert api.bodies[-1]["messages"] == [{"role": "user", "content": "first try\n\nsecond try"}]


# ---------------------------------------------------------------- proposals

def test_write_is_only_a_proposal_until_apply(seeded, tid, api, monkeypatch):
    use(monkeypatch)
    count = lambda: seeded.execute("SELECT COUNT(*) FROM cards").fetchone()[0]  # noqa: E731
    before = count()
    api.replies = [a_calls(("add_flashcards", {"code": "D413", "cards": [{"front": "SSH port?", "back": "22"},
                                                                        {"front": "", "back": "x"}]})),
                   a_text("I've proposed 1 card.")]
    mid = agent.reply(seeded, tid, "make a card")
    assert count() == before
    p = seeded.execute("SELECT * FROM agent_proposals").fetchone()
    assert p["status"] == "pending" and p["message_id"] == mid and len(json.loads(p["payload_json"])["cards"]) == 1
    result, link = agent.apply(seeded, p, FormData([("keep", "0"), ("front_0", "SSH port?"), ("back_0", "22 (TCP)")]))
    assert result == "Added 1 card to D413." and link == "/courses/D413/cards" and count() == before + 1
    assert tuple(seeded.execute("SELECT back, source FROM cards ORDER BY id DESC").fetchone()) == ("22 (TCP)", "ai-accepted")
    p = seeded.execute("SELECT * FROM agent_proposals").fetchone()
    assert p["status"] == "applied"
    with pytest.raises(agent.ApplyError):
        agent.apply(seeded, p, FormData([("keep", "0"), ("front_0", "a"), ("back_0", "b")]))


def test_confidence_and_study_time_proposals(seeded, tid):
    c = catalog.get_course(seeded, "D413")
    competencies.import_list(seeded, c, ["Explain the OSI model"])
    cid = competencies.list_for(seeded, c["id"])[0]["id"]
    k = ctx(seeded, tid)
    out = json.loads(agent.run_tool(k, ai.Call("", "set_confidence", {"code": "D413", "competency_id": cid, "confidence": 4})))
    p = seeded.execute("SELECT * FROM agent_proposals WHERE id = ?", (out["proposal_id"],)).fetchone()
    agent.apply(seeded, p, FormData([("confidence", "3")]))  # the user's edit wins
    assert competencies.list_for(seeded, c["id"])[0]["confidence"] == 3
    bad = json.loads(agent.run_tool(k, ai.Call("", "log_study_time", {"date": "2999-01-01", "minutes": 30})))
    assert "future" in bad["error"]
    out = json.loads(agent.run_tool(k, ai.Call("", "log_study_time", {"date": "2026-09-01", "minutes": 45, "code": "D413"})))
    p = seeded.execute("SELECT * FROM agent_proposals WHERE id = ?", (out["proposal_id"],)).fetchone()
    agent.apply(seeded, p, FormData([("date", "2026-09-01"), ("minutes", "50")]))
    assert seeded.execute("SELECT minutes FROM sessions").fetchone()[0] == 50


def test_every_read_tool_runs_on_seeded_data(seeded, tid):
    k = ctx(seeded, tid)
    args = {"get_overview": {}, "list_courses": {"term": 1}, "get_course": {"code": "D413"},
            "search_notes": {"query": "cloud"}, "read_note": {"code": "D413", "name": "overview"},
            "list_competencies": {"code": "D413"}, "get_card_stats": {}, "get_quiz_history": {"code": "D413"},
            "get_mistakes": {"code": "D413"}, "get_study_time": {}, "list_certs": {}}
    assert set(args) == {n for n, t in agent.TOOLS.items() if t["kind"] == "read"}
    for name, a in args.items():
        out = json.loads(agent.run_tool(k, ai.Call("", name, a)))
        assert "error" not in out, (name, out)


# ---------------------------------------------------------------- memory

def test_memory_carries_into_every_chat(seeded, tid, api, monkeypatch):
    use(monkeypatch)
    api.replies = [a_calls(("remember", {"text": "D413 pre-assessment:  weak on OSPF,\nRADIUS", "code": "D413"}),
                           ("remember", {"text": "  "})), a_text("Noted.")]
    _, meta = message(seeded, agent.reply(seeded, tid, "I bombed OSPF and RADIUS"))
    assert meta["tools"] == ["D413 saved to memory “D413 pre-assessment: weak on OSPF, RADIUS”", "saved to memory"]
    assert "empty" in api.bodies[1]["messages"][-1]["content"][1]["content"]
    m = agent.memories(seeded)
    assert len(m) == 1 and m[0]["text"] == "D413 pre-assessment: weak on OSPF, RADIUS" and m[0]["code"] == "D413"
    other = agent.create_thread(seeded, "a new chat")  # a different chat still sees it
    api.replies = [a_text("Start with OSPF.")]
    agent.reply(seeded, other, "What should I study?")
    assert f"[{m[0]['id']}] D413" in api.bodies[-1]["system"] and "weak on OSPF" in api.bodies[-1]["system"]
    api.replies = [a_calls(("forget", {"memory_id": m[0]["id"]}), ("forget", {"memory_id": 999})), a_text("Done.")]
    agent.reply(seeded, other, "I've got OSPF now")
    assert agent.memories(seeded) == [] and "No memory 999" in api.bodies[-1]["messages"][-1]["content"][1]["content"]
    assert "memory is empty" in agent.system_prompt(seeded, seeded.execute("SELECT * FROM agent_threads").fetchone())


def test_long_card_lists_and_output_budget(seeded, tid, api, monkeypatch):
    use(monkeypatch)
    monkeypatch.setattr(settings, "ai_max_output_tokens", 4096)
    many = [{"front": f"T{i}?", "back": "a"} for i in range(agent.MAX_CARDS + 5)]
    api.replies = [a_calls(("add_flashcards", {"code": "D413", "cards": many}),
                           ("add_flashcards", {"code": "D413", "cards": many[:5]})), a_text("Proposed 45.")]
    agent.reply(seeded, tid, "x" * 9000)  # a pasted list, longer than the old 4,000-character cap
    assert api.bodies[0]["max_tokens"] >= ai.BULK_MAX_TOKENS
    assert len(api.bodies[0]["messages"][0]["content"]) == 9000
    sizes = [len(json.loads(p[0])["cards"]) for p in seeded.execute("SELECT payload_json FROM agent_proposals ORDER BY id")]
    assert sizes == [agent.MAX_CARDS, 5]


# ---------------------------------------------------------------- pages

def test_ask_hidden_when_off(client):
    assert client.get("/ask").status_code == 404
    assert 'href="/ask"' not in client.get("/").text


def test_ask_flow(client, api, monkeypatch):
    use(monkeypatch)
    api.replies = [a_calls(("schedule_exam", {"code": "D413", "date": "2026-10-20"})), a_text("I've proposed Oct 20.")]
    assert 'href="/ask"' in client.get("/").text
    page = client.post("/ask/new", data={"text": "Set my D413 exam for Oct 20", "course": "D413"})
    assert page.status_code == 200 and "proposed Oct 20" in page.text and "Set exam date" in page.text
    conn = db.connect()
    try:
        pid = conn.execute("SELECT id FROM agent_proposals ORDER BY id DESC").fetchone()[0]
        assert conn.execute("SELECT exam_date FROM courses WHERE code = 'D413'").fetchone()[0] is None
        r = client.post(f"/ask/proposals/{pid}/apply", data={"date": "2026-10-21", "quiz_target": ""})
        assert r.status_code == 200 and "D413 exam set for 2026-10-21" in r.text
        assert conn.execute("SELECT exam_date FROM courses WHERE code = 'D413'").fetchone()[0] == "2026-10-21"
    finally:
        conn.close()


def test_memory_page(client, api, monkeypatch):
    use(monkeypatch)
    assert "0 things the agent remembers" in client.get("/ask").text
    client.post("/ask/memory", data={"text": "Prefers short sessions", "course": ""})
    client.post("/ask/memory", data={"text": "Weak on multiplexing", "course": "D413"})
    page = client.get("/ask/memory").text
    assert "Prefers short sessions" in page and "Weak on multiplexing" in page and "added by you" in page
    assert "empty" in client.post("/ask/memory", data={"text": " "}).text
    conn = db.connect()
    try:
        mid = conn.execute("SELECT id FROM agent_memory WHERE text LIKE 'Prefers%'").fetchone()[0]
    finally:
        conn.close()
    client.post(f"/ask/memory/{mid}/delete")
    page = client.get("/ask/memory").text
    assert "Prefers short sessions" not in page and "Weak on multiplexing" in page
    assert "1 thing the agent remembers" in client.get("/ask").text
