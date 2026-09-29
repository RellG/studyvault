"""Provider-agnostic LLM client (PLAN Slice 9 / D8). One call: complete(prompt, max_tokens) -> str.

Provider and model come from AI_PROVIDER / AI_MODEL; neither is hardcoded. Raw httpx rather than a vendor SDK
because the plan asks for one small interface over two providers with the same timeout/retry behaviour.
Every failure becomes AIError with a message fit to show in the UI; nothing here raises anything else.
"""
import logging
import time
from dataclasses import dataclass, field

import httpx

from .config import settings

log = logging.getLogger("studyvault.ai")

TIMEOUT_S = 30.0
RETRY_STATUSES = {408, 429, 500, 502, 503, 504, 529}
MAX_WAIT_S = 30.0  # longest provider-requested wait we sit out before retrying
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
GOOGLE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_transport: httpx.BaseTransport | None = None  # tests inject a MockTransport


class AIError(Exception):
    """User-facing failure: the message is shown as-is."""


def enabled() -> bool:
    return settings.ai_enabled


def describe() -> str:
    return f"{settings.ai_provider} · {settings.ai_model}" if enabled() else "off"


def _retry_after(r: httpx.Response) -> float | None:
    """Seconds the provider asks us to wait: Retry-After header, or Gemini's RetryInfo detail."""
    try:
        if r.headers.get("retry-after"):
            return float(r.headers["retry-after"])
        for d in (r.json().get("error") or {}).get("details") or []:
            if str(d.get("@type", "")).endswith("RetryInfo"):
                return float(str(d.get("retryDelay", "")).rstrip("s"))
    except (ValueError, AttributeError):
        pass
    return None


def _post(url: str, headers: dict, body: dict) -> dict:
    last = None
    tries, attempt = 2, 0  # one retry; a 429 with a short stated wait gets one more (Gemini free tier: 5 requests/min)
    while attempt < tries:
        wait = 1.5
        try:
            with httpx.Client(timeout=TIMEOUT_S, transport=_transport) as client:
                r = client.post(url, headers=headers, json=body)
        except httpx.TimeoutException:
            last = AIError("The AI service didn't answer within 30 seconds. Try again, or with less text.")
        except httpx.HTTPError:
            last = AIError("Couldn't reach the AI service. Is the Pi online?")
        else:
            if r.status_code < 400:
                try:
                    return r.json()
                except ValueError:
                    raise AIError("The AI service sent back something that isn't JSON.")
            if r.status_code in (401, 403):
                raise AIError("The AI service rejected the API key. Check it in .env and restart.")
            if r.status_code == 404:
                raise AIError(f"The AI service doesn't know the model “{settings.ai_model}”. Check AI_MODEL in .env.")
            if r.status_code == 400:
                raise AIError(f"The AI service refused the request: {_error_text(r)}")
            last = AIError("The AI service is busy or rate-limiting (HTTP %d). Try again in a minute." % r.status_code) \
                if r.status_code in RETRY_STATUSES else AIError(f"AI service error (HTTP {r.status_code}): {_error_text(r)}")
            if r.status_code not in RETRY_STATUSES:
                raise last
            after = _retry_after(r) if r.status_code == 429 else None
            if after is not None:
                if after > MAX_WAIT_S:  # e.g. the free tier's daily quota: waiting here would just hang the page
                    raise AIError(f"The AI service's usage limit is used up for now; it asks to wait "
                                  f"{round(after / 60) or 1} min. Try again later.")
                wait, tries = after + 0.5, 3
        attempt += 1
        if attempt < tries:
            time.sleep(wait)
    raise last


def _error_text(r: httpx.Response) -> str:
    try:
        j = r.json()
        return str((j.get("error") or {}).get("message") or j)[:200]
    except ValueError:
        return r.text[:200]


def _anthropic(prompt: str, system: str | None, max_tokens: int) -> str:
    body = {"model": settings.ai_model, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}]}
    if system:
        body["system"] = system
    data = _post(ANTHROPIC_URL, {"x-api-key": settings.anthropic_api_key, "anthropic-version": ANTHROPIC_VERSION,
                                 "content-type": "application/json"}, body)
    stop = data.get("stop_reason")
    if stop == "refusal":
        raise AIError("The model declined this request.")
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    if not text:
        if stop == "max_tokens":
            raise AIError("The model ran out of output tokens before answering. Raise AI_MAX_OUTPUT_TOKENS in .env.")
        raise AIError("The model returned no text.")
    if stop == "max_tokens":
        text += "\n\n*(cut off at the output limit)*"
    return text


def _google(prompt: str, system: str | None, max_tokens: int) -> str:
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens}}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    data = _post(GOOGLE_URL.format(model=settings.ai_model),
                 {"x-goog-api-key": settings.google_api_key, "content-type": "application/json"}, body)
    if (data.get("promptFeedback") or {}).get("blockReason"):
        raise AIError("The model declined this request.")
    cands = data.get("candidates") or []
    if not cands:
        raise AIError("The model returned no answer.")
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    reason = cands[0].get("finishReason")
    if not text:
        if reason == "MAX_TOKENS":
            raise AIError("The model ran out of output tokens before answering. Raise AI_MAX_OUTPUT_TOKENS in .env.")
        raise AIError("The model returned no text." if reason in (None, "STOP") else "The model declined this request.")
    if reason == "MAX_TOKENS":
        text += "\n\n*(cut off at the output limit)*"
    return text


PROVIDERS = {"anthropic": _anthropic, "google": _google}


def _check_provider(table: dict):
    if not enabled():
        raise AIError("AI features are off. Set AI_PROVIDER, AI_MODEL and the matching API key in .env.")
    fn = table.get(settings.ai_provider)
    if not fn:
        raise AIError(f"Unknown AI_PROVIDER “{settings.ai_provider}”. Use anthropic or google.")
    return fn


