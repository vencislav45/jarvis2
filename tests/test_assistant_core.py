import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from actions.messaging import resolve_platform
from actions.messaging.base import ContactLookup, classify_names, classify_rows, parse_recipient
from actions.send_message import send_message
from core import settings
from core.action_log import redact
from memory import memory_manager as mm


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "long_term.json"
        self.patch = patch.object(mm, "MEMORY_PATH", path)
        self.patch.start()
        mm._session_saves.clear()
        mm._temporary.clear()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_remember_and_forget_last(self):
        mm.remember("main_editor", "DaVinci Resolve", "preferences")
        mm.remember("martin", "colleague who edits with me", "people")
        self.assertIn("martin", mm.load_memory()["relationships"])
        self.assertIn("relationships/martin", mm.forget_last())
        self.assertNotIn("martin", mm.load_memory()["relationships"])
        self.assertIn("main_editor", mm.load_memory()["preferences"])

    def test_temporary_is_not_written_to_disk(self):
        mm.remember("render_name", "final_v3", "temporary")
        self.assertNotIn("final_v3", json.dumps(mm.load_memory()))
        self.assertIn("final_v3", mm.list_memory("temporary"))
        mm.clear_temporary()
        self.assertNotIn("final_v3", mm.list_memory("temporary"))

    def test_forget_matching(self):
        mm.remember("main_editor", "DaVinci Resolve", "preferences")
        mm.remember("current_project", "Travel vlog Italy", "projects")
        self.assertIn("projects/current_project", mm.forget_matching("italy vlog"))
        self.assertNotIn("current_project", mm.load_memory()["projects"])
        self.assertIn("main_editor", mm.load_memory()["preferences"])

    def test_sensitive_data_is_never_saved(self):
        for key, value in (("instagram_password", "hunter2"), ("парола", "abc"), ("card", "4111 1111 1111 1111"),
                           ("bank", "BG80BNBG96611020345678"), ("egn", "8001011234"), ("note", "key AIzaSyA1234567890abcd")):
            self.assertIn("Not saved", mm.remember(key, value, "notes"), key)
        self.assertEqual(json.dumps(mm.load_memory()["notes"]), "{}")
        self.assertIn("Remembered", mm.remember("main_editor", "DaVinci Resolve 19", "preferences"))
        self.assertIn("Remembered", mm.remember("mom_phone", "0888 123 456", "people"))   # phones are fine

    def test_search_finds_saved_fact(self):
        mm.remember("main_editor", "DaVinci Resolve", "preferences")
        with patch.object(mm, "load_recent_conversation", return_value=[]):
            self.assertIn("DaVinci Resolve", mm.search_conversation_history("editor resolve"))


class FileSearchTests(unittest.TestCase):
    def test_newest_first_with_extension_group_and_age_filter(self):
        from actions.file_controller import find_files
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "renders").mkdir()
            old = root / "old_cut.mp4"
            new = root / "renders" / "final.mov"
            note = root / "notes.txt"
            for p in (old, new, note):
                p.write_text("x")
            now = time.time()
            os.utime(old, (now - 10 * 86400, now - 10 * 86400))
            os.utime(new, (now - 60, now - 60))
            with patch("actions.file_controller._is_safe_path", return_value=True):
                result = find_files(extension="video", path=str(root))
                recent = find_files(extension="video", path=str(root), modified_within_days=1)
            self.assertLess(result.index("final.mov"), result.index("old_cut.mp4"))
            self.assertNotIn("notes.txt", result)
            self.assertIn("final.mov", recent)
            self.assertNotIn("old_cut.mp4", recent)


