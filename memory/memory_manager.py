import json
import re
from datetime import datetime
from threading import Lock
from pathlib import Path
import sys


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR         = get_base_dir()
MEMORY_PATH      = BASE_DIR / "memory" / "long_term.json"
CONVERSATION_PATH = BASE_DIR / "memory" / "conversation_history.json"
_lock            = Lock()
_conversation_lock = Lock()
MAX_VALUE_LENGTH = 380
MEMORY_MAX_CHARS = 2200
CONVERSATION_MAX_TURNS = 2000

def _empty_memory() -> dict:
    return {
        "identity":      {},
        "preferences":   {},
        "projects":      {},
        "relationships": {},
        "tasks":         {},
        "wishes":        {},
        "notes":         {},
    }


CATEGORIES = tuple(_empty_memory())
TEMPORARY = "temporary"
CATEGORY_ALIASES = {
    "people": "relationships", "person": "relationships", "contacts": "relationships",
    "preference": "preferences", "project": "projects", "task": "tasks", "todo": "tasks",
    "temp": TEMPORARY, "session": TEMPORARY,
}

# Saved during this process, newest last — lets "forget what I just told you" undo them.
_session_saves: list[tuple[str, str]] = []
# Temporary context is held only in this process and never written to disk.
_temporary: dict[str, str] = {}


def normalize_category(category: str) -> str:
    category = (category or "notes").strip().lower()
    category = CATEGORY_ALIASES.get(category, category)
    return category if category in CATEGORIES or category == TEMPORARY else "notes"

def load_memory() -> dict:
    if not MEMORY_PATH.exists():
        return _empty_memory()
    with _lock:
        try:
            data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base = _empty_memory()
                for key in base:
                    if key not in data:
                        data[key] = {}
                return data
            return _empty_memory()
        except Exception as e:
            print(f"[Memory] ⚠️ Load error: {e}")
            return _empty_memory()

def _all_entries(memory: dict) -> list[tuple]:
    entries = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            if isinstance(entry, dict) and "value" in entry:
                entries.append((cat, key, entry))
    return entries


def _trim_to_limit(memory: dict) -> dict:
    if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
        return memory
    entries = _all_entries(memory)
    entries.sort(key=lambda t: t[2].get("updated", "0000-00-00"))
    for cat, key, _ in entries:
        if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
            break
        del memory[cat][key]
        print(f"[Memory] 🗑️  Trimmed {cat}/{key}")
    return memory

def save_memory(memory: dict) -> None:
    if not isinstance(memory, dict):
        return
    memory = _trim_to_limit(memory)
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        MEMORY_PATH.write_text(
            json.dumps(memory, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _truncate_value(val: str) -> str:
    if isinstance(val, str) and len(val) > MAX_VALUE_LENGTH:
        return val[:MAX_VALUE_LENGTH].rstrip() + "…"
    return val


def _recursive_update(target: dict, updates: dict) -> bool:
    changed = False
    for key, value in updates.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, dict) and "value" not in value:
            if key not in target or not isinstance(target[key], dict):
                target[key] = {}
                changed = True
            if _recursive_update(target[key], value):
                changed = True
        else:
            new_val  = _truncate_value(str(value["value"] if isinstance(value, dict) else value))
            entry    = {"value": new_val, "updated": datetime.now().strftime("%Y-%m-%d")}
            existing = target.get(key, {})
            if not isinstance(existing, dict) or existing.get("value") != new_val:
                target[key] = entry
                changed = True
    return changed


def update_memory(memory_update: dict) -> dict:
    if not isinstance(memory_update, dict) or not memory_update:
        return load_memory()
    memory = load_memory()
    if _recursive_update(memory, memory_update):
        save_memory(memory)
        print(f"[Memory] 💾 Saved: {list(memory_update.keys())}")
    return memory

def format_memory_for_prompt(memory: dict | None) -> str:
    if not memory:
        return ""

    lines = []

    identity  = memory.get("identity", {})
    id_fields = ["name", "age", "birthday", "city", "job", "language", "school", "nationality"]
    for field in id_fields:
        entry = identity.get(field)
        if entry:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"{field.title()}: {val}")
    for key, entry in identity.items():
        if key in id_fields:
            continue
        val = entry.get("value") if isinstance(entry, dict) else entry
        if val:
            lines.append(f"{key.replace('_', ' ').title()}: {val}")

    prefs = memory.get("preferences", {})
    if prefs:
        lines.append("")
        lines.append("Preferences:")
        for key, entry in list(prefs.items())[:15]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key.replace('_', ' ').title()}: {val}")

    projects = memory.get("projects", {})
    if projects:
        lines.append("")
        lines.append("Active Projects / Goals:")
        for key, entry in list(projects.items())[:8]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key.replace('_', ' ').title()}: {val}")

    rels = memory.get("relationships", {})
    if rels:
        lines.append("")
        lines.append("People in their life:")
        for key, entry in list(rels.items())[:10]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key.replace('_', ' ').title()}: {val}")

    tasks = memory.get("tasks", {})
    if tasks:
        lines.append("")
        lines.append("Open tasks:")
        for key, entry in list(tasks.items())[:8]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key.replace('_', ' ').title()}: {val}")

    wishes = memory.get("wishes", {})
    if wishes:
        lines.append("")
        lines.append("Wishes / Plans / Wants:")
        for key, entry in list(wishes.items())[:8]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key.replace('_', ' ').title()}: {val}")

    notes = memory.get("notes", {})
    if notes:
        lines.append("")
        lines.append("Other notes:")
        for key, entry in list(notes.items())[:8]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key}: {val}")

    if not lines:
        return ""

    header = "[WHAT YOU KNOW ABOUT THIS PERSON — use naturally, never recite like a list]\n"
    result = header + "\n".join(lines)
    if len(result) > 2000:
        result = result[:1997] + "…"

    return result + "\n"

