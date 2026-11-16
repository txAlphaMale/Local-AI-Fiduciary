"""Fallback model (retry only on a load failure), short setting names with help text, and 'customized' only for real changes."""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import llm  # noqa: E402
import app as appmod  # noqa: E402

OOM = "ollama 500: CUDA error: the resource allocation failed"


def ok_stream(*a, **k):
    yield {"content": "hi", "thinking": None, "tool_calls": None, "done": True, "error": None, "cancelled": False}


class Fallback(unittest.TestCase):
    def setUp(self):
        self.p = mock.patch.object(llm, "FALLBACK_PROVIDER", lambda: ("small-16k", 16384))
        self.p.start()
        self.d = mock.patch.object(llm, "_dialect", lambda: "ollama")
        self.d.start()

    def tearDown(self):
        self.p.stop()
        self.d.stop()

    def test_non_stream_retries_once_on_a_load_failure_and_says_so(self):
        calls = []

        def once(messages, model, **k):
            calls.append((model, k.get("num_ctx")))
            return llm.LLMResult(False, 500, error=OOM) if model == "big" else llm.LLMResult(True, 200, content="ok")
        with mock.patch.object(llm, "_llm_chat_once", once):
            r = llm._llm_chat([{"role": "user", "content": "x"}], "big", num_ctx=32768)
        self.assertEqual((r.ok, r.content, r.fallback_model), (True, "ok", "small-16k"))
        self.assertEqual(calls, [("big", 32768), ("small-16k", 16384)])

    def test_a_timeout_or_bad_request_is_not_retried(self):
        calls = []

        def once(messages, model, **k):
            calls.append(model)
            return llm.LLMResult(False, 400, error="ollama 400: bad request")
        with mock.patch.object(llm, "_llm_chat_once", once):
            r = llm._llm_chat([], "big")
        self.assertFalse(r.ok)
        self.assertEqual(calls, ["big"])

    def test_stream_falls_back_only_if_the_first_chunk_is_a_load_error(self):
        def once(messages, model, **k):
            if model == "big":
                yield {"content": None, "thinking": None, "tool_calls": None, "done": True, "error": OOM, "cancelled": False}
            else:
                yield from ok_stream()
        with mock.patch.object(llm, "_llm_chat_once", once):
            out = list(llm._llm_chat([], "big", stream=True))
        self.assertEqual((out[0]["content"], out[0]["fallback"]), ("hi", "small-16k"))

    def test_stream_error_after_output_began_is_passed_through(self):
        def once(messages, model, **k):
            yield {"content": "partial", "thinking": None, "tool_calls": None, "done": False, "error": None, "cancelled": False}
            yield {"content": None, "thinking": None, "tool_calls": None, "done": True, "error": OOM, "cancelled": False}
        with mock.patch.object(llm, "_llm_chat_once", once):
            out = list(llm._llm_chat([], "big", stream=True))
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1]["error"], OOM)

    def test_no_fallback_for_picture_reading_or_when_it_equals_the_main_model(self):
        self.assertIsNone(llm._fallback_for("big", images=["abc"]))
        self.assertIsNone(llm._fallback_for("small-16k", images=None))

    def test_app_provider_uses_the_saved_model_and_context(self):
        with mock.patch.object(appmod, "_read_settings", lambda: {"FALLBACK_MODEL": "qwen3.6-27b-gpu-16k:latest"}):
            self.assertEqual(appmod._fallback_choice(), ("qwen3.6-27b-gpu-16k:latest", 16384))
        with mock.patch.object(appmod, "_read_settings", lambda: {}), mock.patch.dict(os.environ, {"FALLBACK_MODEL": ""}):
            self.assertIsNone(appmod._fallback_choice())


ROWS = [{"name": "a:latest", "size_gb": 1, "parameters": "", "quantization": "", "capabilities": ["completion", "tools"]}]


class Wiring(unittest.TestCase):
    def test_app_registers_its_provider_with_llm(self):
        self.assertIs(llm.FALLBACK_PROVIDER, appmod._fallback_choice)


class Settings(unittest.TestCase):
    def test_labels_are_short_and_the_long_text_is_in_help(self):
        with mock.patch.object(appmod, "_ollama_models", lambda force=False: ROWS), mock.patch.object(appmod, "_read_settings", lambda: {}):
            d = appmod.get_settings()
        knobs = {k["key"]: k for k in d["settings"]}
        cfg = {k["key"]: k for k in d["config"]}
        for k in ("chat_thinking_mode", "llm_num_ctx", "insider_max_filings"):
            self.assertLessEqual(len(knobs[k]["label"]), 52, k)
            self.assertGreater(len(knobs[k]["help"]), 80, k)
        self.assertLessEqual(len(cfg["ANALYSIS_MODEL"]["label"]), 30)
        self.assertTrue(cfg["FALLBACK_MODEL"]["allow_blank"])
        self.assertEqual(cfg["FALLBACK_MODEL"]["applies"], "immediately")
        self.assertEqual(cfg["FALLBACK_MODEL"]["value"], "")

    def post(self, body, box):
        class Req:
            headers = {}
            client = None

            async def json(self_inner):
                return body
        with mock.patch.object(appmod, "_ollama_models", lambda force=False: ROWS), mock.patch.object(appmod, "_read_settings", lambda: dict(box)), \
                mock.patch.object(appmod.store, "write_json", lambda path, data: (box.clear(), box.update(data))), \
                mock.patch.object(appmod, "_emit_config_changed", lambda *a, **k: None):
            return asyncio.run(appmod.post_settings(Req()))

    def test_a_value_equal_to_the_default_is_not_stored_as_customized(self):
        box = {"insider_max_filings": 20}
        self.post({"insider_max_filings": "80"}, box)
        self.assertNotIn("insider_max_filings", box)
        self.post({"insider_max_filings": "50"}, box)
        self.assertEqual(box["insider_max_filings"], 50)

    def test_fallback_can_be_chosen_validated_and_cleared(self):
        box = {}
        self.post({"FALLBACK_MODEL": "a:latest"}, box)
        self.assertEqual(box["FALLBACK_MODEL"], "a:latest")
        self.post({"FALLBACK_MODEL": ""}, box)
        self.assertNotIn("FALLBACK_MODEL", box)
        self.assertEqual(self.post({"FALLBACK_MODEL": "nope"}, {}).status_code, 400)

    def test_page_sends_only_changed_values_and_shows_a_help_icon(self):
        html = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html"), encoding="utf-8").read()
        self.assertIn("i.value===i.dataset.orig", html)
        self.assertIn("function settingLabel(lab, it)", html)
        self.assertIn("q.title=it.help", html)


if __name__ == "__main__":
    unittest.main()
