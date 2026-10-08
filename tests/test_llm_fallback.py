import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from google.genai.errors import APIError

from core import llm


def busy():
    return APIError(503, {"error": {"code": 503, "message": "This model is currently experiencing high demand.",
                                    "status": "UNAVAILABLE"}})


def retired():
    return APIError(404, {"error": {"code": 404, "message": "This model is no longer available to new users.",
                                    "status": "NOT_FOUND"}})


class FallbackTests(unittest.TestCase):
    def setUp(self):
        llm._unhealthy.clear()
        llm._retired.clear()
        self.chain = patch.object(llm, "chain", side_effect=lambda model: [
            m for m in ["primary", "backup", "last"] if m not in llm._retired])
        self.chain.start()

    def tearDown(self):
        self.chain.stop()
        llm._unhealthy.clear()
        llm._retired.clear()

    def client(self, *outcomes):
        calls = []

        def generate_content(model, contents, config):
            calls.append(model)
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)), calls

    def test_busy_model_falls_back(self):
        client, calls = self.client(busy(), SimpleNamespace(text="OK"))
        self.assertEqual(llm.generate(client, "primary", "hi").text, "OK")
        self.assertEqual(calls, ["primary", "backup"])
        self.assertIn("primary", llm.status()["cooling_down"])

    def test_retired_model_is_never_tried_again(self):
        client, calls = self.client(retired(), SimpleNamespace(text="OK"), SimpleNamespace(text="OK"))
        llm.generate(client, "primary", "hi")
        llm.generate(client, "primary", "hi")
        self.assertEqual(calls, ["primary", "backup", "backup"])

    def test_bad_request_is_not_hidden(self):
        bad = APIError(400, {"error": {"code": 400, "message": "Invalid argument", "status": "INVALID_ARGUMENT"}})
        client, calls = self.client(bad)
        with self.assertRaises(APIError):
            llm.generate(client, "primary", "hi")
        self.assertEqual(calls, ["primary"])

    def test_all_busy_raises_last_error(self):
        client, calls = self.client(busy(), busy(), busy())
        with self.assertRaises(APIError):
            llm.generate(client, "primary", "hi")
        self.assertEqual(calls, ["primary", "backup", "last"])

    def test_timeout_is_set_on_each_request(self):
        seen = []
        client = SimpleNamespace(models=SimpleNamespace(
            generate_content=lambda model, contents, config: seen.append(config.http_options.timeout) or "ok"))
        llm.generate(client, "primary", "hi", timeout=12)
        self.assertEqual(seen, [12_000])


class AsyncFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_hanging_model_is_abandoned_for_the_next(self):
        llm._unhealthy.clear()

        async def generate_content(model, contents, config):
            if model == "primary":
                await asyncio.sleep(5)   # like the 60-90 s hang in the log
            return SimpleNamespace(text="OK")
        aio = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
        with patch.object(llm, "chain", return_value=["primary", "backup"]):
            response, used = await llm.agenerate(aio, "primary", "hi", timeout=0.2)
        self.assertEqual((response.text, used), ("OK", "backup"))
        llm._unhealthy.clear()

    def test_default_chains_skip_retired_25_models(self):
        for tier in llm._TIERS.values():
            self.assertFalse(any(m.startswith("gemini-2.5") for m in tier))


if __name__ == "__main__":
    unittest.main()
