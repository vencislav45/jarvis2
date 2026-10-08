"""A small browser driver interface shared by two backends.

* CdpDriver        — a tab in the user's own Chrome, through the Jarvis Bridge extension
                     (actions/chrome_bridge.py). Uses the user's signed-in accounts.
* PlaywrightDriver — a page in the separate automation window (JARVIS_BROWSER_MODE=separate).

All page logic (finding elements, reading results) is JavaScript in JS_LIB, so the
integrations behave the same on both. Clicks and typing are real input events
(Input.dispatchMouseEvent / insertText), not synthetic DOM events.
"""
from __future__ import annotations

import asyncio
import json
import time


class DriverError(Exception):
    """The page could not do what was asked (element missing, script error)."""


class BrowserClosed(DriverError):
    """The tab/window was closed or the browser went away."""


def is_closed_error(error: BaseException) -> bool:
    text = str(error)
    return any(marker in text for marker in (
        "has been closed", "Target closed", "No tab with id", "Debugger is not attached", "No target with given id",
        "Detached while handling command")) or type(error).__name__ == "TargetClosedError"


# Helpers available as `J` inside every evaluate() call.
JS_LIB = r"""{
  norm(t) { return (t || '').replace(/\s+/g, ' ').trim(); },
  visible(el) {
    if (!el || !el.isConnected) return false;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  },
  editable(el) {
    if (!el) return false;
    if (el.isContentEditable) return true;
    if (el.tagName === 'TEXTAREA') return !el.disabled && !el.readOnly;
    if (el.tagName === 'INPUT') return !el.disabled && !el.readOnly &&
      !['button', 'submit', 'checkbox', 'radio', 'hidden', 'file', 'image', 'reset', 'range', 'color'].includes((el.type || '').toLowerCase());
    return false;
  },
  root(scope) {
    if (!scope) return document;
    if (scope === 'dialog-with-input')
      return [...document.querySelectorAll('[role="dialog"]')].find(d => J.visible(d) && d.querySelector('input')) || null;
    return [...document.querySelectorAll(scope)].find(e => J.visible(e)) || null;
  },
  all(selector, scope) { const r = J.root(scope); return r ? [...r.querySelectorAll(selector)] : []; },
  first(selectors, o) {
    o = o || {};
    for (const sel of selectors) {
      const els = J.all(sel, o.scope).filter(e => J.visible(e) && (!o.editable || J.editable(e)));
      if (els.length) return o.last ? els[els.length - 1] : els[0];
    }
    return null;
  },
  anyVisible(selector) { return !!selector && J.all(selector).some(J.visible); },
  label(el) {
    return J.norm(el.getAttribute('aria-label') || el.innerText || el.textContent || el.getAttribute('title') || '');
  },
  buttonNamed(names, scope) {
    const wanted = names.map(n => n.toLowerCase());
    return J.all('button, a, [role="button"], [role="link"]', scope)
      .find(e => J.visible(e) && wanted.includes(J.label(e).toLowerCase())) || null;
  },
  textEl(text, scope) {
    const root = J.root(scope);
    if (!root) return null;
    const want = J.norm(text).toLowerCase();
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const el = n.parentElement;
      if (el && J.norm(n.nodeValue).toLowerCase() === want && J.visible(el) &&
          !el.closest('input, textarea, [contenteditable="true"], script, style')) return el;
    }
    return null;
  },
  point(el) {
    if (!el) return null;
    el.scrollIntoView({ block: 'center', inline: 'center' });
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  },
  selectContents(el) {
    if (!el) return false;
    el.focus();
    if (typeof el.select === 'function') el.select();
    else {
      const range = document.createRange();
      range.selectNodeContents(el);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }
    return true;
  },
  ROW: '[role="button"], [role="option"], [role="listitem"], [role="row"], [role="checkbox"], [role="radio"], label, a',
  rowMap(scope) {
    // Result rows → their visible leaf texts ("Yuliyan Ivanov", "yuliyan.ivanov_", …).
    const root = J.root(scope);
    const rows = new Map();
    if (!root) return rows;
    for (const el of root.querySelectorAll('span, div, a, h1, h2, h3')) {
      if (el.childElementCount !== 0 || !J.visible(el)) continue;
      if (el.closest('input, textarea, [contenteditable="true"]')) continue;
      const text = J.norm(el.textContent);
      if (!text || text.length > 80) continue;
      const row = el.closest(J.ROW) || el;
      if (!root.contains(row)) continue;
      if (!rows.has(row)) rows.set(row, []);
      const texts = rows.get(row);
      if (!texts.some(t => t.toLowerCase() === text.toLowerCase())) texts.push(text);
    }
    return rows;
  },
  rowTexts(scope) { return [...J.rowMap(scope).values()]; },
  rowPoint(scope, texts, attempt) {
    // attempt 0: the whole row; 1: its first text; 2: a checkbox/radio near it.
    const want = texts.map(t => t.toLowerCase());
    for (const [row, rowTexts] of J.rowMap(scope)) {
      const have = rowTexts.map(t => t.toLowerCase());
      if (!want.every(t => have.includes(t))) continue;
      if (attempt === 1) return J.point(J.textEl(texts[0], scope) || row);
      if (attempt === 2) {
        for (let node = row, i = 0; node && i < 6; node = node.parentElement, i++) {
          const box = node.querySelector('input[type="checkbox"], input[type="radio"], [role="checkbox"], [role="radio"], [aria-checked]');
          if (box && J.visible(box)) return J.point(box);
        }
        return null;
      }
      return J.point(row);
    }
    return null;
  },
  buttonInfo(names, scope) {
    const el = J.buttonNamed(names, scope);
    if (!el) return null;
    const disabled = el.disabled || el.getAttribute('aria-disabled') === 'true' ||
                     !!(el.closest && el.closest('[aria-disabled="true"]'));
    return { point: J.point(el), enabled: !disabled };
  },
  countText(text) {
    // Rendered occurrences of `text` outside input boxes (a typed draft never counts as sent).
    const want = J.norm(text);
    return [...document.querySelectorAll('span, div, p')].filter(e =>
      e.childElementCount === 0 && J.norm(e.textContent) === want &&
      !e.closest('[contenteditable="true"], input, textarea') && J.visible(e)).length;
  },
  scrollRegions() {
    return [...document.querySelectorAll('div, section, main, ul, [role="grid"], [role="list"]')].filter(e => {
      const s = getComputedStyle(e);
      return /(auto|scroll)/.test(s.overflowY) && e.scrollHeight > e.clientHeight + 40 && J.visible(e);
    });
  },
  textBlocks(root) {
    // Outermost visible elements that carry text (one per message / name / preview).
    const picked = [], seen = new Set();
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      if (!n.nodeValue.trim()) continue;
      let el = n.parentElement;
      if (!el || el.closest('input, textarea, [contenteditable="true"], script, style, noscript')) continue;
      const block = el.closest('[dir="auto"]');
      if (block && root.contains(block)) el = block;
      if (seen.has(el) || !J.visible(el)) continue;
      seen.add(el);
      picked.push(el);
    }
    return picked.filter(el => !picked.some(o => o !== el && o.contains(el)));
  },
  threadMessages(composerSelectors, limit) {
    // The chat history is the scrollable area above the message box. Own messages sit at the
    // right edge, the other person's at the left; centred lines are dates/"Seen".
    const composer = J.first(composerSelectors, { editable: true, last: true });
    if (!composer) return null;
    const c = composer.getBoundingClientRect();
    let best = null, bestCount = 0;
    for (const r of J.scrollRegions()) {
      const b = r.getBoundingClientRect();
      if (r.contains(composer) || b.bottom > c.top + 60 || b.right < c.left || b.left > c.right || b.height < 100) continue;
      const count = J.textBlocks(r).length;
      if (count > bestCount) { best = r; bestCount = count; }
    }
    if (!best) return null;
    const box = best.getBoundingClientRect();
    const items = J.textBlocks(best).map(el => {
      const r = el.getBoundingClientRect();
      const left = r.left - box.left, right = box.right - r.right;
      const side = right < left * 0.5 ? 'me' : left < right * 0.5 ? 'them' : 'info';
      return { y: r.top, side, text: J.norm(el.innerText).slice(0, 600) };
    }).filter(i => i.text);
    items.sort((a, b) => a.y - b.y);
    return items.slice(-limit).map(({ side, text }) => ({ side, text }));
  },
  UNREAD_WORDS: /\bunread\b|непрочет|okunmad|не прочетен/i,
  inboxRowElements() {
    // Conversation list: the scroll area with the most row-like entries (name + preview + time).
    const rowSel = 'a[href*="/direct/t/"], a[href*="/messages/t/"], [role="button"], [role="listitem"], [role="row"], [role="link"]';
    const rowsIn = region => {
      const rows = [...region.querySelectorAll(rowSel)].filter(e => {
        if (!J.visible(e)) return false;
        const b = e.getBoundingClientRect(), n = J.textBlocks(e).length;
        return b.height >= 36 && b.height <= 150 && n >= 2 && n <= 8;
      });
      return rows.filter(e => !rows.some(o => o !== e && o.contains(e)));
    };
    let best = [];
    for (const region of J.scrollRegions()) {
      const rows = rowsIn(region);
      if (rows.length > best.length) best = rows;
    }
    // Whole page only as a fallback: it would also pick up side-menu links like "Messages 2".
    return best.length >= 2 ? best : rowsIn(document.body);
  },
  isBlueDot(el) {
    // Small round blue element without text (Instagram/Messenger unread dot).
    if (el.childElementCount || J.norm(el.textContent)) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.width > 18 || Math.abs(r.width - r.height) > 2) return false;
    const m = getComputedStyle(el).backgroundColor.match(/\d+(\.\d+)?/g);
    if (!m) return false;
    const [red, green, blue, alpha] = m.map(Number);
    return (alpha === undefined || alpha > 0.5) && blue > 170 && red < 120;
  },
  rowSignals(row) {
    const blocks = J.textBlocks(row);
    const labels = [row, ...row.querySelectorAll('[aria-label]')].map(e => e.getAttribute('aria-label') || '').join(' ');
    return {
      blocks,
      dot: [...row.querySelectorAll('div, span')].some(J.isBlueDot),
      label: J.UNREAD_WORDS.test(labels) || J.UNREAD_WORDS.test(row.innerText),
      bold: blocks.slice(1).some(b => Number(getComputedStyle(b).fontWeight) >= 600),
    };
  },
  countIn(el) {
    // A badge: a small element whose whole text is a number ("3", "9+").
    if (!el) return 0;
    for (const e of [el, ...el.querySelectorAll('*')]) {
      const t = J.norm(e.textContent);
      if (/^\d{1,3}\+?$/.test(t) && e.childElementCount === 0 && J.visible(e)) return parseInt(t, 10);
    }
    return 0;
  },
  inboxSignals() {
    // "(3) Inbox • Direct": only trust a title counter on a messages page (elsewhere it can count likes etc.).
    const title = /direct|inbox|message|chat|съобщ|чат/i.test(document.title) ? (document.title.match(/^\((\d+)\)/) || [])[1] : null;
    const navLink = [...document.querySelectorAll('a[href*="/direct/inbox"], a[href*="/messages"]')].find(J.visible);
    const requests = [...document.querySelectorAll('a[href*="/direct/requests"], [role="tab"], [role="button"], a')]
      .find(e => /^(requests|заявки|istekler|message requests)\b/i.test(J.norm(e.innerText)) && J.visible(e));
    const requestText = requests ? J.norm(requests.innerText) : '';
    return {
      title_count: title ? parseInt(title, 10) : 0,
      badge_count: J.countIn(navLink),
      requests: requests ? (parseInt((requestText.match(/\d+/) || ['0'])[0], 10) || J.countIn(requests)) : 0,
    };
  },
  inboxRows(limit) {
    const rows = J.inboxRowElements().slice(0, limit).map(row => {
      const s = J.rowSignals(row);
      return { texts: [...new Set(s.blocks.map(b => J.norm(b.innerText)).filter(Boolean))],
               dot: s.dot, label: s.label, bold: s.bold };
    });
    // Bold only means "unread" if some rows are bold and others are not.
    const boldRows = rows.filter(r => r.bold).length;
    const boldUseful = boldRows > 0 && boldRows < rows.length;
    for (const r of rows) {
      r.unread = r.dot || r.label || (boldUseful && r.bold);
      delete r.dot; delete r.label; delete r.bold;
    }
    return { rows, ...J.inboxSignals() };
  },
  inboxDebug() {
    // Layout report for troubleshooting. Contains NO names or message text: only sizes,
    // weights, colours and lengths, so it is safe to store and share.
    const rows = J.inboxRowElements();
    const describe = el => {
      const s = getComputedStyle(el), r = el.getBoundingClientRect();
      return { tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '', w: Math.round(r.width),
               h: Math.round(r.height), weight: s.fontWeight, color: s.color, chars: J.norm(el.innerText).length };
    };
    return {
      url_path: location.pathname, rows_found: rows.length, signals: J.inboxSignals(),
      rows: rows.slice(0, 15).map(row => {
        const s = J.rowSignals(row);
        const small = [...row.querySelectorAll('div, span')].filter(e => {
          const r = e.getBoundingClientRect();
          return !e.childElementCount && r.width > 3 && r.width <= 18 && r.height > 3 && r.height <= 18;
        }).map(e => ({ w: Math.round(e.getBoundingClientRect().width), bg: getComputedStyle(e).backgroundColor,
                       radius: getComputedStyle(e).borderRadius }));
        return { row: describe(row), blocks: s.blocks.map(describe), dot: s.dot, unread_label: s.label,
                 aria_labels: row.querySelectorAll('[aria-label]').length, small_elements: small.slice(0, 6) };
      }),
    };
  },
  smartFind(description, editable) {
    const want = J.norm(description).toLowerCase();
    if (!want) return null;
    const selector = editable
      ? 'input, textarea, [contenteditable="true"], [role="textbox"], [role="searchbox"], [role="combobox"]'
      : 'button, a, input[type="submit"], input[type="button"], [role="button"], [role="link"], [role="tab"], [role="menuitem"], [role="checkbox"], [role="option"], summary, label';
    let best = null, bestScore = 0;
    for (const el of J.all(selector)) {
      if (!J.visible(el) || (editable && !J.editable(el))) continue;
      const labels = [el.getAttribute('aria-label'), el.getAttribute('placeholder'), el.getAttribute('title'),
                      el.getAttribute('alt'), el.getAttribute('name'), el.labels && el.labels[0] && el.labels[0].innerText,
                      editable ? '' : el.innerText].map(J.norm).filter(Boolean).map(s => s.toLowerCase());
      for (const l of labels) {
        const score = l === want ? 3 : l.startsWith(want) ? 2 : l.includes(want) ? 1 : 0;
        if (score > bestScore) { bestScore = score; best = el; }
      }
    }
    return best || (editable ? null : J.textEl(description));
  }
}"""