_SENSITIVE_KEY = re.compile(r"pass(word)?|парол|pin\b|пин\b|cvv|cvc|card|карт|iban|egn|егн|ssn|passport|паспорт|"
                            r"secret|token|api.?key|ключ|2fa|otp|код за", re.I)
_SENSITIVE_VALUE = re.compile(r"\b(?:\d[ -]?){13,19}\b|"            # card-like numbers
                              r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b|"   # IBAN
                              r"\b(?:ЕГН|EGN)\b|"                    # personal ID numbers (by label;
                              r"\b(AIza|sk-|ghp_|xox[bp]-)[\w-]{10,}",  # phone numbers stay allowed)
                              re.I)


def is_sensitive(key: str, value: str) -> bool:
    return bool(_SENSITIVE_KEY.search(key or "") or _SENSITIVE_VALUE.search(value or ""))


def remember(key: str, value: str, category: str = "notes") -> str:
    category = normalize_category(category)
    key = re.sub(r"[^\w]+", "_", (key or "").strip().lower()).strip("_")
    value = (value or "").strip()
    if not key or not value:
        return "Nothing saved: a key and a value are required."
    if is_sensitive(key, value):
        return ("Not saved: this looks like a password, card/bank/ID number or access key, which I never store. "
                "Tell the user that.")
    if category == TEMPORARY:
        _temporary[key] = _truncate_value(value)
    else:
        update_memory({category: {key: {"value": value}}})
    _session_saves.append((category, key))
    return f"Remembered: {category}/{key}"


def forget(key: str, category: str = "notes") -> str:
    category = normalize_category(category)
    if category == TEMPORARY:
        return f"Forgotten: {category}/{key}" if _temporary.pop(key, None) is not None else f"Not found: {category}/{key}"
    memory = load_memory()
    cat    = memory.get(category, {})
    if key in cat:
        del cat[key]
        memory[category] = cat
        save_memory(memory)
        return f"Forgotten: {category}/{key}"
    return f"Not found: {category}/{key}"


forget_memory = forget


def forget_last(count: int = 1) -> str:
    """Undo the most recent saves made in this session."""
    removed = []
    for _ in range(max(1, min(count, 10))):
        if not _session_saves:
            break
        category, key = _session_saves.pop()
        if forget(key, category).startswith("Forgotten"):
            removed.append(f"{category}/{key}")
    return ("Forgotten: " + ", ".join(removed)) if removed else "Nothing was saved in this session to forget."


def forget_matching(query: str, category: str = "") -> str:
    """Remove saved entries whose key or value contains every word of ``query``."""
    terms = [t for t in re.findall(r"[^\W_]{2,}", (query or "").casefold())]
    if not terms:
        return "Say what to forget (a name, topic, or detail)."
    categories = [normalize_category(category)] if category else [*CATEGORIES, TEMPORARY]
    removed = []
    for key, value in list(_temporary.items()):
        if TEMPORARY in categories and all(t in f"{key} {value}".casefold() for t in terms):
            del _temporary[key]
            removed.append(f"{TEMPORARY}/{key}")
    memory = load_memory()
    for cat in categories:
        items = memory.get(cat)
        if not isinstance(items, dict):
            continue
        for key, entry in list(items.items()):
            value = entry.get("value", "") if isinstance(entry, dict) else str(entry)
            if all(t in f"{key} {value}".casefold() for t in terms):
                del items[key]
                removed.append(f"{cat}/{key}")
    if any(not r.startswith(TEMPORARY) for r in removed):
        save_memory(memory)
    return ("Forgotten: " + ", ".join(removed)) if removed else "No saved memory matched that."


def clear_temporary() -> str:
    count = len(_temporary)
    _temporary.clear()
    return f"Cleared {count} temporary item(s)."


def clear_all_memory() -> str:
    """Erase every saved fact (conversation history is kept). Callers must confirm first."""
    _temporary.clear()
    _session_saves.clear()
    memory = load_memory()
    sessions = memory.get("sessions", [])
    fresh = _empty_memory()
    fresh["sessions"] = sessions if isinstance(sessions, list) else []
    save_memory(fresh)
    return "All saved facts were erased."


