"""Chat elapsed-time stamps and queue behaviour (D3): the clock that matters starts when the model starts, never while queued; queue wait is unlimited by default."""
import glob, json, os, re, sys, tempfile, threading, time, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="elapsed-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod  # noqa: E402

APP = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()


def events(gen):
    out = []
    for raw in gen:
        ev, data = "message", ""
        for line in raw.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        out.append((ev, json.loads(data) if data else {}))
    return out


def fake_llm(*a, **k):
    yield {"content": "hello", "thinking": None, "tool_calls": None, "done": False, "error": None, "cancelled": False}
    yield {"content": None, "thinking": None, "tool_calls": None, "done": True, "error": None, "cancelled": False}


class TestFormat(unittest.TestCase):
    def test_server_format(self):
        for s, want in ((0, "0s"), (45, "45s"), (60, "1m"), (930, "15m 30s"), (900, "15m"), (3600, "1h"), (3720, "1h 2m")):
            self.assertEqual(appmod._fmt_elapsed(s), want, s)

    def test_client_format_matches(self):
        body = HTML.split("function fmtDur(s){")[1].split("function chatMsgFromServer")[0]
        self.assertIn("m+'m '+x+'s'", body)
        self.assertIn("h+'h '+m+'m'", body)


class TestQueueAndStamp(unittest.TestCase):
    def run_chat(self):
        with mock.patch.object(appmod.llm, "_llm_chat", side_effect=fake_llm), mock.patch.object(appmod, "_chat_summarise_old"):
            return events(appmod._chat_stream("fiduciary", None, "hi", req_id="t1"))

    def test_answer_carries_run_and_queue_seconds_and_they_are_stored(self):
        ev = self.run_chat()
        done = [d for e, d in ev if e == "done"][0]
        self.assertIn("elapsed_s", done)
        self.assertEqual(done["queued_s"], 0)
        msgs = appmod._chat_messages(done["session_id"])
        stamp = json.loads([m for m in msgs if m["role"] == "assistant"][0]["tool_json"])
        self.assertEqual(stamp, {"elapsed_s": done["elapsed_s"], "queued_s": 0})
        appmod._chat_delete_session(done["session_id"])

    def test_queue_wait_is_unlimited_by_default_and_is_not_run_time(self):
        self.assertEqual(appmod.CHAT_QUEUE_TIMEOUT, 0)
        self.assertEqual(appmod._queue_deadline(), float("inf"))
        appmod._strat_lock.acquire()
        threading.Timer(2.2, appmod._strat_lock.release).start()
        with mock.patch.object(appmod, "CHAT_QUEUE_TIMEOUT", 0):
            ev = self.run_chat()
        names = [e for e, _ in ev]
        self.assertIn("status", names)
        self.assertEqual([d["state"] for e, d in ev if e == "status"][0], "waiting")
        done = [d for e, d in ev if e == "done"][0]
        self.assertGreaterEqual(done["queued_s"], 2)               # waited
        self.assertLess(done["elapsed_s"], 2)                      # the wait is not counted as running time
        appmod._chat_delete_session(done["session_id"])

    def test_a_limit_is_opt_in_and_counts_only_queue_time(self):
        appmod._strat_lock.acquire()
        threading.Timer(3.5, appmod._strat_lock.release).start()
        with mock.patch.object(appmod, "CHAT_QUEUE_TIMEOUT", 1):
            ev = self.run_chat()
        self.assertEqual([e for e, _ in ev if e == "error"], ["error"])
        time.sleep(2.6)                                               # let the timer release the lock for the other tests

    def test_working_status_is_sent_when_the_model_starts_after_a_wait(self):
        src = APP.split("def _chat_stream(")[1].split("CHAT_IMAGE_MAX =")[0]
        self.assertEqual(src.count("_mark_run()"), 3)                 # definition + vision + generation
        self.assertNotIn("time.time() + CHAT_QUEUE_TIMEOUT", src)


class TestClient(unittest.TestCase):
    def test_timers_and_stamps_are_wired(self):
        for needle in ("function chatLiveElapsed(asst)", "setInterval(chatTick,1000)", "id='chat-live-elapsed'", "chatStampText(m.stamp)",
                       "asst.stamp={elapsed_s:o.elapsed_s", "o.state==='working'", "map(chatMsgFromServer)"):
            self.assertIn(needle, HTML, needle)
        self.assertIn("the clock starts when the model starts", HTML)
        self.assertNotRegex(HTML.split("function chatLiveElapsed")[1].split("function chatTick")[0], r"\bon[a-z]+\s*=")

    def test_long_run_is_only_a_hint_not_a_cutoff(self):
        body = HTML.split("function chatLiveElapsed")[1].split("function chatTick")[0]
        self.assertIn("Stop cancels, otherwise keep waiting", body)
        self.assertNotIn("abort", body)


if __name__ == "__main__":
    unittest.main()
