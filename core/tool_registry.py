"""Tool registry: one place that validates, gates, runs, retries and logs tool calls.

Adding a tool:
    1. add its declaration to core/tool_declarations.py
    2. register a handler (see core/builtin_tools.py)
    3. if it can change something, add a rule to core/permissions.classify
"""
from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from core.action_log import log_action
from core.permissions import ConfirmationError, ConfirmationGate, Risk, classify

Handler = Callable[[dict], Any] | Callable[[dict], Awaitable[Any]]

NO_RESULT = ("The tool finished without reporting a result, so the outcome is unverified. "
             "Do not tell the user it succeeded; check the state or say it is unconfirmed.")

# Transient failures worth one more try on tools that are safe to repeat.
_TRANSIENT = (TimeoutError, asyncio.TimeoutError, ConnectionError)

CONFIRM_DECLARATION = {
    "name": "confirm_action",
    "description": ("Execute an action that is waiting for confirmation, ONLY after the user has clearly said yes "
                    "to the exact action you described. The system independently checks the user's reply."),
    "parameters": {"type": "OBJECT", "properties": {
        "confirmation_id": {"type": "STRING", "description": "confirmation_id returned with confirmation_required"}},
        "required": []},
}
CANCEL_DECLARATION = {
    "name": "cancel_action",
    "description": "Discard an action waiting for confirmation when the user declines, cancels, or changes details.",
    "parameters": {"type": "OBJECT", "properties": {
        "confirmation_id": {"type": "STRING", "description": "Omit to cancel every pending action"}},
        "required": []},
}


@dataclass
class Tool:
    name: str
    declaration: dict
    handler: Handler
    retry_safe: bool = False   # read-only tools may be retried once on transient errors
    # Runs before the confirmation question (read-only): returns {} to continue, {"args": …}
    # to continue with corrected arguments, or {"ok": False, …} to stop without asking the user.
    preflight: Callable[[dict], dict] | None = None
    # False for tools whose results are private (e.g. message contents): the log keeps only the status.
    log_result: bool = True


class ToolRegistry:
    def __init__(self, gate: ConfirmationGate | None = None):
        self.gate = gate or ConfirmationGate()
        self._tools: dict[str, Tool] = {}
        self.last_trigger = ""
        self.register(CONFIRM_DECLARATION, self._confirm)
        self.register(CANCEL_DECLARATION, self._cancel)

    # ── registration ──────────────────────────────────────────────────────────
    def register(self, declaration: dict, handler: Handler, *, retry_safe: bool = False,
                 preflight: Callable[[dict], dict] | None = None, log_result: bool = True) -> None:
        name = declaration["name"]
        self._tools[name] = Tool(name, declaration, handler, retry_safe, preflight, log_result)

    def names(self) -> list[str]:
        return list(self._tools)

    def declarations(self) -> list[dict]:
        return [t.declaration for t in self._tools.values()]

    def note_user_input(self, text: str) -> None:
        self.last_trigger = text
        self.gate.note_user_input(text)

    # ── execution ─────────────────────────────────────────────────────────────
    async def execute(self, name: str, args: dict | None) -> dict:
        """Run a model-requested tool call. Returns the FunctionResponse payload."""
        args = dict(args or {})
        tool = self._tools.get(name)
        if tool is None:
            log_action(name, args, status="unknown_tool", trigger=self.last_trigger)
            return {"ok": False, "error": f"Unknown tool: {name}"}
        if tool.preflight is not None:
            try:
                checked = await asyncio.to_thread(tool.preflight, args)
            except Exception as exc:  # noqa: BLE001
                checked = {"ok": False, "error": f"{name} check failed: {type(exc).__name__}: {exc}"}
            if checked.get("ok") is False:
                log_action(name, args, status="blocked_before_confirmation", result=checked.get("error"),
                           trigger=self.last_trigger)
                return checked
            args = dict(checked.get("args") or args)
        if name not in {"confirm_action", "cancel_action"}:
            decision = self.gate.check(name, args)
            if not decision.allowed:
                log_action(name, args, status="awaiting_confirmation", risk=decision.risk.label,
                           trigger=self.last_trigger)
                return decision.pending.to_prompt()
        return await self._run(tool, args, classify(name, args))

    async def _run(self, tool: Tool, args: dict, risk: Risk) -> dict:
        started = time.monotonic()
        attempts = 2 if tool.retry_safe else 1
        for attempt in range(attempts):
            try:
                if inspect.iscoroutinefunction(tool.handler):
                    result = await tool.handler(args)
                else:   # action modules block (UI automation, HTTP) — keep the event loop free
                    result = await asyncio.to_thread(tool.handler, args)
                payload = self._normalize(result)
                status = "ok" if payload.get("ok", True) else "failed"
                logged = payload.get("result") if tool.log_result else "<private result not logged>"
                log_action(tool.name, args, status=status, risk=risk.label, result=logged,
                           error=payload.get("error", ""), duration_ms=int((time.monotonic() - started) * 1000),
                           trigger=self.last_trigger)
                return payload
            except _TRANSIENT as exc:
                if attempt + 1 < attempts:
                    await asyncio.sleep(1.0)
                    continue
                error = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # noqa: BLE001 - every tool failure must reach the model honestly
                error = f"{type(exc).__name__}: {exc}"
            log_action(tool.name, args, status="error", risk=risk.label, error=error,
                       duration_ms=int((time.monotonic() - started) * 1000), trigger=self.last_trigger)
            return {"ok": False, "error": f"{tool.name} failed: {error}. Nothing indicates it succeeded; "
                                          "tell the user honestly."}
        raise AssertionError("unreachable")

    @staticmethod
    def _normalize(result: Any) -> dict:
        if isinstance(result, dict):
            return result if ("result" in result or "error" in result or "status" in result) else {"result": result}
        if result is None or (isinstance(result, str) and not result.strip()):
            return {"ok": False, "result": NO_RESULT}
        return {"result": result}

    # ── confirmation tools ────────────────────────────────────────────────────
    async def _confirm(self, args: dict) -> dict:
        try:
            action = self.gate.confirm(str(args.get("confirmation_id", "")).strip())
        except ConfirmationError as exc:
            return {"ok": False, "status": "not_confirmed", "error": str(exc)}
        tool = self._tools.get(action.tool)
        if tool is None:
            return {"ok": False, "error": f"Tool {action.tool} is no longer available."}
        payload = await self._run(tool, action.args, action.risk)
        return {**payload, "confirmed_action": action.summary}

    async def _cancel(self, args: dict) -> dict:
        removed = self.gate.cancel(str(args.get("confirmation_id", "")).strip())
        for action in removed:
            log_action(action.tool, action.args, status="cancelled", risk=action.risk.label, trigger=self.last_trigger)
        if not removed:
            return {"result": "Nothing was pending."}
        return {"result": "Cancelled, nothing was executed: " + "; ".join(a.summary for a in removed)}
