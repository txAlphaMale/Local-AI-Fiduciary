# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_job_cancel.py -- STOP for queued and running jobs (Jason 2026-11-13, after starting the scout by accident), and the scout waiting for
real data before it writes a note."""
import datetime, glob, os, re, sys, tempfile, threading, time, types, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="jobcancel-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    with open(_f) as _fh:
        for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', _fh.read()):
            os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod, scout, sleeves, store, ticker_state  # noqa: E402

REQ = types.SimpleNamespace(headers={}, client=None, url=types.SimpleNamespace(path="/api/jobs/x/cancel"), cookies={}, state=types.SimpleNamespace())


def wait_for(cond, secs=5.0):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class TestCancel(unittest.TestCase):
    def setUp(self):
        self.ran = []
        self.hold = threading.Event()

        def blocker():
            self.ran.append("blocker")
            while not appmod._job_cancelled() and not self.hold.is_set():
                time.sleep(0.01)

        def second():
            self.ran.append("second")

        def stuck():
            self.ran.append("stuck")
            self.hold.wait(5)
        appmod._JOB_KINDS["t_block"] = ("Test blocker", False, blocker)
        appmod._JOB_KINDS["t_second"] = ("Test second", False, second)
        appmod._JOB_KINDS["t_stuck"] = ("Test stuck", False, stuck)
        appmod._CANCELLABLE_KINDS.add("t_block")

    def tearDown(self):
        self.hold.set()
        wait_for(lambda: not any(j["status"] in ("queued", "running") for j in appmod._JOBS.values()))
        for k in ("t_block", "t_second", "t_stuck"):
            appmod._JOB_KINDS.pop(k, None)
        appmod._CANCELLABLE_KINDS.discard("t_block")

    def status(self, jid):
        return appmod._JOBS[jid]["status"]

    def test_stopping_a_running_job_ends_it_as_cancelled(self):
        jid, _ = appmod._enqueue("t_block")
        self.assertTrue(wait_for(lambda: self.status(jid) == "running"))
        out = appmod.post_job_cancel(jid, REQ)
        self.assertEqual((out["ok"], out["was"]), (True, "running"))
        self.assertTrue(wait_for(lambda: self.status(jid) == "cancelled"))

    def test_a_queued_job_is_dropped_and_never_starts(self):
        first, _ = appmod._enqueue("t_block")
        self.assertTrue(wait_for(lambda: self.status(first) == "running"))
        queued, _ = appmod._enqueue("t_second")
        self.assertEqual(self.status(queued), "queued")
        self.assertEqual(appmod.post_job_cancel(queued, REQ)["was"], "queued")
        self.assertEqual(self.status(queued), "cancelled")
        appmod.post_job_cancel(first, REQ)
        self.assertTrue(wait_for(lambda: self.status(first) == "cancelled"))
        time.sleep(0.2)
        self.assertNotIn("second", self.ran)

    def test_a_running_job_that_cannot_stop_part_way_says_so_instead_of_pretending(self):
        jid, _ = appmod._enqueue("t_stuck")
        self.assertTrue(wait_for(lambda: self.status(jid) == "running"))
        r = appmod.post_job_cancel(jid, REQ)
        self.assertEqual(r.status_code, 409)
        self.assertIn("restart", r.body.decode())
        self.hold.set()

    def test_unknown_and_finished_jobs(self):
        self.assertEqual(appmod.post_job_cancel("nope-1", REQ).status_code, 404)
        jid, _ = appmod._enqueue("t_second")
        self.assertTrue(wait_for(lambda: self.status(jid) == "done"))
        self.assertEqual(appmod.post_job_cancel(jid, REQ).status_code, 400)


class TestJobsStopBetweenNames(unittest.TestCase):
    def setUp(self):
        store.init_db()
        ticker_state.ensure()
        ticker_state.ensure_scout()
        with store._conn() as c:
            for t in ("ticker_state", "scout_notes", "scout_picks"):
                c.execute(f"DELETE FROM {t}")
        base = {"revenue": 5e7, "rev_growth_pct": 60.0, "share_change_pct": 5.0, "runway_months": 30.0, "net_margin_pct": 5.0}
        ticker_state.upsert_many([{"ticker": t, "cik": str(i + 1), "name": t, "exchange": "NYSE", "state": "active", "reason": "", "reason_kind": "screened",
                                   "next_check": None, "s1": {**base, "rev_growth_pct": 90.0 - i}, "scores": {"score": 5}}
                                  for i, t in enumerate(("AAA", "BBB", "CCC", "DDD"))], "2026-11-14")
        d = tempfile.mkdtemp(dir=_TMP)
        real = appmod._sleeve_list
        self.patches = [mock.patch.object(appmod, "_sleeve_list", lambda: [s for s in real() if s["id"] == "growth"]),
                        mock.patch.object(appmod, "MARKET_META_FILE", os.path.join(d, "m.json")),
                        mock.patch.object(appmod, "SLEEVES_FILE", os.path.join(d, "cancel-sleeves.json")),
                        mock.patch.object(appmod, "FINNHUB", "k"), mock.patch.object(appmod, "_finnhub_open", return_value=True)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_market_data_stops_when_asked_and_keeps_what_it_fetched(self):
        calls = {"n": 0}

        def fetch(t):
            calls["n"] += 1
            return 10.0, {"market_cap_musd": 1.0}, {"finnhubIndustry": "Tech"}
        stops = iter([False, False, True])
        with mock.patch.object(appmod, "_job_cancelled", side_effect=lambda: next(stops, True)):
            appmod._do_market_data(fetch=fetch)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(len(ticker_state.s2_index()), 2)
        self.assertIn("stopped by you", appmod.get_market_data_status()["last_run"]["stopped"])

    def test_scout_is_a_cancellable_kind_and_stops_between_names(self):
        for k in ("market_data", "signals", "scout"):
            self.assertIn(k, appmod._CANCELLABLE_KINDS)
        for t in ("AAA", "BBB", "CCC", "DDD"):
            ticker_state.set_s2(t, {"available": True, "price": 20.0, "fetched_at": int(time.time())})
            ticker_state.set_s3(t, "analyst", {"available": True, "buy_pct": 70.0, "total": 5, "fetched_at": 1})
        n = {"n": 0}

        def facts(t, row, name, nightly):
            n["n"] += 1
            return ({"sec": {"a": 1}, "market": {"price": 20.0}, "insiders": {}, "analysts": {"buy_pct": 70}, "news": [{"id": "news:1", "title": "x", "text": "y"}],
                     "filings": []}, None)
        good = ('{"thesis": "A reasonable thesis about growth and cash runway here.", "evidence": [{"claim": "a", "source": "sec"}, {"claim": "b", "source": "news:1"}],'
                ' "risks": ["r"], "disproof": ["d"], "catalysts": [], "horizon": "1-3 years", "conviction": 3}')
        stops = iter([False, True])
        with mock.patch.object(appmod, "_job_cancelled", side_effect=lambda: next(stops, True)), \
             mock.patch.object(appmod, "SCOUT_META_FILE", os.path.join(tempfile.mkdtemp(dir=_TMP), "sc.json")), \
             mock.patch.object(appmod, "_text_model", return_value="m"):
            appmod._do_scout(generate=lambda p: (good, None), facts_fn=facts, spy_fn=lambda: 1.0)
            self.assertIn("stopped by you", appmod.get_scout_status()["last_run"]["stopped"])
        self.assertEqual((n["n"], len(ticker_state.list_notes())), (1, 1))


class TestScoutWaitsForData(unittest.TestCase):
    TODAY = datetime.date(2026, 11, 14)

    def row(self, t, price=20.0, signals=True):
        base = {"revenue": 5e7, "rev_growth_pct": 60.0, "share_change_pct": 5.0, "runway_months": 30.0, "first_filed": "2015-01-01"}
        return {"ticker": t, "state": "active", "cik": t, "s1": base, "scores": {"score": 5},
                "s2": {"available": True, "price": price} if price else None,
                "s3": {"analyst": {"available": True, "buy_pct": 70.0, "total": 5}} if signals else {}}

    def sl(self):
        return [{"id": "growth", **sleeves.BUILTIN["growth"]}]

    def test_names_without_a_price_or_any_signal_are_not_scouted_yet(self):
        rows = [self.row("OK"), self.row("NOPRICE", price=None), self.row("NOSIG", signals=False)]
        self.assertEqual([g["ticker"] for g in scout.pick_targets(self.sl(), rows, {}, self.TODAY)], ["OK"])

    def test_a_note_written_before_price_and_signals_existed_does_not_block_a_proper_one(self):
        now = datetime.datetime.combine(self.TODAY, datetime.time(12, 0)).timestamp()
        notes = {"A": {"created_at": now - 86400, "inputs": {"price": None, "insider_buyers": 0, "analyst_buy_pct": None}},
                 "B": {"created_at": now - 86400, "inputs": {"price": 20.0, "insider_buyers": 0, "analyst_buy_pct": 70.0}}}
        got = scout.pick_targets(self.sl(), [self.row("A"), self.row("B")], notes, self.TODAY)
        self.assertEqual([(g["ticker"], g["why"][:13]) for g in got], [("A", "the last note")])


class TestPage(unittest.TestCase):
    def test_task_schedule_has_a_running_and_queued_panel_with_stop(self):
        with open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8") as f:
            html = f.read()
        for needle in ("function jobsNowPanel()", "RUNNING AND QUEUED NOW", "■ STOP", "/cancel'", "wrap.appendChild(jobsNowPanel());"):
            self.assertIn(needle, html, needle)
        with open(os.path.join(_HERE, "app.py"), encoding="utf-8") as f:
            self.assertIn('@app.post("/api/jobs/{jid}/cancel")', f.read())


if __name__ == "__main__":
    unittest.main()
