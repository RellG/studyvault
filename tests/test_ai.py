import json
import re

import httpx
import pytest

from app import ai, ai_actions
from app.config import settings


@pytest.fixture
def mock_api(monkeypatch):
    """Route AI calls to a fake server; returns the list of captured requests."""
    class Calls(list):
        def set(self, fn):
            state["handler"] = fn

    calls = Calls()
    state = {"handler": None}

    def handle(request: httpx.Request):
        calls.append(request)
        return state["handler"](request)

    monkeypatch.setattr(ai, "_transport", httpx.MockTransport(handle))
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    return calls


def use(monkeypatch, provider="anthropic", model="some-model-from-env"):
    monkeypatch.setattr(settings, "ai_provider", provider)
    monkeypatch.setattr(settings, "ai_model", model)
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test" if provider == "anthropic" else "")
    monkeypatch.setattr(settings, "google_api_key", "g-test" if provider == "google" else "")


def anthropic_reply(text, stop="end_turn"):
    return httpx.Response(200, json={"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": text}],
                                     "stop_reason": stop})


def test_disabled_without_config(monkeypatch):
    use(monkeypatch)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    assert ai.enabled() is False
    with pytest.raises(ai.AIError, match="off"):
        ai.complete("hi")


def test_anthropic_request_shape(monkeypatch, mock_api):
    use(monkeypatch)
    mock_api.set(lambda r: anthropic_reply("hello"))
    assert ai.complete("prompt text", system="sys") == "hello"
    req = mock_api[0]
    assert str(req.url) == ai.ANTHROPIC_URL
    assert req.headers["x-api-key"] == "sk-test" and req.headers["anthropic-version"] == "2023-06-01"
    body = json.loads(req.content)
    assert body == {"model": "some-model-from-env", "max_tokens": settings.ai_max_output_tokens,
                    "messages": [{"role": "user", "content": "prompt text"}], "system": "sys"}


def test_google_request_shape(monkeypatch, mock_api):
    use(monkeypatch, "google", "gemini-from-env")
    mock_api.set(lambda r: httpx.Response(200, json={"candidates": [
        {"content": {"parts": [{"text": "thinking...", "thought": True}, {"text": "hi there"}]}, "finishReason": "STOP"}]}))
    assert ai.complete("p", max_tokens=100) == "hi there"
    req = mock_api[0]
    assert "models/gemini-from-env:generateContent" in str(req.url) and req.headers["x-goog-api-key"] == "g-test"
    assert json.loads(req.content)["generationConfig"] == {"maxOutputTokens": 100}


@pytest.mark.parametrize("response,match", [
    (httpx.Response(401, json={"error": {"message": "bad key"}}), "API key"),
    (httpx.Response(404, json={}), "doesn't know the model"),
    (httpx.Response(400, json={"error": {"message": "too long"}}), "too long"),
    (httpx.Response(200, json={"content": [], "stop_reason": "refusal"}), "declined"),
    (httpx.Response(200, json={"content": [], "stop_reason": "max_tokens"}), "AI_MAX_OUTPUT_TOKENS"),
    (httpx.Response(200, text="<html>"), "isn't JSON"),
])
def test_errors_fail_soft(monkeypatch, mock_api, response, match):
    use(monkeypatch)
    mock_api.set(lambda r: response)
    with pytest.raises(ai.AIError, match=match):
        ai.complete("x")


def test_one_retry_then_success(monkeypatch, mock_api):
    use(monkeypatch)
    replies = iter([httpx.Response(529, json={}), anthropic_reply("ok")])
    mock_api.set(lambda r: next(replies))
    assert ai.complete("x") == "ok" and len(mock_api) == 2


def test_network_down(monkeypatch, mock_api):
    use(monkeypatch)

    def boom(r):
        raise httpx.ConnectError("no route")
    mock_api.set(boom)
    with pytest.raises(ai.AIError, match="Couldn't reach"):
        ai.complete("x")
    assert len(mock_api) == 2  # tried twice


