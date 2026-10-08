"""Central configuration.

Secrets and machine-specific values come from environment variables, loaded
from a project-level ``.env`` file. ``config/api_keys.json`` is still read as a
legacy fallback so existing installs keep working until migrated:

    python -m core.settings --migrate
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"
LEGACY_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

_loaded = False


def _parse_env(text: str) -> dict[str, str]:
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            values[key] = value
    return values


def load_env(path: Path = ENV_PATH) -> None:
    """Load .env once. Real environment variables take precedence over the file."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        for key, value in _parse_env(path.read_text(encoding="utf-8")).items():
            os.environ.setdefault(key, value)
    except OSError:
        pass


def _legacy_config() -> dict:
    try:
        data = json.loads(LEGACY_CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get(name: str, default: str = "", legacy_key: str | None = None) -> str:
    load_env()
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if legacy_key:
        legacy = _legacy_config().get(legacy_key)
        if isinstance(legacy, str) and legacy.strip():
            return legacy.strip()
    return default


def get_int(name: str, default: int) -> int:
    try:
        return int(get(name, str(default)))
    except ValueError:
        return default


def gemini_api_key() -> str:
    key = get("GEMINI_API_KEY", legacy_key="gemini_api_key")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not configured. Add it to .env (see .env.example).")
    return key


def has_gemini_api_key() -> bool:
    return bool(get("GEMINI_API_KEY", legacy_key="gemini_api_key"))


def live_model() -> str:
    return get("JARVIS_LIVE_MODEL", "models/gemini-2.5-flash-native-audio-preview-12-2025")


def live_voice() -> str:
    return get("JARVIS_VOICE", "Charon")


def set_env_value(name: str, value: str) -> None:
    """Create or replace ``name`` in .env and apply it to this process."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    lines = [line for line in lines if line.split("=", 1)[0].strip().removeprefix("export ").strip() != name]
    lines.append(f"{name}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[name] = value


def save_gemini_key(key: str) -> None:
    set_env_value("GEMINI_API_KEY", key.strip())


def migrate_legacy_key() -> str:
    """Move gemini_api_key from config/api_keys.json into .env."""
    config = _legacy_config()
    key = str(config.get("gemini_api_key") or "").strip()
    if not key:
        return "No key in config/api_keys.json; nothing to migrate."
    load_env()
    if not os.environ.get("GEMINI_API_KEY"):
        save_gemini_key(key)
    config["gemini_api_key"] = ""
    LEGACY_CONFIG_PATH.write_text(json.dumps(config, indent=4), encoding="utf-8")
    return f"Moved the Gemini key to {ENV_PATH} and removed it from {LEGACY_CONFIG_PATH.name}."


if __name__ == "__main__":
    if "--migrate" in sys.argv:
        print(migrate_legacy_key())
    else:
        print(f".env: {ENV_PATH} ({'found' if ENV_PATH.exists() else 'missing'})")
        print(f"Gemini key configured: {has_gemini_api_key()}")
