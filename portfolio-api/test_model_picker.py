"""Model choices are made in SETTINGS from the models Ollama has, and apply at once (no .env edit, no restart)."""
import asyncio
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as appmod  # noqa: E402

ROWS = [{"name": "qwen3.6-27b-gpu", "size_gb": 17.0, "parameters": "27.8B", "quantization": "Q4_K_M", "capabilities": ["completion", "vision", "tools", "thinking"]},
        {"name": "qwen2.5vl:7b", "size_gb": 6.0, "parameters": "8.3B", "quantization": "Q4_K_M", "capabilities": ["completion", "vision"]},
        {"name": "bge-m3:latest", "size_gb": 1.2, "parameters": "567M", "quantization": "F16", "capabilities": ["embedding"]},
        {"name": "qwen3.5:9b", "size_gb": 6.6, "parameters": "9B", "quantization": "Q4_K_M", "capabilities": ["completion", "tools", "thinking"]}]


class Live(unittest.TestCase):
    def test_saved_choice_beats_the_compose_default_and_applies_without_restart(self):
        with mock.patch.dict(os.environ, {"ANALYSIS_MODEL": "qwen3.6:27b"}), mock.patch.object(appmod, "_read_settings", lambda: {"ANALYSIS_MODEL": "qwen3.6-27b-gpu"}):
            self.assertEqual(appmod._analysis_model(), "qwen3.6-27b-gpu")
            self.assertEqual(appmod._text_model(), "qwen3.6-27b-gpu")

    def test_nothing_saved_falls_back_to_env_then_default(self):
        with mock.patch.object(appmod, "_read_settings", lambda: {}):
            with mock.patch.dict(os.environ, {"ANALYSIS_MODEL": "from-env"}):
                self.assertEqual(appmod._analysis_model(), "from-env")
            with mock.patch.dict(os.environ, {"ANALYSIS_MODEL": "", "VISION_MODEL": ""}):
                self.assertEqual(appmod._vision_model(), "qwen2.5vl:7b")

    def test_context_size_is_one_live_knob_with_a_floor(self):
        self.assertEqual(appmod.SETTING_SPECS["llm_num_ctx"]["default"], 32768)
        with mock.patch.object(appmod, "_knob", lambda k: 100):
            self.assertEqual(appmod._num_ctx(), 2048)
        with mock.patch.object(appmod, "_knob", lambda k: "garbage"):
            self.assertEqual(appmod._num_ctx(), 32768)


class Options(unittest.TestCase):
    def test_chat_model_choices_need_tool_use_and_picture_choices_need_vision(self):
        with mock.patch.object(appmod, "_ollama_models", lambda force=False: ROWS):
            self.assertEqual(appmod._model_options_for("ANALYSIS_MODEL"), ["qwen3.6-27b-gpu", "qwen3.5:9b"])          # no embedding or vision-only model
            self.assertEqual(appmod._model_options_for("VISION_MODEL"), ["qwen3.6-27b-gpu", "qwen2.5vl:7b"])

    def test_unreachable_ollama_means_a_text_box(self):
        with mock.patch.object(appmod, "_ollama_models", lambda force=False: None):
            self.assertIsNone(appmod._model_options_for("ANALYSIS_MODEL"))

    def test_settings_shows_the_dropdown_with_the_current_value_always_present(self):
        with mock.patch.object(appmod, "_ollama_models", lambda force=False: ROWS), \
                mock.patch.object(appmod, "_read_settings", lambda: {"ANALYSIS_MODEL": "gone-model"}):
            cfg = {c["key"]: c for c in appmod.get_settings()["config"]}["ANALYSIS_MODEL"]
        self.assertEqual((cfg["applies"], cfg["env_locked"], cfg["value"]), ("immediately", False, "gone-model"))
        self.assertEqual(cfg["options"][0], "gone-model")
        self.assertIn("qwen3.6-27b-gpu", cfg["options"])

    def test_models_listing_reads_ollama_and_marks_capabilities(self):
        class R:
            def __init__(self, j):
                self._j = j

            def json(self):
                return self._j
        def get(url, **k):
            return R({"models": [{"name": "m1", "size": 17e9}]})

        def post(url, json=None, **k):
            return R({"capabilities": ["completion", "tools"], "details": {"parameter_size": "27B", "quantization_level": "Q4_K_M"}})
        appmod._OLLAMA_MODELS_CACHE.update(at=0.0, rows=None)
        with mock.patch.object(appmod.llm, "_dialect", lambda: "ollama"), mock.patch.object(appmod.requests, "get", get), mock.patch.object(appmod.requests, "post", post):
            rows = appmod._ollama_models(force=True)
        self.assertEqual(rows, [{"name": "m1", "size_gb": 17.0, "parameters": "27B", "quantization": "Q4_K_M", "capabilities": ["completion", "tools"]}])
        appmod._OLLAMA_MODELS_CACHE.update(at=0.0, rows=None)

    def test_other_providers_have_no_ollama_list(self):
        with mock.patch.object(appmod.llm, "_dialect", lambda: "anthropic"):
            self.assertIsNone(appmod._ollama_models(force=True))


class Save(unittest.TestCase):
    def post(self, body, store_box):
        class Req:
            headers = {}
            client = None

            async def json(self_inner):
                return body
        with mock.patch.object(appmod, "_ollama_models", lambda force=False: ROWS), mock.patch.object(appmod, "_read_settings", lambda: dict(store_box)), \
                mock.patch.object(appmod.store, "write_json", lambda path, data: store_box.update(data)), mock.patch.object(appmod, "_emit_config_changed", lambda *a, **k: None):
            return asyncio.run(appmod.post_settings(Req()))

    def test_saving_a_model_applies_live_and_needs_no_restart(self):
        box = {}
        r = self.post({"ANALYSIS_MODEL": "qwen3.6-27b-gpu"}, box)
        self.assertEqual(box["ANALYSIS_MODEL"], "qwen3.6-27b-gpu")
        self.assertIn("ANALYSIS_MODEL", r["applied"])
        self.assertEqual(r["restart_required"], [])

    def test_a_model_ollama_does_not_have_is_refused(self):
        box = {}
        r = self.post({"ANALYSIS_MODEL": "typo-model"}, box)
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("ANALYSIS_MODEL", box)


if __name__ == "__main__":
    unittest.main()
