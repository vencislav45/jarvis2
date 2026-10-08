import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import action_log
from core.permissions import ConfirmationError, ConfirmationGate, Risk, classify, is_affirmative
from core.tool_registry import NO_RESULT, ToolRegistry


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def gate(clock=None, auto=()):
    return ConfirmationGate(policy_loader=lambda: {"auto_approve_medium": set(auto)}, clock=clock or Clock())


class ClassifyTests(unittest.TestCase):
    def test_low_risk_actions(self):
        self.assertEqual(classify("open_app", {"app_name": "Chrome"}), Risk.LOW)
        self.assertEqual(classify("browser_control", {"action": "search", "query": "x"}), Risk.LOW)
        self.assertEqual(classify("file_controller", {"action": "find", "extension": ".mp4"}), Risk.LOW)
        self.assertEqual(classify("system_command", {"command": "Get-Process | Sort-Object CPU"}), Risk.LOW)

    def test_medium_risk_actions(self):
        self.assertEqual(classify("send_message", {"receiver": "Mom"}), Risk.MEDIUM)
        self.assertEqual(classify("file_controller", {"action": "delete", "path": "x"}), Risk.MEDIUM)
        self.assertEqual(classify("system_command", {"command": "New-Item test.txt"}), Risk.MEDIUM)
        self.assertEqual(classify("system_command", {"command": "Get-Date; Remove-Item a.txt"}), Risk.MEDIUM)
        self.assertEqual(classify("browser_control", {"action": "smart_click", "description": "Post button"}), Risk.MEDIUM)
        self.assertEqual(classify("computer_settings", {"action": "shutdown"}), Risk.MEDIUM)

    def test_high_risk_actions(self):
        self.assertEqual(classify("browser_control", {"action": "click", "text": "Buy now"}), Risk.HIGH)
        self.assertEqual(classify("system_command", {"command": "Set-ExecutionPolicy Unrestricted"}), Risk.HIGH)
        self.assertEqual(classify("system_command", {"command": "Remove-Item C:\\x -Recurse -Force"}), Risk.HIGH)

    def test_commands_hidden_in_script_blocks_or_methods_need_confirmation(self):
        for command in ("Get-ChildItem C:\\Users -Recurse | ForEach-Object { Remove-Item $_.FullName }",
                        "Get-Process | Where-Object { Stop-Process -Id $_.Id }",
                        "Get-ChildItem | % { $_.Delete() }",
                        "(Get-Item C:\\x.txt).Delete()",
                        "[System.IO.File]::Delete('C:\\x.txt')",
                        "gci | ri", "Get-Service | Stop-Service", "Get-Process | ForEach-Object { kill $_.Id }"):
            self.assertGreaterEqual(classify("system_command", {"command": command}), Risk.MEDIUM, command)

    def test_recursive_delete_is_high_but_recursive_listing_is_low(self):
        self.assertEqual(classify("system_command", {"command": "Remove-Item C:\\x -Recurse"}), Risk.HIGH)
        self.assertEqual(classify("system_command", {"command": "rm C:\\x -r"}), Risk.HIGH)
        for command in ("Get-ChildItem C:\\Users -Recurse -Force",
                        "Get-ChildItem 'C:\\Program Files' -Recurse | Sort-Object Length -Descending | Select-Object -First 5",
                        "Get-ChildItem $env:USERPROFILE\\Videos -Recurse -Include *.mp4 | Where-Object { $_.LastWriteTime -gt (Get-Date).AddDays(-1) }",
                        "Get-PSDrive -PSProvider FileSystem | ForEach-Object { $_.Name.ToUpper() }"):
            self.assertEqual(classify("system_command", {"command": command}), Risk.LOW, command)

    def test_affirmative_detection(self):
        for text in ("Yes", "yes, send it", "Да", "давай", "ok go ahead"):
            self.assertTrue(is_affirmative(text), text)
        for text in ("No", "не", "yes but change it to 8", "wait", "cancel that", ""):
            self.assertFalse(is_affirmative(text), text)


