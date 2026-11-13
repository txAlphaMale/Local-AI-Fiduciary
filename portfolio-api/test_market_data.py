# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_market_data.py -- idea scavenger phase 4b (Jason 2026-11-13): market data (price, size, yield, beta, sector) for the stocks that
pass a sleeve's SEC rules, fetched in nightly tranches behind the Finnhub budget gate; paused is never recorded as 'no data'."""
import datetime, glob, json, os, re, sys, tempfile, time, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="mktdata-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    with open(_f) as _fh:
        for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', _fh.read()):
            os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod, market_data as md, sleeves, store, ticker_state  # noqa: E402

TODAY = datetime.date(2026, 11, 13)


def row(t, **s1):
    base = {"revenue": 5e7, "rev_growth_pct": 30.0, "share_change_pct": 5.0, "runway_months": 30.0, "net_margin_pct": 5.0}
    base.update(s1)
    return {"ticker": t, "name": t, "state": "active", "s1": base, "scores": {"score": 5}}


class TestSelection(unittest.TestCase):
    def test_candidates_are_sleeve_passers_in_rank_order_without_duplicates(self):
        rows = [row("A", rev_growth_pct=40.0), row("B", rev_growth_pct=90.0), row("SLOW", rev_growth_pct=5.0)]
        out = md.candidates([{"id": "growth", **sleeves.BUILTIN["growth"]}, {"id": "g2", **sleeves.BUILTIN["growth"]}], rows, TODAY)
        self.assertEqual(out, ["B", "A"])

    def test_market_rules_do_not_hide_a_candidate_before_its_market_data_exists(self):
        sl = {"id": "x", **sleeves.BUILTIN["growth"], "rules": [{"metric": "rev_growth_pct", "op": ">=", "value": 25},
                                                                  {"metric": "market_cap", "op": "<=", "value": 1e9}]}
        self.assertEqual(md.candidates([sl], [row("A")], TODAY), ["A"])

    def test_balance_takes_healthy_names_capped(self):
        rows = [row("H%d" % i) for i in range(5)] + [{**row("WEAK"), "scores": {"score": 2}}]
        with mock.patch.object(md, "BALANCE_CANDIDATES", 3):
            out = md.candidates([{"id": "balance", **sleeves.BUILTIN["balance"]}], rows, TODAY)
        self.assertEqual(len(out), 3)
        self.assertNotIn("WEAK", out)

    def test_disabled_sleeves_are_skipped(self):
        self.assertEqual(md.candidates([{"id": "g", **sleeves.BUILTIN["growth"], "enabled": False}], [row("A")], TODAY), [])

    def test_tranche_is_never_checked_first_then_stalest_and_skips_fresh(self):
        now = 1_800_000_000
        s2 = {"FRESH": {"fetched_at": now - 3600, "available": True}, "OLD": {"fetched_at": now - 10 * 86400, "available": True},
              "OLDER": {"fetched_at": now - 20 * 86400, "available": True},
              "NODATA_RECENT": {"fetched_at": now - 10 * 86400, "available": False},
              "NODATA_OLD": {"fetched_at": now - 40 * 86400, "available": False}}
        got, due = md.select_tranche(["FRESH", "OLD", "NEW", "OLDER", "NODATA_RECENT", "NODATA_OLD"], s2, now=now, limit=10)
        self.assertEqual(got, ["NEW", "NODATA_OLD", "OLDER", "OLD"])
        self.assertEqual(due, 4)
        self.assertEqual(md.select_tranche(["NEW", "OLD", "OLDER"], s2, now=now, limit=2)[0], ["NEW", "OLDER"])

    def test_window_and_deadline(self):
        inside = datetime.datetime(2026, 11, 13, 4, 0)
        self.assertEqual(md.deadline_for(inside), datetime.datetime(2026, 11, 13, 6, 0).timestamp())
        self.assertIsNone(md.deadline_for(datetime.datetime(2026, 11, 13, 14, 0)))     # manual run at 2 p.m.: no cutoff
        self.assertFalse(md.in_window(datetime.datetime(2026, 11, 13, 6, 0)))


class TestRecord(unittest.TestCase):
    def test_build_record(self):
        r = md.build_record(20.0, {"market_cap_musd": 1500.0, "pe_ttm": 30.0, "dividend_yield_pct": 1.2, "beta": 1.4,
                                   "avg_volume_3m_m": 2.0, "high_52w": 25.0, "low_52w": 10.0},
                            {"finnhubIndustry": " Aerospace & Defense "}, now=1_800_000_000)
        self.assertEqual((r["price"], r["market_cap"], r["sector"], r["available"]), (20.0, 1_500_000_000, "Aerospace & Defense", True))
        self.assertEqual((r["avg_dollar_volume"], r["pct_off_52w_high"]), (40_000_000, -20.0))

    def test_nothing_from_finnhub_is_recorded_as_unavailable(self):
        r = md.build_record(None, None, {}, now=1)
        self.assertFalse(r["available"])
        self.assertEqual(r["fetched_at"], 1)


