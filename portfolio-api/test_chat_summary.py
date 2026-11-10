"""Rolling summary of chat turns that fell out of the model's window."""
import glob, os, re, sqlite3, sys, tempfile, unittest
from types import SimpleNamespace
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="chat-summary-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod  # noqa: E402

APP = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
OK = SimpleNamespace(ok=True, content="SUMMARY: user asked about a 401k split; assumed 24% bracket; open item: new plan details.", error="")


def make_session(n_pairs):
    sid = appmod._chat_create_session("fiduciary", "t")
    for i in range(n_pairs):
        appmod._chat_add_message(sid, "user", f"question {i} with number {i * 111}")
        appmod._chat_add_message(sid, "assistant", f"answer {i}")
    return sid


class TestSummary(unittest.TestCase):
    def test_summarises_turns_outside_the_window_and_is_incremental(self):
        sid = make_session(15)                                   # 30 turns; window 24 -> 6 old
        with mock.patch.object(appmod.llm, "_llm_chat", return_value=OK) as m:
            appmod._chat_summarise_old(sid)
            self.assertEqual(m.call_count, 1)
            prompt = m.call_args[0][0][0]["content"]
            self.assertIn("question 0 with number 0", prompt)
            self.assertNotIn("question 14", prompt)              # recent turns are not summarised, they are still in the window
            text, upto = appmod._chat_summary_get(sid)
            self.assertIn("SUMMARY", text); self.assertGreater(upto, 0)
            appmod._chat_summarise_old(sid)                      # nothing new has scrolled out
            self.assertEqual(m.call_count, 1)
            appmod._chat_add_message(sid, "user", "one more"); appmod._chat_add_message(sid, "assistant", "ok")
            appmod._chat_summarise_old(sid)
            self.assertEqual(m.call_count, 2)
            self.assertIn("SUMMARY", m.call_args[0][0][0]["content"])   # previous summary is merged in

    def test_not_enough_old_turns_no_call(self):
        sid = make_session(13)                                   # 26 turns -> only 2 old (< 4)
        with mock.patch.object(appmod.llm, "_llm_chat", return_value=OK) as m:
            appmod._chat_summarise_old(sid)
        self.assertEqual(m.call_count, 0); self.assertEqual(appmod._chat_summary_get(sid), ("", 0))

    def test_failure_keeps_old_summary_and_never_raises(self):
        sid = make_session(15)
        with mock.patch.object(appmod.llm, "_llm_chat", return_value=OK):
            appmod._chat_summarise_old(sid)
        before = appmod._chat_summary_get(sid)
        appmod._chat_add_message(sid, "user", "x"); appmod._chat_add_message(sid, "assistant", "y")
        with mock.patch.object(appmod.llm, "_llm_chat", return_value=SimpleNamespace(ok=False, content="", error="down")):
            appmod._chat_summarise_old(sid)
        self.assertEqual(appmod._chat_summary_get(sid), before)
        with mock.patch.object(appmod.llm, "_llm_chat", side_effect=RuntimeError("boom")):
            appmod._chat_add_message(sid, "user", "x2"); appmod._chat_add_message(sid, "assistant", "y2")
            appmod._chat_summarise_old(sid)                      # must not raise
        self.assertEqual(appmod._chat_summary_get(sid), before)

    def test_summary_is_encrypted_at_rest(self):
        sid = make_session(15)
        with mock.patch.object(appmod.llm, "_llm_chat", return_value=OK):
            appmod._chat_summarise_old(sid)
        raw = sqlite3.connect(appmod.CHAT_DB).execute("SELECT summary FROM sessions WHERE id=?", (sid,)).fetchone()[0]
        self.assertNotIn("401k", raw)

    def test_wired_into_the_reply_path(self):
        self.assertIn("Summary of the earlier part of this conversation", APP)
        self.assertRegex(APP, r'_chat_add_message\(session_id, "assistant", final_content(, tool_json=json\.dumps\(stamp\))?\)\n\s+threading\.Thread\(target=_chat_summarise_old')


if __name__ == "__main__":
    unittest.main()
