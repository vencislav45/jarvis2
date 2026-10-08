"""Risk classification and the confirmation gate for tool calls.

Every tool call is classified LOW / MEDIUM / HIGH before it runs.

* LOW runs immediately (open apps/sites, search, read).
* MEDIUM (send messages, delete/modify files, run non-read-only commands) is held
  until the user confirms, unless config/permissions.json auto-approves that tool.
* HIGH (purchases, payments, security changes, destructive system commands) is
  always held for confirmation and can never be auto-approved.

A held action can only be released by ``confirm`` when the *user* has said or typed
an affirmative reply after the action was proposed. The model cannot confirm on
its own, and the confirmed call runs with the arguments that were shown to the user,
not with new arguments.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Callable

from core.settings import BASE_DIR

POLICY_PATH = BASE_DIR / "config" / "permissions.json"
CONFIRMATION_TTL_SECONDS = 300   # voice back-and-forth can take a while
REPLY_GAP_SECONDS = 4.0          # transcript chunks closer than this are one utterance


class Risk(IntEnum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2

    @property
    def label(self) -> str:
        return self.name.lower()


# ── Classification ────────────────────────────────────────────────────────────

_PURCHASE = re.compile(r"\b(buy|purchase|pay|payment|checkout|place order|order now|subscribe|donate|transfer|"
                       r"купи|плати|плащане|поръчай|поръчка|абонирай|преведи)\w*", re.I)
_IRREVERSIBLE = re.compile(r"\b(delete|remove|send|submit|post|publish|share|confirm|unfollow|block|"
                           r"изтрий|премахни|изпрати|прати|публикувай|сподели|потвърди|блокирай)\w*", re.I)
_POWER = re.compile(r"\b(shut ?down|restart|reboot|hibernate|sleep|log ?off|sign ?out|"
                    r"изключи|рестартирай|приспи)\b", re.I)

# A pipeline segment may start with one of these read-only commands.
_READ_ONLY_START = re.compile(
    r"^(get|test|measure|select|where|sort|format|out-string|out-host|group|compare|find|resolve|convertto|"
    r"convertfrom|join-path|split-path|write-output|write-host|ls|dir|gci|gps|ps|cat|type|echo|pwd|whoami|"
    r"hostname|ipconfig|systeminfo|tasklist|where\.exe|%|\?|foreach-object|select-string)\b", re.I)
# Every Verb-Noun cmdlet anywhere in the command (including inside { } blocks) must use one of these verbs.
_READ_ONLY_VERBS = {"get", "test", "measure", "select", "where", "sort", "format", "group", "compare", "find",
                    "resolve", "convertto", "convertfrom", "join", "split", "foreach"}
_READ_ONLY_CMDLETS = {"out-string", "out-host", "write-output", "write-host", "write-verbose"}
# Aliases/commands that change things, wherever they appear.
_WRITE_ALIASES = re.compile(r"(?<![\w$.-])(rm|ri|del|erase|rd|rmdir|mv|mi|move|cp|copy|cpi|ren|rni|kill|spps|"
                            r"saps|start|ni|mkdir|md|iex|icm|iwr|irm|curl|wget|sal|nal|sv|set|clc|clv|tee|"
                            r"taskkill|shutdown|sc|reg|net|netsh|schtasks|powershell|pwsh|cmd)(?![\w-])", re.I)
_SAFE_METHODS = {"tostring", "toupper", "tolower", "trim", "trimend", "trimstart", "substring", "split",
                 "replace", "contains", "startswith", "endswith", "gettype", "equals", "padleft", "padright",
                 "adddays", "addhours", "addminutes", "addseconds", "addmonths", "addyears", "round"}
_REMOVAL = r"(remove-item|(?<![\w-])(ri|rm|del|erase|rd|rmdir)(?![\w-]))"
_DANGEROUS_PS = re.compile(
    r"format-volume|clear-disk|remove-partition|set-executionpolicy|set-mppreference|disable-|bcdedit|"
    r"reg(\.exe)?\s+delete|remove-itemproperty|netsh\s+advfirewall|net\s+user|net\s+localgroup|cipher\s+/w|"
    r"vssadmin|wbadmin|takeown|icacls|" + _REMOVAL + r"[^|;]*(-recurse|\s/s\b|\s-r\b)|del\s+/[sq]|"
    r"invoke-webrequest.*\|\s*iex|iex\s*\(|invoke-expression|set-localuser|remove-localuser|"
    r"stop-computer|restart-computer", re.I)


def _powershell_risk(command: str) -> Risk:
    if _DANGEROUS_PS.search(command):
        return Risk.HIGH
    if re.search(r"[>;&`]|\$\(|::", command) or _WRITE_ALIASES.search(command):
        return Risk.MEDIUM
    for verb, noun in re.findall(r"(?<![\w$.\\/:-])([A-Za-z]+)-([A-Za-z]+)\b", command):
        if verb.lower() not in _READ_ONLY_VERBS and f"{verb}-{noun}".lower() not in _READ_ONLY_CMDLETS:
            return Risk.MEDIUM   # e.g. Remove-Item / Stop-Process hidden inside ForEach-Object { … }
    if any(m.lower() not in _SAFE_METHODS for m in re.findall(r"\.(\w+)\s*\(", command)):
        return Risk.MEDIUM       # e.g. (Get-Item x).Delete()
    segments = [s.strip() for s in command.split("|") if s.strip()]
    if segments and all(_READ_ONLY_START.match(s) for s in segments):
        return Risk.LOW
    return Risk.MEDIUM


def _ui_target_risk(text: str) -> Risk:
    if _PURCHASE.search(text):
        return Risk.HIGH
    if _IRREVERSIBLE.search(text):
        return Risk.MEDIUM
    return Risk.LOW


def classify(tool: str, args: dict) -> Risk:
    action = str(args.get("action", "")).strip().lower()
    if tool == "send_message":
        return Risk.MEDIUM
    if tool == "system_command":
        return _powershell_risk(str(args.get("command", "")))
    if tool == "file_controller":
        if action in {"delete", "move", "rename", "write", "organize_desktop"}:
            return Risk.MEDIUM
        target = f"{args.get('path', '')}/{args.get('name', '')}".lower().rstrip("/")
        if action == "open" and target.endswith((".exe", ".msi", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".lnk", ".scr")):
            return Risk.MEDIUM   # "opening" these runs a program
        return Risk.LOW
    if tool == "desktop_control":
        return Risk.MEDIUM if action in {"organize", "clean"} else Risk.LOW
    if tool == "computer_settings":
        text = f"{action} {args.get('description', '')}"
        return Risk.MEDIUM if _POWER.search(text) else Risk.LOW
    if tool == "browser_control":
        if action == "send_message":
            return Risk.MEDIUM
        if action in {"click", "smart_click", "press", "fill_form"}:
            return _ui_target_risk(f"{args.get('text', '')} {args.get('description', '')} {args.get('selector', '')}")
        return Risk.LOW
    if tool == "computer_control" and action in {"click", "screen_click", "double_click"}:
        return _ui_target_risk(str(args.get("description", "")))
    if tool == "code_helper" and action in {"run", "build"}:
        return Risk.MEDIUM
    if tool == "dev_agent":
        return Risk.MEDIUM
    if tool == "game_updater" and args.get("shutdown_when_done"):
        return Risk.MEDIUM
    if tool == "memory_forget" and str(args.get("scope", "")).lower() == "all":
        return Risk.MEDIUM
    return Risk.LOW


def describe(tool: str, args: dict) -> str:
    """Plain-language description of the exact action, shown to the user before confirming."""
    action = str(args.get("action", "")).strip()
    if tool == "send_message" or (tool == "browser_control" and action == "send_message"):
        text = args.get("message_text") or args.get("message") or ""
        return f"Send to {args.get('receiver', '?')} on {args.get('platform', '?')}: \"{text}\""
    if tool == "file_controller":
        target = "/".join(str(p) for p in (args.get("path"), args.get("name")) if p)
        if action == "delete":
            return f"Move {target} to the Recycle Bin"
        if action in {"move", "rename"}:
            return f"{action.title()} {target} -> {args.get('destination') or args.get('new_name')}"
        if action == "write":
            return f"Overwrite {target}"
        return f"{action} {target}".strip()
    if tool == "system_command":
        return f"Run PowerShell: {args.get('command', '')}"
    if tool in {"browser_control", "computer_control"}:
        target = args.get("description") or args.get("text") or args.get("selector") or ""
        return f"{action} \"{target}\"".strip()
    if tool == "dev_agent":
        return f"Build and run a project: {args.get('description', '')[:120]}"
    details = ", ".join(f"{k}={v}" for k, v in args.items() if k != "action")
    return f"{tool} {action} {details}".strip()


# ── Policy ────────────────────────────────────────────────────────────────────

def load_policy(path: Path = POLICY_PATH) -> dict:
    """``{"auto_approve_medium": ["tool_name", ...]}``; HIGH is never auto-approved."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            approved = data.get("auto_approve_medium", [])
            return {"auto_approve_medium": {str(t) for t in approved} if isinstance(approved, list) else set()}
    except (OSError, ValueError):
        pass
    return {"auto_approve_medium": set()}