class TestStorageAndJob(unittest.TestCase):
    def setUp(self):
        store.init_db()
        ticker_state.ensure()
        with store._conn() as c:
            c.execute("DELETE FROM ticker_state")
        mk = lambda t, g, sc=5: {"ticker": t, "cik": str(abs(hash(t)) % 10**9).zfill(10), "name": t, "exchange": "NYSE", "state": "active", "reason": "",
                                 "reason_kind": "screened", "next_check": None, "s1": row(t, rev_growth_pct=g)["s1"], "scores": {"score": sc}}
        ticker_state.upsert_many([mk("AAA", 90.0), mk("BBB", 60.0), mk("CCC", 40.0), mk("DDD", 3.0, 3)], "2026-11-13")
        d = tempfile.mkdtemp(dir=_TMP)
        real = appmod._sleeve_list
        # The "Insiders are buying" sleeve's SEC-side rule is just "has revenue", so it would nominate every healthy name here.
        self.patches = [mock.patch.object(appmod, "_sleeve_list", lambda: [s for s in real() if s["id"] != "insiders"]),
                        mock.patch.object(appmod, "MARKET_META_FILE", os.path.join(d, "m.json")),
                        mock.patch.object(appmod, "FINNHUB", "test-key"),
                        mock.patch.object(appmod, "SLEEVES_FILE", os.path.join(d, "mktdata-sleeves.json"))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        store.write_json(appmod.SLEEVES_FILE, {"custom": {}})      # app-state keys are by file name; leave nothing behind
        for p in self.patches:
            p.stop()

    def fake(self, t):
        return 10.0 + len(t), {"market_cap_musd": 500.0, "beta": 1.1}, {"finnhubIndustry": "Technology"}

    def test_set_s2_does_not_move_state_and_s2_is_visible_to_sleeves(self):
        before = ticker_state.get_all_states()["AAA"]
        ticker_state.set_s2("AAA", md.build_record(12.0, {"market_cap_musd": 800.0}, {"finnhubIndustry": "Energy"}))
        self.assertEqual(ticker_state.get_all_states()["AAA"], before)
        r = [x for x in ticker_state.screened_rows(sleeves.STATES) if x["ticker"] == "AAA"][0]
        self.assertEqual(r["s2"]["market_cap"], 800_000_000)
        self.assertEqual(sleeves.value_of(r, "market_cap"), 800_000_000)
        self.assertIn("AAA", ticker_state.s2_index())

    def test_job_fetches_candidates_in_order_and_saves_summary(self):
        with mock.patch.object(appmod, "_finnhub_open", return_value=True):
            appmod._do_market_data(fetch=self.fake)
        idx = ticker_state.s2_index()
        self.assertTrue({"AAA", "BBB", "CCC"} <= set(idx))
        self.assertNotIn("DDD", idx)                              # DDD fails the growth rule, so no market data is spent on it
        last = appmod.get_market_data_status()["last_run"]
        self.assertEqual((last["fetched"], last["remaining"], last["stopped"]), (3, 0, None))

    def test_job_stops_when_finnhub_pauses_and_leaves_the_rest_untouched(self):
        calls = {"n": 0}

        def fake(t):
            calls["n"] += 1
            return self.fake(t)
        states = iter([True, False])                               # open before AAA's fetch, tripped by the time it returns
        with mock.patch.object(appmod, "_finnhub_open", side_effect=lambda: next(states, False)):
            appmod._do_market_data(fetch=fake)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(ticker_state.s2_index(), {})              # the interrupted ticker is NOT recorded as 'no data'
        last = appmod.get_market_data_status()["last_run"]
        self.assertEqual(last["fetched"], 0)
        self.assertIn("paused", last["stopped"])

    def test_job_needs_a_finnhub_key_and_respects_the_tranche_limit(self):
        with mock.patch.object(appmod, "FINNHUB", ""):
            with self.assertRaises(RuntimeError):
                appmod._do_market_data(fetch=self.fake)
        with mock.patch.object(appmod, "_finnhub_open", return_value=True):
            appmod._do_market_data(limit=2, fetch=self.fake)
        self.assertEqual(len(ticker_state.s2_index()), 2)
        self.assertEqual(appmod.get_market_data_status()["last_run"]["remaining"], 1)

    def test_sleeve_endpoint_counts_awaiting_then_confirmed(self):
        by = {s["id"]: s for s in appmod.get_sleeves()["sleeves"]}
        self.assertEqual(by["balance"]["awaiting_market_data"], 3)
        with mock.patch.object(appmod, "_finnhub_open", return_value=True), \
             mock.patch.object(appmod, "_held_sector_weights", return_value={"Technology": 50.0}):
            appmod._do_market_data(fetch=lambda t: (10.0, {"market_cap_musd": 500.0}, {"finnhubIndustry": "Energy"}))
            by = {s["id"]: s for s in appmod.get_sleeves()["sleeves"]}
        self.assertEqual(by["balance"]["gap_sectors"][0][0], "Energy")
        self.assertGreaterEqual(by["balance"]["match_count"], 1)


class TestFinnhubWiring(unittest.TestCase):
    def test_batch_mode_reaches_the_gate_and_profile_goes_through_it(self):
        seen = []

        def chk(name, batch=False, max_wait=0):
            seen.append((name, batch))
            return {"ok": False}
        with mock.patch.object(appmod, "FINNHUB", "k"), mock.patch.object(appmod._budget, "check", side_effect=chk):
            appmod._finnhub_json("quote", "X")
            with appmod._finnhub_batch():
                appmod._finnhub_json("quote", "X")
            self.assertEqual(seen, [("finnhub", False), ("finnhub", True)])
            seen.clear()
            with mock.patch.object(store, "cached_fetch", side_effect=lambda src, key, ttl, fn, **kw: (fn(), {})):
                appmod._profile("ZZZ")
        self.assertEqual(seen, [("finnhub", False)])               # _profile used to call requests directly, bypassing the gate

    def test_job_schedule_and_endpoints_exist(self):
        with open(os.path.join(_HERE, "app.py"), encoding="utf-8") as f:
            src = f.read()
        for needle in ('"market_data":   ("Market data for sleeve candidates"', "/api/run-market-data", "/api/market-data",
                       '_SCHEDULE_ORDER = ["scout", "universe", "market_data",'):
            self.assertIn(needle, src, needle)
        with open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8") as f:
            self.assertIn("market_data: '/api/run-market-data'", f.read())


if __name__ == "__main__":
    unittest.main()
