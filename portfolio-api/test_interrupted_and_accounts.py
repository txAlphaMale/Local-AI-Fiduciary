"""D2 (restart leaves visible traces), D4 (long-job estimate before REGENERATE), D5 (accounts are not holdings)."""
import glob, json, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="d25-test-")
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


class TestInterruptedJobs(unittest.TestCase):
    def setUp(self):
        self.inflight = os.path.join(_TMP, "inflight.json")
        self.hist = os.path.join(_TMP, "hist.json")
        appmod.store.write_json(self.inflight, {})
        appmod.store.write_json(self.hist, {})
        self.patches = [mock.patch.object(appmod, "JOB_INFLIGHT_FILE", self.inflight), mock.patch.object(appmod, "JOB_HISTORY_FILE", self.hist)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        for k in [k for k in appmod._JOBS if k.endswith("-x") or k.startswith("zz")]:
            appmod._JOBS.pop(k, None)
            if k in appmod._JOBS_ORDER:
                appmod._JOBS_ORDER.remove(k)

    def test_inflight_file_mirrors_queued_and_running_jobs_only(self):
        appmod._JOBS["zz-1"] = {"id": "zz-1", "kind": "narrative", "label": "x", "status": "running", "source": "manual", "submitted": 1, "started": 2}
        appmod._JOBS["zz-2"] = {"id": "zz-2", "kind": "analysis", "label": "y", "status": "done", "source": "cron", "submitted": 1, "started": 2}
        appmod._inflight_write()
        self.assertEqual(list(appmod.store.read_json_strict(self.inflight)), ["zz-1"])

    def test_leftover_jobs_become_interrupted_visible_and_are_recorded_and_cleared(self):
        appmod.store.write_json(self.inflight, {"narr-7": {"kind": "narrative", "label": "State-of-the-Wealth brief", "status": "running", "source": "manual",
                              "submitted": 100, "started": 110},
                   "anal-3": {"kind": "analysis", "status": "queued", "source": "cron", "submitted": 120, "started": None},
                   "bogus-1": {"kind": "not_a_kind", "status": "queued"}})
        rec = appmod._jobs_recover_interrupted()
        self.assertEqual(sorted(r["kind"] for r in rec), ["analysis", "narrative"])
        self.assertTrue(all(r["status"] == "interrupted" for r in rec))
        n = [r for r in rec if r["kind"] == "narrative"][0]
        self.assertIn("while this was running", n["error"])
        self.assertIn("waiting in the queue", [r for r in rec if r["kind"] == "analysis"][0]["error"])
        self.assertIn(n["id"], appmod._JOBS)                                         # visible in /api/jobs
        h = appmod._read_job_history()
        self.assertEqual(h["narrative"][-1]["status"], "interrupted")
        self.assertEqual(appmod.store.read_json_strict(self.inflight), {})                         # cleared: recovery happens once
        self.assertEqual(appmod._jobs_recover_interrupted(), [])
        self.assertEqual(appmod._history_summary(h["narrative"])["typical_seconds"], None)   # an interrupted run never counts as a typical duration

    def test_hooks_are_wired(self):
        self.assertIn("_jobs_recover_interrupted()\nthreading.Thread(target=_job_worker", APP)
        self.assertEqual(APP.count("_inflight_write()"), 5)                          # def + enqueue + start + finish + cancel


class TestInterruptedChat(unittest.TestCase):
    def test_a_trailing_user_message_gets_a_visible_note_once(self):
        sid = appmod._chat_create_session("fiduciary", "t")
        appmod._chat_add_message(sid, "user", "will this be answered?")
        sid2 = appmod._chat_create_session("fiduciary", "ok")
        appmod._chat_add_message(sid2, "user", "q")
        appmod._chat_add_message(sid2, "assistant", "a")
        try:
            self.assertGreaterEqual(appmod._chat_mark_interrupted(), 1)
            msgs = appmod._chat_messages(sid)
            self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
            self.assertIn("No answer was saved", msgs[-1]["content"])
            self.assertEqual(json.loads(msgs[-1]["tool_json"]), {"interrupted": True})
            self.assertEqual(len(appmod._chat_messages(sid2)), 2)                    # answered sessions are untouched
            self.assertEqual(appmod._chat_mark_interrupted(), 0)                      # idempotent
        finally:
            appmod._chat_delete_session(sid); appmod._chat_delete_session(sid2)

    def test_runs_at_startup(self):
        self.assertIn("_chat_init()\n_chat_mark_interrupted()", APP)


class TestLongJobEstimate(unittest.TestCase):
    def test_jobs_endpoint_carries_typical_seconds_for_gpu_jobs(self):
        hist = {"narrative": [{"started": 10, "finished": 1810, "status": "done"}, {"started": 10, "finished": 2010, "status": "done"}]}
        with mock.patch.object(appmod, "_read_job_history", return_value=hist):
            r = appmod.get_jobs()
        self.assertEqual(r["typical_by_kind"]["narrative"], 1900)
        self.assertNotIn("knowledge_reindex_zz", r["typical_by_kind"])

    def test_regenerate_asks_first_when_the_job_is_long(self):
        src = HTML.split("gb.onclick=async()=>{")[1].split("nbox.appendChild(gb);")[0]
        for needle in ("typical_by_kind", "t>=300", "confirm(", "chat messages sent meanwhile wait in a queue"):
            self.assertIn(needle, src, needle)


class TestAccountsTool(unittest.TestCase):
    DATA = {"accounts": [
        {"name": "WF IRA - XX6140", "institution": "Wells Fargo", "type": "Investments", "subtype": "IRA", "balance": 236000.0, "holdings_count": 5, "is_asset": True, "include_nw": True},
        {"name": "Voya HSA Cash", "institution": "Voya", "type": "Investments", "subtype": "HSA", "balance": 3400.0, "holdings_count": 0, "is_asset": True, "include_nw": True},
        {"name": "Ally Savings (...1234)", "institution": "Ally", "type": "Depository", "subtype": "Savings", "balance": 20000.0, "holdings_count": 0, "is_asset": True, "include_nw": True},
        {"name": "Mortgage", "institution": "Bank", "type": "Loan", "subtype": "", "balance": -90000.0, "holdings_count": 0, "is_asset": False, "include_nw": True}],
        "consolidated": [{"ticker": "VTI", "name": "Total", "value": 10.0}] * 28}

    def test_counts_are_explicit_and_account_numbers_are_stripped(self):
        with mock.patch.object(appmod, "_data", return_value=self.DATA):
            r = appmod._fiduciary_dispatch("get_accounts", {})
        c = r["counts"]
        self.assertEqual((c["accounts_total"], c["asset_accounts"], c["liability_accounts"], c["investment_accounts"],
                          c["accounts_with_holdings_listed"]), (4, 3, 1, 2, 1))
        names = [a["name"] for a in r["accounts"]]
        self.assertIn("WF IRA", names)
        self.assertIn("Ally Savings", names)
        self.assertFalse(any("6140" in n or "1234" in n for n in names))
        self.assertIn("ACCOUNTS", r["note"])

    def test_holdings_tool_says_it_is_not_an_account_count(self):
        with mock.patch.object(appmod, "_data", return_value=dict(self.DATA, net_worth=1)):
            r = appmod._fiduciary_dispatch("get_holdings", {})
        self.assertIn("NOT accounts", r["holdings_count_note"])
        self.assertIn("get_accounts", r["holdings_count_note"])

    def test_tool_is_registered_and_the_prompt_explains_the_difference(self):
        self.assertIn("get_accounts", [t["function"]["name"] for t in appmod.FIDUCIARY_TOOLS])
        self.assertIn("never answer an account question by counting holdings", appmod.FIDUCIARY_PROMPT.replace("\n", " "))


if __name__ == "__main__":
    unittest.main()