def expression(fn: str, arg=None) -> str:
    """JS expression that runs ``fn(J, arg)`` with the helper library."""
    return f"(() => {{ const J = {JS_LIB}; return ({fn})(J, {json.dumps(arg)}); }})()"


# Key name → (key, code, windowsVirtualKeyCode, text) for CDP key events.
_KEYS = {
    "enter": ("Enter", "Enter", 13, "\r"), "escape": ("Escape", "Escape", 27, ""), "tab": ("Tab", "Tab", 9, ""),
    "backspace": ("Backspace", "Backspace", 8, ""), "delete": ("Delete", "Delete", 46, ""),
    "arrowdown": ("ArrowDown", "ArrowDown", 40, ""), "arrowup": ("ArrowUp", "ArrowUp", 38, ""),
    "arrowleft": ("ArrowLeft", "ArrowLeft", 37, ""), "arrowright": ("ArrowRight", "ArrowRight", 39, ""),
    "pagedown": ("PageDown", "PageDown", 34, ""), "pageup": ("PageUp", "PageUp", 33, ""),
    "home": ("Home", "Home", 36, ""), "end": ("End", "End", 35, ""), "space": (" ", "Space", 32, " "),
    "f5": ("F5", "F5", 116, ""),
}
_MODIFIERS = {"alt": 1, "control": 2, "ctrl": 2, "meta": 4, "shift": 8}


