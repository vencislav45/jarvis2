"""Jarvis Bridge: work inside the user's own Chrome, with their own accounts.

Chrome (136+) does not allow automation tools to control the everyday profile, so
Jarvis cannot drive it with Playwright. Instead the user installs the small
"Jarvis Bridge" extension (folder chrome_extension/) once. The extension connects
to this local WebSocket server (127.0.0.1 only, and only that extension's origin
is accepted) and carries out tab and DevTools commands. Chrome shows its
"started debugging this browser" banner while Jarvis is acting in a tab.

    bridge = get_bridge()          # started at assistant startup
    bridge.run_in_tab(fn, key="instagram")   # fn(driver) runs on a Jarvis tab
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlsplit
from typing import Awaitable, Callable

from core import settings
from core.settings import BASE_DIR
from actions.web_driver import BrowserClosed, CdpDriver, DriverError, is_closed_error

EXTENSION_ID = "ggpnjnlpggecgidekekiopohlldhbeam"   # fixed by the "key" in chrome_extension/manifest.json
EXTENSION_DIR = BASE_DIR / "chrome_extension"
STATE_PATH = BASE_DIR / "config" / "bridge_state.json"
DEFAULT_PORT = 48761   # must match PORT in chrome_extension/background.js
IDLE_DETACH_SECONDS = 15

INSTALL_STEPS = (
    "The Jarvis Bridge extension is not installed in Chrome yet (needed once, so I can work in your Chrome "
    "with your accounts). I opened chrome://extensions in Chrome and copied the extension folder path. "
    "In that tab: 1) switch on 'Developer mode' (top right), 2) click 'Load unpacked', 3) paste the path "
    f"with Ctrl+V ({EXTENSION_DIR}) and press 'Select Folder'. Then ask me again. If the page did not open, "
    "type chrome://extensions in the address bar."
)
_install_offered_at = 0.0


def extension_installed() -> bool:
    """True if any Chrome profile on this PC has the Jarvis Bridge extension (enabled or not)."""
    import os
    user_data = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    for prefs in [*user_data.glob("*/Secure Preferences"), *user_data.glob("*/Preferences")]:
        try:
            if EXTENSION_ID in prefs.read_text(encoding="utf-8", errors="ignore"):
                return True
        except OSError:
            continue
    return False


def offer_install() -> None:
    """Open chrome://extensions and copy the folder path, at most once every few minutes."""
    global _install_offered_at
    if time.monotonic() - _install_offered_at < 300 and _install_offered_at:
        return
    _install_offered_at = time.monotonic()
    try:
        import pyperclip
        pyperclip.copy(str(EXTENSION_DIR))
    except Exception:  # noqa: BLE001 - the path is also in the message
        pass
    try:
        from actions.browser_control import _open_native
        _open_native("chrome://extensions", "chrome")
    except Exception as e:  # noqa: BLE001
        print(f"[Bridge] Could not open chrome://extensions: {e}")


class BridgeUnavailable(DriverError):
    """The extension is not connected (not installed, Chrome closed, or still reconnecting)."""


def browser_mode() -> str:
    """my_chrome (default): your Chrome via the extension. separate: the old standalone automation window."""
    return "separate" if settings.get("JARVIS_BROWSER_MODE", "my_chrome").lower() == "separate" else "my_chrome"


