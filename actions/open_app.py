import os
import time
import subprocess
import platform
import shutil
import webbrowser

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

_SYSTEM = platform.system()

_WEB_APPS = {
    "instagram": "https://www.instagram.com/",
    "tiktok": "https://www.tiktok.com/",
    "netflix": "https://www.netflix.com/",
}

_APP_ALIASES: dict[str, dict[str, str]] = {

    "chrome":             {"Windows": "chrome",                  "Darwin": "Google Chrome",        "Linux": "google-chrome"},
    "google chrome":      {"Windows": "chrome",                  "Darwin": "Google Chrome",        "Linux": "google-chrome"},
    "firefox":            {"Windows": "firefox",                 "Darwin": "Firefox",              "Linux": "firefox"},
    "edge":               {"Windows": "msedge",                  "Darwin": "Microsoft Edge",       "Linux": "microsoft-edge"},
    "brave":              {"Windows": "brave",                   "Darwin": "Brave Browser",        "Linux": "brave-browser"},
    "safari":             {"Windows": "msedge",                  "Darwin": "Safari",               "Linux": "firefox"},
    "opera":              {"Windows": "opera",                   "Darwin": "Opera",                "Linux": "opera"},
    "whatsapp":           {"Windows": "WhatsApp",                "Darwin": "WhatsApp",             "Linux": "whatsapp"},
    "telegram":           {"Windows": "Telegram",                "Darwin": "Telegram",             "Linux": "telegram"},
    "discord":            {"Windows": "Discord",                 "Darwin": "Discord",              "Linux": "discord"},
    "slack":              {"Windows": "Slack",                   "Darwin": "Slack",                "Linux": "slack"},
    "zoom":               {"Windows": "Zoom",                    "Darwin": "zoom.us",              "Linux": "zoom"},
    "teams":              {"Windows": "msteams",                 "Darwin": "Microsoft Teams",      "Linux": "teams"},
    "skype":              {"Windows": "skype",                   "Darwin": "Skype",                "Linux": "skype"},
    "signal":             {"Windows": "signal",                  "Darwin": "Signal",               "Linux": "signal"},
    "spotify":            {"Windows": "Spotify",                 "Darwin": "Spotify",              "Linux": "spotify"},
    "vlc":                {"Windows": "vlc",                     "Darwin": "VLC",                  "Linux": "vlc"},
    "netflix":            {"Windows": "Netflix",                 "Darwin": "Netflix",              "Linux": "firefox"},
    "vscode":             {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "visual studio code": {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "code":               {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "terminal":           {"Windows": "wt",                      "Darwin": "Terminal",             "Linux": "x-terminal-emulator"},
    "cmd":                {"Windows": "cmd.exe",                 "Darwin": "Terminal",             "Linux": "bash"},
    "powershell":         {"Windows": "powershell.exe",          "Darwin": "Terminal",             "Linux": "bash"},
    "postman":            {"Windows": "Postman",                 "Darwin": "Postman",              "Linux": "postman"},
    "git":                {"Windows": "git-bash",                "Darwin": "Terminal",             "Linux": "bash"},
    "figma":              {"Windows": "Figma",                   "Darwin": "Figma",                "Linux": "figma"},
    "blender":            {"Windows": "blender",                 "Darwin": "Blender",              "Linux": "blender"},
    "word":               {"Windows": "winword",                 "Darwin": "Microsoft Word",       "Linux": "libreoffice --writer"},
    "excel":              {"Windows": "excel",                   "Darwin": "Microsoft Excel",      "Linux": "libreoffice --calc"},
    "powerpoint":         {"Windows": "powerpnt",                "Darwin": "Microsoft PowerPoint", "Linux": "libreoffice --impress"},
    "libreoffice":        {"Windows": "soffice",                 "Darwin": "LibreOffice",          "Linux": "libreoffice"},
    "notepad":            {"Windows": "notepad.exe",             "Darwin": "TextEdit",             "Linux": "gedit"},
    "textedit":           {"Windows": "notepad.exe",             "Darwin": "TextEdit",             "Linux": "gedit"},
    "explorer":           {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "file explorer":      {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "finder":             {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "task manager":       {"Windows": "taskmgr.exe",             "Darwin": "Activity Monitor",     "Linux": "gnome-system-monitor"},
    "settings":           {"Windows": "ms-settings:",            "Darwin": "System Preferences",   "Linux": "gnome-control-center"},
    "calculator":         {"Windows": "calc.exe",                "Darwin": "Calculator",           "Linux": "gnome-calculator"},
    "paint":              {"Windows": "mspaint.exe",             "Darwin": "Preview",              "Linux": "gimp"},
    "instagram":          {"Windows": "Instagram",               "Darwin": "Instagram",            "Linux": "firefox"},
    "tiktok":             {"Windows": "TikTok",                  "Darwin": "TikTok",               "Linux": "firefox"},
    "notion":             {"Windows": "Notion",                  "Darwin": "Notion",               "Linux": "notion"},
    "obsidian":           {"Windows": "Obsidian",                "Darwin": "Obsidian",             "Linux": "obsidian"},
    "capcut":             {"Windows": "CapCut",                  "Darwin": "CapCut",               "Linux": "capcut"},
    "steam":              {"Windows": "steam",                   "Darwin": "Steam",                "Linux": "steam"},
    "epic":               {"Windows": "EpicGamesLauncher",       "Darwin": "Epic Games Launcher",  "Linux": "legendary"},
    "epic games":         {"Windows": "EpicGamesLauncher",       "Darwin": "Epic Games Launcher",  "Linux": "legendary"},
}


def _normalize(raw: str) -> str:
    key = raw.lower().strip()

    if key in _APP_ALIASES:
        return _APP_ALIASES[key].get(_SYSTEM, raw)

    for alias_key, os_map in _APP_ALIASES.items():
        if alias_key in key or key in alias_key:
            return os_map.get(_SYSTEM, raw)

    return raw  

_SHELL_CHARS = set('&|<>^%"`;\r\n')


def _launch_windows(app_name: str) -> bool:
    # Never hand the name to a shell: "calc.exe & del ..." must not run a second command.
    if _SHELL_CHARS & set(app_name):
        print(f"[open_app] Refusing app name with shell characters: {app_name!r}")
        return False

    executable = shutil.which(app_name) or shutil.which(app_name.split(".")[0])
    if executable:
        try:
            subprocess.Popen([executable], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.5)
            return True
        except Exception as e:
            print(f"[open_app] subprocess failed: {e}")

    if ":" in app_name:   # protocol/URI apps such as ms-settings: or spotify:
        try:
            os.startfile(app_name)
            time.sleep(1.0)
            return True
        except Exception:
            pass

    try:
        import pyautogui
        pyautogui.PAUSE = 0.1
        pyautogui.press("win")
        time.sleep(0.7)
        pyautogui.write(app_name, interval=0.05)
        time.sleep(0.9)
        pyautogui.press("enter")
        time.sleep(2.5)
        return True
    except Exception as e:
        print(f"[open_app] Start Menu search failed: {e}")

    return False


def _launch_macos(app_name: str) -> bool:

    try:
        result = subprocess.run(
            ["open", "-a", app_name],
            capture_output=True, timeout=8
        )
        if result.returncode == 0:
            time.sleep(1.0)
            return True
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["open", "-a", f"{app_name}.app"],
            capture_output=True, timeout=8
        )
        if result.returncode == 0:
            time.sleep(1.0)
            return True
    except Exception:
        pass

    binary = shutil.which(app_name) or shutil.which(app_name.lower())
    if binary:
        try:
            subprocess.Popen(
                [binary],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(1.0)
            return True
        except Exception:
            pass

    try:
        import pyautogui
        pyautogui.hotkey("command", "space")
        time.sleep(0.6)
        pyautogui.write(app_name, interval=0.05)
        time.sleep(0.8)
        pyautogui.press("enter")
        time.sleep(1.5)
        return True
    except Exception as e:
        print(f"[open_app] Spotlight failed: {e}")

    return False


_LINUX_TERMINAL_FALLBACKS = [
    "x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal",
    "xterm", "lxterminal", "mate-terminal", "tilix", "alacritty", "kitty",
]

def _launch_linux(app_name: str) -> bool:

    # terminal emulators: try common ones in order
    if app_name in ("x-terminal-emulator", "gnome-terminal", "terminal"):
        for term in _LINUX_TERMINAL_FALLBACKS:
            if shutil.which(term):
                try:
                    subprocess.Popen([term], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    time.sleep(1.0)
                    return True
                except Exception:
                    continue

    binary = (
        shutil.which(app_name) or
        shutil.which(app_name.lower()) or
        shutil.which(app_name.lower().replace(" ", "-")) or
        shutil.which(app_name.lower().replace(" ", "_"))
    )
    if binary:
        try:
            subprocess.Popen(
                [binary],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(1.0)
            return True
        except Exception:
            pass

    try:
        subprocess.run(
            ["xdg-open", app_name],
            capture_output=True, timeout=5
        )
        return True
    except Exception:
        pass

    for desktop_name in [
        app_name.lower(),
        app_name.lower().replace(" ", "-"),
        app_name.lower().replace(" ", ""),
    ]:
        try:
            result = subprocess.run(
                ["gtk-launch", desktop_name],
                capture_output=True, timeout=5
            )
            if result.returncode == 0:
                return True
        except Exception:
            pass

    return False


_OS_LAUNCHERS = {
    "Windows": _launch_windows,
    "Darwin":  _launch_macos,
    "Linux":   _launch_linux,
}

# Install locations that are not on PATH. Override or add any app in .env with
# JARVIS_APP_<NAME>=C:\path\to\app.exe  (e.g. JARVIS_APP_DAVINCI_RESOLVE=...).
_KNOWN_PATHS = {
    "davinci resolve": [r"%ProgramFiles%\Blackmagic Design\DaVinci Resolve\Resolve.exe"],
    "resolve":         [r"%ProgramFiles%\Blackmagic Design\DaVinci Resolve\Resolve.exe"],
    "viber":           [r"%LOCALAPPDATA%\Viber\Viber.exe"],
}
# Process names used to tell whether an app is already running.
_PROCESS_NAMES = {
    "chrome": "chrome.exe", "google chrome": "chrome.exe", "edge": "msedge.exe", "firefox": "firefox.exe",
    "davinci resolve": "resolve.exe", "resolve": "resolve.exe", "viber": "viber.exe", "spotify": "spotify.exe",
    "discord": "discord.exe", "telegram": "telegram.exe", "steam": "steam.exe", "notepad": "notepad.exe",
}


def _configured_path(app_name: str) -> str | None:
    import os
    import re
    from core import settings
    env_name = "JARVIS_APP_" + re.sub(r"[^A-Z0-9]+", "_", app_name.upper()).strip("_")
    candidates = [settings.get(env_name)] + _KNOWN_PATHS.get(app_name.casefold(), [])
    for candidate in candidates:
        if candidate:
            path = os.path.expandvars(candidate)
            if os.path.exists(path):
                return path
    return None


def _app_windows(app_name: str) -> list:
    if _SYSTEM != "Windows":
        return []
    try:
        import pygetwindow
        needle = app_name.casefold()
        # Match "AppName" or "Document - AppName", not a browser tab that merely mentions the app.
        def matches(title: str) -> bool:
            title = title.casefold().replace("​", "").strip()
            return title == needle or title.startswith(needle + " - ") or title.endswith(("- " + needle, "– " + needle))
        return [w for w in pygetwindow.getAllWindows() if w.title and matches(w.title)]
    except Exception:
        return []


def _is_running(app_name: str) -> bool:
    process = _PROCESS_NAMES.get(app_name.casefold())
    if process and _PSUTIL:
        try:
            return any((p.info.get("name") or "").casefold() == process for p in psutil.process_iter(["name"]))
        except Exception:
            pass
    return bool(_app_windows(app_name))


def _bring_to_front(app_name: str) -> bool:
    for window in _app_windows(app_name):
        try:
            if window.isMinimized:
                window.restore()
            window.activate()
            return True
        except Exception:
            continue
    return False


def _wait_until_running(app_name: str, seconds: float = 8.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _is_running(app_name):
            return True
        time.sleep(0.5)
    return False


def open_app(
    parameters=None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    app_name = (parameters or {}).get("app_name", "").strip()

    if not app_name:
        return "No application name provided."

    web_url = _WEB_APPS.get(app_name.casefold())
    if web_url:
        try:
            # Open in the preferred browser (Chrome), not necessarily the Windows default (Edge).
            from actions.browser_control import _automation_browser, _open_native
            result = _open_native(web_url, _automation_browser())
            if result.startswith("Opened"):
                if player:
                    player.write_log(f"[open_app] Opened {app_name} in browser")
                return f"Opened {app_name} in your browser."
        except Exception as e:
            print(f"[open_app] Browser launch failed: {e}")
        try:
            if webbrowser.open(web_url, new=2):
                return f"Opened {app_name} in your default browser."
        except Exception as e:
            print(f"[open_app] Browser fallback failed: {e}")

    launcher = _OS_LAUNCHERS.get(_SYSTEM)
    if launcher is None:
        return f"Unsupported operating system: {_SYSTEM}"

    if _is_running(app_name):
        focused = _bring_to_front(app_name)
        return f"{app_name} is already open" + ("; brought it to the front." if focused else ".")

    normalized = _normalize(app_name)
    print(f"[open_app] Launching: '{app_name}' → '{normalized}' ({_SYSTEM})")

    if player:
        player.write_log(f"[open_app] {app_name}")

    try:
        configured = _configured_path(app_name) if _SYSTEM == "Windows" else None
        if configured:
            subprocess.Popen([configured], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            launched = True
        else:
            launched = launcher(normalized) or (normalized.lower() != app_name.lower() and launcher(app_name))
        if not launched:
            return f"Could not launch {app_name}. It may not be installed, or set JARVIS_APP_<NAME> in .env to its path."
        if _wait_until_running(app_name):
            return f"Opened {app_name}."
        return (f"Started {app_name}, but no window or process has appeared yet to confirm it. "
                "It may still be loading, or the name may not match an installed app.")
    except Exception as e:
        print(f"[open_app] Error: {e}")
        return f"Failed to open {app_name}: {e}"
