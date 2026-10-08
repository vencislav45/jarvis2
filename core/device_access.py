"""Device access preferences, separate from API credentials."""
import json
import sys
from pathlib import Path

BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "device_access.json"


def full_access_enabled() -> bool:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("full_device_access") is True
    except (OSError, ValueError, AttributeError):
        return False


def path_allowed(target: Path) -> bool:
    resolved = target.expanduser().resolve()
    return full_access_enabled() or resolved.is_relative_to(Path.home().resolve())
