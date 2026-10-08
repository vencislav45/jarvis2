"""browser_control actions carried out in the user's own Chrome (their accounts).

Opening sites and searching use the Jarvis Bridge extension when it is connected
(so Jarvis knows which tab it opened) and otherwise just launch Chrome normally.
Reading, clicking and typing need the extension; without it the user gets the
one-time install steps instead of a separate, signed-out window.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote_plus

from actions.chrome_bridge import BridgeUnavailable, get_bridge
from actions.web_driver import DriverError

SEARCH_ENGINES = {
    "google": "https://www.google.com/search?q=",
    "bing": "https://www.bing.com/search?q=",
    "duckduckgo": "https://duckduckgo.com/?q=",
    "yandex": "https://yandex.com/search/?text=",
}
NAVIGATION = {"go_to", "search", "new_tab"}
INTERACTIVE = {"click", "type", "scroll", "fill_form", "smart_click", "smart_type", "get_text", "get_url", "press",
               "list_tabs", "switch_tab", "close_tab", "screenshot", "back", "forward", "reload", "media"}


def _notify(message: str) -> None:
    from actions import messaging
    messaging.notify(message)


def handle(action: str, params: dict) -> str | None:
    """Result text, or None when browser_control should handle the action itself."""
    if action in NAVIGATION:
        return _navigate(action, params)
    if action == "list_browsers":
        return f"Browser: your Google Chrome. Jarvis Bridge: {get_bridge().status()}."
    if action not in INTERACTIVE:
        return None
    bridge = get_bridge()
    try:
        bridge.ensure_connected(_notify)
    except BridgeUnavailable as e:
        return None if action == "close_tab" else str(e)   # close_tab can still use the keyboard shortcut
    try:
        return bridge.run(_ACTIONS[action](bridge, params), timeout=60)
    except DriverError as e:
        return f"Chrome {action} failed: {e}"
    except TimeoutError:
        return f"Chrome {action} timed out."


def _navigate(action: str, params: dict) -> str:
    from actions.browser_control import _normalize_url, _open_native, _registry
    if action == "search":
        base = SEARCH_ENGINES.get(str(params.get("engine") or "google").lower(), SEARCH_ENGINES["google"])
        url = base + quote_plus(params.get("query", ""))
    else:
        url = _normalize_url(params.get("url", "")) if params.get("url") else "https://www.google.com/"
    bridge = get_bridge()
    if bridge.connected():
        try:
            bridge.call("open_tab", url=url)
            return f"Opened {url} in your Chrome."
        except DriverError as e:
            print(f"[MyChrome] Bridge open failed ({e}); launching Chrome directly")
    result = _open_native(url, "chrome")
    if result.startswith("Opened"):
        _registry.note_native_url(url)
        return f"Opened {url} in your Chrome."
    return result


# ── interactive actions (run on the bridge loop, active tab of the focused Chrome window) ──

async def _active(bridge) -> dict:
    tab = await bridge.acall("active_tab")
    if not tab:
        raise DriverError("No Chrome window is open.")
    return tab


async def _on_active(bridge, fn):
    return await bridge.with_tab(fn, key=None)


async def _get_text(bridge, p):
    async def run(d):
        page = await d.evaluate("(J) => ({title: document.title, url: location.href, "
                                "text: (document.body ? document.body.innerText : '').slice(0, 6000)})")
        return f"{page['title']} — {page['url']}\n\n{page['text']}"
    return await _on_active(bridge, run)


async def _get_url(bridge, p):
    return (await _active(bridge))["url"]


async def _click_point(d, js: str, arg: dict, what: str) -> str:
    point = await d.evaluate(js, arg)
    if not point:
        return f"Could not find {what} on the page."
    await d.click_at(point["x"], point["y"])
    return f"Clicked {what}."


async def _click(bridge, p):
    selector, text = p.get("selector"), p.get("text")

    async def run(d):
        if selector:
            return await _click_point(d, "(J, a) => J.point(J.first([a.s]))", {"s": selector}, f"'{selector}'")
        if text:
            return await _click_point(d, "(J, a) => J.point(J.textEl(a.t) || J.smartFind(a.t, false))",
                                      {"t": text}, f"'{text}'")
        return "Give a selector or the visible text to click."
    return await _on_active(bridge, run)


async def _smart_click(bridge, p):
    description = p.get("description", "")
    return await _on_active(bridge, lambda d: _click_point(
        d, "(J, a) => J.point(J.smartFind(a.t, false))", {"t": description}, f"'{description}'"))


async def _type_into(d, js: str, arg: dict, text: str, clear: bool, what: str) -> str:
    point = await d.evaluate(js, arg)
    if not point:
        return f"Could not find {what}."
    await d.click_at(point["x"], point["y"])
    if clear:
        await d.evaluate("(J) => J.selectContents(document.activeElement)")
    await d.insert_text(text)
    return f"Typed into {what}."


async def _type(bridge, p):
    selector, text, clear = p.get("selector"), p.get("text", ""), p.get("clear_first", True) is not False

    async def run(d):
        if selector:
            return await _type_into(d, "(J, a) => J.point(J.first([a.s], {editable: true}))", {"s": selector},
                                    text, clear, f"'{selector}'")
        if clear:
            await d.evaluate("(J) => J.selectContents(document.activeElement)")
        await d.insert_text(text)
        return "Typed into the focused field."
    return await _on_active(bridge, run)


async def _smart_type(bridge, p):
    description, text = p.get("description", ""), p.get("text", "")
    return await _on_active(bridge, lambda d: _type_into(
        d, "(J, a) => J.point(J.smartFind(a.t, true))", {"t": description}, text, True, f"'{description}'"))


async def _fill_form(bridge, p):
    fields = p.get("fields") or {}

    async def run(d):
        results = []
        for selector, value in fields.items():
            outcome = await _type_into(d, "(J, a) => J.point(J.first([a.s], {editable: true}))", {"s": selector},
                                       str(value), True, selector)
            results.append(("✓ " if outcome.startswith("Typed") else "✗ ") + selector)
        return "Form filled: " + ", ".join(results)
    return await _on_active(bridge, run)


async def _press(bridge, p):
    key = p.get("key") or "Enter"

    async def run(d):
        await d.press(key)
        return f"Pressed {key}."
    return await _on_active(bridge, run)


async def _scroll(bridge, p):
    amount = int(p.get("amount") or 500) * (-1 if str(p.get("direction", "down")).lower() == "up" else 1)

    async def run(d):
        await d.evaluate("(J, a) => window.scrollBy(0, a.y)", {"y": amount})
        return f"Scrolled {'up' if amount < 0 else 'down'}."
    return await _on_active(bridge, run)


async def _list_tabs(bridge, p):
    tabs = await bridge.acall("list_tabs")
    if not tabs:
        return "No Chrome tabs are open."
    return "\n".join(f"{i}. {t['title'] or '(untitled)'} — {t['url']}{' (active)' if t['active'] else ''}"
                     for i, t in enumerate(tabs, 1))


async def _switch_tab(bridge, p):
    tabs = await bridge.acall("list_tabs")
    index, text = int(p.get("index") or 0), str(p.get("text") or "").casefold()
    target = None
    if text:
        target = next((t for t in tabs if text in t["title"].casefold() or text in t["url"].casefold()), None)
    elif 1 <= index <= len(tabs):
        target = tabs[index - 1]
    if target is None:
        return "No Chrome tab matched. Use list_tabs to see open tabs."
    await bridge.acall("activate", tabId=target["id"])
    return f"Switched to: {target['title']} — {target['url']}"


async def _close_tab(bridge, p):
    tab = await _active(bridge)
    await bridge.acall("close_tab", tabId=tab["id"])
    return f"Closed the tab: {tab['title'] or tab['url']}"


def _tab_command(cmd: str, label: str):
    async def run(bridge, p):
        tab = await _active(bridge)
        await bridge.acall(cmd, tabId=tab["id"])
        return f"{label}."
    return run


async def _screenshot(bridge, p):
    path = Path(p.get("path") or Path.home() / "Desktop" / "jarvis_screenshot.png")

    async def run(d):
        path.write_bytes(await d.screenshot())
        return f"Screenshot saved: {path}"
    return await _on_active(bridge, run)


def parse_seconds(value) -> float | None:
    """"1:20" → 80, "1:02:03" → 3723, "90" → 90, "1m20s" / "1 min 20 sec" → 80."""
    import re
    text = str(value or "").strip().lower()
    if not text:
        return None
    if re.fullmatch(r"\d+(:\d{1,2}){1,2}", text):
        seconds = 0
        for part in text.split(":"):
            seconds = seconds * 60 + int(part)
        return float(seconds)
    unit = r"[^\W\d_]*"   # rest of the unit word (letters only: "min", "минути", "sec")
    match = re.fullmatch(rf"(?:(\d+)\s*(?:h|ч){unit})?\s*(?:(\d+)\s*(?:m|мин){unit})?\s*"
                         rf"(?:(\d+(?:\.\d+)?)\s*(?:s|сек){unit})?", text)
    if match and any(match.groups()):
        h, m, s = (float(g) if g else 0 for g in match.groups())
        return h * 3600 + m * 60 + s
    try:
        return float(text)
    except ValueError:
        return None


# Works on the main <video> of the active tab (YouTube, Vimeo, most players).
_MEDIA_JS = """(J, a) => {
  const videos = [...document.querySelectorAll('video')].filter(v => J.visible(v))
    .sort((x, y) => (y.clientWidth * y.clientHeight) - (x.clientWidth * x.clientHeight));
  const v = videos[0];
  if (!v) return {error: 'There is no video on this page.'};
  const c = a.command, n = a.number;
  if (c === 'play') v.play();
  else if (c === 'pause') v.pause();
  else if (c === 'toggle') { if (v.paused) v.play(); else v.pause(); }
  else if (c === 'seek') v.currentTime = Math.max(0, Math.min(n, v.duration || n));
  else if (c === 'forward') v.currentTime = Math.min(v.currentTime + (n || 10), v.duration || 1e9);
  else if (c === 'back') v.currentTime = Math.max(0, v.currentTime - (n || 10));
  else if (c === 'mute') v.muted = true;
  else if (c === 'unmute') v.muted = false;
  else if (c === 'volume') { v.muted = false; v.volume = Math.max(0, Math.min(1, n / 100)); }
  else if (c === 'speed') v.playbackRate = Math.max(0.25, Math.min(4, n));
  else if (c !== 'status') return {error: 'Unknown media command: ' + c};
  return {time: v.currentTime, duration: v.duration, paused: v.paused, muted: v.muted,
          volume: Math.round(v.volume * 100), speed: v.playbackRate, title: document.title};
}"""


def _clock(seconds) -> str:
    if seconds is None or seconds != seconds:   # NaN for live streams
        return "?"
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60}:{rest % 60:02d}"


async def _media(bridge, p):
    command = str(p.get("command") or p.get("text") or "status").strip().lower()
    command = {"seek_to": "seek", "jump": "seek", "go_to": "seek", "resume": "play", "stop": "pause",
               "rewind": "back", "backward": "back", "skip": "forward", "fast_forward": "forward",
               "playback_rate": "speed", "rate": "speed"}.get(command, command)
    number = parse_seconds(p.get("value")) if command in ("seek", "forward", "back") else None
    if command in ("volume", "speed"):
        try:
            number = float(str(p.get("value")).rstrip("%x "))
        except (TypeError, ValueError):
            return f"Give a value for {command} (volume 0-100, speed like 1.5)."
    if command == "seek" and number is None:
        return "Give the time to jump to, e.g. 1:20."

    async def run(d):
        state = await d.evaluate(_MEDIA_JS, {"command": command, "number": number})
        if not state or state.get("error"):
            return (state or {}).get("error", "Could not control the video.")
        return (f"{state['title']}: {'paused' if state['paused'] else 'playing'} at {_clock(state['time'])} "
                f"of {_clock(state['duration'])}, volume {state['volume']}%{' (muted)' if state['muted'] else ''}, "
                f"speed {state['speed']}x.")
    return await _on_active(bridge, run)


_ACTIONS = {
    "media": _media,
    "get_text": _get_text, "get_url": _get_url, "click": _click, "smart_click": _smart_click, "type": _type,
    "smart_type": _smart_type, "fill_form": _fill_form, "press": _press, "scroll": _scroll,
    "list_tabs": _list_tabs, "switch_tab": _switch_tab, "close_tab": _close_tab, "screenshot": _screenshot,
    "back": _tab_command("back", "Went back"), "forward": _tab_command("forward", "Went forward"),
    "reload": _tab_command("reload", "Reloaded the page"),
}