def list_memory(category: str = "") -> str:
    categories = [normalize_category(category)] if category else [*CATEGORIES, TEMPORARY]
    memory = load_memory()
    lines = []
    for cat in categories:
        items = _temporary if cat == TEMPORARY else memory.get(cat, {})
        for key, entry in (items or {}).items():
            value = entry.get("value", "") if isinstance(entry, dict) else str(entry)
            lines.append(f"{cat}/{key}: {value}")
    return "\n".join(lines) if lines else "Nothing is saved in that category."


# ── Session memory ─────────────────────────────────────────────────────────────

_SESSION_MAX = 3   # safety cap — in practice 0-1 entries after pop


def save_session_summary(summary: str, language: str = "") -> None:
    """Append a 1-2 sentence session summary to long_term.json['sessions']."""
    summary = (summary or "").strip()
    if not summary:
        return
    memory   = load_memory()
    sessions = memory.get("sessions", [])
    if not isinstance(sessions, list):
        sessions = []
    entry: dict = {
        "date":    datetime.now().strftime("%Y-%m-%d"),
        "summary": summary[:280],
    }
    if language:
        entry["language"] = language
    sessions.append(entry)
    memory["sessions"] = sessions[-_SESSION_MAX:]
    with _lock:
        MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        MEMORY_PATH.write_text(
            json.dumps(memory, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    print(f"[Memory] 📝 Session saved ({entry['date']}): {summary[:60]}…")


def pop_last_session() -> dict | None:
    """
    Return AND remove the most recent session entry.
    Calling this consumes the entry so it is never repeated in future briefings.
    """
    with _lock:
        if not MEMORY_PATH.exists():
            return None
        try:
            memory   = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
            sessions = memory.get("sessions", [])
            if not isinstance(sessions, list) or not sessions:
                return None
            entry = sessions.pop()          # remove the last entry
            memory["sessions"] = sessions
            MEMORY_PATH.write_text(
                json.dumps(memory, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            return entry
        except Exception as e:
            print(f"[Memory] ⚠️ pop_last_session error: {e}")
            return None


def append_conversation_turn(speaker: str, text: str) -> None:
    """Persist completed dialogue locally for context in later launches."""
    text = (text or "").strip()
    speaker = (speaker or "").strip()
    if not text or not speaker:
        return
    with _conversation_lock:
        try:
            history = json.loads(CONVERSATION_PATH.read_text(encoding="utf-8"))
            if not isinstance(history, list):
                history = []
            history = [item for item in history if isinstance(item, dict)]
        except (OSError, ValueError):
            history = []
        if history and history[-1].get("speaker") == speaker and history[-1].get("text") == text:
            return
        history.append({"speaker": speaker[:32], "text": text[:4000],
                        "time": datetime.now().isoformat(timespec="minutes")})
        history = history[-CONVERSATION_MAX_TURNS:]
        CONVERSATION_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONVERSATION_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def load_recent_conversation(limit: int = 12) -> list[dict]:
    """Load recent persisted dialogue, tolerating missing or damaged history files."""
    try:
        history = json.loads(CONVERSATION_PATH.read_text(encoding="utf-8"))
        if isinstance(history, list):
            return [item for item in history[-max(0, limit):] if isinstance(item, dict)]
    except (OSError, ValueError):
        pass
    return []


def search_conversation_history(query: str, limit: int = 5) -> str:
    """Search saved dialogue and durable facts for earlier details, newest first."""
    terms = set(re.findall(r"[^\W_]{3,}", (query or "").casefold(), flags=re.UNICODE))
    terms -= {
        "what", "when", "where", "which", "who", "have", "tell", "said", "about", "that", "with", "from",
        "какво", "какви", "кога", "къде", "който", "която", "което", "това", "тези", "беше", "били", "кажи", "ми",
    }
    if not terms:
        return "Please search again with a specific name, topic, or detail."

    matches: list[tuple[int, str]] = []
    history = load_recent_conversation(CONVERSATION_MAX_TURNS)
    for item in reversed(history):
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        haystack = f"{item.get('speaker', '')} {text}".casefold()
        score = sum(term in haystack for term in terms)
        if score:
            stamp = str(item.get("time", ""))
            speaker = str(item.get("speaker", "Unknown"))
            matches.append((score, f"{stamp} — {speaker}: {text[:700]}"))

    memory = load_memory()
    for category in (*CATEGORIES, TEMPORARY):
        items = _temporary if category == TEMPORARY else memory.get(category, {})
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            value = entry.get("value", "") if isinstance(entry, dict) else str(entry)
            haystack = f"{key} {value}".casefold()
            score = sum(term in haystack for term in terms)
            if score and value:
                matches.append((score + 1, f"Saved {category}/{key}: {value}"))

    if not matches:
        return "No matching saved conversation or memory was found."
    matches.sort(key=lambda result: result[0], reverse=True)
    return "\n".join(text for _, text in matches[:max(1, min(limit, 10))])