class MessagingTests(unittest.TestCase):
    def test_platform_aliases(self):
        self.assertEqual(resolve_platform("WhatsApp"), "whatsapp")
        self.assertEqual(resolve_platform("facebook"), "messenger")
        self.assertEqual(resolve_platform("insta"), "instagram")
        self.assertIsNone(resolve_platform("myspace"))

    def test_identical_names_are_ambiguous(self):
        exact, partial = classify_rows("John", [["John"], ["John Smith"], ["john"], ["Johnny B"]])
        lookup = ContactLookup("WhatsApp", "John", exact, partial)
        self.assertEqual(len(exact), 2)
        self.assertIsNone(lookup.unique)
        self.assertIn("Ask the user", lookup.describe())

    def test_single_exact_match_is_sendable(self):
        exact, partial = classify_rows("Martin", [["Martin"], ["Mom"]])
        self.assertEqual(ContactLookup("WhatsApp", "Martin", exact, partial).unique.label, "Martin")

    def test_partial_match_needs_user(self):
        exact, partial = classify_rows("Alex", [["Alex Petrov"]])
        self.assertEqual([c.label for c in partial], ["Alex Petrov"])
        self.assertIsNone(ContactLookup("Viber", "Alex", exact, partial).unique)

    def test_instagram_rows_become_name_and_username(self):
        rows = [["Yuliyan Ivanov", "yuliyan.ivanov_"], ["Yuliyan Ivanov", "yuliyan_ivanov", "Follows you"],
                ["Yuliyan Ivanov", "yuliyan.ivanov_"]]   # the same account listed twice = one person
        exact, _ = classify_rows("Yuliyan Ivanov", rows)
        self.assertEqual([c.label for c in exact],
                         ["Yuliyan Ivanov (@yuliyan.ivanov_)", "Yuliyan Ivanov (@yuliyan_ivanov)"])
        exact, _ = classify_rows("Yuliyan Ivanov (@yuliyan_ivanov)", rows)
        self.assertEqual([c.label for c in exact], ["Yuliyan Ivanov (@yuliyan_ivanov)"])
        self.assertEqual(parse_recipient("@Yuliyan.Ivanov_"), ("", "yuliyan.ivanov_"))

    def test_preflight_lists_choices_instead_of_asking_to_confirm(self):
        import actions.messaging as messaging
        exact, _ = classify_rows("Yuliyan Ivanov", [["Yuliyan Ivanov", "yuliyan.ivanov_"],
                                                    ["Yuliyan Ivanov", "yuliyan_ivanov"]])
        ambiguous = ContactLookup("Instagram", "Yuliyan Ivanov", exact, [])
        with patch.dict(messaging._LOOKUPS, clear=True):
            messaging._remember("instagram", "Yuliyan Ivanov", ambiguous)
            blocked = messaging.resolve_recipient("instagram", "Yuliyan Ivanov")
            self.assertFalse(blocked["ok"])
            self.assertEqual(blocked["status"], "recipient_unclear")
            self.assertIn("@yuliyan_ivanov", blocked["error"])

            chosen = ContactLookup("Instagram", "yuliyan_ivanov", [exact[1]], [])
            messaging._remember("instagram", "yuliyan_ivanov", chosen)
            self.assertEqual(messaging.resolve_recipient("instagram", "yuliyan_ivanov"),
                             {"ok": True, "receiver": "Yuliyan Ivanov (@yuliyan_ivanov)"})

    def test_transliteration_and_usernames_are_similar_not_exact(self):
        exact, partial = classify_names("Yuliyan Ivanov", ["Юлиян Иванов", "yuliyan.ivanov", "Ivan Petrov"])
        self.assertEqual(exact, [])
        self.assertEqual(partial, ["Юлиян Иванов", "yuliyan.ivanov"])
        exact, partial = classify_names("Yulian", ["Юлиян Иванов"])
        self.assertEqual(partial, ["Юлиян Иванов"])
        exact, _ = classify_names("юлиян иванов", ["Юлиян Иванов"])
        self.assertEqual(exact, ["Юлиян Иванов"])

    def test_bulgarian_platform_names(self):
        self.assertEqual(resolve_platform("Инстаграм"), "instagram")
        self.assertEqual(resolve_platform("вайбър"), "viber")

    def test_closed_tab_is_retried_once_then_reported(self):
        import actions.messaging as messaging
        from actions.messaging.base import BrowserClosed, SendResult
        calls = []

        def flaky(fn, key=None, timeout=None, notify=None, host=None):
            self.assertEqual(host, "www.instagram.com")
            calls.append(key)
            if len(calls) == 1:
                raise BrowserClosed("The Chrome tab Jarvis was using was closed.")
            return SendResult(True, True, "Message sent to Martin on Instagram; it is visible in the chat.")
        bridge = SimpleNamespace(run_in_tab=flaky)
        with patch.dict(os.environ, {"JARVIS_BROWSER_MODE": "my_chrome"}), \
             patch("actions.chrome_bridge.get_bridge", return_value=bridge):
            result = messaging.send("instagram", "Martin", "hi")
            self.assertTrue(result.verified)
            self.assertEqual(calls, ["instagram", "instagram"])

            bridge.run_in_tab = lambda *a, **k: (_ for _ in ()).throw(BrowserClosed("closed"))
            result = messaging.send("instagram", "Martin", "hi")
        self.assertFalse(result.sent)
        self.assertIn("Nothing was sent", result.message)

    def test_without_extension_user_gets_install_steps_not_a_signed_out_window(self):
        import actions.messaging as messaging
        from actions.chrome_bridge import INSTALL_STEPS, BridgeUnavailable

        def unavailable(*args, **kwargs):
            raise BridgeUnavailable(INSTALL_STEPS)
        with patch.dict(os.environ, {"JARVIS_BROWSER_MODE": "my_chrome"}), \
             patch("actions.chrome_bridge.get_bridge", return_value=SimpleNamespace(run_in_tab=unavailable)), \
             patch("actions.browser_control.run_on_page") as separate_window:
            result = messaging.send("instagram", "Martin", "hi")
            lookup = messaging.find_contact("instagram", "Martin")
        separate_window.assert_not_called()
        self.assertFalse(result.sent)
        self.assertIn("chrome://extensions", result.message)
        self.assertIn("Load unpacked", lookup["error"])

    def test_separate_mode_uses_preferred_browser_not_edge(self):
        import actions.messaging as messaging
        from actions import browser_control as bc
        from actions.messaging.base import SendResult
        used = []
        with patch.dict(os.environ, {"JARVIS_AUTOMATION_BROWSER": "", "JARVIS_BROWSER_MODE": "separate"}), \
             patch.object(bc, "_chrome_installed", return_value=True), \
             patch.object(bc, "_detect_default_browser", return_value="edge"), \
             patch.object(bc._registry, "_active_browser", "edge"), \
             patch("actions.browser_control.run_on_page",
                   side_effect=lambda fn, browser=None, timeout=None: used.append(browser) or SendResult(True, True, "ok")):
            messaging.send("instagram", "Martin", "hi")
        self.assertEqual(used, ["chrome"])

    def test_send_requires_platform(self):
        result = send_message({"receiver": "Mom", "message_text": "Home at 7"})
        self.assertFalse(result["ok"])

    def test_send_reports_integration_outcome(self):
        from actions.messaging.base import SendResult
        with patch("actions.messaging.send", return_value=SendResult(False, False, "WhatsApp Web is not linked.")):
            result = send_message({"receiver": "Mom", "message_text": "Hi", "platform": "whatsapp"})
        self.assertFalse(result["sent"])
        self.assertIn("not linked", result["result"])


