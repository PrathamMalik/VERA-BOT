"""Provider-agnostic LLM client (standard library only).

Environment variables:
  LLM_PROVIDER   gemini (default) | anthropic | openai | groq | deepseek | openrouter
  LLM_API_KEY    your key — empty = rules-only mode (the bot still works fully)
  LLM_MODEL      optional; for gemini/anthropic the newest suitable model is auto-picked
  LLM_TIMEOUT    seconds per call (default 8)
  LLM_RPM        max AI calls per minute (default 10 — safe for Gemini's free tier)
"""
from __future__ import annotations

import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request

OPENAI_COMPAT = {
    "openai": ("https://api.openai.com/v1/chat/completions", "gpt-4o-mini"),
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "openai/gpt-oss-120b"),
    "deepseek": ("https://api.deepseek.com/v1/chat/completions", "deepseek-chat"),
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions", "google/gemini-2.5-flash"),
}
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
_model_cache: dict[str, str] = {}
_lock = threading.Lock()


def provider() -> str:
    return os.environ.get("LLM_PROVIDER", "gemini").strip().lower()


def enabled() -> bool:
    return bool(os.environ.get("LLM_API_KEY", "").strip())


def timeout() -> float:
    try:
        return float(os.environ.get("LLM_TIMEOUT", "8"))
    except ValueError:
        return 8.0


# ------------------------------------------------------------------ rate limiter (non-blocking)
class Limiter:
    """Sliding one-minute window. try_acquire() never waits: if the budget is spent the caller uses its fallback."""

    def __init__(self):
        self.calls: list[float] = []
        self.lock = threading.Lock()

    def rpm(self):
        try:
            return int(os.environ.get("LLM_RPM", "10"))
        except ValueError:
            return 10

    def try_acquire(self) -> bool:
        now = time.time()
        with self.lock:
            self.calls = [t for t in self.calls if now - t < 60]
            if len(self.calls) >= self.rpm():
                return False
            self.calls.append(now)
            return True


LIMITER = Limiter()


def _post(url, headers, payload, t):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"content-type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=t, context=ssl.create_default_context()) as r:
        return json.loads(r.read().decode())


def _get(url, headers=None, t=5):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=t, context=ssl.create_default_context()) as r:
        return json.loads(r.read().decode())


def _pick_gemini(models: list[dict]) -> str | None:
    """Newest stable 'flash-lite' model that supports generateContent (free tier: 15 RPM / 500 per day,
    vs 5 RPM / 20 per day for plain Flash). Falls back to plain Flash if no Flash-Lite is listed."""
    best = None
    for want_lite in (True, False):
        for m in models:
            name = m.get("name", "").split("/")[-1]
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            if "flash" not in name or re.search(r"image|tts|live|audio|exp|preview|thinking|8b|transcribe|translate", name):
                continue
            if ("lite" in name) != want_lite:
                continue
            v = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
            ver = float(v.group(1)) if v else 0
            if best is None or ver > best[0] or (ver == best[0] and len(name) < len(best[1])):
                best = (ver, name)
        if best:
            return best[1]
    return None


def model_name() -> str:
    if os.environ.get("LLM_MODEL"):
        return os.environ["LLM_MODEL"]
    p = provider()
    with _lock:
        if p in _model_cache:
            return _model_cache[p]
    key = os.environ.get("LLM_API_KEY", "")
    m = None
    try:
        if p == "gemini":
            m = _pick_gemini(_get(f"{GEMINI_BASE}/models?pageSize=200", {"x-goog-api-key": key}).get("models", []))
        elif p == "anthropic":
            data = _get("https://api.anthropic.com/v1/models?limit=100", {"x-api-key": key, "anthropic-version": "2023-06-01"})
            sonnets = sorted([d for d in data.get("data", []) if "sonnet" in d.get("id", "")], key=lambda d: d.get("created_at", ""), reverse=True)
            m = sonnets[0]["id"] if sonnets else None
    except Exception:
        m = None
    m = m or {"gemini": "gemini-3.5-flash-lite", "anthropic": "claude-sonnet-4-5"}.get(p) or OPENAI_COMPAT.get(p, OPENAI_COMPAT["openai"])[1]
    with _lock:
        _model_cache[p] = m
    return m


def complete(system: str, user: str, max_tokens: int = 700, t: float | None = None, json_mode: bool = True) -> str:
    """One deterministic (temperature 0) completion. Raises on any error / rate limit — callers fall back to rules."""
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        raise RuntimeError("LLM disabled (no LLM_API_KEY)")
    if not LIMITER.try_acquire():
        raise RuntimeError("LLM per-minute budget used up")
    t = t or timeout()
    p = provider()
    model = model_name()
    if p == "gemini":
        cfg = {"temperature": 0, "maxOutputTokens": max_tokens}
        if json_mode:
            cfg["responseMimeType"] = "application/json"
        if "2.5-flash" in model:
            cfg["thinkingConfig"] = {"thinkingBudget": 0}  # faster, fits the 30s rule
        elif re.search(r"gemini-[3-9]", model):
            cfg["thinkingConfig"] = {"thinkingLevel": "minimal"}  # Gemini 3+: least thinking = fastest
        body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": cfg}
        url = f"{GEMINI_BASE}/models/{model}:generateContent"
        gh = {"x-goog-api-key": key}  # header works for both old AIza… and new AQ.… keys
        try:
            data = _post(url, gh, body, t)
        except urllib.error.HTTPError as e:
            if e.code == 400 and "thinkingConfig" in cfg:  # model doesn't accept the thinking setting -> retry once without it
                cfg.pop("thinkingConfig")
                data = _post(url, gh, body, t)
            elif e.code == 404 and os.environ.get("LLM_MODEL"):  # model name typo / retired -> auto-pick and remember it
                os.environ.pop("LLM_MODEL")
                with _lock:
                    _model_cache.pop(p, None)
                raise
            else:
                raise
        return "".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"])
    if p == "anthropic":
        data = _post("https://api.anthropic.com/v1/messages", {"x-api-key": key, "anthropic-version": "2023-06-01"},
                     {"model": model, "max_tokens": max_tokens, "temperature": 0, "system": system,
                      "messages": [{"role": "user", "content": user}]}, t)
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    url, _ = OPENAI_COMPAT.get(p, OPENAI_COMPAT["openai"])
    payload = {"model": model, "temperature": 0, "max_tokens": max_tokens,
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if "gpt-oss" in model:  # reasoning model: keep thinking short so replies stay fast and within token limits
        payload["reasoning_effort"] = "low"
        payload["max_tokens"] = max_tokens + 600
    data = _post(url, {"authorization": f"Bearer {key}"}, payload, t)
    return data["choices"][0]["message"]["content"]


def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise
