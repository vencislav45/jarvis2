
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import platform
import shutil
import subprocess
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from playwright.async_api import (
    async_playwright,
    BrowserContext,
    Page,
    Playwright,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeout,
)
_OS = platform.system()   # "Windows" | "Darwin" | "Linux"

def _normalize_url(url: str) -> str:
    """
    Bare words like "instagram" → "https://instagram.com"
    Domains like "instagram.com" → "https://instagram.com"
    Full URLs pass through unchanged.
    """
    url = url.strip()
    if not url:
        return "about:blank"
    if "://" in url:
        return url
    # No dot at all → assume .com  (e.g. "instagram" → "instagram.com")
    if "." not in url:
        url = url + ".com"
    return "https://" + url


def _find_opera_windows() -> Optional[str]:
    local  = os.environ.get("LOCALAPPDATA", "")
    prog   = os.environ.get("PROGRAMFILES", "")
    prog86 = os.environ.get("PROGRAMFILES(X86)", "")

    candidates = [
        Path(local)  / "Programs" / "Opera"    / "opera.exe",
        Path(local)  / "Programs" / "Opera GX" / "opera.exe",
        Path(prog)   / "Opera"    / "opera.exe",
        Path(prog86) / "Opera"    / "opera.exe",
    ]
    for p in candidates:
        if p.exists():
            print(f"[Browser] Opera found at: {p}")
            return str(p)

    try:
        import winreg
        keys = [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\opera.exe",
            r"SOFTWARE\Clients\StartMenuInternet\OperaStable\shell\open\command",
            r"SOFTWARE\Clients\StartMenuInternet\OperaGXStable\shell\open\command",
            r"SOFTWARE\Clients\StartMenuInternet\opera\shell\open\command",
        ]
        for key_path in keys:
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    k   = winreg.OpenKey(hive, key_path)
                    val = winreg.QueryValue(k, None)
                    winreg.CloseKey(k)
                    exe = val.strip().strip('"').split('"')[0].split(" --")[0].strip()
                    if exe and Path(exe).exists():
                        print(f"[Browser] Opera found via registry: {exe}")
                        return exe
                except Exception:
                    continue
    except Exception:
        pass

    return shutil.which("opera") or None

def _find_exe_windows(prog_name: str) -> Optional[str]:
    try:
        import winreg
        paths_to_try = [
            rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{prog_name}.exe",
            rf"SOFTWARE\Clients\StartMenuInternet\{prog_name}\shell\open\command",
        ]
        for key_path in paths_to_try:
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    k   = winreg.OpenKey(hive, key_path)
                    val = winreg.QueryValue(k, None)
                    winreg.CloseKey(k)
                    exe = val.strip().strip('"').split('"')[0].split(" --")[0].strip()
                    if exe and Path(exe).exists():
                        return exe
                except Exception:
                    continue
    except Exception:
        pass
    return None

_BROWSER_SPECS: dict[str, dict] = {
    "Windows": {
        "chrome":   {"engine": "chromium", "channel": "chrome",  "bins": []},
        "edge":     {"engine": "chromium", "channel": "msedge",  "bins": []},
        "firefox":  {"engine": "firefox",  "channel": None,      "bins": ["firefox.exe"]},
        "opera":    {"engine": "chromium", "channel": None,      "bins": ["opera.exe"],  "special": "opera_windows"},
        "operagx":  {"engine": "chromium", "channel": None,      "bins": [],             "special": "opera_windows"},
        "brave":    {"engine": "chromium", "channel": None,      "bins": ["brave.exe"]},
        "vivaldi":  {"engine": "chromium", "channel": None,      "bins": ["vivaldi.exe"]},
        "safari":   None,
    },
    "Darwin": {
        "chrome":   {"engine": "chromium", "channel": "chrome",  "bins": []},
        "edge":     {"engine": "chromium", "channel": "msedge",  "bins": ["microsoft-edge"]},
        "firefox":  {"engine": "firefox",  "channel": None,      "bins": ["firefox"]},
        "opera":    {"engine": "chromium", "channel": None,      "bins": ["opera"]},
        "operagx":  {"engine": "chromium", "channel": None,      "bins": ["opera"]},
        "brave":    {"engine": "chromium", "channel": None,      "bins": ["brave browser", "brave"]},
        "vivaldi":  {"engine": "chromium", "channel": None,      "bins": ["vivaldi"]},
        "safari":   {"engine": "webkit",   "channel": None,      "bins": []},
    },
    "Linux": {
        "chrome":   {"engine": "chromium", "channel": None,
                     "bins": ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"]},
        "edge":     {"engine": "chromium", "channel": None,
                     "bins": ["microsoft-edge", "microsoft-edge-stable"]},
        "firefox":  {"engine": "firefox",  "channel": None, "bins": ["firefox"]},
        "opera":    {"engine": "chromium", "channel": None, "bins": ["opera", "opera-stable"]},
        "operagx":  {"engine": "chromium", "channel": None, "bins": ["opera", "opera-stable"]},
        "brave":    {"engine": "chromium", "channel": None, "bins": ["brave-browser", "brave"]},
        "vivaldi":  {"engine": "chromium", "channel": None, "bins": ["vivaldi-stable", "vivaldi"]},
        "safari":   None,
    },
}

_ALIASES: dict[str, str] = {
    "google chrome":   "chrome",
    "google-chrome":   "chrome",
    "microsoft edge":  "edge",
    "ms edge":         "edge",
    "msedge":          "edge",
    "mozilla firefox": "firefox",
    "opera gx":        "operagx",
    "opera_gx":        "operagx",
}


def _resolve_browser(name: str) -> dict | None:
    name   = _ALIASES.get(name.lower().strip(), name.lower().strip())
    os_map = _BROWSER_SPECS.get(_OS, {})
    spec   = os_map.get(name)
    if spec is None:
        return None

    engine  = spec["engine"]
    channel = spec.get("channel")
    bins    = spec.get("bins", [])
    exe     = None

    if spec.get("special") == "opera_windows":
        exe = _find_opera_windows()
        if not exe:
            print(f"[Browser] ⚠️  Opera executable not found on Windows.")
        return {"engine": engine, "exe": exe, "channel": channel}

    for b in bins:
        found = shutil.which(b)
        if found:
            exe = found
            break

    if not exe and _OS == "Darwin":
        app_names = {
            "chrome":  ["Google Chrome.app"],
            "edge":    ["Microsoft Edge.app"],
            "firefox": ["Firefox.app"],
            "opera":   ["Opera.app", "Opera GX.app"],
            "brave":   ["Brave Browser.app"],
            "vivaldi": ["Vivaldi.app"],
        }
        for app in app_names.get(name, []):
            app_dir = Path("/Applications") / app / "Contents" / "MacOS"
            if app_dir.exists():
                found_bins = list(app_dir.iterdir())
                if found_bins:
                    exe = str(found_bins[0])
                    break

    if not exe and _OS == "Windows" and not channel:
        exe = _find_exe_windows(name)

    return {"engine": engine, "exe": exe, "channel": channel}


def _detect_default_browser() -> str:
    try:
        if _OS == "Windows":
            import winreg
            k = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations"
                r"\UrlAssociations\http\UserChoice",
            )
            prog_id = winreg.QueryValueEx(k, "ProgId")[0].lower()
            winreg.CloseKey(k)
            for kw in ("edge", "firefox", "opera", "brave", "vivaldi", "chrome"):
                if kw in prog_id:
                    return kw
        elif _OS == "Darwin":
            out = subprocess.run(
                ["defaults", "read",
                 "com.apple.LaunchServices/com.apple.launchservices.secure",
                 "LSHandlers"],
                capture_output=True, text=True, timeout=5,
            ).stdout.lower()
            for kw in ("firefox", "opera", "brave", "vivaldi", "safari", "chrome", "edge"):
                if kw in out:
                    return kw
        elif _OS == "Linux":
            out = subprocess.run(
                ["xdg-settings", "get", "default-web-browser"],
                capture_output=True, text=True, timeout=5,
            ).stdout.lower()
            for kw in ("firefox", "opera", "brave", "vivaldi", "chrome", "edge"):
                if kw in out:
                    return kw
    except Exception:
        pass
    return "chrome"


def _is_closed_error(error: Exception) -> bool:
    return "has been closed" in str(error) or "Target closed" in str(error) or type(error).__name__ == "TargetClosedError"


def _automation_profile_dir(browser: str) -> str:
    """Dedicated, persistent profile for the automation browser (sign in once, stays signed in)."""
    from core import settings
    base = settings.get("JARVIS_BROWSER_PROFILE_DIR") or str(Path.home() / ".jarvis_profiles")
    path = Path(base).expanduser() / browser
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


_PROCESS_BROWSERS = {"chrome.exe": "chrome", "chrome": "chrome", "msedge.exe": "edge", "msedge": "edge",
                     "brave.exe": "brave", "brave": "brave", "firefox.exe": "firefox", "firefox": "firefox",
                     "opera.exe": "opera", "vivaldi.exe": "vivaldi", "vivaldi": "vivaldi"}


def _running_browser() -> Optional[str]:
    """The browser the user is actually using right now (most processes wins)."""
    try:
        import psutil
        counts: dict[str, int] = {}
        for proc in psutil.process_iter(["name"]):
            name = _PROCESS_BROWSERS.get((proc.info.get("name") or "").lower())
            if name:
                counts[name] = counts.get(name, 0) + 1
        return max(counts, key=counts.get) if counts else None
    except Exception:
        return None


def _chrome_installed() -> bool:
    if _OS == "Windows":
        return bool(_find_exe_windows("chrome") or shutil.which("chrome"))
    return bool(_resolve_browser("chrome"))


def _automation_browser() -> str:
    """Browser for messaging and opened sites: JARVIS_AUTOMATION_BROWSER in .env, else Google Chrome
    when installed (never silently Edge just because it is the Windows default), else the browser in
    use, else the system default."""
    from core import settings
    configured = settings.get("JARVIS_AUTOMATION_BROWSER").lower().strip()
    if configured:
        return _ALIASES.get(configured, configured)
    if _chrome_installed():
        return "chrome"
    return _running_browser() or _detect_default_browser()


_SEARCH_ENGINES: dict[str, str] = {
    "google":     "https://www.google.com/search?q=",
    "bing":       "https://www.bing.com/search?q=",
    "duckduckgo": "https://duckduckgo.com/?q=",
    "yandex":     "https://yandex.com/search/?text=",
}

_MAC_APP_NAMES: dict[str, str] = {
    "chrome":  "Google Chrome",
    "edge":    "Microsoft Edge",
    "firefox": "Firefox",
    "opera":   "Opera",
    "operagx": "Opera GX",
    "brave":   "Brave Browser",
    "vivaldi": "Vivaldi",
    "safari":  "Safari",
}

# Windows registry lookup names for browsers whose spec has no explicit binary
_WIN_EXE_HINTS: dict[str, str] = {"chrome": "chrome", "edge": "msedge"}


def _open_native(url: str, browser_name: Optional[str]) -> str:
    """
    Kullanıcının GERÇEK tarayıcısını normal şekilde açar — kendi profili,
    giriş yapılmış hesapları ve eklentileriyle. Otomasyon bağlanmaz, bu yüzden
    about:blank sekmesi veya boş profil ASLA görünmez.
    url boş ise tarayıcı URL'siz başlatılır (kendi açılış sayfası /
    oturum geri yükleme ile) — tıpkı kullanıcının kendisi açmış gibi.
    Windows / macOS / Linux üçünde de çalışır.
    """
    url = _normalize_url(url) if url and url.strip() else ""
    if url == "about:blank":
        url = ""

    name = None
    if browser_name:
        name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
    elif not url:
        # URL yok → sadece pencere açılacak; varsayılan tarayıcının exe'si gerekir
        name = _detect_default_browser()

    # Specific browser → launch its own executable, exactly like the user would.
    if name:
        if _OS == "Darwin":
            app = _MAC_APP_NAMES.get(name)
            if app:
                cmd = ["open", "-a", app] + ([url] if url else [])
                try:
                    subprocess.run(cmd, check=True, timeout=10)
                    return f"Opened in {name}: {url}" if url else f"Opened {name}."
                except Exception as e:
                    print(f"[Browser] 'open -a {app}' failed ({e}), trying binary…")

        spec = _resolve_browser(name)
        exe  = spec.get("exe") if spec else None
        if not exe and _OS == "Windows":
            if name in ("opera", "operagx"):
                exe = _find_opera_windows()
            else:
                exe = _find_exe_windows(_WIN_EXE_HINTS.get(name, name))
        if exe:
            try:
                subprocess.Popen(
                    [exe, url] if url else [exe],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                return f"Opened in {name}: {url}" if url else f"Opened {name}."
            except Exception as e:
                print(f"[Browser] Native launch failed for {name}: {e}")
        print(f"[Browser] '{name}' not found — falling back to default browser.")

    if not url:
        return "Could not find a browser to open."

    # Default browser via the OS — exactly like the user clicking a link.
    try:
        if _OS == "Windows":
            os.startfile(url)                       # ShellExecute → default browser
        elif _OS == "Darwin":
            subprocess.run(["open", url], check=True, timeout=10)
        else:
            subprocess.Popen(
                ["xdg-open", url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        return f"Opened in your default browser: {url}"
    except Exception:
        try:
            if webbrowser.open(url):
                return f"Opened in your default browser: {url}"
        except Exception:
            pass
        return f"Could not open a browser for: {url}"


class _BrowserSession:
    """
    One automation browser (own thread + event loop) on the dedicated JARVIS profile.
    If its window is closed the session relaunches it on the next action.
    """

    def __init__(self, browser_name: str):
        self.browser_name = browser_name
        self._spec        = _resolve_browser(browser_name)

        self._loop:    asyncio.AbstractEventLoop | None = None
        self._thread:  threading.Thread | None          = None
        self._ready    = threading.Event()

        self._pw:      Playwright     | None = None
        self._context: BrowserContext | None = None
        self._page:    Page           | None = None

    def thread_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._loop and self._loop.is_running())

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name=f"BrowserThread-{self.browser_name}",
        )
        self._thread.start()
        self._ready.wait(timeout=20)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._async_init())
        self._ready.set()
        self._loop.run_forever()

    async def _async_init(self):
        self._pw = await async_playwright().start()

    def run(self, coro, timeout: int = 60) -> str:
        if not self._loop:
            raise RuntimeError(f"Session for '{self.browser_name}' not started.")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()   # don't let a timed-out action keep clicking/typing in the background
            raise

    def close(self):
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._async_close(), self._loop).result(10)

    async def _async_close(self):
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._context = self._page = None

    async def _adopt_page(self) -> Page:
        """
        launch_persistent_context zaten bir başlangıç sekmesi açar.
        Yeni bir boş sekme (about:blank) açmak yerine o sekmeyi devralır —
        böylece kullanıcı fazladan boş sekme görmez.
        """
        await asyncio.sleep(0.3)
        pages = self._context.pages
        return pages[0] if pages else await self._context.new_page()

    def _mark_closed(self, *_):
        """The automation window/context went away (user closed it, crash). Relaunch on next use."""
        if self._context is not None:
            print(f"[Browser] {self.browser_name} automation window closed — will relaunch on next use.")
        self._context = None
        self._page = None

    def is_open(self) -> bool:
        return self._context is not None

    async def _launch(self):
        """
        Start (or restart) the automation browser on the dedicated JARVIS profile.

        The user's everyday profile is never automated: Chrome/Edge 136+ refuse
        automation on the default profile, and it is locked while the browser is
        open. Logins made once in the JARVIS window persist across sessions.
        """
        if self._context is not None:
            return

        if self._spec is None:
            raise RuntimeError(f"'{self.browser_name}' is not supported for automation on {_OS}.")

        engine_name = self._spec["engine"]
        exe         = self._spec["exe"]
        channel     = self._spec["channel"]
        engine_obj  = getattr(self._pw, engine_name)
        profile     = _automation_profile_dir(self.browser_name)

        kwargs: dict = {"headless": False, "viewport": None, "no_viewport": True, "timeout": 30_000}
        if engine_name == "chromium":
            kwargs["args"] = ["--start-maximized", "--no-first-run", "--no-default-browser-check",
                              "--disable-default-apps"]
        if exe:
            kwargs["executable_path"] = exe
        elif channel:
            kwargs["channel"] = channel

        try:
            context = await engine_obj.launch_persistent_context(profile, **kwargs)
        except Exception as e:
            detail = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
            if any(k in str(e) for k in ("ProcessSingleton", "user data directory is already in use", "lock")):
                raise RuntimeError(f"The JARVIS {self.browser_name} window is already open in another process. "
                                   "Close that window and try again.") from e
            raise RuntimeError(f"Could not start {self.browser_name} for automation: {detail}") from e

        self._context = context
        context.on("close", self._mark_closed)
        self._page = await self._adopt_page()
        await asyncio.sleep(0.3)
        if self._context is None or self._page.is_closed():
            self._mark_closed()
            raise RuntimeError(f"{self.browser_name} closed immediately after starting. "
                               "Check that the browser is installed and not blocked by a policy.")
        print(f"[Browser] ✅ Launched {self.browser_name} with JARVIS profile: {profile}")

    async def with_page(self, fn):
        """Run ``await fn(page)`` on this session's active page (used by integrations)."""
        return await fn(await self._get_page())

    async def list_tabs(self) -> str:
        await self._launch()
        pages = [p for p in self._context.pages if not p.is_closed()]
        if not pages:
            return "No open tabs."
        lines = []
        for index, page in enumerate(pages, 1):
            try:
                title = await page.title()
            except Exception:
                title = ""
            marker = " (active)" if page is self._page else ""
            lines.append(f"{index}. {title or '(untitled)'} — {page.url}{marker}")
        return "\n".join(lines)

    async def switch_tab(self, index: int = 0, text: str = "") -> str:
        await self._launch()
        pages = [p for p in self._context.pages if not p.is_closed()]
        target = None
        if text:
            needle = text.casefold()
            for page in pages:
                try:
                    if needle in (await page.title()).casefold() or needle in page.url.casefold():
                        target = page
                        break
                except Exception:
                    continue
        elif 1 <= index <= len(pages):
            target = pages[index - 1]
        if target is None:
            return f"No tab matched {'“' + text + '”' if text else f'number {index}'}. Use list_tabs to see open tabs."
        self._page = target
        await target.bring_to_front()
        return f"Switched to: {await target.title()} — {target.url}"

    async def _get_page(self) -> Page:
        """Return a usable page, relaunching the browser if its window was closed."""
        for attempt in range(2):
            await self._launch()
            try:
                if self._page is None or self._page.is_closed():
                    open_pages = [p for p in self._context.pages if not p.is_closed()]
                    self._page = open_pages[-1] if open_pages else await self._context.new_page()
                return self._page
            except PlaywrightError as e:
                if attempt or not _is_closed_error(e):
                    raise
                self._mark_closed()   # stale context: relaunch once
        raise RuntimeError("unreachable")

    async def go_to(self, url: str) -> str:

        url      = _normalize_url(url)
        page     = await self._get_page()
        prev_url = page.url

        async def _do_goto(p: Page) -> str:
            """Attempt navigation and return the resulting URL (may still be blank)."""
            try:
                await p.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(0.3)
            except PlaywrightTimeout:
                pass   # page may have partially loaded — check URL below
            except Exception as e:
                print(f"[Browser] goto exception (non-fatal): {e}")
            return p.url

        result_url = await _do_goto(page)

        if result_url in ("about:blank", "", None, prev_url) and prev_url in ("about:blank", "", None):
            print(f"[Browser] Still blank after goto — retrying on new tab: {url}")
            try:
                new_page   = await self._context.new_page()
                self._page = new_page
                result_url = await _do_goto(new_page)
            except Exception as e:
                print(f"[Browser] New-tab retry failed: {e}")

        if result_url and result_url not in ("about:blank", "", None):
            return f"Opened: {result_url}"
        return f"Could not open: {url}"

    async def search(self, query: str, engine: str = "google") -> str:
        base = _SEARCH_ENGINES.get(engine.lower(), _SEARCH_ENGINES["google"])
        return await self.go_to(base + query.replace(" ", "+"))

    async def click(self, selector: str = None, text: str = None) -> str:
        page = await self._get_page()
        try:
            if text:
                await page.get_by_text(text, exact=False).first.click(timeout=8_000)
                return f"Clicked text: '{text}'"
            if selector:
                await page.click(selector, timeout=8_000)
                return f"Clicked selector: {selector}"
            return "No selector or text provided."
        except PlaywrightTimeout:
            return "Element not found (timeout)."
        except Exception as e:
            return f"Click error: {e}"

    async def type_text(self, selector: str = None, text: str = "",
                        clear_first: bool = True) -> str:
        page = await self._get_page()
        try:
            el = page.locator(selector).first if selector else page.locator(":focus")
            if clear_first:
                await el.clear()
            await el.type(text, delay=50)
            return "Text typed."
        except Exception as e:
            return f"Type error: {e}"

    async def scroll(self, direction: str = "down", amount: int = 500) -> str:
        page = await self._get_page()
        try:
            y = amount if direction == "down" else -amount
            await page.mouse.wheel(0, y)
            return f"Scrolled {direction}."
        except Exception as e:
            return f"Scroll error: {e}"

    async def press(self, key: str) -> str:
        page = await self._get_page()
        try:
            await page.keyboard.press(key)
            return f"Pressed: {key}"
        except Exception as e:
            return f"Key error: {e}"

    async def get_text(self) -> str:
        page = await self._get_page()
        try:
            text = await page.inner_text("body")
            return text[:4_000]
        except Exception as e:
            return f"Could not get page text: {e}"

    async def get_url(self) -> str:
        page = await self._get_page()
        return page.url

    async def fill_form(self, fields: dict) -> str:
        page    = await self._get_page()
        results = []
        for selector, value in fields.items():
            try:
                el = page.locator(selector).first
                await el.clear()
                await el.type(str(value), delay=40)
                results.append(f"✓ {selector}")
            except Exception as e:
                results.append(f"✗ {selector}: {e}")
        return "Form filled: " + ", ".join(results)

    async def smart_click(self, description: str) -> str:
        page = await self._get_page()
        for role in ("button", "link", "searchbox", "textbox", "menuitem", "tab"):
            try:
                loc = page.get_by_role(role, name=description)
                if await loc.count() > 0:
                    await loc.first.click(timeout=5_000)
                    return f"Clicked ({role}): '{description}'"
            except Exception:
                pass
        for attempt in (
            lambda: page.get_by_text(description, exact=False).first.click(timeout=5_000),
            lambda: page.get_by_placeholder(description, exact=False).first.click(timeout=5_000),
            lambda: page.locator(
                f'[alt*="{description}" i],[title*="{description}" i],'
                f'[aria-label*="{description}" i]'
            ).first.click(timeout=5_000),
        ):
            try:
                await attempt()
                return f"Clicked: '{description}'"
            except Exception:
                pass
        return f"Could not find element: '{description}'"

    async def smart_type(self, description: str, text: str) -> str:
        page = await self._get_page()
        candidates = [
            ("placeholder", page.get_by_placeholder(description, exact=False)),
            ("label",       page.get_by_label(description, exact=False)),
            ("role",        page.get_by_role("textbox", name=description)),
            ("searchbox",   page.get_by_role("searchbox")),
            ("combobox",    page.get_by_role("combobox", name=description)),
        ]
        for method, loc in candidates:
            try:
                el = loc.first
                if await el.count() == 0:
                    continue
                await el.clear()
                await el.type(text, delay=50)
                return f"Typed into ({method}): '{description}'"
            except Exception:
                continue
        return f"Could not find input: '{description}'"

    async def new_tab(self, url: str = "") -> str:
        page = await self._get_page()
        ctx  = page.context
        new  = await ctx.new_page()
        self._page = new
        if url:
            return await self.go_to(url)
        return "New tab opened."

    async def close_tab(self) -> str:
        page = self._page
        if page and not page.is_closed():
            ctx   = page.context
            await page.close()
            pages = ctx.pages
            self._page = pages[-1] if pages else None
            return "Tab closed."
        return "No active tab to close."

    async def screenshot(self, path: str = None) -> str:
        page = await self._get_page()
        try:
            save_path = path or str(Path.home() / "Desktop" / "jarvis_screenshot.png")
            await page.screenshot(path=save_path, full_page=False)
            return f"Screenshot saved: {save_path}"
        except Exception as e:
            return f"Screenshot error: {e}"

    async def back(self) -> str:
        page = await self._get_page()
        try:
            await page.go_back(timeout=10_000)
            return f"Navigated back: {page.url}"
        except Exception as e:
            return f"Back error: {e}"

    async def forward(self) -> str:
        page = await self._get_page()
        try:
            await page.go_forward(timeout=10_000)
            return f"Navigated forward: {page.url}"
        except Exception as e:
            return f"Forward error: {e}"

    async def reload(self) -> str:
        page = await self._get_page()
        try:
            await page.reload(timeout=15_000)
            return f"Page reloaded: {page.url}"
        except Exception as e:
            return f"Reload error: {e}"

    async def close_browser(self) -> str:
        await self._async_close()
        return f"{self.browser_name} closed."

class _SessionRegistry:
    """Tüm aktif tarayıcı oturumlarını yönetir."""

    def __init__(self):
        self._sessions:        dict[str, _BrowserSession] = {}
        self._active_browser:  str                        = ""
        self._lock             = threading.Lock()
        self._last_native_url: str                        = ""

    def has(self, browser_name: str | None = None) -> bool:
        """Bu tarayıcı için (veya hiç) aktif bir otomasyon oturumu var mı?"""
        with self._lock:
            if not browser_name:
                return bool(self._sessions)
            name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
            return name in self._sessions

    def note_native_url(self, url: str) -> None:
        self._last_native_url = url

    def pop_native_url(self) -> str:
        """Son native açılan URL'yi bir kez döndürür (tekrarı önlemek için tüketilir)."""
        url, self._last_native_url = self._last_native_url, ""
        return url

    def _get_or_create(self, browser_name: str) -> _BrowserSession:
        with self._lock:
            existing = self._sessions.get(browser_name)
            if existing is not None and not existing.thread_alive():
                self._sessions.pop(browser_name)   # its event loop died; start fresh
            if browser_name not in self._sessions:
                sess = _BrowserSession(browser_name)
                sess.start()
                self._sessions[browser_name] = sess
                print(f"[Registry] New session: {browser_name}")
            return self._sessions[browser_name]

    def get(self, browser_name: str | None = None) -> _BrowserSession:
        if not browser_name:
            browser_name = self._active_browser or _automation_browser()
        browser_name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
        sess = self._get_or_create(browser_name)
        self._active_browser = browser_name
        return sess

    def switch(self, browser_name: str) -> str:
        browser_name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
        self._get_or_create(browser_name)
        self._active_browser = browser_name
        return f"Active browser → {browser_name}"

    def close_one(self, browser_name: str) -> str:
        with self._lock:
            sess = self._sessions.pop(browser_name, None)
        if sess:
            sess.close()
            if self._active_browser == browser_name:
                self._active_browser = ""
            return f"{browser_name} closed."
        return f"No active session for: {browser_name}"

    def close_all(self) -> str:
        with self._lock:
            names    = list(self._sessions.keys())
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._active_browser = ""
        for s in sessions:
            try:
                s.close()
            except Exception:
                pass
        return "All browsers closed: " + (", ".join(names) if names else "none")

    def list_sessions(self) -> str:
        with self._lock:
            if not self._sessions:
                return ("No JARVIS automation browser is running. It starts automatically when needed "
                        f"(it will use {_automation_browser()}).")
            lines = []
            for name, sess in self._sessions.items():
                state = "window open" if sess.is_open() and sess.thread_alive() else "window closed (reopens on next action)"
                marker = " ◀ active" if name == self._active_browser else ""
                lines.append(f"  • {name}: {state}{marker}")
            return ("JARVIS automation browsers (separate from your everyday browser windows):\n"
                    + "\n".join(lines))


_registry = _SessionRegistry()


def _close_native_tab(url: str | None, browser_name: str | None = None) -> str:
    """Close the last site opened in the user's real browser, with focus checks."""
    try:
        import pyautogui
        import pygetwindow
        active = pygetwindow.getActiveWindow()
        title = (active.title or "").strip() if active else ""
        if not title:
            return "Could not verify an active browser window; nothing was closed."

        browser_name = (_ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
                        if browser_name else _detect_default_browser())
        browser_tokens = {
            "chrome": ("chrome",), "edge": ("edge",), "firefox": ("firefox",),
            "brave": ("brave",), "opera": ("opera",), "operagx": ("opera gx",),
            "vivaldi": ("vivaldi",), "safari": ("safari",),
        }
        title_lower = title.casefold()
        if not any(token in title_lower for token in browser_tokens.get(browser_name, (browser_name,))):
            return f"The active window is not {browser_name}; nothing was closed."

        host_parts = (urlsplit(url).hostname or "").lower().split(".") if url else []
        site_hint = next((part for part in host_parts if part and part != "www"), "")
        if site_hint and site_hint not in title_lower:
            return "The last opened website is not the active tab; nothing was closed."

        pyautogui.hotkey("command", "w") if _OS == "Darwin" else pyautogui.hotkey("ctrl", "w")
        time.sleep(0.35)
        current = pygetwindow.getActiveWindow()
        current_title = (current.title or "").strip() if current else ""
        if not current_title or current_title != title:
            return f"Closed the {site_hint or 'active'} browser tab."
        return "Sent the browser close-tab shortcut, but could not verify that the tab closed."
    except Exception as e:
        return f"Could not close the browser tab: {e}"