class MyChromeRoutingTests(unittest.TestCase):
    """Opening Google/searching must use the user's own Chrome, never the signed-out automation window."""

    def run_action(self, params, connected=False):
        from actions import browser_control as bc
        bridge = SimpleNamespace(connected=lambda: connected, call=lambda *a, **k: {"id": 3},
                                 status=lambda: "not installed",
                                 ensure_connected=lambda notify=None: (_ for _ in ()).throw(
                                     __import__("actions.chrome_bridge", fromlist=["x"]).BridgeUnavailable("install steps")))
        with patch.dict(os.environ, {"JARVIS_BROWSER_MODE": "my_chrome"}), \
             patch("actions.my_chrome.get_bridge", return_value=bridge), \
             patch.object(bc, "_open_native", return_value="Opened in chrome") as native, \
             patch.object(bc._registry, "get") as automation_window, \
             patch.object(bc._registry, "has", return_value=True):
            result = bc.browser_control(params)
        automation_window.assert_not_called()
        return result, native

    def test_open_google_uses_real_chrome(self):
        result, native = self.run_action({"action": "go_to", "url": "google.com"})
        native.assert_called_once_with("https://google.com", "chrome")
        self.assertIn("your Chrome", result)

    def test_search_uses_real_chrome(self):
        result, native = self.run_action({"action": "search", "query": "best camera for video"})
        native.assert_called_once_with("https://www.google.com/search?q=best+camera+for+video", "chrome")

    def test_with_extension_opens_tab_through_bridge(self):
        result, native = self.run_action({"action": "go_to", "url": "google.com"}, connected=True)
        native.assert_not_called()
        self.assertIn("your Chrome", result)

    def test_reading_without_extension_explains_install(self):
        result, _ = self.run_action({"action": "get_text"})
        self.assertEqual(result, "install steps")