def cdp_key(spec: str) -> tuple[dict, int]:
    """Translate "Shift+Enter" / "Control+a" / "Escape" into CDP key event fields + modifier mask."""
    parts = spec.split("+")
    modifiers = sum(_MODIFIERS.get(p.strip().lower(), 0) for p in parts[:-1])
    name = parts[-1].strip()
    if name.lower() in _KEYS:
        key, code, vk, text = _KEYS[name.lower()]
    elif len(name) == 1:
        key, code, vk, text = name, f"Key{name.upper()}" if name.isalpha() else "", ord(name.upper()), name
    else:
        raise DriverError(f"Unsupported key: {spec}")
    if modifiers & (1 | 2 | 4):
        text = ""   # shortcuts don't type characters
    fields = {"key": key, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
    if text:
        fields["text"] = text
    return fields, modifiers


class CdpDriver:
    """One tab of the user's Chrome, driven over the Jarvis Bridge (Chrome DevTools Protocol)."""

    def __init__(self, bridge, tab_id: int):
        self.bridge = bridge
        self.tab_id = tab_id

    async def _cdp(self, method: str, params: dict | None = None, timeout: float = 30):
        try:
            return await self.bridge.acall("cdp", timeout=timeout, tabId=self.tab_id, method=method,
                                           params=params or {})
        except BrowserClosed:
            raise
        except Exception as e:  # noqa: BLE001
            if is_closed_error(e):
                raise BrowserClosed("The Chrome tab Jarvis was using was closed.") from e
            raise DriverError(str(e)) from e

    async def evaluate(self, fn: str, arg=None):
        result = await self._cdp("Runtime.evaluate", {"expression": expression(fn, arg), "returnByValue": True,
                                                      "awaitPromise": True, "userGesture": True})
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            text = (details.get("exception") or {}).get("description") or details.get("text") or "script error"
            raise DriverError(text.splitlines()[0])
        return (result.get("result") or {}).get("value")

    async def _try_eval(self, js: str):
        try:
            result = await self._cdp("Runtime.evaluate", {"expression": js, "returnByValue": True}, timeout=10)
            return (result.get("result") or {}).get("value")
        except DriverError:
            return None   # mid-navigation: no execution context yet

    async def goto(self, url: str, timeout: float = 30) -> None:
        before = await self._try_eval("performance.timeOrigin")
        result = await self._cdp("Page.navigate", {"url": url})
        if result.get("errorText"):
            raise DriverError(f"Could not open {url}: {result['errorText']}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            state = await self._try_eval("[performance.timeOrigin, document.readyState]")
            if state and state[0] != before and state[1] in ("interactive", "complete"):
                return
        # Same-document navigations keep timeOrigin; carry on with whatever loaded.

    async def current_url(self) -> str:
        return await self._try_eval("location.href") or ""

    async def click_at(self, x: float, y: float) -> None:
        for event in ({"type": "mouseMoved"}, {"type": "mousePressed", "buttons": 1},
                      {"type": "mouseReleased", "buttons": 0}):
            params = {"x": x, "y": y, "button": "left" if event["type"] != "mouseMoved" else "none",
                      "clickCount": 1 if event["type"] != "mouseMoved" else 0, **event}
            await self._cdp("Input.dispatchMouseEvent", params)

    async def insert_text(self, text: str) -> None:
        await self._cdp("Input.insertText", {"text": text})

    async def press(self, key: str) -> None:
        fields, modifiers = cdp_key(key)
        await self._cdp("Input.dispatchKeyEvent", {"type": "keyDown" if "text" in fields else "rawKeyDown",
                                                   "modifiers": modifiers, **fields})
        await self._cdp("Input.dispatchKeyEvent", {"type": "keyUp", "modifiers": modifiers,
                                                   **{k: v for k, v in fields.items() if k != "text"}})

    async def wait(self, ms: int) -> None:
        await asyncio.sleep(ms / 1000)

    async def bring_to_front(self) -> None:
        try:
            await self.bridge.acall("activate", tabId=self.tab_id)
        except Exception as e:  # noqa: BLE001
            if is_closed_error(e):
                raise BrowserClosed("The Chrome tab Jarvis was using was closed.") from e

    async def screenshot(self) -> bytes:
        import base64
        data = await self._cdp("Page.captureScreenshot", {"format": "png"})
        return base64.b64decode(data["data"])


class PlaywrightDriver:
    """A Playwright page (the separate automation window)."""

    def __init__(self, page):
        self.page = page

    async def _guard(self, coro):
        try:
            return await coro
        except Exception as e:  # noqa: BLE001
            if is_closed_error(e):
                raise BrowserClosed("The Jarvis browser window was closed.") from e
            raise DriverError(str(e).splitlines()[0] if str(e) else type(e).__name__) from e

    async def evaluate(self, fn: str, arg=None):
        return await self._guard(self.page.evaluate(expression(fn, arg)))

    async def goto(self, url: str, timeout: float = 30) -> None:
        await self._guard(self.page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000)))

    async def current_url(self) -> str:
        return self.page.url

    async def click_at(self, x: float, y: float) -> None:
        await self._guard(self.page.mouse.click(x, y))

    async def insert_text(self, text: str) -> None:
        await self._guard(self.page.keyboard.insert_text(text))

    async def press(self, key: str) -> None:
        await self._guard(self.page.keyboard.press(key))

    async def wait(self, ms: int) -> None:
        await self._guard(self.page.wait_for_timeout(ms))

    async def bring_to_front(self) -> None:
        await self._guard(self.page.bring_to_front())

    async def screenshot(self) -> bytes:
        return await self._guard(self.page.screenshot())
