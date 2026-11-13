# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_signals.py -- idea scavenger phase 4c (Jason 2026-11-13): analyst ratings, insider buying and short interest for the stocks that
pass a sleeve's SEC rules, fetched in nightly tranches behind the Finnhub and SEC gates. Deferred is never recorded as 'no data'."""
import datetime, glob, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="signals-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    with open(_f) as _fh:
        for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', _fh.read()):
            os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod, market_data as md, signals, sleeves, store, ticker_state  # noqa: E402


class TestRecords(unittest.TestCase):
    def test_analyst_record(self):
        r = signals.build_analyst([{"period": "2026-11-01", "strongBuy": 5, "buy": 5, "hold": 4, "sell": 1, "strongSell": 0}], now=1)
        self.assertEqual((r["available"], r["total"], r["buy_pct"], r["period"]), (True, 15, 66.7, "2026-11-01"))
        none = signals.build_analyst([], now=1)
        self.assertEqual((none["available"], none["buy_pct"]), (False, None))

    def test_insider_record_from_the_existing_aggregate(self):
        agg = {"available": True, "net_value": 250000.0, "net_buy_pct": 60.0, "signal": "cluster buying", "filings": 6, "as_of": "2026-11-05",
               "lookback_days": 120, "buy": {"count": 4, "insiders": 3, "value": 300000.0}, "sell": {"count": 1, "insiders": 1, "value": 50000.0}}
        r = signals.build_insider(agg, now=1)
        self.assertEqual((r["buyers"], r["net_value"], r["signal"]), (3, 250000.0, "cluster buying"))
        self.assertFalse(signals.build_insider({"available": False, "reason": "no Form 4"}, now=1)["available"])

    def test_short_record_and_display_strings(self):
        self.assertTrue(signals.build_short({"days_to_cover": 4.2, "change_pct": -3.0, "settlement_date": "2026-10-31"}, now=1)["available"])
        self.assertFalse(signals.build_short(None, now=1)["available"])
        ins, an = signals.display({"insider": {"available": True, "net_value": 250000.0, "signal": "cluster buying", "buyers": 3},
                                   "analyst": {"available": True, "buy_pct": 66.7, "total": 15}})
        self.assertEqual((ins, an), ("cluster buying (+$250K, 3 buying)", "67% buy (15)"))
        self.assertEqual(signals.display(None), (None, None))