def complete(prompt: str, max_tokens: int | None = None, system: str | None = None) -> str:
    fn = _check_provider(PROVIDERS)
    max_tokens = max_tokens or settings.ai_max_output_tokens
    started = time.monotonic()
    # Sizes only, never contents (PLAN Slice 9).
    log.debug("ai request provider=%s prompt_chars=%d system_chars=%d max_tokens=%d",
              settings.ai_provider, len(prompt), len(system or ""), max_tokens)
    text = fn(prompt, system, max_tokens)
    log.debug("ai response chars=%d in %.1fs", len(text), time.monotonic() - started)
    return text


# ---------------------------------------------------------------- tool calling (PLAN Slice 11, the study agent)
#
# chat() takes a provider-neutral conversation and returns one model turn. Messages are dicts:
#   {"role": "user", "text": ...}          {"role": "assistant", "text": ...}      (earlier turns: final text only)
#   {"role": "assistant", "turn": Turn}    {"role": "tool", "results": [(Call, "json text"), ...]}   (inside one turn)
# A Turn keeps the provider's own reply (`native`) so it is replayed verbatim: Gemini 3 rejects function-call
# history that loses its thought signatures.

@dataclass
class Call:
    id: str
    name: str
    args: dict


@dataclass
class Turn:
    text: str
    calls: list[Call] = field(default_factory=list)
    native: object = None
    truncated: bool = False


def _anthropic_chat(system: str, messages: list[dict], tools: list[dict], max_tokens: int) -> Turn:
    msgs = []
    for m in messages:
        if m["role"] == "tool":
            msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": c.id, "content": r}
                                                     for c, r in m["results"]]})
        elif "turn" in m:
            msgs.append({"role": "assistant", "content": m["turn"].native})
        else:
            msgs.append({"role": m["role"], "content": m["text"]})
    body = {"model": settings.ai_model, "max_tokens": max_tokens, "system": system, "messages": msgs,
            "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]}
    data = _post(ANTHROPIC_URL, {"x-api-key": settings.anthropic_api_key, "anthropic-version": ANTHROPIC_VERSION,
                                 "content-type": "application/json"}, body)
    stop = data.get("stop_reason")
    if stop == "refusal":
        raise AIError("The model declined this request.")
    content = data.get("content") or []
    text = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
    calls = [Call(b.get("id", ""), b.get("name", ""), b.get("input") or {}) for b in content if b.get("type") == "tool_use"]
    if not text and not calls:
        if stop == "max_tokens":
            raise AIError("The model ran out of output tokens before answering. Raise AI_MAX_OUTPUT_TOKENS in .env.")
        raise AIError("The model returned no text.")
    return Turn(text, calls, content, stop == "max_tokens")


def _google_chat(system: str, messages: list[dict], tools: list[dict], max_tokens: int) -> Turn:
    contents = []
    for m in messages:
        if m["role"] == "tool":
            contents.append({"role": "user", "parts": [
                {"functionResponse": {"name": c.name, "response": {"result": r}, **({"id": c.id} if c.id else {})}}
                for c, r in m["results"]]})
        elif "turn" in m:
            contents.append({"role": "model", "parts": m["turn"].native})
        else:
            contents.append({"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["text"]}]})
    decls = []
    for t in tools:
        d = {"name": t["name"], "description": t["description"]}
        if t["parameters"].get("properties"):  # Gemini rejects an object schema with no properties
            d["parameters"] = t["parameters"]
        decls.append(d)
    body = {"contents": contents, "systemInstruction": {"parts": [{"text": system}]},
            "tools": [{"functionDeclarations": decls}], "generationConfig": {"maxOutputTokens": max_tokens}}
    data = _post(GOOGLE_URL.format(model=settings.ai_model),
                 {"x-goog-api-key": settings.google_api_key, "content-type": "application/json"}, body)
    if (data.get("promptFeedback") or {}).get("blockReason"):
        raise AIError("The model declined this request.")
    cands = data.get("candidates") or []
    if not cands:
        raise AIError("The model returned no answer.")
    reason = cands[0].get("finishReason")
    if reason == "MALFORMED_FUNCTION_CALL":
        raise AIError("The model sent a garbled action. Try again, or rephrase.")
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    calls = [Call(p["functionCall"].get("id", ""), p["functionCall"].get("name", ""), p["functionCall"].get("args") or {})
             for p in parts if "functionCall" in p]
    if not text and not calls:
        if reason == "MAX_TOKENS":
            raise AIError("The model ran out of output tokens before answering. Raise AI_MAX_OUTPUT_TOKENS in .env.")
        raise AIError("The model returned no text." if reason in (None, "STOP") else "The model declined this request.")
    return Turn(text, calls, parts, reason == "MAX_TOKENS")


CHAT_PROVIDERS = {"anthropic": _anthropic_chat, "google": _google_chat}


def chat(system: str, messages: list[dict], tools: list[dict], max_tokens: int | None = None) -> Turn:
    """One model turn with tools available. `tools` items: {"name", "description", "parameters" (JSON schema)}."""
    fn = _check_provider(CHAT_PROVIDERS)
    max_tokens = max_tokens or settings.ai_max_output_tokens
    started = time.monotonic()
    log.debug("ai chat provider=%s messages=%d system_chars=%d tools=%d", settings.ai_provider, len(messages),
              len(system), len(tools))
    turn = fn(system, messages, tools, max_tokens)
    log.debug("ai chat reply chars=%d calls=%d in %.1fs", len(turn.text), len(turn.calls), time.monotonic() - started)
    return turn