# ── User replies ──────────────────────────────────────────────────────────────

_YES = re.compile(r"\b(yes|yeah|yep|yup|sure|confirm(ed)?|correct|affirmative|ok(ay)?|go ahead|do it|send it|"
                  r"да|добре|давай|потвърждавам|потвърди|изпрати|пращай|точно|разбира се|ок|"
                  r"evet|tamam)\b", re.I)
_NO = re.compile(r"\b(no|nope|don'?t|do not|cancel|stop|wait|hold on|but|instead|change|wrong|never ?mind|"
                 r"не|недей|откажи|отказ|спри|чакай|но|вместо|промени|грешно|"
                 r"hayır|iptal)\b", re.I)


def is_affirmative(text: str) -> bool:
    text = text or ""
    return bool(_YES.search(text)) and not _NO.search(text)


# ── Gate ──────────────────────────────────────────────────────────────────────

class ConfirmationError(Exception):
    pass


@dataclass
class PendingAction:
    id: str
    tool: str
    args: dict
    risk: Risk
    summary: str
    created: float

    def to_prompt(self) -> dict:
        return {
            "status": "confirmation_required",
            "confirmation_id": self.id,
            "risk": self.risk.label,
            "action": self.summary,
            "instruction": ("Nothing has been executed. Ask the user one short question to confirm this exact "
                            "action. After the user answers yes, call confirm_action with this confirmation_id. "
                            "If they decline or change details, call cancel_action."),
        }


