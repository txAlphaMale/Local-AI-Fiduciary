import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import llm  # noqa: E402

try:
    import app as appmod
except Exception:
    appmod = None


class FakeResp:
    status_code = 200
    text = ""

    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_lines(self):
        return iter(self._lines)

    def close(self):
        pass


class OllamaTiming(unittest.TestCase):
    def test_converts_nanoseconds_and_drops_missing_fields(self):
        t = llm.ollama_timing({"load_duration": 12_340_000_000, "prompt_eval_duration": 2_500_000_000, "eval_duration": 90_000_000_000,
                               "total_duration": 105_000_000_000, "prompt_eval_count": 8000, "eval_count": 900})
        self.assertEqual(t, {"load_s": 12.34, "prompt_s": 2.5, "eval_s": 90.0, "total_s": 105.0, "prompt_tokens": 8000, "eval_tokens": 900})
        self.assertEqual(llm.ollama_timing({"done": True}), {})

    def test_stream_puts_timing_on_the_final_chunk_only(self):
        lines = [json.dumps({"message": {"thinking": "hm"}, "done": False}).encode(),
                 json.dumps({"message": {"content": "hi"}, "done": False}).encode(),
                 json.dumps({"message": {}, "done": True, "load_duration": 3_000_000_000, "eval_count": 7}).encode()]
        with mock.patch.object(llm.requests, "post", return_value=FakeResp(lines)):
            out = list(llm._ollama_chat_stream([{"role": "user", "content": "x"}], "m", None, 0.1, 1000, None, 5, None))
        self.assertEqual([("timing" in c) for c in out], [False, False, True])
        self.assertEqual(out[-1]["timing"], {"load_s": 3.0, "eval_tokens": 7})


@unittest.skipIf(appmod is None, "app not importable")
class TurnTiming(unittest.TestCase):
    def test_splits_thinking_and_answer_seconds_and_sums_calls(self):
        tm = appmod._new_chat_timing()
        appmod._add_call_timing(tm, {"t0": 100.0, "think0": 105.0, "answer0": 165.0,
                                     "ollama": {"load_s": 20.0, "prompt_s": 4.0, "prompt_tokens": 9000, "eval_tokens": 500}}, 175.0)
        appmod._add_call_timing(tm, {"t0": 200.0, "think0": None, "answer0": 230.0, "ollama": {"load_s": 0.1, "prompt_s": 6.0}}, 240.0)
        tm["tools_s"] = 3.04
        out = appmod._finish_chat_timing(tm)
        self.assertEqual((out["calls"], out["think_s"], out["answer_s"], out["load_s"], out["prompt_s"], out["tools_s"]),
                         (2, 60.0, 20.0, 20.1, 10.0, 3.0))
        self.assertEqual((out["prompt_tokens"], out["wall_model_s"]), (9000, 115.0))

    def test_no_ollama_numbers_means_the_fields_are_left_out_not_zero(self):
        tm = appmod._new_chat_timing()
        appmod._add_call_timing(tm, {"t0": 0.0, "think0": None, "answer0": 1.0, "ollama": {}}, 5.0)
        out = appmod._finish_chat_timing(tm)
        self.assertNotIn("load_s", out)
        self.assertEqual(out["answer_s"], 4.0)

    def test_recent_timings_are_exposed_on_api_gpu(self):
        appmod._RECENT_CHAT_TIMING.clear()
        appmod._remember_chat_timing({"elapsed_s": 50, "queued_s": 2, "timing": {"calls": 1, "think_s": 30.0}})
        r = appmod.get_gpu()
        self.assertEqual(r["recent_chat_timing"][0]["think_s"], 30.0)
        self.assertEqual(r["recent_chat_timing"][0]["elapsed_s"], 50)


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(appmod is None, "app not importable")
class ThinkingKnob(unittest.TestCase):
    """Mode 0 (default) never sends think; mode 1 turns reasoning off only for the call after the app printed a ticker's facts."""

    def run_chat(self, mode):
        seen = []
        research = {"found": True, "ticker": "QXO", "screen": {"name": "QXO Inc", "price": 12.6}, "scout_note": None}

        def fake(messages, model, **k):
            seen.append(k.get("think", "absent"))
            if len(seen) == 1:
                yield {"content": None, "thinking": "hm", "done": True, "error": None, "cancelled": False,
                       "tool_calls": [{"function": {"name": "get_idea_research", "arguments": {"ticker": "QXO"}}}]}
            else:
                yield {"content": "Short commentary.", "thinking": None, "tool_calls": None, "done": True, "error": None,
                       "cancelled": False}
        with mock.patch.object(appmod.llm, "_llm_chat", side_effect=fake), mock.patch.object(appmod, "_chat_summarise_old"), \
                mock.patch.object(appmod, "_idea_research", return_value=research), \
                mock.patch.object(appmod, "_chat_thinking_mode", return_value=mode):
            out = []
            for raw in appmod._chat_stream("fiduciary", None, "Tell me about QXO", req_id="th" + str(mode)):
                out.append(raw)
        return seen, "".join(out)

    def test_default_mode_never_sends_think(self):
        seen, _ = self.run_chat(0)
        self.assertEqual(seen, [None, None])

    def test_mode_one_turns_reasoning_off_only_after_the_facts_are_printed(self):
        seen, out = self.run_chat(1)
        self.assertEqual(seen, [None, False])
        self.assertIn("no_think_calls", out)

    def test_knob_is_registered_with_a_safe_default(self):
        spec = appmod.SETTING_SPECS["chat_thinking_mode"]
        self.assertEqual((spec["default"], spec["cast"]), (0, int))
        with mock.patch.dict(os.environ, {"CHAT_THINKING_MODE": "banana"}):
            self.assertEqual(appmod._chat_thinking_mode(), 0)


class StreamThinkField(unittest.TestCase):
    def test_think_is_sent_only_when_asked_and_retried_without_if_rejected(self):
        class R(FakeResp):
            def __init__(self, status, text, lines=()):
                super().__init__(list(lines)); self.status_code = status; self.text = text
        bodies = []

        def post(url, json=None, **k):
            bodies.append(dict(json))
            if "think" in json:
                return R(400, "model does not support think")
            return R(200, "", [b'{"message": {"content": "ok"}, "done": true}'])
        with mock.patch.object(llm.requests, "post", side_effect=post):
            out = list(llm._ollama_chat_stream([{"role": "user", "content": "x"}], "m", None, 0.1, 1000, None, 5, None, think=False))
        self.assertEqual(["think" in b for b in bodies], [True, False])
        self.assertEqual(out[-1]["content"], "ok")
        bodies.clear()
        with mock.patch.object(llm.requests, "post", side_effect=post):
            list(llm._ollama_chat_stream([{"role": "user", "content": "x"}], "m", None, 0.1, 1000, None, 5, None))
        self.assertEqual(["think" in b for b in bodies], [False])