class TestJob(unittest.TestCase):
    def setUp(self):
        store.init_db()
        ticker_state.ensure()
        with store._conn() as c:
            c.execute("DELETE FROM ticker_state")
        base = {"revenue": 5e7, "rev_growth_pct": 60.0, "share_change_pct": 5.0, "runway_months": 30.0, "net_margin_pct": 5.0}
        mk = lambda t, g: {"ticker": t, "cik": str(abs(hash(t)) % 10**9).zfill(10), "name": t, "exchange": "NYSE", "state": "active",
                           "reason": "", "reason_kind": "screened", "next_check": None, "s1": {**base, "rev_growth_pct": g},
                           "scores": {"score": 5}}
        ticker_state.upsert_many([mk("AAA", 90.0), mk("BBB", 70.0), mk("CCC", 40.0)], "2026-11-13")
        d = tempfile.mkdtemp(dir=_TMP)
        real = appmod._sleeve_list
        self.patches = [mock.patch.object(appmod, "_sleeve_list", lambda: [s for s in real() if s["id"] == "growth"]),
                        mock.patch.object(appmod, "SIGNALS_META_FILE", os.path.join(d, "sig.json")),
                        mock.patch.object(appmod, "SLEEVES_FILE", os.path.join(d, "signals-sleeves.json")),
                        mock.patch.object(appmod, "FINNHUB", "k"),
                        mock.patch.object(appmod.shortint, "configured", return_value=False)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    ok_analyst = staticmethod(lambda t: ([{"period": "2026-11-01", "strongBuy": 3, "buy": 3, "hold": 1}], True))
    ok_insider = staticmethod(lambda t: {"available": True, "net_value": 100000.0, "signal": "net buying", "filings": 2, "lookback_days": 120,
                                         "buy": {"insiders": 2, "value": 100000.0}, "sell": {"insiders": 0, "value": 0.0}})

    def test_fetches_both_groups_and_the_sleeve_table_shows_them(self):
        with mock.patch.object(appmod, "_provider_open", return_value=True):
            appmod._do_signals(fetch_analyst=self.ok_analyst, fetch_insider=self.ok_insider)
        self.assertEqual(set(ticker_state.s3_index("analyst")), {"AAA", "BBB", "CCC"})
        self.assertEqual(set(ticker_state.s3_index("insider")), {"AAA", "BBB", "CCC"})
        st = appmod.get_signals_status()
        self.assertEqual(st["last_run"]["groups"]["short"]["skipped"][:7], "no FINR")
        row = [r for r in appmod.get_sleeves()["sleeves"] if r["id"] == "growth"][0]["preview"][0]
        self.assertEqual(row["analyst"], "86% buy (7)")
        self.assertIn("net buying", row["insider"])

    def test_failed_and_deferred_calls_are_left_alone_not_recorded_as_no_data(self):
        def analyst(t):
            return ([], False) if t == "BBB" else self.ok_analyst(t)
        with mock.patch.object(appmod, "_provider_open", return_value=True):
            appmod._do_signals(fetch_analyst=analyst, fetch_insider=lambda t: {"available": False, "error": True})
        self.assertNotIn("BBB", ticker_state.s3_index("analyst"))
        self.assertEqual(ticker_state.s3_index("insider"), {})                # errors recorded nothing
        g = appmod.get_signals_status()["last_run"]["groups"]
        self.assertEqual((g["analyst"]["errors"], g["insider"]["errors"]), (1, 3))

    def test_stops_when_a_gate_pauses_mid_run_and_does_not_record_the_interrupted_name(self):
        states = iter([True, False])                                           # open before the 1st analyst call, paused by the time it returns
        with mock.patch.object(appmod, "_provider_open", side_effect=lambda name: True if name == "sec" else next(states, False)):
            appmod._do_signals(fetch_analyst=self.ok_analyst, fetch_insider=self.ok_insider)
        self.assertEqual(ticker_state.s3_index("analyst"), {})
        self.assertIn("paused", appmod.get_signals_status()["last_run"]["groups"]["analyst"]["stopped"])

    def test_no_coverage_is_a_real_answer_and_is_rechecked_much_later(self):
        with mock.patch.object(appmod, "_provider_open", return_value=True):
            appmod._do_signals(fetch_analyst=lambda t: ([], True), fetch_insider=self.ok_insider)
        idx = ticker_state.s3_index("analyst")
        self.assertTrue(idx and all(v["available"] is False for v in idx.values()))

    def test_tranche_limit_and_short_group_when_credentials_exist(self):
        with mock.patch.object(appmod, "_provider_open", return_value=True), mock.patch.object(appmod.shortint, "configured", return_value=True):
            appmod._do_signals(limit_analyst=1, limit_insider=1, fetch_analyst=self.ok_analyst, fetch_insider=self.ok_insider,
                               fetch_short=lambda t: {"days_to_cover": 3.0, "change_pct": 5.0, "settlement_date": "2026-10-31"})
        self.assertEqual(len(ticker_state.s3_index("analyst")), 1)
        self.assertEqual(len(ticker_state.s3_index("short")), 3)
        self.assertEqual(appmod.get_signals_status()["last_run"]["groups"]["analyst"]["remaining"], 2)


class TestWiring(unittest.TestCase):
    def test_job_schedule_endpoints_page(self):
        with open(os.path.join(_HERE, "app.py"), encoding="utf-8") as f:
            src = f.read()
        for needle in ('"signals":       ("Signals for sleeve candidates"', "/api/run-signals", "/api/signals",
                       '_SCHEDULE_ORDER = ["universe", "market_data", "signals",'):
            self.assertIn(needle, src, needle)
        with open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8") as f:
            html = f.read()
        for needle in ("signals: '/api/run-signals'", "INSIDERS</th>", "ANALYSTS</th>", "m.note"):
            self.assertIn(needle, html, needle)

    def test_insiders_sleeve_exists_and_nominates_a_bounded_candidate_list(self):
        self.assertIn("insiders", sleeves.BUILTIN)
        clean, err = sleeves.validate(sleeves.BUILTIN["insiders"])
        self.assertIsNone(err)
        rows = [{"ticker": "T%d" % i, "state": "active", "s1": {"revenue": 5e7}, "scores": {"score": 5}} for i in range(900)]
        got = md.candidates([{"id": "insiders", **sleeves.BUILTIN["insiders"]}], rows, datetime.date(2026, 11, 13))
        self.assertEqual(len(got), md.BALANCE_CANDIDATES)


if __name__ == "__main__":
    unittest.main()