class ChromeBridge:
    def __init__(self, port: int | None = None, any_origin_for_tests: bool = False,
                 state_path: Path = STATE_PATH):
        self.port = port or settings.get_int("JARVIS_BRIDGE_PORT", DEFAULT_PORT)
        # Only the Jarvis Bridge extension may connect (web pages cannot fake the Origin header).
        self.allowed_origins = None if any_origin_for_tests else {f"chrome-extension://{EXTENSION_ID}"}
        self.state_path = state_path
        self.loop: asyncio.AbstractEventLoop | None = None
        self.error = ""
        self.version = ""
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._conn = None
        self._connected = None   # asyncio.Event, created in the loop
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._tabs: dict[str, int] = {}          # key → tab id opened by Jarvis
        self._detach_timers: dict[int, asyncio.TimerHandle] = {}

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def start(self) -> "ChromeBridge":
        if self._thread and self._thread.is_alive():
            return self
        self._thread = threading.Thread(target=self._run, daemon=True, name="JarvisBridge")
        self._thread.start()
        self._ready.wait(10)
        return self

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._connected = asyncio.Event()
        try:
            self.loop.run_until_complete(self._serve())
        except Exception as e:  # noqa: BLE001 - e.g. port already in use
            self.error = f"{type(e).__name__}: {e}"
            print(f"[Bridge] ❌ Could not start on 127.0.0.1:{self.port}: {self.error}")
        finally:
            self._ready.set()

    async def _serve(self) -> None:
        from websockets.asyncio.server import serve

        def check(connection, request):
            origin = request.headers.get("Origin", "")
            if request.path != "/jarvis-bridge" or (self.allowed_origins is not None and origin not in self.allowed_origins):
                return connection.respond(HTTPStatus.FORBIDDEN, "Forbidden\n")
            return None

        async with serve(self._handler, "127.0.0.1", self.port, process_request=check, max_size=32 * 2**20):
            print(f"[Bridge] Listening on 127.0.0.1:{self.port} for the Jarvis Bridge Chrome extension")
            self._ready.set()
            asyncio.get_running_loop().create_task(self._keepalive())
            await asyncio.Future()   # run until the process exits

    async def _keepalive(self) -> None:
        # Messages keep the extension's service worker awake while connected.
        while True:
            await asyncio.sleep(20)
            if self._conn is not None:
                try:
                    await self._conn.send(json.dumps({"type": "ping"}))
                except Exception:  # noqa: BLE001
                    pass

    async def _handler(self, connection) -> None:
        previous, self._conn = self._conn, connection
        if previous is not None:
            await previous.close()
        try:
            async for raw in connection:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if msg.get("type") == "hello":
                    self.version = str(msg.get("version", ""))
                    self._mark_seen()
                    self._connected.set()
                    print(f"[Bridge] ✅ Chrome extension connected (v{self.version})")
                    continue
                future = self._pending.pop(msg.get("id"), None)
                if future is not None and not future.done():
                    if msg.get("ok"):
                        future.set_result(msg.get("result"))
                    else:
                        future.set_exception(DriverError(msg.get("error") or "Chrome command failed"))
        finally:
            if self._conn is connection:
                self._conn = None
                self._connected.clear()
                self._tabs.clear()
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(BridgeUnavailable("Chrome disconnected from Jarvis."))
                self._pending.clear()
                print("[Bridge] Chrome extension disconnected")

    # ── state ─────────────────────────────────────────────────────────────────
    def connected(self) -> bool:
        return self._conn is not None and self._connected is not None and self._connected.is_set()

    def ever_connected(self) -> bool:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8")).get("seen") is True
        except (OSError, ValueError, AttributeError):
            return False

    def _mark_seen(self) -> None:
        if not self.ever_connected():
            try:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                self.state_path.write_text(json.dumps({"seen": True}), encoding="utf-8")
            except OSError:
                pass

    def status(self) -> str:
        if self.error:
            return f"Bridge server failed to start: {self.error}"
        if self.connected():
            return f"Connected to your Chrome (Jarvis Bridge v{self.version})"
        if self.ever_connected() or extension_installed():
            return "Extension installed but not connected right now (Chrome closed, extension off, or reconnecting)"
        return "Jarvis Bridge extension not installed"

    # ── commands ──────────────────────────────────────────────────────────────
    async def acall(self, cmd: str, timeout: float = 30, **args):
        """Send one command to the extension (call from the bridge loop)."""
        if self._conn is None:
            raise BridgeUnavailable(INSTALL_STEPS)
        self._next_id += 1
        msg_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        try:
            await self._conn.send(json.dumps({"id": msg_id, "cmd": cmd, "args": args}))
        except Exception as e:  # noqa: BLE001 - connection dropped between check and send
            self._pending.pop(msg_id, None)
            raise BridgeUnavailable("Chrome disconnected from Jarvis.") from e
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as e:
            raise DriverError(f"Chrome did not answer '{cmd}' in time.") from e
        finally:
            self._pending.pop(msg_id, None)

    def run(self, coro: Awaitable, timeout: float = 60):
        """Run a coroutine on the bridge loop from any other thread and wait for it."""
        if self.loop is None or not self.loop.is_running():
            raise BridgeUnavailable(f"Jarvis Bridge server is not running. {self.error}".strip())
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            # Stop the work too: otherwise it could still click or send after we reported a timeout.
            future.cancel()
            raise

    def call(self, cmd: str, timeout: float = 30, **args):
        return self.run(self.acall(cmd, timeout=timeout, **args), timeout=timeout + 5)

    def ensure_connected(self, notify: Callable[[str], None] | None = None) -> None:
        """Make sure the extension is connected, starting Chrome if needed. Raises BridgeUnavailable."""
        if self.connected():
            return
        if self.error:
            raise BridgeUnavailable(f"Jarvis Bridge server could not start ({self.error}).")
        if not (self.ever_connected() or extension_installed()):
            offer_install()
            raise BridgeUnavailable(INSTALL_STEPS)
        # Installed: Chrome may be closed, or the extension's worker is asleep and reconnects within 30 s.
        if not _chrome_running():
            from actions.browser_control import _open_native
            _open_native("", "chrome")
        if notify:
            notify("Waiting for Chrome's Jarvis Bridge extension to connect…")
        deadline = time.monotonic() + 40   # the extension retries at least every 30 s
        while time.monotonic() < deadline:
            if self.connected():
                return
            time.sleep(0.25)
        raise BridgeUnavailable("Chrome's Jarvis Bridge extension did not connect. Check it is enabled in "
                                "chrome://extensions, then try again.")

    # ── tabs ──────────────────────────────────────────────────────────────────
    async def tab_for(self, key: str | None, host: str | None = None) -> int:
        """The tab Jarvis uses for ``key``, or the active tab when key is None.

        Jarvis's earlier tab is reused only while it is still on ``host``: if the user has since
        used that tab for something else, it is left alone and a new tab is opened."""
        if key is None:
            tab = await self.acall("active_tab")
            if not tab:
                raise BrowserClosed("No Chrome window is open.")
            if tab["url"].startswith(("chrome://", "chrome-extension://", "edge://")):
                raise DriverError("The active tab is a Chrome settings page, which extensions may not control. "
                                  "Switch to a normal web page.")
            return tab["id"]
        tab_id = self._tabs.get(key)
        if tab_id is not None:
            try:
                tab = await self.acall("tab_info", tabId=tab_id)
                current = urlsplit(tab["url"]).hostname or ""
                if not host or tab["url"] in ("", "about:blank") or current == host or current.endswith("." + host):
                    return tab_id
            except DriverError:
                pass
            self._tabs.pop(key, None)
        tab = await self.acall("open_tab", url="about:blank")
        self._tabs[key] = tab["id"]
        return tab["id"]

    def _schedule_detach(self, tab_id: int) -> None:
        # Detach (removes Chrome's debugging banner) once Jarvis has been idle on the tab.
        timer = self._detach_timers.pop(tab_id, None)
        if timer:
            timer.cancel()

        async def detach_quietly():
            try:
                await self.acall("detach", tabId=tab_id)
            except DriverError:
                pass   # tab already closed or Chrome disconnected

        def detach():
            self._detach_timers.pop(tab_id, None)
            if self._conn is not None:
                asyncio.ensure_future(detach_quietly())
        self._detach_timers[tab_id] = self.loop.call_later(IDLE_DETACH_SECONDS, detach)

    async def with_tab(self, fn: Callable[[CdpDriver], Awaitable], key: str | None = None, host: str | None = None):
        tab_id = await self.tab_for(key, host)
        timer = self._detach_timers.pop(tab_id, None)
        if timer:
            timer.cancel()
        try:
            return await fn(CdpDriver(self, tab_id))
        except DriverError as e:
            if key is not None and is_closed_error(e):
                self._tabs.pop(key, None)
            raise
        finally:
            self._schedule_detach(tab_id)

    def run_in_tab(self, fn: Callable[[CdpDriver], Awaitable], key: str | None = None, timeout: float = 120,
                   notify: Callable[[str], None] | None = None, host: str | None = None):
        """Run ``fn(driver)`` on a tab of the user's Chrome (``key``: reuse Jarvis's tab for that job
        while it is still on ``host``)."""
        self.ensure_connected(notify)
        return self.run(self.with_tab(fn, key, host), timeout=timeout)


def _chrome_running() -> bool:
    try:
        import psutil
        return any((p.info.get("name") or "").lower() == "chrome.exe" for p in psutil.process_iter(["name"]))
    except Exception:  # noqa: BLE001
        return True


_bridge: ChromeBridge | None = None
_lock = threading.Lock()


def get_bridge() -> ChromeBridge:
    global _bridge
    with _lock:
        if _bridge is None:
            _bridge = ChromeBridge().start()
        return _bridge
