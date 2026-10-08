"""Structured audit log of tool calls (logs/actions.jsonl).

Each line records when a tool ran, what triggered it, its arguments, risk level,
outcome and error. Secrets are redacted and free-text bodies (message text, file
content) are reduced to their length so personal content is not written to disk.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from core.settings import BASE_DIR

LOG_DIR = BASE_DIR / "logs"
LOG_PATH = LOG_DIR / "actions.jsonl"
MAX_BYTES = 5_000_000
_lock = threading.Lock()

_SECRET_FIELD = re.compile(r"pass(word)?|secret|token|api[_-]?key|otp|pin|cvv|card|iban|credential", re.I)
_CONTENT_FIELDS = {"message_text", "message", "content", "text", "code", "value"}
_TOKEN_LIKE = re.compile(r"\b(AIza[0-9A-Za-z_\-]{20,}|sk-[0-9A-Za-z]{20,}|[0-9A-Za-z_\-]{40,})\b")


def redact(value: Any, field: str = "") -> Any:
    if field and _SECRET_FIELD.search(field):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, field) for v in value]
    if isinstance(value, str):
        if field in _CONTENT_FIELDS:
            return f"<{len(value)} chars>"
        return _TOKEN_LIKE.sub("[REDACTED]", value)
    return value


def _rotate() -> None:
    try:
        if LOG_PATH.stat().st_size > MAX_BYTES:
            LOG_PATH.replace(LOG_PATH.with_suffix(".jsonl.1"))
    except OSError:
        pass


def _hide_bodies(trigger: str, args: dict) -> str:
    """Remove message/file bodies from the user's command too, not only from the arguments."""
    for field in _CONTENT_FIELDS:
        body = (args or {}).get(field)
        if isinstance(body, str) and len(body) >= 3 and body in trigger:
            trigger = trigger.replace(body, f"<{len(body)} chars>")
    return trigger


def log_action(tool: str, args: dict, *, status: str, risk: str = "",
               result: Any = None, error: str = "", duration_ms: int | None = None,
               trigger: str = "") -> None:
    """Append one record. Logging failures never break tool execution."""
    trigger = _hide_bodies(trigger, args)
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "tool": tool,
        "status": status,
        "risk": risk,
        "args": redact(args or {}),
        "result": redact(str(result))[:300] if result is not None else None,
        "error": redact(error)[:500] if error else None,
        "duration_ms": duration_ms,
        "trigger": redact(trigger)[:200] if trigger else None,
    }
    try:
        with _lock:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            _rotate()
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def recent_actions(limit: int = 10) -> list[dict]:
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(line) for line in lines if line.strip()]
    except (OSError, ValueError):
        return []
