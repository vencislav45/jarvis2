"""Browser-based messaging (WhatsApp Web, Instagram Direct, Facebook Messenger).

These services have no official API for personal-account messaging, so they are
driven through a browser driver (actions/web_driver.py): by default a tab in the
user's own Chrome via the Jarvis Bridge extension, so their existing logins are
used. The integration:

* never logs in, solves CAPTCHAs, or works around 2FA / anti-automation checks:
  when the account is signed out it shows the login page and waits for the user
  to sign in themselves;
* sends only to a recipient whose name matches exactly one visible result;
* reports "sent" only after the message text appears in the conversation.

Web UIs change without notice; when an expected element is missing the action
fails with an explanation instead of guessing.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from actions.messaging.base import (BrowserClosed, Candidate, ContactLookup, DriverError, IntegrationError,
                                    NotSignedIn, SendResult, classify_rows, parse_recipient)
from core import settings

# Buttons that only close informational popups ("Turn on notifications?").
_DISMISS = ["Not Now", "Не сега", "Сега не", "Şimdi Değil"]


def login_wait_seconds() -> int:
    return max(0, min(600, settings.get_int("JARVIS_LOGIN_WAIT_SECONDS", 120)))


def _notify(message: str) -> None:
    from actions import messaging
    messaging.notify(message)


# JS snippets: called as fn(J, arg) — see web_driver.JS_LIB.
_STATE = "(J, a) => ({url: location.href, login: J.anyVisible(a.login), ready: J.anyVisible(a.ready)})"
_POINT_FIRST = "(J, a) => J.point(J.first(a.selectors, a))"
_SELECT_FIRST = "(J, a) => J.selectContents(J.first(a.selectors, a))"
_POINT_BUTTON = "(J, a) => J.point(J.buttonNamed(a.names, a.scope))"
_BUTTON_INFO = "(J, a) => J.buttonInfo(a.names, a.scope)"
_ROW_POINT = "(J, a) => J.rowPoint(a.scope, a.texts, a.attempt)"
_HAS_ANY_TEXT = "(J, a) => a.texts.some(t => !!J.textEl(t, null))"
_ROW_TEXTS = "(J, a) => J.rowTexts(a.scope)"
_COUNT_TEXT = "(J, a) => J.countText(a.text)"
_CHAT_STATE = ("(J, a) => ({url: location.href, dialog: !!J.root(a.scope), "
               "composer: !!J.first(a.composer, {editable: true})})")
_INBOX_ROWS = "(J, a) => J.inboxRows(a.limit)"
_INBOX_DEBUG = "(J, a) => J.inboxDebug()"
_THREAD_MESSAGES = "(J, a) => J.threadMessages(a.composer, a.limit)"


@dataclass
class WebMessenger:
    platform: str
    url: str
    login_url_markers: tuple[str, ...]
    login_selector: str
    ready_selector: str
    search_boxes: list[str]
    result_scope: str | None = None          # CSS selector or 'dialog-with-input'
    composer: list[str] = field(default_factory=lambda: [
        '[contenteditable="true"][role="textbox"]', '[contenteditable="true"][aria-label*="message" i]',
        'textarea[placeholder*="message" i]'])
    open_chat_buttons: tuple[str, ...] = ()
    chat_url_marker: str = ""
    home_url: str = ""
    inbox_url: str = ""   # conversation list (read_inbox)

    # ── helpers ───────────────────────────────────────────────────────────────
    @staticmethod
    async def _click(d, js: str, arg: dict) -> bool:
        point = await d.evaluate(js, arg)
        if not point:
            return False
        await d.click_at(point["x"], point["y"])
        return True

    # ── page state ────────────────────────────────────────────────────────────
    async def state(self, d) -> str:
        """login | ready | elsewhere | loading"""
        info = await d.evaluate(_STATE, {"login": self.login_selector, "ready": self.ready_selector}) or {}
        url = str(info.get("url", "")).casefold()
        if any(marker in url for marker in self.login_url_markers) or info.get("login"):
            return "login"
        if info.get("ready"):
            return "ready"
        if self.home_url and url.startswith(self.home_url) and not url.startswith(self.url.casefold()):
            return "elsewhere"   # signed in but redirected (home feed, "save login info" page…)
        return "loading"

    async def dismiss_popups(self, d) -> None:
        if await self._click(d, _POINT_BUTTON, {"names": _DISMISS, "scope": None}):
            await d.wait(500)

    async def open(self, d) -> None:
        """Open the messaging page; if signed out, wait for the user to sign in in that tab."""
        await d.goto(self.url)
        await d.bring_to_front()
        wait = login_wait_seconds()
        deadline = time.monotonic() + max(wait, 30)
        asked, last_nav = False, time.monotonic()
        while time.monotonic() < deadline:
            current = await self.state(d)
            if current == "ready":
                await self.dismiss_popups(d)
                if asked:
                    _notify(f"{self.platform} sign-in detected — continuing.")
                return
            await self.dismiss_popups(d)
            if current == "login":
                if not wait:
                    break
                if not asked:
                    asked = True
                    deadline = time.monotonic() + wait
                    await d.bring_to_front()
                    _notify(f"{self.platform} is signed out in this browser — please sign in in the tab I opened "
                            f"(waiting up to {wait} s).")
            elif current == "elsewhere" and time.monotonic() - last_nav > 4:
                last_nav = time.monotonic()
                await d.goto(self.url)
            await d.wait(1_000)
        if asked or await self.state(d) == "login":
            raise NotSignedIn(f"{self.platform} isn't signed in. Sign in in the tab I opened, then ask me again.")
        raise IntegrationError(f"{self.platform} did not finish loading (the page layout may have changed).")

    async def search(self, d, query: str) -> list[list[str]]:
        args = {"selectors": self.search_boxes, "editable": True}
        if not await self._click(d, _POINT_FIRST, args):
            raise IntegrationError(f"Could not find the {self.platform} search box.")
        await d.evaluate(_SELECT_FIRST, args)   # replace any previous search text
        await d.insert_text(query)
        # Wait until the result list stops changing (results load over the network).
        previous, stable = None, 0
        for _ in range(16):
            await d.wait(500)
            rows = await self.result_rows(d)
            stable = stable + 1 if rows == previous and rows else 0
            previous = rows
            if stable >= 2:
                break
        return previous or []

    async def result_rows(self, d) -> list[list[str]]:
        return await d.evaluate(_ROW_TEXTS, {"scope": self.result_scope}) or []

    async def _chat_open(self, d) -> bool:
        state = await d.evaluate(_CHAT_STATE, {"scope": self.result_scope, "composer": self.composer}) or {}
        if self.chat_url_marker and self.chat_url_marker in str(state.get("url", "")):
            return True
        return bool(state.get("composer")) and not state.get("dialog")

    async def select(self, d, person: Candidate) -> None:
        """Open the conversation with ``person`` (click its result row, then the Chat button if there is one)."""
        names = list(self.open_chat_buttons)
        button = None
        for attempt in range(3):   # whole row → its text → its checkbox
            point = await d.evaluate(_ROW_POINT, {"scope": self.result_scope, "texts": list(person.texts),
                                                  "attempt": attempt})
            if not point:
                if attempt == 0:
                    raise IntegrationError(f"{person.label} disappeared from the {self.platform} results.")
                continue
            await d.click_at(point["x"], point["y"])
            await d.wait(800)
            if await self._chat_open(d):
                return
            if not names:
                break
            button = await d.evaluate(_BUTTON_INFO, {"names": names, "scope": self.result_scope}) or \
                await d.evaluate(_BUTTON_INFO, {"names": names, "scope": None})
            if button and button["enabled"]:
                await d.click_at(button["point"]["x"], button["point"]["y"])
                break
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if await self._chat_open(d):
                await d.wait(800)
                return
            await d.wait(300)
        detail = ("no 'Chat' button found" if names and not button else
                  "the 'Chat' button stayed disabled (the person was not selected)" if button and not button["enabled"]
                  else "the chat did not appear")
        raise IntegrationError(f"Could not open the {self.platform} conversation with {person.label}: {detail}. "
                               "Nothing was sent.")

    async def verify_chat(self, d, person: Candidate) -> None:
        """Confirm the opened conversation belongs to ``person`` before typing anything."""
        if not await d.evaluate(_HAS_ANY_TEXT, {"texts": list(person.texts[:2])}):
            raise IntegrationError(f"Opened a {self.platform} chat but could not confirm it is {person.label}; "
                                   "nothing was sent.")

    # ── high-level operations ─────────────────────────────────────────────────
    async def find(self, d, query: str) -> ContactLookup:
        await self.open(d)
        name, user = parse_recipient(query)
        exact, partial = classify_rows(query, await self.search(d, user or name))
        return ContactLookup(self.platform, query, exact, partial)

    async def send(self, d, recipient: str, text: str) -> SendResult:
        submitted = False
        try:
            lookup = await self.find(d, recipient)
            if lookup.unique is None:
                return SendResult(False, False, lookup.describe() + " No message was sent.")
            person = lookup.unique
            await self.select(d, person)
            await self.verify_chat(d, person)
            if not await self._click(d, _POINT_FIRST, {"selectors": self.composer, "editable": True, "last": True}):
                return SendResult(False, False, f"Opened the chat with {person.label} but found no message box. "
                                                "Nothing was sent.")
            first_line = text.split("\n")[0].strip()
            before = await d.evaluate(_COUNT_TEXT, {"text": first_line})
            lines = text.split("\n")
            for index, line in enumerate(lines):
                if line:
                    await d.insert_text(line)
                if index < len(lines) - 1:
                    await d.press("Shift+Enter")   # new line without sending
            submitted = True
            await d.press("Enter")
            for _ in range(20):
                await d.wait(500)
                if await d.evaluate(_COUNT_TEXT, {"text": first_line}) > before:
                    return SendResult(True, True, f"Message sent to {person.label} on {self.platform}; "
                                                  "it is visible in the chat.")
        except (NotSignedIn, IntegrationError):
            raise
        except DriverError as e:
            if not submitted:
                if isinstance(e, BrowserClosed):
                    raise
                raise IntegrationError(f"{self.platform} page error before sending: {e}") from e
            # Enter may already have sent it — report as unverified, never as failed.
        return SendResult(True, False, f"The message was submitted to {recipient} on {self.platform} but I could "
                                       "not see it in the chat. Check before resending to avoid a duplicate.")

    # ── reading (read-only: nothing is typed or sent) ─────────────────────────
    async def read_inbox(self, d, count: int = 10) -> dict:
        """{"rows": [{"texts": [name, last message, time…], "unread": bool}], "title_count", "badge_count",
        "requests"} — the counts are Instagram's own unread indicators."""
        await d.goto(self.inbox_url or self.url)
        await d.bring_to_front()
        deadline = time.monotonic() + 25
        inbox: dict = {}
        while time.monotonic() < deadline:
            await self.dismiss_popups(d)
            inbox = await d.evaluate(_INBOX_ROWS, {"limit": count}) or {}
            if len(inbox.get("rows", [])) >= min(count, 2):
                break
            # Base check only: Instagram's own state() would open the "New message" dialog here.
            if await WebMessenger.state(self, d) == "login":
                raise NotSignedIn(f"{self.platform} isn't signed in. Sign in in the tab I opened, then ask again.")
            await d.wait(1_000)
        if inbox.get("rows") and not any(r["unread"] for r in inbox["rows"]):
            await d.wait(1_500)   # unread dots/badges can render a moment after the list
            inbox = await d.evaluate(_INBOX_ROWS, {"limit": count}) or inbox
        await self._save_layout_report(d)
        if not inbox.get("rows"):
            raise IntegrationError(f"Could not find the {self.platform} conversation list (the page layout may "
                                   "have changed).")
        return inbox

    async def _save_layout_report(self, d) -> None:
        """Anonymised inbox layout (sizes/colours/lengths, no names or text) for troubleshooting."""
        try:
            import json
            from core.action_log import LOG_DIR
            report = await d.evaluate(_INBOX_DEBUG)
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            (LOG_DIR / f"{self.platform.lower()}_inbox_layout.json").write_text(
                json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
        except Exception as e:  # noqa: BLE001 - diagnostics must never break reading
            print(f"[Messaging] Layout report skipped: {e}")

    async def read_conversation(self, d, recipient: str, count: int = 15) -> tuple[Candidate | None, ContactLookup, list]:
        """Open the chat with ``recipient`` and return its latest messages [{"side": me|them|info, "text"}]."""
        lookup = await self.find(d, recipient)
        if lookup.unique is None:
            return None, lookup, []
        person = lookup.unique
        await self.select(d, person)
        await self.verify_chat(d, person)
        messages: list = []
        for _ in range(10):   # history loads a moment after the chat opens
            await d.wait(700)
            latest = await d.evaluate(_THREAD_MESSAGES, {"composer": self.composer, "limit": count}) or []
            if latest and latest == messages:
                break
            messages = latest
        if not messages:
            raise IntegrationError(f"Opened the chat with {person.label} but could not read its messages "
                                   "(the page layout may have changed).")
        return person, lookup, messages


_NEW_MESSAGE = ["New message", "Send message", "Ново съобщение", "Изпрати съобщение", "Yeni mesaj", "Mesaj gönder"]


class InstagramDirect(WebMessenger):
    async def state(self, d) -> str:
        current = await super().state(d)
        if current in ("loading", "elsewhere") and "/direct/" in await d.current_url():
            # Signed in on the inbox without the dialog: open "New message" ourselves.
            if await self._click(d, _POINT_BUTTON, {"names": _NEW_MESSAGE, "scope": None}):
                await d.wait(1_000)
                return await super().state(d)
            return "loading"
        return current


_WA_NAMES = ("(J, a) => [...document.querySelectorAll('#pane-side [role=\"listitem\"], #pane-side [role=\"row\"]')]"
             ".map(r => (r.querySelector('span[title]') || {}).title || '').filter(Boolean)")
_WA_POINT = ("(J, a) => J.point([...document.querySelectorAll('#pane-side span[title]')]"
             ".find(s => s.title === a.name && J.visible(s)))")
_WA_HEADER = "(J, a) => { const h = document.querySelector('#main header'); return h ? h.innerText : ''; }"


class WhatsAppWeb(WebMessenger):
    async def result_rows(self, d) -> list[list[str]]:
        # One name per result row: the first titled span is the chat/contact name.
        return [[name] for name in await d.evaluate(_WA_NAMES) or []]

    async def select(self, d, person: Candidate) -> None:
        if not await self._click(d, _WA_POINT, {"name": person.name}):
            raise IntegrationError(f"'{person.name}' disappeared from the WhatsApp results.")
        await d.wait(1_000)

    async def verify_chat(self, d, person: Candidate) -> None:
        header = str(await d.evaluate(_WA_HEADER) or "").casefold()
        if person.name.casefold() not in header:
            raise IntegrationError(f"The open WhatsApp chat is not {person.name}; nothing was sent.")


SERVICES: dict[str, WebMessenger] = {
    "whatsapp": WhatsAppWeb(
        platform="WhatsApp", url="https://web.whatsapp.com/", login_url_markers=(),
        login_selector='canvas[aria-label], [data-ref]', ready_selector="#pane-side",
        search_boxes=['#side [contenteditable="true"]', '#side input[type="text"]', 'input[aria-label*="Search" i]'],
        composer=['#main footer [contenteditable="true"]'],
    ),
    "instagram": InstagramDirect(
        platform="Instagram", url="https://www.instagram.com/direct/new/",
        login_url_markers=("/accounts/login", "/challenge", "/checkpoint", "/two_factor"),
        login_selector='input[name="username"], input[name="password"]',
        ready_selector='[role="dialog"] input',
        search_boxes=['[role="dialog"] input[name="queryBox"]', '[role="dialog"] input[type="text"]',
                      '[role="dialog"] input'],
        result_scope="dialog-with-input",
        open_chat_buttons=("Chat", "Next", "Чат", "Напред", "Sohbet", "İleri"),
        chat_url_marker="/direct/t/",
        home_url="https://www.instagram.com/",
        inbox_url="https://www.instagram.com/direct/inbox/",
    ),
    "messenger": WebMessenger(
        platform="Messenger", url="https://www.facebook.com/messages/t/",
        login_url_markers=("/login", "checkpoint", "two_step_verification"),
        login_selector='input[name="email"], input[name="pass"]',
        ready_selector='input[aria-label*="Messenger" i], input[placeholder*="Messenger" i], input[type="search"]',
        search_boxes=['input[aria-label*="Messenger" i]', 'input[placeholder*="Messenger" i]', 'input[type="search"]'],
        result_scope='[role="navigation"]',
        home_url="https://www.facebook.com/",
    ),
}
