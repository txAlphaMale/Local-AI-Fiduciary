# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_job_history.py -- TASK SCHEDULE shows last/typical duration per job and warns about expected overlaps (tester 2026-10-21)."""
import datetime, glob, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="jobhist-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod  # noqa: E402

HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()


class FakeStore:
    def __init__(self): self.d = {}
    def read(self, p): return self.d.get(p)
    def write(self, p, v): self.d[p] = v


class TestHistory(unittest.TestCase):
    def setUp(self):
        self.fs = FakeStore()
        self.patches = [mock.patch.object(appmod.store, "read_json_strict", self.fs.read),
                        mock.patch.object(appmod.store, "write_json", self.fs.write)]
        for p in self.patches: p.start()
    def tearDown(self):
        for p in self.patches: p.stop()

    def test_runs_are_recorded_trimmed_and_summarised(self):
        for i in range(12):
            appmod._record_job_run("analysis", 1000 + i * 100, 1000 + i * 100 + 60 + i, "done", "scheduler")
        appmod._record_job_run("analysis", 5000, 5030, "error", "manual")
        h = appmod._read_job_history()
        self.assertEqual(len(h["analysis"]), appmod._JOB_HISTORY_KEEP)
        s = appmod._history_summary(h["analysis"])
        self.assertEqual((s["last"]["seconds"], s["last"]["status"], s["last"]["source"]), (30, "error", "manual"))
        # typical = median of the last 5 DONE runs (durations 66..71 for i=7..11 -> 67,68,69,70,71 -> 69), the failed run excluded
        self.assertEqual(s["typical_seconds"], 69)

    def test_no_history_means_no_numbers_and_bad_records_are_ignored(self):
        self.assertEqual(appmod._history_summary(None), {"last": None, "typical_seconds": None, "runs": 0})
        self.assertEqual(appmod._history_summary([{"started": None, "finished": 5}, "junk"])["runs"], 0)
        appmod._record_job_run("x", None, 5, "done", "manual")                 # a job that never started is not recorded
        self.assertEqual(appmod._read_job_history(), {})

    def test_recording_failure_never_breaks_a_job(self):
        with mock.patch.object(appmod.store, "write_json", side_effect=OSError("disk full")):
            appmod._record_job_run("analysis", 1, 9, "done", "manual")          # must not raise

    def test_eta_for_a_running_job_survives_a_restart(self):
        appmod._record_job_run("analysis", 1000, 1600, "done", "scheduler")
        self.assertEqual(appmod._job_typical_seconds("analysis", []), 600)       # no in-memory jobs, persisted history used


class TestConflicts(unittest.TestCase):
    def sched(self, **kw):
        base = lambda h, m, dow="*", dom="*": {"hour": h, "minute": m, "dow": dow, "dom": dom, "label": "x", "cadence": ""}
        return {k: base(*v) if isinstance(v, tuple) else v for k, v in kw.items()}

    def test_overlap_is_flagged_on_the_job_that_runs_long(self):
        s = self.sched(a=(20, 0), b=(20, 30))
        c = appmod._schedule_conflicts(s, {"a": 3600, "b": 60}, days=2, today=datetime.date(2026, 10, 21))
        self.assertEqual([(x["with"], x["due"], x["ends"]) for x in c["a"]], [("b", "20:30", "21:00")])
        self.assertNotIn("b", c)                                                  # b is short: nothing to warn about

    def test_back_to_back_and_no_history_and_other_weekdays_are_not_flagged(self):
        today = datetime.date(2026, 10, 21)                                        # a Wednesday (weekday 2)
        s = self.sched(a=(20, 0), b=(21, 0))
        self.assertEqual(appmod._schedule_conflicts(s, {"a": 3600}, 1, today), {})          # ends exactly when b starts
        self.assertEqual(appmod._schedule_conflicts(s, {}, 14, today), {})                  # no history -> no warning
        s = self.sched(a=(20, 0, "*"), b=(20, 30, "4"))                                      # b only on Fridays
        self.assertEqual(appmod._schedule_conflicts(s, {"a": 3600}, 1, today), {})          # today is Wednesday
        c = appmod._schedule_conflicts(s, {"a": 3600}, 7, today)                             # but a Friday falls inside the window
        self.assertEqual(c["a"][0]["day"], "Fri")

    def test_a_pair_is_reported_once_even_if_it_recurs_daily(self):
        s = self.sched(a=(20, 0), b=(20, 30))
        c = appmod._schedule_conflicts(s, {"a": 3600}, 14, datetime.date(2026, 10, 21))
        self.assertEqual(len(c["a"]), 1)