class GateTests(unittest.TestCase):
    def test_low_risk_runs_without_confirmation(self):
        self.assertTrue(gate().check("open_app", {"app_name": "Chrome"}).allowed)

    def test_model_cannot_confirm_without_user_reply(self):
        g = gate()
        pending = g.check("send_message", {"receiver": "Martin", "message_text": "hi", "platform": "whatsapp"}).pending
        with self.assertRaises(ConfirmationError):
            g.confirm(pending.id)

    def test_reply_before_proposal_does_not_count(self):
        clock = Clock()
        g = gate(clock)
        g.note_user_input("yes")
        clock.now += 1
        pending = g.check("send_message", {"receiver": "Martin", "message_text": "hi", "platform": "x"}).pending
        with self.assertRaises(ConfirmationError):
            g.confirm(pending.id)

    def test_user_yes_releases_exact_action(self):
        clock = Clock()
        g = gate(clock)
        pending = g.check("send_message", {"receiver": "Martin", "message_text": "hi", "platform": "x"}).pending
        clock.now += 2
        g.note_user_input("Yes, send it")
        action = g.confirm(pending.id)
        self.assertEqual(action.args["receiver"], "Martin")
        with self.assertRaises(ConfirmationError):   # single use
            g.confirm(pending.id)

    def test_user_no_blocks(self):
        clock = Clock()
        g = gate(clock)
        pending = g.check("file_controller", {"action": "delete", "path": "x"}).pending
        clock.now += 1
        g.note_user_input("no, don't")
        with self.assertRaises(ConfirmationError):
            g.confirm(pending.id)

    def test_expiry(self):
        clock = Clock()
        g = gate(clock)
        pending = g.check("file_controller", {"action": "delete", "path": "x"}).pending
        clock.now += 500
        g.note_user_input("yes")
        with self.assertRaises(ConfirmationError):
            g.confirm(pending.id)

    def test_slow_answer_within_five_minutes_still_counts(self):
        # From the log: "да" arrived 129 s after the question and was rejected as expired.
        clock = Clock()
        g = gate(clock)
        pending = g.check("send_message", {"receiver": "x", "message_text": "hi", "platform": "instagram"}).pending
        clock.now += 129
        g.note_user_input("да")
        self.assertEqual(g.confirm(pending.id).id, pending.id)

    def test_only_the_latest_reply_counts(self):
        # Misheard "Na 1 julien." first, then a clear "Да." a few seconds later.
        clock = Clock()
        g = gate(clock)
        pending = g.check("send_message", {"receiver": "x", "message_text": "hi", "platform": "instagram"}).pending
        clock.now += 5
        g.note_user_input("no wait")
        clock.now += 10
        g.note_user_input("Да.")
        self.assertEqual(g.confirm(pending.id).id, pending.id)

    def test_transcript_chunks_of_one_reply_are_joined(self):
        clock = Clock()
        g = gate(clock)
        pending = g.check("send_message", {"receiver": "x", "message_text": "hi", "platform": "instagram"}).pending
        clock.now += 5
        g.note_user_input("yes,")
        clock.now += 1
        g.note_user_input("but change it to 8")
        with self.assertRaises(ConfirmationError):
            g.confirm(pending.id)

    def test_reusing_an_executed_id_explains_and_points_to_current(self):
        # From the log: the model re-sent an old confirmation id after that action had already run.
        clock = Clock()
        g = gate(clock)
        first = g.check("send_message", {"receiver": "a", "message_text": "hi", "platform": "instagram"}).pending
        clock.now += 2
        g.note_user_input("yes")
        g.confirm(first.id)
        clock.now += 2
        second = g.check("send_message", {"receiver": "b", "message_text": "hi", "platform": "instagram"}).pending
        with self.assertRaises(ConfirmationError) as ctx:
            g.confirm(first.id)
        self.assertIn("already ran", str(ctx.exception))
        self.assertIn(second.id, str(ctx.exception))

    def test_new_proposal_replaces_older_one_of_same_tool(self):
        g = gate()
        old = g.check("send_message", {"receiver": "a", "message_text": "hi", "platform": "x"}).pending
        new = g.check("send_message", {"receiver": "b", "message_text": "hi", "platform": "x"}).pending
        self.assertEqual([p.id for p in g.pending()], [new.id])
        with self.assertRaises(ConfirmationError) as ctx:
            g.confirm(old.id)
        self.assertIn("replaced", str(ctx.exception))

    def test_auto_approve_medium_but_never_high(self):
        g = gate(auto=("file_controller", "browser_control"))
        self.assertTrue(g.check("file_controller", {"action": "delete"}).allowed)
        self.assertFalse(g.check("browser_control", {"action": "click", "text": "Place order"}).allowed)


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        log_dir = Path(self.tmp.name)
        self.patches = [patch.object(action_log, "LOG_DIR", log_dir),
                        patch.object(action_log, "LOG_PATH", log_dir / "actions.jsonl")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def registry(self, clock=None):
        return ToolRegistry(gate(clock))

    @staticmethod
    def decl(name):
        return {"name": name, "parameters": {"type": "OBJECT", "properties": {}}}

    async def test_unknown_tool(self):
        result = await self.registry().execute("nope", {})
        self.assertFalse(result["ok"])

    async def test_empty_result_is_not_success(self):
        reg = self.registry()
        reg.register(self.decl("open_app"), lambda args: None)
        result = await reg.execute("open_app", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], NO_RESULT)

    async def test_exception_is_reported(self):
        reg = self.registry()

        def boom(args):
            raise RuntimeError("WhatsApp is not logged in")
        reg.register(self.decl("open_app"), boom)
        result = await reg.execute("open_app", {})
        self.assertFalse(result["ok"])
        self.assertIn("WhatsApp is not logged in", result["error"])

    async def test_retry_safe_tool_retries_transient_error(self):
        reg = self.registry()
        calls = []

        def flaky(args):
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionError("reset")
            return "headlines"
        reg.register(self.decl("news_report"), flaky, retry_safe=True)
        result = await reg.execute("news_report", {})
        self.assertEqual(result["result"], "headlines")
        self.assertEqual(len(calls), 2)

    async def test_confirmation_round_trip_runs_original_arguments(self):
        clock = Clock()
        reg = self.registry(clock)
        sent = []
        reg.register(self.decl("send_message"), lambda args: sent.append(args) or {"result": "sent", "ok": True})
        args = {"receiver": "Mom", "message_text": "Home at 7", "platform": "whatsapp"}
        held = await reg.execute("send_message", args)
        self.assertEqual(held["status"], "confirmation_required")
        self.assertEqual(sent, [])

        refused = await reg.execute("confirm_action", {"confirmation_id": held["confirmation_id"]})
        self.assertEqual(refused["status"], "not_confirmed")
        self.assertEqual(sent, [])

        clock.now += 3
        reg.note_user_input("да")
        done = await reg.execute("confirm_action", {"confirmation_id": held["confirmation_id"]})
        self.assertEqual(done["result"], "sent")
        self.assertEqual(sent, [args])

    async def test_preflight_can_block_before_any_confirmation(self):
        reg = self.registry()
        sent = []
        reg.register(self.decl("send_message"), lambda args: sent.append(args),
                     preflight=lambda args: {"ok": False, "status": "recipient_unclear", "error": "2 accounts"})
        result = await reg.execute("send_message", {"receiver": "John", "message_text": "hi", "platform": "x"})
        self.assertEqual(result["status"], "recipient_unclear")
        self.assertEqual(reg.gate.pending(), [])
        self.assertEqual(sent, [])

    async def test_preflight_resolved_receiver_is_what_gets_confirmed(self):
        clock = Clock()
        reg = self.registry(clock)
        sent = []
        reg.register(self.decl("send_message"), lambda args: sent.append(args) or {"result": "sent"},
                     preflight=lambda args: {"args": {**args, "receiver": "John (@john.b)"}})
        held = await reg.execute("send_message", {"receiver": "john.b", "message_text": "hi", "platform": "x"})
        self.assertIn("John (@john.b)", held["action"])
        clock.now += 2
        reg.note_user_input("yes")
        await reg.execute("confirm_action", {"confirmation_id": held["confirmation_id"]})
        self.assertEqual(sent[0]["receiver"], "John (@john.b)")

    async def test_cancel(self):
        reg = self.registry()
        reg.register(self.decl("send_message"), lambda args: "sent")
        held = await reg.execute("send_message", {"receiver": "x", "message_text": "y", "platform": "z"})
        cancelled = await reg.execute("cancel_action", {})
        self.assertIn("nothing was executed", cancelled["result"])
        self.assertEqual(reg.gate.pending(), [])
        self.assertTrue(held["confirmation_id"])


if __name__ == "__main__":
    unittest.main()
