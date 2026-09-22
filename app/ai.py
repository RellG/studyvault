"""Provider-agnostic LLM client (PLAN Slice 9 / D8). One call: complete(prompt, max_tokens) -> str.

Provider and model come from AI_PROVIDER / AI_MODEL; neither is hardcoded. Raw httpx rather than a vendor SDK
because the plan asks for one small interface over two providers with the same timeout/retry behaviour.
Every failure becomes AIError with a message fit to show in the UI; nothing here raises anything else.
"""
import logging
import time

import httpx

from .config import settings

log = logging.getLogger("studyvault.ai")

TIMEOUT_S = 30.0
RETRY_STATUSES = {408, 429, 500, 502, 503, 504, 529}
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


def _post(url: str, headers: dict, body: dict) -> dict:
    last = None
    for attempt in range(2):  # one retry
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
        if attempt == 0:
            time.sleep(1.5)
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


def complete(prompt: str, max_tokens: int | None = None, system: str | None = None) -> str:
    if not enabled():
        raise AIError("AI features are off. Set AI_PROVIDER, AI_MODEL and the matching API key in .env.")
    fn = PROVIDERS.get(settings.ai_provider)
    if not fn:
        raise AIError(f"Unknown AI_PROVIDER “{settings.ai_provider}”. Use anthropic or google.")
    max_tokens = max_tokens or settings.ai_max_output_tokens
    started = time.monotonic()
    # Sizes only, never contents (PLAN Slice 9).
    log.debug("ai request provider=%s prompt_chars=%d system_chars=%d max_tokens=%d",
              settings.ai_provider, len(prompt), len(system or ""), max_tokens)
    text = fn(prompt, system, max_tokens)
    log.debug("ai response chars=%d in %.1fs", len(text), time.monotonic() - started)
    return text
