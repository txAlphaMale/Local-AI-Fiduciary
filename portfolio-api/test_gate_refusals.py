# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_gate_refusals.py -- RESULT-020 F1-F7 (Cowork, 2026-11-13). A call the budget gate refused (pacing or pause) or that failed must never
be recorded as 'no data': background callers wait for a pacing slot, jobs check for refusals before saving, and records written before the
fix are re-fetched."""
import datetime, glob, os, re, sys, tempfile, time, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="gaterefuse-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    with open(_f) as _fh:
        for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', _fh.read()):
            os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod, budget, edgar, market_data as md, signals, sleeves, store, ticker_state  # noqa: E402

TODAY = datetime.date(2026, 11, 14)


class Resp:
    def __init__(self, code, headers=None): self.status_code, self.headers = code, headers or {}


class TestWaiting(unittest.TestCase):
    def test_background_finnhub_calls_wait_for_a_slot_and_interactive_ones_do_not(self):
        seen = []

        def chk(name, batch=False, max_wait=0):
            seen.append((batch, max_wait))
            return {"ok": False, "reason": "pacing (45/45 calls in the last minute)"}
        with mock.patch.object(appmod, "FINNHUB", "k"), mock.patch.object(appmod._budget, "check", side_effect=chk):
            appmod._fh_reset()
            self.assertEqual(appmod._finnhub_json("quote", "X"), (None, False))
            with appmod._finnhub_batch():
                appmod._finnhub_json("quote", "X")
        self.assertEqual(seen, [(False, 20), (True, 75)])               # 75 s outlasts the 60 s pacing window
        n, why = appmod._fh_failures()
        self.assertEqual(n, 2)
        self.assertIn("pacing", why)

    def test_sec_background_requests_wait_longer_than_the_window_and_refusals_are_counted(self):
        clk = type("C", (), {"t": 1_800_000_000.0, "__call__": lambda s: s.t})()
        b = budget.Budget(clock=clk, load=lambda: None, save=lambda d: None)
        edgar.set_gate(b)
        self.addCleanup(edgar.set_gate, None)
        with mock.patch.object(edgar.requests, "get", return_value=Resp(403)):
            edgar.reset_deferrals()
            with edgar.batch_mode():
                edgar._get("https://data.sec.gov/x")                    # 403 pauses SEC
                with self.assertRaises(edgar.SecDeferred):
                    edgar._get("https://data.sec.gov/x")
        self.assertEqual(edgar.deferral_count(), 1)
        edgar.reset_deferrals()
        self.assertEqual(edgar.deferral_count(), 0)


class TestMarketDataJob(unittest.TestCase):
    def setUp(self):
        store.init_db()
        ticker_state.ensure()
        with store._conn() as c:
            c.execute("DELETE FROM ticker_state")
        base = {"revenue": 5e7, "rev_growth_pct": 60.0, "share_change_pct": 5.0, "runway_months": 30.0, "net_margin_pct": 5.0}
        mk = lambda t, g: {"ticker": t, "cik": str(abs(hash(t)) % 10**9).zfill(10), "name": t, "exchange": "NYSE", "state": "active", "reason": "",
                           "reason_kind": "screened", "next_check": None, "s1": {**base, "rev_growth_pct": g}, "scores": {"score": 5}}
        ticker_state.upsert_many([mk("AAA", 90.0), mk("BBB", 70.0), mk("CCC", 40.0)], "2026-11-14")
        d = tempfile.mkdtemp(dir=_TMP)
        real = appmod._sleeve_list
        self.patches = [mock.patch.object(appmod, "_sleeve_list", lambda: [s for s in real() if s["id"] == "growth"]),
                        mock.patch.object(appmod, "MARKET_META_FILE", os.path.join(d, "m.json")),
                        mock.patch.object(appmod, "SIGNALS_META_FILE", os.path.join(d, "s.json")),
                        mock.patch.object(appmod, "SLEEVES_FILE", os.path.join(d, "refusal-sleeves.json")),
                        mock.patch.object(appmod, "FINNHUB", "k"),
                        mock.patch.object(appmod, "_finnhub_open", return_value=True),
                        mock.patch.object(appmod, "_provider_open", return_value=True),
                        mock.patch.object(appmod.shortint, "configured", return_value=False)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_a_refused_lookup_is_not_recorded_as_no_data_and_the_reason_is_kept(self):
        def fetch(t):
            if t == "BBB":
                appmod._fh_failed("gate: Finnhub market data deferred: pacing (45/45 calls in the last minute).")
                return None, None, {}
            return 10.0, {"market_cap_musd": 500.0}, {"finnhubIndustry": "Technology"}
        appmod._do_market_data(fetch=fetch)
        idx = ticker_state.s2_index()
        self.assertEqual(set(idx), {"AAA", "CCC"})
        last = appmod.get_market_data_status()["last_run"]
        self.assertEqual((last["fetched"], last["errors"]), (2, 1))
        self.assertIn("pacing", last["first_error"])

    def test_eight_refusals_in_a_row_stop_the_run_with_the_reason(self):
        ticker_state.upsert_many([{"ticker": f"X{i}", "cik": str(i + 10), "name": "x", "exchange": "NYSE", "state": "active", "reason": "",
                                   "reason_kind": "screened", "next_check": None,
                                   "s1": {"revenue": 5e7, "rev_growth_pct": 50.0 + i, "share_change_pct": 1.0, "runway_months": 30.0},
                                   "scores": {"score": 5}} for i in range(12)], "2026-11-14")

        def fetch(t):
            appmod._fh_failed("HTTP 429 on quote")
            return None, None, {}
        appmod._do_market_data(fetch=fetch)
        last = appmod.get_market_data_status()["last_run"]
        self.assertEqual((last["fetched"], last["errors"]), (0, 8))
        self.assertIn("kept refusing", last["stopped"])
        self.assertEqual(ticker_state.s2_index(), {})

    def test_records_from_before_the_fix_are_fetched_again_first(self):
        ticker_state.set_s2("AAA", {"available": False, "fetched_at": int(time.time()), "price": None})          # v1: a refusal saved as no data
        ticker_state.set_s2("BBB", {"available": True, "fetched_at": int(time.time()), "price": 9.0, "v": md.RECORD_VERSION})
        got, due = md.select_tranche(["AAA", "BBB", "CCC"], ticker_state.s2_index(), limit=10, version=md.RECORD_VERSION)
        self.assertEqual((sorted(got), due), (["AAA", "CCC"], 2))

    def test_new_records_carry_the_version(self):
        self.assertEqual(md.build_record(10.0, {"market_cap_musd": 1.0}, {}, now=1)["v"], md.RECORD_VERSION)


class TestSignalsJob(unittest.TestCase):
    setUp = TestMarketDataJob.setUp
    tearDown = TestMarketDataJob.tearDown

    def test_analyst_failures_log_the_error_class(self):
        def fa(t):
            appmod._fh_failed("HTTP 403 on stock/recommendation")
            return [], False
        appmod._do_signals(fetch_analyst=fa, fetch_insider=lambda t: {"available": True, "net_value": 1.0, "buy": {}, "sell": {}})
        g = appmod.get_signals_status()["last_run"]["groups"]["analyst"]
        self.assertEqual((g["done"], g["errors"]), (0, 3))
        self.assertIn("403", g["first_error"])
        self.assertEqual(ticker_state.s3_index("analyst"), {})

    def test_an_sec_refusal_during_the_insider_lookup_is_not_recorded_as_no_filings(self):
        def fi(t):
            edgar._MODE.deferred = getattr(edgar._MODE, "deferred", 0) + 1
            return {"ticker": t, "available": False, "reason": "no Form 4 filings"}
        appmod._do_signals(fetch_analyst=lambda t: ([{"strongBuy": 1}], True), fetch_insider=fi)
        self.assertEqual(ticker_state.s3_index("insider"), {})
        self.assertEqual(appmod.get_signals_status()["last_run"]["groups"]["insider"]["errors"], 3)

    def test_insider_signal_does_not_cache_a_deferred_lookup_as_empty(self):
        def fake(t, days=120, limit=16):
            edgar._MODE.deferred = getattr(edgar._MODE, "deferred", 0) + 1
            return []
        with mock.patch.object(edgar, "recent_form4", side_effect=fake):
            res = appmod._insider_signal("QQQQ")
        self.assertTrue(res.get("error"))
        self.assertFalse(res.get("available"))

    def test_signal_records_carry_the_version_and_old_ones_are_refetched(self):
        self.assertEqual(signals.build_analyst([], now=1)["v"], signals.RECORD_VERSION)
        ticker_state.set_s3("AAA", "analyst", {"available": False, "fetched_at": int(time.time())})
        got, _ = md.select_tranche(["AAA"], ticker_state.s3_index("analyst"), version=signals.RECORD_VERSION, refresh_days=7)
        self.assertEqual(got, ["AAA"])


class TestSmallFixes(unittest.TestCase):
    def test_insider_text_names_sellers_when_net_selling(self):
        sell = {"insider": {"available": True, "net_value": -380000.0, "signal": "net selling", "buyers": 2, "sellers": 1}}
        self.assertEqual(signals.display(sell)[0], "net selling (-$380K, 1 selling)")
        buy = {"insider": {"available": True, "net_value": 807000.0, "signal": "cluster buying", "buyers": 2, "sellers": 0}}
        self.assertEqual(signals.display(buy)[0], "cluster buying (+$807K, 2 buying)")

    def test_growth_ranking_is_capped_and_ties_go_to_the_bigger_business(self):
        mk = lambda t, g, rev: {"ticker": t, "state": "active", "s1": {"revenue": rev, "rev_growth_pct": g, "share_change_pct": 5.0, "runway_months": 30.0,
                                                                       "first_filed": "2015-01-01"}, "scores": {"score": 4}}
        rows = [mk("TINYBASE", 230000.0, 12e6), mk("BIGGER", 400.0, 900e6), mk("MID", 120.0, 50e6)]
        ev = sleeves.evaluate(sleeves.BUILTIN["growth"], rows, TODAY)
        self.assertEqual([r["ticker"] for r in ev["matches"]], ["BIGGER", "TINYBASE", "MID"])    # both capped at 150: bigger revenue first
        self.assertEqual(sleeves.BUILTIN["growth"]["rank_by"], "growth_capped")
        self.assertEqual(sleeves.value_of(rows[0], "growth_capped"), 150.0)

    def test_idea_hunting_counter_is_saved_periodically_so_a_restart_keeps_it(self):
        saves = []
        clk = type("C", (), {"t": 1_800_000_000.0, "__call__": lambda s: s.t})()
        b = budget.Budget(clock=clk, load=lambda: None, save=lambda d: saves.append(d["finnhub"]["day_count"]))
        for i in range(45):
            clk.t += 1.5
            b.check("finnhub", batch=True)
        self.assertEqual(saves, [20, 40])

    def test_page_wording_names_signals(self):
        with open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8") as f:
            self.assertIn("wait for market data or signals", f.read())


if __name__ == "__main__":
    unittest.main()
