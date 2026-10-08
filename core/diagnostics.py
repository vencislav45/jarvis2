"""Self-diagnostics ("Jarvis, check yourself").

Every check inspects the real environment and reports ok / warning / failed with a
short detail. Nothing here changes the system.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from core import settings

REQUIRED_PACKAGES = {
    "google.genai": "Gemini API", "sounddevice": "audio", "PyQt6": "interface", "playwright": "browser automation",
    "pyautogui": "desktop control", "psutil": "system monitor", "requests": "web requests",
    "bs4": "web page parsing", "mss": "screen capture", "pyperclip": "clipboard",
}
WINDOWS_PACKAGES = {"pywinauto": "Windows UI automation", "pycaw": "volume control", "win32api": "Windows API"}

_LOCAL = Path(os.environ.get("LOCALAPPDATA", ""))
_PROGRAMS = [Path(os.environ.get(v, "")) for v in ("ProgramFiles", "ProgramFiles(x86)")]

BROWSERS = {
    "Chrome": [p / "Google/Chrome/Application/chrome.exe" for p in [*_PROGRAMS, _LOCAL]],
    "Edge": [p / "Microsoft/Edge/Application/msedge.exe" for p in _PROGRAMS],
}
APPS = {
    "DaVinci Resolve": [p / "Blackmagic Design/DaVinci Resolve/Resolve.exe" for p in _PROGRAMS],
    "Viber": [_LOCAL / "Viber/Viber.exe"],
    "WhatsApp": [],  # Microsoft Store app — detected via its package below
}


@dataclass
class Check:
    name: str
    status: str   # ok | warning | failed
    detail: str

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def _first_existing(paths) -> Path | None:
    return next((p for p in paths if str(p) not in ("", ".") and p.exists()), None)


def _store_app_installed(name: str) -> bool:
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                              f"(Get-AppxPackage -Name '*{name}*' | Select-Object -First 1).Name"],
                             capture_output=True, text=True, timeout=15)
        return bool(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def check_python() -> Check:
    v = sys.version_info
    ok = (3, 11) <= (v.major, v.minor) < (3, 13)
    return Check("Python", "ok" if ok else "warning", f"{v.major}.{v.minor}.{v.micro}" + ("" if ok else " (3.11 or 3.12 supported)"))


def check_packages() -> list[Check]:
    wanted = dict(REQUIRED_PACKAGES, **(WINDOWS_PACKAGES if os.name == "nt" else {}))
    missing = []
    for module, purpose in wanted.items():
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append(f"{module} ({purpose})")
    if missing:
        return [Check("Packages", "failed", "Missing: " + ", ".join(missing))]
    return [Check("Packages", "ok", f"{len(wanted)} required packages present")]


def check_api() -> Check:
    if not settings.has_gemini_api_key():
        return Check("Gemini API key", "failed", "Not configured — set GEMINI_API_KEY in .env")
    source = ".env/environment" if os.environ.get("GEMINI_API_KEY") else "config/api_keys.json (legacy — run python -m core.settings --migrate)"
    return Check("Gemini API key", "ok" if os.environ.get("GEMINI_API_KEY") else "warning", f"Configured in {source}")


def check_audio() -> list[Check]:
    try:
        import sounddevice as sd
        results = []
        for kind, label in (("input", "Microphone"), ("output", "Speakers")):
            try:
                device = sd.query_devices(kind=kind)
                results.append(Check(label, "ok", device["name"]))
            except Exception as exc:  # noqa: BLE001 - PortAudio raises various types
                results.append(Check(label, "failed", f"No default {kind} device: {exc}"))
        return results
    except Exception as exc:  # noqa: BLE001
        return [Check("Audio", "failed", f"sounddevice unavailable: {exc}")]


def check_browsers() -> list[Check]:
    results = []
    for name, paths in BROWSERS.items():
        path = _first_existing(paths) or (shutil.which("chrome") if name == "Chrome" else None)
        results.append(Check(name, "ok" if path else "warning", str(path) if path else "Not found"))
    if not any(r.status == "ok" for r in results):
        pw_dir = _LOCAL / "ms-playwright"
        has_chromium = pw_dir.exists() and any(pw_dir.glob("chromium*"))
        results.append(Check("Playwright browser", "ok" if has_chromium else "failed",
                             "Bundled Chromium installed" if has_chromium else
                             "No Chrome/Edge found — run: python -m playwright install chromium"))
    return results


def check_chrome_bridge() -> Check:
    from actions.chrome_bridge import browser_mode, get_bridge
    if browser_mode() != "my_chrome":
        return Check("Chrome bridge", "ok", "Not used (JARVIS_BROWSER_MODE=separate)")
    bridge = get_bridge()
    status = bridge.status()
    if bridge.connected():
        return Check("Chrome bridge", "ok", status)
    return Check("Chrome bridge", "failed" if bridge.error else "warning",
                 status + ("" if bridge.ever_connected() or bridge.error else
                           " — needed to read, click and message inside your Chrome (see chrome_extension/)"))


def check_apps() -> list[Check]:
    results = []
    for name, paths in APPS.items():
        path = _first_existing(paths)
        if path:
            results.append(Check(name, "ok", str(path)))
        elif name == "WhatsApp" and _store_app_installed("WhatsApp"):
            results.append(Check(name, "ok", "Microsoft Store app installed"))
        else:
            results.append(Check(name, "warning", "Not installed or not in the usual location"))
    return results


def check_tools(registry) -> Check:
    if registry is None:
        return Check("Tools", "warning", "Registry not available")
    names = registry.names()
    return Check("Tools", "ok", f"{len(names)} tools registered")


def check_logs() -> Check:
    from core.action_log import LOG_DIR
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        probe = LOG_DIR / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return Check("Action log", "ok", str(LOG_DIR))
    except OSError as exc:
        return Check("Action log", "failed", f"Cannot write logs: {exc}")


def run_all(registry=None) -> dict:
    checks: list[Check] = [check_python(), *check_packages(), check_api(), *check_audio(),
                           *check_browsers(), check_chrome_bridge(), *check_apps(), check_tools(registry), check_logs()]
    failed = [c.name for c in checks if c.status == "failed"]
    warnings = [c.name for c in checks if c.status == "warning"]
    if not failed and not warnings:
        summary = "Everything is operational."
    elif not failed:
        summary = "Operational. Warnings: " + ", ".join(warnings) + "."
    else:
        summary = "Problems found: " + ", ".join(failed) + "." + (" Warnings: " + ", ".join(warnings) + "." if warnings else "")
    return {"summary": summary, "checks": [c.as_dict() for c in checks]}


def format_report(report: dict) -> str:
    icon = {"ok": "OK  ", "warning": "WARN", "failed": "FAIL"}
    lines = [report["summary"], ""]
    lines += [f"[{icon[c['status']]}] {c['name']}: {c['detail']}" for c in report["checks"]]
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_report(run_all()))