@dataclass
class Decision:
    allowed: bool
    risk: Risk
    pending: PendingAction | None = None


@dataclass
class ConfirmationGate:
    policy_loader: Callable[[], dict] = load_policy
    clock: Callable[[], float] = time.monotonic
    ttl: float = CONFIRMATION_TTL_SECONDS
    _pending: dict = field(default_factory=dict)
    _inputs: list = field(default_factory=list)
    _history: dict = field(default_factory=dict)   # id → (state, summary) for actions no longer pending
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def note_user_input(self, text: str) -> None:
        """Record what the user actually said/typed (input transcription or text box)."""
        if text and text.strip():
            with self._lock:
                self._inputs.append((self.clock(), text.strip()))
                del self._inputs[:-50]

    def check(self, tool: str, args: dict) -> Decision:
        risk = classify(tool, args)
        if risk == Risk.LOW:
            return Decision(True, risk)
        if risk == Risk.MEDIUM and tool in self.policy_loader()["auto_approve_medium"]:
            return Decision(True, risk)
        pending = PendingAction(secrets.token_hex(3), tool, dict(args), risk, describe(tool, args), self.clock())
        with self._lock:
            self._expire()
            for key, old in list(self._pending.items()):
                if old.tool == tool:   # a new proposal replaces the previous one of the same kind
                    self._retire(key, "replaced")
            self._pending[pending.id] = pending
        return Decision(False, risk, pending)

    def pending(self) -> list[PendingAction]:
        with self._lock:
            self._expire()
            return list(self._pending.values())

    def confirm(self, confirmation_id: str = "") -> PendingAction:
        with self._lock:
            self._expire()
            action = self._select(confirmation_id)
            reply = self._latest_reply(action.created)
            if not reply:
                raise ConfirmationError("The user has not replied yet. Ask them to confirm; do not confirm on their behalf.")
            if not is_affirmative(reply):
                raise ConfirmationError(f"The user's latest reply was not a clear yes: \"{reply[-120:]}\". "
                                        "Ask again (short yes/no question) or call cancel_action.")
            self._retire(action.id, "done")
            return action

    def cancel(self, confirmation_id: str = "") -> list[PendingAction]:
        with self._lock:
            keys = [confirmation_id] if confirmation_id else list(self._pending)
            removed = [self._pending[k] for k in keys if k in self._pending]
            for action in removed:
                self._retire(action.id, "cancelled")
            return removed

    def _latest_reply(self, since: float) -> str:
        """The user's most recent utterance after ``since`` (transcript chunks < 4 s apart belong together)."""
        replies = [(at, text) for at, text in self._inputs if at >= since]
        if not replies:
            return ""
        group = [replies[-1]]
        for at, text in reversed(replies[:-1]):
            if group[0][0] - at > REPLY_GAP_SECONDS:
                break
            group.insert(0, (at, text))
        return " ".join(text for _, text in group)

    _STATES = {
        "done": "already ran — do not confirm it again; if the user wants to retry, propose the action anew",
        "cancelled": "was cancelled",
        "expired": "expired because there was no answer for 5 minutes — propose it again",
        "replaced": "was replaced by a newer proposal",
    }

    def _select(self, confirmation_id: str) -> PendingAction:
        if confirmation_id and confirmation_id in self._pending:
            return self._pending[confirmation_id]
        if not confirmation_id and len(self._pending) == 1:
            return next(iter(self._pending.values()))
        waiting = "; ".join(f"{p.id} ({p.summary})" for p in self._pending.values())
        if confirmation_id in self._history:
            state, summary = self._history[confirmation_id]
            raise ConfirmationError(f"Action {confirmation_id} ({summary}) {self._STATES[state]}."
                                    + (f" Waiting for confirmation now: {waiting}." if waiting else ""))
        if not self._pending:
            raise ConfirmationError("There is no action waiting for confirmation. Propose the action again.")
        raise ConfirmationError(f"Unknown confirmation_id. Waiting for confirmation: {waiting}.")

    def _retire(self, key: str, state: str) -> None:
        action = self._pending.pop(key, None)
        if action is not None:
            self._history[key] = (state, action.summary)
            for old in list(self._history)[:-50]:
                del self._history[old]

    def _expire(self) -> None:
        now = self.clock()
        for key in [k for k, p in self._pending.items() if now - p.created > self.ttl]:
            self._retire(key, "expired")
