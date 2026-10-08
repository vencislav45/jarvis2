"""Gemini text-model calls with automatic fallback.

Google regularly overloads (503 "high demand", 504 "deadline expired"), rate-limits
(429) or retires (404 "no longer available to new users") individual models. Every
tool asks for a model by name; this module tries that model first and, when it is
unavailable, moves on to the next model of the same tier instead of failing or
hanging. Unhealthy models are skipped for a few minutes; retired ones for the rest
of the run.

    from core import llm
    response = llm.generate(client, "gemini-3.6-flash", contents, config)
    response = await llm.agenerate(client.aio, "gemini-3.8-flash", contents, config)

Model choices can be overridden in .env: JARVIS_MODEL, JARVIS_FAST_MODEL,
JARVIS_REASONING_MODEL, JARVIS_FALLBACK_MODELS (comma-separated).
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Callable

from google.genai import types

from core import settings

# Ordered fallbacks per tier (verified available to this API key on 2026-10-07).
_TIERS = {
    "reasoning": ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
                  "gemini-3.1-flash-lite"],
    "standard": ["gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash", "gemini-3.5-flash",
                 "gemini-3.1-flash-lite"],
    "fast": ["gemini-3.1-flash-lite", "gemini-flash-lite-latest", "gemini-3.6-flash", "gemini-3.7-flash"],
}
# Seconds to wait for one model before trying the next one.
_TIMEOUTS = {"reasoning": 45, "standard": 40, "fast": 20}
_COOLDOWN = 180          # skip an overloaded model for this long
_lock = threading.Lock()
_unhealthy: dict[str, float] = {}   # model → time it may be tried again
_retired: set[str] = set()          # 404: gone for this API key


def tier_of(model: str) -> str:
    model = model.removeprefix("models/")
    if "lite" in model:
        return "fast"
    if model == _primary("reasoning"):
        return "reasoning"
    return "standard"


def _primary(tier: str) -> str:
    env = {"reasoning": "JARVIS_REASONING_MODEL", "standard": "JARVIS_MODEL", "fast": "JARVIS_FAST_MODEL"}[tier]
    configured = settings.get(env)
    if not configured and tier == "reasoning":
        try:
            from core.desktop_agent import load_settings
            configured = load_settings()["model"]
        except Exception:  # noqa: BLE001
            configured = ""
    return configured or _TIERS[tier][0]


def chain(model: str) -> list[str]:
    """Requested model first, then the tier's fallbacks; healthy models before cooling-down ones."""
    tier = tier_of(model)
    extra = [m.strip() for m in settings.get("JARVIS_FALLBACK_MODELS").split(",") if m.strip()]
    ordered: list[str] = []
    for name in [model.removeprefix("models/"), _primary(tier), *_TIERS[tier], *extra]:
        if name and name not in ordered and name not in _retired:
            ordered.append(name)
    now = time.monotonic()
    with _lock:
        healthy = [m for m in ordered if _unhealthy.get(m, 0) <= now]
        cooling = [m for m in ordered if _unhealthy.get(m, 0) > now]
    return healthy + cooling


def _code(error: BaseException) -> int | None:
    code = getattr(error, "code", None)
    return code if isinstance(code, int) else None


def _gone(error: BaseException) -> bool:
    text = str(error).lower()
    return _code(error) == 404 or "no longer available" in text or "is not found for api version" in text


def _transient(error: BaseException) -> bool:
    text = str(error).lower()
    return (isinstance(error, (TimeoutError, asyncio.TimeoutError)) or _code(error) in (429, 500, 502, 503, 504)
            or any(k in text for k in ("timed out", "timeout", "deadline", "high demand", "unavailable",
                                       "resource_exhausted", "overloaded")))


def _with_timeout(config: Any, seconds: float) -> types.GenerateContentConfig:
    if config is None:
        config = types.GenerateContentConfig()
    elif isinstance(config, dict):
        config = types.GenerateContentConfig(**config)
    if config.http_options is None or config.http_options.timeout is None:
        config = config.model_copy(update={"http_options": types.HttpOptions(timeout=int(seconds * 1000))})
    return config


def _record_failure(model: str, error: BaseException, notify: Callable[[str], None] | None) -> None:
    with _lock:
        if _gone(error):
            _retired.add(model)
        else:
            _unhealthy[model] = time.monotonic() + _COOLDOWN
    reason = "retired" if _gone(error) else "busy" if _code(error) != 429 else "rate-limited"
    message = f"Model {model} is {reason}; trying another model."
    print(f"[LLM] {message} ({type(error).__name__}: {str(error)[:120]})")
    if notify:
        notify(message)


def _record_success(model: str) -> None:
    with _lock:
        _unhealthy.pop(model, None)


def generate(client, model: str, contents, config=None, *, timeout: float | None = None,
             notify: Callable[[str], None] | None = None):
    """client.models.generate_content with fallback models. Raises the last error if all fail."""
    seconds = timeout or _TIMEOUTS[tier_of(model)]
    last: BaseException | None = None
    for name in chain(model):
        try:
            response = client.models.generate_content(model=name, contents=contents,
                                                      config=_with_timeout(config, seconds))
            _record_success(name)
            return response
        except Exception as e:  # noqa: BLE001
            if not (_gone(e) or _transient(e)):
                raise   # bad request / auth problems are not fixed by another model
            _record_failure(name, e, notify)
            last = e
    raise last or RuntimeError("No Gemini model is available.")


async def agenerate(aio_client, model: str, contents, config=None, *, timeout: float | None = None,
                    notify: Callable[[str], None] | None = None, cancelled: Callable[[], bool] | None = None):
    """Async version for client.aio. Returns (response, model_used)."""
    seconds = timeout or _TIMEOUTS[tier_of(model)]
    last: BaseException | None = None
    for name in chain(model):
        if cancelled and cancelled():
            raise asyncio.CancelledError()
        try:
            response = await asyncio.wait_for(
                aio_client.models.generate_content(model=name, contents=contents, config=config),
                timeout=seconds)
            _record_success(name)
            return response, name
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            if not (_gone(e) or _transient(e)):
                raise
            _record_failure(name, e, notify)
            last = e
    raise last or RuntimeError("No Gemini model is available.")


def status() -> dict:
    now = time.monotonic()
    with _lock:
        return {"retired": sorted(_retired),
                "cooling_down": {m: int(t - now) for m, t in _unhealthy.items() if t > now}}
