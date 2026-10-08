import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from google.genai import types
from core.desktop_agent import DesktopAgent

DECL = {"name": "file_controller", "parameters": {"type": "OBJECT", "properties": {
    "action": {"type": "STRING"}, "path": {"type": "STRING"}}, "required": ["action"]}}


def call(name, **args):
    return SimpleNamespace(candidates=[SimpleNamespace(content=types.Content(role="model", parts=[
        types.Part(function_call=types.FunctionCall(name=name, args=args))]))])


def finish():
    return call("finish_task", status="complete", summary="Verified the file.", evidence_steps=[1])


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from core import llm
        llm._unhealthy.clear()
        llm._retired.clear()
        # One model unless a test sets self.models — keeps call counts predictable.
        self.models = None
        self.chain = patch("core.llm.chain", side_effect=lambda model: [model] + [
            m for m in (self.models or []) if m != model])   # like llm.chain: requested model first
        self.chain.start()

    def tearDown(self):
        from core import llm
        self.chain.stop()
        llm._unhealthy.clear()
        llm._retired.clear()

    async def test_overloaded_model_switches_to_backup(self):
        # From the log: gemini-3.8-flash hung / answered 503 "high demand" and the task timed out.
        from google.genai.errors import APIError
        self.models = ["test", "backup"]   # build() configures the agent with model "test"
        busy = APIError(503, {"error": {"message": "This model is currently experiencing high demand."}})
        agent, execute, generate = self.build([busy, call("file_controller", action="read"), finish(),
                                               SimpleNamespace(text='{"verified":true,"reason":"ok"}')])
        result = await agent.run("Verify file")
        self.assertEqual(result["status"], "complete")
        used = [c.kwargs["model"] for c in generate.call_args_list]
        self.assertEqual(used, ["test", "backup", "backup", "backup"])   # stays on the model that answers

    def build(self, replies, max_rounds=8, execute=None):
        generate = AsyncMock(side_effect=replies)
        execute = execute or AsyncMock(return_value=SimpleNamespace(response={"result": "file exists, contents correct"}))
        agent = DesktopAgent(SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate))),
            [DECL], execute, lambda: (b"image", "image/png"), threading.Event(), lambda message: None,
            {"model": "test", "max_rounds": max_rounds, "max_actions": 4, "request_timeout_seconds": 1})
        return agent, execute, generate

    async def test_verified_success(self):
        agent, execute, generate = self.build([call("file_controller", action="read", path="C:/test/notes.txt"), finish(),
                                              SimpleNamespace(text='{"verified":true,"reason":"Read verified"}')])
        result = await agent.run("Verify the file")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["steps"]), 1)
        self.assertEqual(generate.await_count, 3)
        self.assertEqual(result["steps"][0]["arguments"]["path"], "C:/test/notes.txt")
        review_prompt = generate.call_args_list[-1].kwargs["contents"][0].parts[0].text
        self.assertIn("C:/test/notes.txt", review_prompt)

    async def test_reviewer_rejects_then_reports_blocker(self):
        agent, _, _ = self.build([call("file_controller", action="read"), finish(),
            SimpleNamespace(text='{"verified":false,"reason":"Wrong contents"}'),
            call("finish_task", status="blocked", summary="Wrong contents", evidence_steps=[])])
        self.assertEqual((await agent.run("Verify"))["status"], "blocked")

    async def test_unknown_tool_and_invalid_args_never_execute(self):
        agent, execute, _ = self.build([call("invented_tool", action="delete"),
            call("file_controller", path="x"), call("file_controller", action=42)], max_rounds=3)
        self.assertEqual((await agent.run("Inspect"))["status"], "blocked")
        execute.assert_not_awaited()

    async def test_cancellation_between_actions(self):
        agent, execute, _ = self.build([call("file_controller", action="read"), finish()])
        async def stop_after_first(fc):
            agent.cancelled.set()
            return SimpleNamespace(response={"result": "done"})
        execute.side_effect = stop_after_first
        self.assertEqual((await agent.run("Inspect"))["status"], "stopped")
        self.assertEqual(execute.await_count, 1)

    async def test_repeated_actions_are_bounded(self):
        agent, execute, _ = self.build([call("file_controller", action="read")] * 5, max_rounds=5)
        self.assertEqual((await agent.run("Inspect"))["status"], "blocked")
        self.assertEqual(execute.await_count, 3)

    async def test_no_evidence_cannot_finish(self):
        agent, execute, _ = self.build([finish()], max_rounds=1)
        self.assertEqual((await agent.run("Inspect"))["status"], "blocked")
        execute.assert_not_awaited()

    async def test_service_timeout(self):
        agent, _, _ = self.build([asyncio.TimeoutError()])
        self.assertEqual((await agent.run("Inspect"))["status"], "blocked")

    async def test_rate_limit_retries_reasoning_not_actions(self):
        from google.genai.errors import APIError
        limited = APIError(429, {"error": {"message": "Slow down"}})
        agent, execute, generate = self.build([call("file_controller", action="read"), limited,
            finish(), SimpleNamespace(text='{"verified":true,"reason":"Verified"}')])
        with patch("core.desktop_agent.retry_delay", return_value=0):
            self.assertEqual((await agent.run("Verify file"))["status"], "complete")
        self.assertEqual(execute.await_count, 1)
        self.assertEqual(generate.await_count, 4)

    async def test_daily_quota_does_not_retry(self):
        from google.genai.errors import APIError
        error = APIError(429, {"error": {"details": [{"quotaId": "GenerateRequestsPerDay-FreeTier"}]}})
        agent, execute, generate = self.build([error])
        result = await agent.run("Inspect")
        self.assertIn("daily API quota", result["summary"])
        self.assertEqual(generate.await_count, 1)
        execute.assert_not_awaited()

    async def test_tool_error_can_be_corrected(self):
        agent, execute, _ = self.build([call("file_controller", action="read", path="wrong"),
            call("file_controller", action="read", path="right"), finish(),
            SimpleNamespace(text='{"verified":true,"reason":"Corrected"}')])
        execute.side_effect = [OSError("not found"), SimpleNamespace(response={"result": "correct"})]
        self.assertEqual((await agent.run("Read file"))["status"], "complete")
        self.assertEqual(execute.await_count, 2)