class TestApiAndUi(unittest.TestCase):
    def test_every_scheduled_job_is_listed_and_the_payload_carries_the_new_fields(self):
        self.assertEqual(set(appmod._SCHEDULE_ORDER), set(appmod._SCHEDULE_DEFAULTS))       # backtest/narrative were scheduled but hidden
        with mock.patch.object(appmod, "_read_job_history", lambda: {"analysis": [{"started": 100, "finished": 460, "status": "done", "source": "scheduler"}]}):
            jobs = {j["kind"]: j for j in appmod.get_schedule()["jobs"]}
        self.assertEqual(jobs["analysis"]["last_run"]["seconds"], 360); self.assertEqual(jobs["analysis"]["typical_seconds"], 360)
        self.assertIsNone(jobs["discovery"]["last_run"]); self.assertEqual(jobs["discovery"]["conflicts"], [])

    def test_ui_shows_last_run_typical_and_overlap_warning(self):
        for needle in ("LAST RUN", "TYPICAL", "no history yet", "typically runs until", "that job will wait in the queue", "j.last_run", "j.conflicts"):
            self.assertIn(needle, HTML)

    def test_every_listed_job_has_a_run_now_endpoint_and_manual_backtest_goes_through_the_queue(self):
        eps = re.search(r"const SCHEDULE_RUN_ENDPOINTS = \{(.*?)\};", HTML, re.S).group(1)
        for k in appmod._SCHEDULE_ORDER:
            self.assertIn(k + ":", eps)
        src = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
        self.assertIn('_enqueue("backtest", source="manual")', src.split("def run_backtest_ep")[1][:400])

    def test_schedule_table_fits_its_frame(self):
        """Tester screenshot 2026-10-22 (Linux): the table ran past the panel because the global nowrap td rule + a one-line warning
        made the JOB column ~720px wide and the three buttons sat in separate columns."""
        body = HTML.split("function scheduleContent()")[1].split("function saveSchedule")[0]
        self.assertIn('class="l sym" style="white-space:normal;min-width:150px;max-width:340px;"', body)      # job name + warnings wrap
        self.assertIn("overflow-x:auto", body)                                                           # last resort: scroll inside the box
        self.assertEqual(body.count("<th"), 6)                                                           # 6 columns (was 7): the buttons share one cell
        self.assertEqual(body.count("A('click','saveSchedule',j.kind)") + body.count("A('click','runScheduleNow',j.kind)"), 2)   # both buttons share one cell (inline handlers are gone: R-23)

    def test_worker_records_each_finished_run(self):
        src = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
        self.assertIn('_record_job_run(rec["kind"], rec.get("started"), rec["finished"], rec["status"], rec.get("source"))',
                      src.split("def _job_worker():")[1].split("threading.Thread(target=_job_worker")[0])



class TestSilentFailures(unittest.TestCase):
    """The backtest / opportunity jobs used to swallow every exception and so were always recorded as 'done'."""
    def test_backtest_failure_propagates(self):
        with mock.patch.object(appmod.backtest_mod, "run", side_effect=RuntimeError("boom")), \
             mock.patch.object(appmod.store, "read_json_strict", lambda p: None):
            with self.assertRaises(RuntimeError):
                appmod._do_backtest()
        self.assertFalse(appmod._backtest_lock.locked())

    def test_opportunities_failure_propagates(self):
        with mock.patch.object(appmod, "_opportunities", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                appmod._do_opportunities()
        self.assertFalse(appmod._opp_lock.locked())

    def test_unsupported_symbols_are_not_errors(self):
        fund = {"ticker": "VTSAX", "available": False}
        with mock.patch.object(appmod, "_screen_one", return_value=fund), \
             mock.patch.object(appmod, "_read_scanner", return_value={"watchlist": ["VTSAX"], "criteria": {}}), \
             mock.patch.object(appmod, "_data", return_value={"consolidated": []}):
            res = appmod._opportunities()
        self.assertEqual((res["unsupported_count"], res["error_count"]), (1, 0))
        self.assertEqual(res["unavailable_watchlist"][0]["status"], "unsupported")
        with mock.patch.object(appmod, "_opportunities", return_value=res), \
             mock.patch.object(appmod.store, "write_json", lambda p, v: None):
            self.assertIsNone(appmod._do_opportunities())          # job still "done"

    def test_all_lookups_raising_fails_the_job(self):
        res = {"screened": 3, "error_count": 3}
        with mock.patch.object(appmod, "_opportunities", return_value=res), \
             mock.patch.object(appmod.store, "write_json") as w:
            with self.assertRaises(RuntimeError):
                appmod._do_opportunities()
            w.assert_not_called()

    def test_lock_held_reports_skipped(self):
        with appmod._backtest_lock:
            self.assertEqual(appmod._do_backtest(), "skipped")
        with appmod._opp_lock:
            self.assertEqual(appmod._do_opportunities(), "skipped")

    def test_every_trigger_goes_through_the_queue(self):
        src = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
        self.assertNotIn("target=_do_opportunities", src)
        self.assertNotIn("target=_do_backtest", src)
        self.assertNotIn("_run_quietly", src)
        for k in ("opportunities", "backtest"):
            self.assertIn(f'_enqueue("{k}", source="manual")', src)
            self.assertIn(f'_enqueue("{k}", source="auto")', src)

    def test_worker_and_ui_know_skipped(self):
        src = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
        self.assertIn('"skipped" if fn() == "skipped" else "done"', src)
        self.assertIn("SKIPPED", HTML)

if __name__ == "__main__":
    unittest.main()
