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