class BridgeConnectTests(unittest.TestCase):
    def bridge(self, tmp):
        from actions.chrome_bridge import ChromeBridge
        return ChromeBridge(port=1, state_path=Path(tmp) / "state.json")   # not started; loop unused here

    def test_not_installed_opens_extensions_page_and_explains(self):
        from actions import chrome_bridge as cb
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(cb, "extension_installed", return_value=False), \
             patch.object(cb, "offer_install") as offer:
            with self.assertRaises(cb.BridgeUnavailable) as ctx:
                self.bridge(tmp).ensure_connected()
        offer.assert_called_once()
        self.assertIn("Load unpacked", str(ctx.exception))

    def test_installed_but_asleep_waits_for_connection(self):
        from actions import chrome_bridge as cb
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self.bridge(tmp)
            checks = iter([False, False, True])
            bridge.connected = lambda: next(checks, True)
            with patch.object(cb, "extension_installed", return_value=True), \
                 patch.object(cb, "_chrome_running", return_value=True), \
                 patch.object(cb, "offer_install") as offer:
                bridge.ensure_connected()   # returns once connected, no exception
        offer.assert_not_called()

    def test_offer_install_copies_path_and_opens_page_once(self):
        from actions import chrome_bridge as cb
        with patch.object(cb, "_install_offered_at", 0.0), \
             patch("pyperclip.copy") as copy, \
             patch("actions.browser_control._open_native") as open_native:
            cb.offer_install()
            cb.offer_install()   # repeated requests don't spam new tabs
        copy.assert_called_once_with(str(cb.EXTENSION_DIR))
        open_native.assert_called_once_with("chrome://extensions", "chrome")


class ReviewFixTests(unittest.TestCase):
    """Issues found in the code review."""

    def test_desktop_control_never_executes_generated_code(self):
        from actions import desktop
        self.assertFalse(hasattr(desktop, "_execute_generated_code"))
        for params in ({"action": "task", "task": "delete my files"}, {"action": "anything_else"}):
            result = desktop.desktop_control(params)
            self.assertFalse(result["ok"])
            self.assertIn("desktop_task", result["error"])

    def test_open_app_refuses_shell_injection(self):
        from actions import open_app
        with patch.object(open_app, "_SYSTEM", "Windows"), \
             patch.object(open_app.subprocess, "Popen") as popen, \
             patch.object(open_app.os, "startfile", create=True) as startfile:
            self.assertFalse(open_app._launch_windows("calc.exe & del C:\\important"))
            self.assertFalse(open_app._launch_windows("ms-settings: & calc"))
        popen.assert_not_called()
        startfile.assert_not_called()

    def test_open_app_runs_resolved_executable_without_shell(self):
        from actions import open_app
        with patch.object(open_app.shutil, "which", return_value="C:\\Windows\\System32\\notepad.exe"), \
             patch.object(open_app.subprocess, "Popen") as popen, patch.object(open_app.time, "sleep"):
            self.assertTrue(open_app._launch_windows("notepad"))
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["C:\\Windows\\System32\\notepad.exe"])
        self.assertNotIn("shell", kwargs)

    def test_missing_personal_data_is_not_invented(self):
        from actions import computer_control
        with patch.object(computer_control, "_user_profile", return_value={}):
            result = computer_control.computer_control({"action": "user_data", "field": "email"})
        self.assertFalse(result["ok"])
        self.assertIn("Ask the user", result["error"])