def browser_control(
    parameters:    dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params  = parameters or {}
    action  = params.get("action", "").lower().strip()
    browser = params.get("browser", "").lower().strip() or None
    result  = "Unknown action."

    # Default: everything happens in the user's own Chrome with their accounts (Jarvis Bridge
    # extension). The separate signed-out automation window is used only with
    # JARVIS_BROWSER_MODE=separate or when another browser is named explicitly.
    from actions.chrome_bridge import browser_mode
    if browser_mode() == "my_chrome" and _ALIASES.get(browser or "chrome", browser or "chrome") == "chrome":
        from actions import my_chrome
        handled = my_chrome.handle(action, params)
        if handled is not None:
            _log(player, handled)
            return handled

    if action == "switch":
        target = browser or params.get("target", "").lower().strip()
        result = _registry.switch(target) if target else "Please specify a browser."
        _log(player, result)
        return result

    if action == "list_browsers":
        result = _registry.list_sessions()
        _log(player, result)
        return result

    if action == "close_all":
        result = _registry.close_all()
        _log(player, result)
        return result

    if action == "send_message":
        platform = params.get("platform", "").strip().casefold()
        receiver = params.get("receiver", "").strip()
        message = params.get("message", "").strip()
        if not receiver or not message:
            result = "A recipient and message are required; no message was sent."
        else:
            from actions import messaging
            try:
                result = messaging.send(platform, receiver, message).message
            except Exception as e:
                result = f"Could not complete the {platform or 'social'} message action: {e}"
        _log(player, result)
        return result

    if action == "close":
        target = browser or _registry._active_browser
        if target and _registry.has(target):
            result = _registry.close_one(target)
        elif _registry._last_native_url:
            native_url = _registry.pop_native_url()
            result = _close_native_tab(native_url, target)
            if not result.startswith("Closed"):
                _registry.note_native_url(native_url)
        else:
            result = "No opened browser tab is available to close."
        _log(player, result)
        return result

    # ── Gezinme HER ZAMAN native ─────────────────────────────────────────────
    # go_to / search / new_tab siteyi kullanıcının kendi tarayıcısında açar —
    # kendi profili, giriş yapılmış hesapları ve açılış sayfasıyla; tıpkı
    # kullanıcının kendisi açmış gibi. about:blank'li kontrollü pencere burada
    # asla açılmaz. Tek istisna: hâlihazırda süren bir otomasyon akışı varsa
    # gezinme o pencerede devam eder (çok adımlı görevler bölünmesin diye).
    if action in ("go_to", "search", "new_tab"):
        if _registry.has(browser):
            sess = _registry.get(browser)
            try:
                if action == "search":
                    result = sess.run(sess.search(params.get("query", ""),
                                                  params.get("engine", "google")))
                elif action == "new_tab":
                    result = sess.run(sess.new_tab(params.get("url", "")))
                else:
                    result = sess.run(sess.go_to(params.get("url", "")))
            except concurrent.futures.TimeoutError:
                result = f"Browser action '{action}' timed out (60s)."
            except Exception as e:
                result = f"Browser error ({action}): {e}"
            _log(player, result)
            return result

        if action == "search":
            base    = _SEARCH_ENGINES.get(params.get("engine", "google").lower(),
                                          _SEARCH_ENGINES["google"])
            nav_url = base + params.get("query", "").replace(" ", "+")
        else:
            nav_url = params.get("url", "").strip()

        result = _open_native(nav_url, browser or _automation_browser())
        if result.startswith("Opened") and nav_url:
            _registry.note_native_url(_normalize_url(nav_url))
        _log(player, result)
        return result

    if action == "close_tab" and not _registry.has(browser):
        native_url = _registry.pop_native_url() or None
        result = _close_native_tab(native_url, browser)
        if not result.startswith("Closed"):
            if native_url:
                _registry.note_native_url(native_url)
        _log(player, result)
        return result

    # ── Etkileşimli aksiyonlar (tıklama/yazma/okuma…) ────────────────────────
    # Bunlar fiziksel olarak kontrol edilebilir bir tarayıcı gerektirir;
    # yalnızca burada otomasyon penceresi açılır ve açılır açılmaz kullanıcının
    # son gezindiği sayfaya gider — boş sayfada beklemez.
    try:
        sess = _registry.get(browser)
    except Exception as e:
        result = f"Could not start browser session: {e}"
        _log(player, result)
        return result

    try:
        last = _registry.pop_native_url()
        if last:
            try:
                sess.run(sess.go_to(last))
            except Exception as e:
                print(f"[Browser] Could not resume last page ({last}): {e}")

        if action == "click":
            result = sess.run(sess.click(params.get("selector"), params.get("text")))
        elif action == "type":
            result = sess.run(sess.type_text(
                params.get("selector"), params.get("text", ""), params.get("clear_first", True)))
        elif action == "scroll":
            result = sess.run(sess.scroll(params.get("direction", "down"), int(params.get("amount", 500))))
        elif action == "fill_form":
            result = sess.run(sess.fill_form(params.get("fields", {})))
        elif action == "smart_click":
            result = sess.run(sess.smart_click(params.get("description", "")))
        elif action == "smart_type":
            result = sess.run(sess.smart_type(params.get("description", ""), params.get("text", "")))
        elif action == "get_text":
            result = sess.run(sess.get_text())
        elif action == "get_url":
            result = sess.run(sess.get_url())
        elif action == "press":
            result = sess.run(sess.press(params.get("key", "Enter")))
        elif action == "close_tab":
            result = sess.run(sess.close_tab())
        elif action == "list_tabs":
            result = sess.run(sess.list_tabs())
        elif action == "switch_tab":
            result = sess.run(sess.switch_tab(int(params.get("index") or 0), params.get("text", "")))
        elif action == "screenshot":
            result = sess.run(sess.screenshot(params.get("path")))
        elif action == "back":
            result = sess.run(sess.back())
        elif action == "forward":
            result = sess.run(sess.forward())
        elif action == "reload":
            result = sess.run(sess.reload())
        else:
            result = f"Unknown browser action: '{action}'"

    except concurrent.futures.TimeoutError:
        result = f"Browser action '{action}' timed out (60s)."
    except Exception as e:
        result = f"Browser error ({action}): {e}"

    _log(player, result)
    return result


def run_on_page(fn, browser: str | None = None, timeout: int = 90):
    """Run ``await fn(page)`` in the automation browser (signed-in JARVIS/real profile)."""
    sess = _registry.get(browser)
    return sess.run(sess.with_page(fn), timeout=timeout)


def _log(player, text: str):
    short = str(text)[:80]
    print(f"[Browser] {short}")
    if player:
        player.write_log(f"[browser] {short[:60]}")
