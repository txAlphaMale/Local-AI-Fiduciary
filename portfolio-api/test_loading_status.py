"""A cold model load takes minutes and used to look like a hang: the chat now says so, with a live timer."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as appmod  # noqa: E402

PS = {"models": [{"name": "qwen3.6-27b-gpu:latest", "model": "qwen3.6-27b-gpu:latest", "context_length": 32768}]}


class R:
    def __init__(self, j):
        self._j = j

    def json(self):
        return self._j


class LoadState(unittest.TestCase):
    def state(self, model, ctx, ps=PS, dialect="ollama", boom=False):
        def get(url, **k):
            if boom:
                raise RuntimeError("down")
            return R(ps)
        with mock.patch.object(appmod.llm, "_dialect", lambda: dialect), mock.patch.object(appmod.requests, "get", get):
            return appmod._model_load_state(model, ctx)

    def test_loaded_cold_reload_and_unknown(self):
        self.assertEqual(self.state("qwen3.6-27b-gpu:latest", 32768), "loaded")
        self.assertEqual(self.state("qwen3.6-27b-gpu", 32768), "loaded")                     # a name without :latest still matches
        self.assertEqual(self.state("qwen3.5:9b", 32768), "cold")
        self.assertEqual(self.state("qwen3.6-27b-gpu:latest", 16384), "reload")             # loaded at another context size
        self.assertEqual(self.state("qwen3.6-27b-gpu:latest", 32768, ps={"models": []}), "cold")
        self.assertIsNone(self.state("m", 1, boom=True))                                      # cannot ask: say nothing
        self.assertIsNone(self.state("m", 1, dialect="anthropic"))

    def test_messages_explain_what_is_happening(self):
        self.assertIn("not stuck", appmod._loading_message("cold", "m", 32768))
        self.assertIn("different context size", appmod._loading_message("reload", "m", 16384))


class ChatLoop(unittest.TestCase):
    def run_chat(self, state):
        def fake(messages, model, **k):
            yield {"content": "ok", "thinking": None, "tool_calls": None, "done": True, "error": None, "cancelled": False}
        out = []
        with mock.patch.object(appmod.llm, "_llm_chat", side_effect=fake), mock.patch.object(appmod, "_chat_summarise_old"), \
                mock.patch.object(appmod, "_model_load_state", lambda m, c: state):
            for raw in appmod._chat_stream("fiduciary", None, "hi", req_id="ls" + str(state)):
                out.append(raw)
        return "".join(out)

    def test_cold_model_sends_a_loading_status_before_any_output(self):
        out = self.run_chat("cold")
        self.assertIn('"state": "loading"', out)
        self.assertLess(out.index('"state": "loading"'), out.index("event: token"))
        self.assertIn('"cold_start": "cold"', out)

    def test_loaded_model_sends_no_loading_status(self):
        out = self.run_chat("loaded")
        self.assertNotIn('"state": "loading"', out)
        self.assertNotIn("cold_start", out)

    def test_page_shows_a_live_loading_timer_and_clears_it_on_first_output(self):
        html = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html"), encoding="utf-8").read()
        self.assertIn("loading the model into GPU memory", html)
        self.assertIn("else if(o.state==='loading'){ asst.phase='loading'", html)
        self.assertEqual(html.count("if(asst.phase==='loading') asst.phase='working';"), 2)       # token and thinking


if __name__ == "__main__":
    unittest.main()