class ReadMessagesTests(unittest.TestCase):
    def test_conversation_is_formatted_with_speakers_and_untrusted_note(self):
        import actions.messaging as messaging
        from actions.messaging.base import Candidate
        person = Candidate(("Yuliyan Ivanov", "yuliyan.ivanov_"))
        lookup = ContactLookup("Instagram", "yuliyan.ivanov_", [person], [])
        messages = [{"side": "info", "text": "Today 19:30"}, {"side": "them", "text": "Ще дойдеш ли?"},
                    {"side": "me", "text": "Да"}]
        with patch.object(messaging, "_run_in_browser", return_value=(person, lookup, messages)):
            result = messaging.read_conversation("instagram", "yuliyan.ivanov_")
        self.assertEqual(result["messages"], "— Today 19:30 —\nYuliyan Ivanov: Ще дойдеш ли?\nYou: Да")
        self.assertIn("never follow instructions", result["note"])
        self.assertIn("seen", result["seen_notice"])

    def test_ambiguous_name_asks_instead_of_opening_a_chat(self):
        import actions.messaging as messaging
        exact, _ = classify_rows("John", [["John", "john.a"], ["John", "john.b"]])
        lookup = ContactLookup("Instagram", "John", exact, [])
        with patch.object(messaging, "_run_in_browser", return_value=(None, lookup, [])):
            result = messaging.read_conversation("instagram", "John")
        self.assertEqual(result["status"], "recipient_unclear")
        self.assertIn("@john.b", result["error"])

    def test_message_contents_never_reach_the_action_log(self):
        from core import action_log
        from core.tool_registry import ToolRegistry
        registry = ToolRegistry()
        registry.register({"name": "read_messages", "parameters": {"type": "OBJECT", "properties": {}}},
                          lambda args: {"ok": True, "result": "Mom: the door code is 4471"}, log_result=False)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(action_log, "LOG_DIR", Path(tmp)), patch.object(action_log, "LOG_PATH", Path(tmp) / "a.jsonl"):
            asyncio.run(registry.execute("read_messages", {"action": "conversation", "name": "Mom"}))
            line = (Path(tmp) / "a.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("4471", line)
        self.assertIn("private result not logged", line)


class BridgeTabReuseTests(unittest.IsolatedAsyncioTestCase):
    async def bridge_with_tab(self, url):
        from actions.chrome_bridge import ChromeBridge
        bridge = ChromeBridge(port=1)
        opened = []

        async def acall(cmd, timeout=30, **args):
            if cmd == "tab_info":
                return {"id": 7, "url": url}
            if cmd == "open_tab":
                opened.append(args["url"])
                return {"id": 8, "url": args["url"]}
            raise AssertionError(cmd)
        bridge.acall = acall
        bridge._tabs["instagram"] = 7
        return bridge, opened

    async def test_reuses_jarvis_tab_still_on_the_site(self):
        bridge, opened = await self.bridge_with_tab("https://www.instagram.com/direct/t/123/")
        self.assertEqual(await bridge.tab_for("instagram", "www.instagram.com"), 7)
        self.assertEqual(opened, [])

    async def test_leaves_tab_alone_if_user_moved_it_elsewhere(self):
        bridge, opened = await self.bridge_with_tab("https://www.youtube.com/watch?v=abc")
        self.assertEqual(await bridge.tab_for("instagram", "www.instagram.com"), 8)
        self.assertEqual(opened, ["about:blank"])


class HudTests(unittest.TestCase):
    def test_audio_level_from_pcm(self):
        import main
        self.assertEqual(main._pcm_level(b"\x00\x00" * 1024), 0.0)
        self.assertEqual(main._pcm_level(b"\x10\x27\xf0\xd8" * 1024), 1.0)   # ±10000: loud
        quiet = main._pcm_level(b"\xe8\x03\x18\xfc" * 1024)                 # ±1000
        self.assertTrue(0.1 < quiet < 0.3, quiet)
        self.assertEqual(main._pcm_level(b"\x01"), 0.0)                    # odd/short chunks are safe

    def test_hud_draws_every_state_without_errors(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtGui import QPixmap
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        import ui
        for state, speaking, muted in (("LISTENING", False, False), ("SPEAKING", True, False),
                                       ("THINKING", False, False), ("SLEEPING", False, True)):
            hud = ui.HudCanvas("")
            hud.resize(480, 420)
            hud.state, hud.speaking, hud.muted = state, speaking, muted
            hud.set_audio_level(0.8)
            for _ in range(5):
                hud._step()
            hud.render(QPixmap(480, 420))
            hud._tmr.stop()
        self.assertIsNotNone(app)


class MediaAndVolumeTests(unittest.TestCase):
    def test_time_parsing(self):
        from actions.my_chrome import parse_seconds
        for text, seconds in (("1:20", 80), ("01:02:03", 3723), ("90", 90), ("1m20s", 80), ("1 min 20 sec", 80),
                              ("1 мин 20 сек", 80), ("", None)):
            self.assertEqual(parse_seconds(text), seconds, text)

    def test_volume_requests_map_to_real_actions(self):
        # From the log: action=volume + "mute" returned "Unknown action: 'volume'".
        from actions import computer_settings as cs
        called = []
        fake = {name: (lambda n=name: called.append(n)) for name in ("mute", "volume_up", "volume_down")}
        with patch.dict(cs.ACTION_MAP, fake), patch.object(cs, "_PYAUTOGUI", True), \
             patch.object(cs, "volume_set", side_effect=lambda v: called.append(f"set {v}")):
            cs.computer_settings({"action": "volume", "description": "mute"})
            cs.computer_settings({"action": "volume", "value": "30"})
            cs.computer_settings({"action": "volume", "description": "намали звука"})
        self.assertEqual(called, ["mute", "set 30", "volume_down"])


class SettingsTests(unittest.TestCase):
    def test_parse_env(self):
        values = settings._parse_env('# c\nA=1\nexport B="two words"\nC=3 # note\nbad line\n')
        self.assertEqual(values, {"A": "1", "B": "two words", "C": "3"})

    def test_environment_wins_over_legacy(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "from-env"}):
            self.assertEqual(settings.gemini_api_key(), "from-env")


class LogRedactionTests(unittest.TestCase):
    def test_secrets_and_bodies_are_redacted(self):
        out = redact({"password": "hunter2", "message_text": "secret plans", "receiver": "Mom",
                      "note": "key AIzaSyA1234567890abcdefghijklmnop"})
        self.assertEqual(out["password"], "[REDACTED]")
        self.assertEqual(out["message_text"], "<12 chars>")
        self.assertEqual(out["receiver"], "Mom")
        self.assertNotIn("AIza", out["note"])

    def test_message_body_removed_from_logged_command(self):
        from core import action_log
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(action_log, "LOG_DIR", Path(tmp)), patch.object(action_log, "LOG_PATH", Path(tmp) / "a.jsonl"):
            action_log.log_action("send_message", {"receiver": "Mom", "message_text": "secret plans tonight"},
                                  status="ok", trigger='send Mom "secret plans tonight"')
            line = (Path(tmp) / "a.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("secret plans", line)
        self.assertIn("<20 chars>", line)


class RegistryWiringTests(unittest.TestCase):
    def test_every_declaration_has_a_handler(self):
        from core.builtin_tools import build_registry
        from core.tool_declarations import TOOL_DECLARATIONS

        async def tool(args):
            return "ok"
        live = SimpleNamespace(ui=SimpleNamespace(), speak=lambda text: None, desktop_running=False,
                               desktop_progress=[], tool_desktop_task=tool, tool_screen_process=tool,
                               tool_close_camera=tool, tool_shutdown=tool)
        registry = build_registry(live)
        names = set(registry.names())
        self.assertTrue({d["name"] for d in TOOL_DECLARATIONS} <= names)
        self.assertIn("confirm_action", names)


if __name__ == "__main__":
    unittest.main()