def test_truncated_text_is_flagged(monkeypatch, mock_api):
    use(monkeypatch)
    mock_api.set(lambda r: anthropic_reply("partial", stop="max_tokens"))
    assert "cut off" in ai.complete("x")


def test_flashcards_and_questions_parse(monkeypatch, mock_api):
    use(monkeypatch)
    mock_api.set(lambda r: anthropic_reply('Here you go:\n```json\n[{"front": "SSH port?", "back": "22"}, {"front": ""}]\n```'))
    assert ai_actions.flashcards("notes") == [{"front": "SSH port?", "back": "22"}]
    mock_api.set(lambda r: anthropic_reply(json.dumps([
        {"kind": "mc", "prompt": "Q?", "choices": ["a", "b", "c", "d"], "correct": [1], "explanation": "b"},
        {"kind": "mc", "prompt": "bad", "choices": ["a"], "correct": [5]},
        {"kind": "mc", "prompt": "Two?", "choices": ["a", "b", "c"], "correct": [0, 2]}])))
    qs = ai_actions.questions("notes")
    assert len(qs) == 2 and qs[0]["choices_text"] == "a\n* b\nc\nd" and qs[1]["kind"] == "multi"
    with pytest.raises(ai_actions.ActionError):
        ai_actions.flashcards("")
    with pytest.raises(ai_actions.ActionError):
        ai_actions.flashcards("x" * (ai_actions.MAX_INPUT_CHARS + 1))


def test_only_selected_notes_are_sent(monkeypatch, mock_api):
    use(monkeypatch)
    mock_api.set(lambda r: anthropic_reply("explained"))
    ai_actions.explain("SELECTED PASSAGE ONLY")
    body = json.loads(mock_api[0].content)
    assert "SELECTED PASSAGE ONLY" in body["messages"][0]["content"]


def test_pages_hidden_when_off(client):
    assert client.get("/courses/D413/ai").status_code == 404
    assert "AI assist" not in client.get("/courses/D413").text


def test_ai_flow_drafts_then_accept(client, monkeypatch, mock_api):
    use(monkeypatch)
    assert "AI assist" in client.get("/courses/D413").text
    mock_api.set(lambda r: anthropic_reply('[{"front": "802.11ax name?", "back": "Wi-Fi 6"}, {"front": "junk", "back": "x"}]'))
    r = client.post("/courses/D413/ai/run", data={"action": "cards", "text": "802.11ax is Wi-Fi 6"})
    assert r.status_code == 200 and "Draft from the model" in r.text
    from app import db
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0  # nothing saved yet
    # keep only the first, edited
    r = client.post("/courses/D413/ai/accept", data={"kind": "cards", "keep": "0", "front_0": "802.11ax marketing name?",
                                                     "back_0": "Wi-Fi 6", "front_1": "junk", "back_1": "x"}, follow_redirects=False)
    assert r.status_code == 303
    rows = conn.execute("SELECT front, source FROM cards").fetchall()
    assert [tuple(r) for r in rows] == [("802.11ax marketing name?", "ai-accepted")]
    # explain → save to the Notebook
    mock_api.set(lambda r: anthropic_reply("**Plainly:** it's faster."))
    r = client.post("/courses/D413/ai/run", data={"action": "explain", "text": "OFDMA"})
    assert "<strong>Plainly:</strong>" in r.text
    client.post("/courses/D413/ai/accept", data={"kind": "text", "title": "Explained differently", "text": "**Plainly:** it's faster."})
    assert "Explained differently" in client.get("/courses/D413/notes/notebook").text
    # API down → error shown, page still works
    mock_api.set(lambda r: httpx.Response(503, json={}))
    r = client.post("/courses/D413/ai/run", data={"action": "cards", "text": "x"})
    assert r.status_code == 502 and "busy" in r.text and "Nothing was saved" in r.text
    conn.close()
