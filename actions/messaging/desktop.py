"""Desktop-app messaging (Viber, Telegram, Signal, Discord, …).

These apps expose no API for personal accounts and their windows give no reliable,
readable signal of which chat is open, so the assistant cannot verify the recipient
before sending. By default it therefore prepares a *draft*: it opens the app,
searches the contact, opens the top result and types the message without pressing
Enter. The user checks the recipient on screen and sends it.

Set JARVIS_DESKTOP_MESSAGING=send in .env to press Enter automatically; the result
is still reported as unverified.
"""
from __future__ import annotations

import time

from actions.messaging.base import SendResult
from core import settings


def _mode() -> str:
    return "send" if settings.get("JARVIS_DESKTOP_MESSAGING", "draft").lower() == "send" else "draft"


def send_via_desktop(app_name: str, receiver: str, message: str) -> SendResult:
    from actions import send_message as legacy   # reuses the existing app/search helpers

    legacy._require_pyautogui()
    if not legacy._open_app(app_name):
        return SendResult(False, False, f"Could not open {app_name}. Nothing was sent.")
    time.sleep(1.0)
    legacy._search_in_app(receiver)
    legacy.pyautogui.press("enter")
    time.sleep(0.8)
    legacy._paste_text(message)
    if _mode() == "draft":
        return SendResult(False, False,
                          f"Opened {app_name}, selected the top search result for '{receiver}' and typed the message "
                          f"as a draft. I can't verify the recipient in {app_name}, so I did not press send. "
                          "Check the chat on screen and press Enter to send it.")
    time.sleep(0.2)
    legacy.pyautogui.press("enter")
    return SendResult(True, False, f"Typed and submitted the message in {app_name} to the top result for '{receiver}'. "
                                   f"{app_name} gives no confirmation I can read, so please check it went to the right chat.")
