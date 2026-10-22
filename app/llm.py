"""
LLM client. Any OpenAI-compatible chat API works: Gemini, DeepSeek, Groq,
OpenAI, Anthropic's compatibility endpoint, or a local server.

Only two env vars matter: LLM_PROVIDER (a preset) and LLM_API_KEY.
LLM_MODEL / LLM_BASE_URL override the preset.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from dotenv import load_dotenv

load_dotenv()

PRESETS: dict[str, dict[str, str]] = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-3.8-flash",
    },
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "groq": {"base_url": "https://api.groq.com/openai/v1", "model": "openai/gpt-oss-120b"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "anthropic": {"base_url": "https://api.anthropic.com/v1/", "model": "claude-sonnet-4-5"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "model": "google/gemini-2.5-flash"},
    "custom": {"base_url": "", "model": ""},
}


class Settings:
    def __init__(self) -> None:
        self.provider = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
        preset = PRESETS.get(self.provider, PRESETS["custom"])
        self.base_url = os.getenv("LLM_BASE_URL") or preset["base_url"]
        self.model = os.getenv("LLM_MODEL") or preset["model"]
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.mock = os.getenv("MOCK_MODE", "0") == "1"
        self.max_pages = int(os.getenv("MAX_PAGES", "8"))
        # Budgets for the extract call. Free tiers cap tokens per minute and (Groq at least)
        # count max_tokens toward that cap, so the budget is derived from LLM_TPM when known:
        #   output  = 40% of TPM, capped at 7000 (a full guide is ~2-3k tokens)
        #   sources = (TPM - output - prompt overhead) * ~3.5 chars/token
        # Groq free tier: gpt-oss-120b 8000, llama-3.3-70b 12000. Explicit MAX_SOURCE_CHARS /
        # MAX_OUTPUT_TOKENS override the derivation. Without a TPM the defaults are generous.
        self.tpm = int(os.getenv("LLM_TPM") or ("8000" if self.provider == "groq" else "0"))
        if self.tpm:
            out = min(7000, int(self.tpm * 0.4))
            src = max(int((self.tpm - out - 1700) * 3.5), 4000)
        else:
            out, src = 7000, 80000
        self.max_output_tokens = int(os.getenv("MAX_OUTPUT_TOKENS") or out)
        self.max_source_chars = int(os.getenv("MAX_SOURCE_CHARS") or src)
        self.public_base_url = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"

    def ready(self) -> bool:
        return self.mock or bool(self.api_key and self.base_url and self.model)


settings = Settings()


def _client():
    from openai import OpenAI
    return OpenAI(api_key=settings.api_key, base_url=settings.base_url)


def _extract_json(text: str) -> Any:
    """Tolerant JSON extraction: handles fenced blocks and leading prose."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("Model did not return JSON: " + text[:120].replace("\n", " "))


class QuotaExhausted(RuntimeError):
    """Daily/long-window quota is gone; retrying now is pointless."""


class RequestTooLarge(RuntimeError):
    """The prompt exceeds the provider's per-request or per-minute token limit.
    `ratio` is limit/requested when the provider reports both (Groq does), else None."""

    def __init__(self, msg: str, ratio: float | None):
        super().__init__(msg)
        self.ratio = ratio


def _too_large(err: Exception) -> RequestTooLarge | None:
    text = str(err)
    if not re.search(r"too large|reduce your message|context length|maximum context|too many tokens", text, re.I):
        return None
    m = re.search(r"Limit\s*([0-9]+).*?Requested\s*([0-9]+)", text, re.I | re.S)
    ratio = int(m.group(1)) / int(m.group(2)) if m and int(m.group(2)) else None
    return RequestTooLarge(text[:300], ratio)


def _retry_delay(err: Exception, attempt: int) -> float:
    """Free tiers send the wait time in the error body; honor it, else back off.
    Raises QuotaExhausted when the advised wait is longer than 10 minutes."""
    text = str(err)
    m = re.search(r"'retryDelay':\s*'([0-9]+)s'", text) or \
        re.search(r"retry in\s*([0-9]+(?:\.[0-9]+)?)s", text, re.I) or \
        re.search(r"retry(?:Delay|\s+in)[^0-9]*([0-9]+(?:\.[0-9]+)?)", text, re.I)
    if m:
        secs = float(m.group(1))
        h = re.search(r"retry in\s*([0-9]+)h", text, re.I)
        if h:
            secs = max(secs, int(h.group(1)) * 3600)
        if secs > 600:
            raise QuotaExhausted(
                f"The model's quota is exhausted for about {secs/3600:.1f} hours. "
                "Switch LLM_PROVIDER (e.g. groq) or wait. Original: " + text[:300])
        return min(secs + 2, 120)
    return min(15 * (2 ** attempt), 120)


def _create(client, **kwargs):
    """One chat completion with retries on rate limits and transient errors."""
    from openai import APIConnectionError, APIStatusError, RateLimitError
    last: Exception | None = None
    for attempt in range(4):
        try:
            return client.chat.completions.create(**kwargs)
        except RateLimitError as e:
            big = _too_large(e)
            if big:
                raise big from e
            last = e
            delay = _retry_delay(e, attempt)
            print(f"  [llm] rate limited; waiting {delay:.0f}s (attempt {attempt + 1}/4)")
            time.sleep(delay)
        except APIStatusError as e:
            big = _too_large(e)
            if big or e.status_code == 413:
                raise (big or RequestTooLarge(str(e)[:300], None)) from e
            if e.status_code in (500, 502, 503, 504):
                last = e
                time.sleep(5 * (attempt + 1))
            else:
                raise
        except APIConnectionError as e:
            last = e
            time.sleep(5 * (attempt + 1))
    raise last  # type: ignore[misc]


def chat_json(system: str, user: str, temperature: float = 0.1, max_tokens: int = 6000) -> Any:
    """Ask for a JSON object. Tries native JSON mode, falls back to parsing."""
    client = _client()
    kwargs: dict[str, Any] = dict(
        model=settings.model,
        temperature=temperature,
        max_tokens=max_tokens,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    extra = _reasoning_extra()
    try:
        resp = _create(client, response_format={"type": "json_object"}, **kwargs, **extra)
    except Exception as e:
        from openai import RateLimitError
        if isinstance(e, (RateLimitError, RequestTooLarge, QuotaExhausted)):
            raise
        # some providers reject response_format or the reasoning knob; retry plain
        resp = _create(client, **kwargs)
    content = resp.choices[0].message.content or ""
    if not content.strip():
        fr = resp.choices[0].finish_reason
        raise ValueError(f"Model returned no content (finish_reason={fr}); if this is a reasoning model, "
                         "its thinking consumed max_tokens")
    return _extract_json(content)


def _reasoning_extra() -> dict[str, Any]:
    """Reasoning models spend max_tokens on thinking before answering. For structured extraction
    we want the answer, so ask for low effort where the API supports it (Groq gpt-oss, OpenAI o-series)."""
    m = settings.model.lower()
    if "gpt-oss" in m or re.match(r"o[1-9]", m):
        return {"extra_body": {"reasoning_effort": "low"}}
    return {}


def chat_text(system: str, user: str, temperature: float = 0.2, max_tokens: int = 1500) -> str:
    client = _client()
    resp = _create(
        client,
        model=settings.model,
        temperature=temperature,
        max_tokens=max_tokens,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    return (resp.choices[0].message.content or "").strip()
