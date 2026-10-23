# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_perf_history.py -- (1) never record / hide half-loaded value snapshots, (2) a return series that contributions cannot inflate,
(3) cost-basis history, (4) the window/rebase/bucket helpers behind the dashboard charts. (2026-10-23)"""
import datetime, glob, json, os, re, shutil, subprocess, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="perfhist-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod  # noqa: E402
import performance, store, valuehist  # noqa: E402

HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()


def row(d, inv, positions=None): return {"date": d, "invested_value": inv, "positions": positions}


class TestGuards(unittest.TestCase):
    def test_account_error_means_incomplete(self):
        d = {"by_account": [{"account": "x", "error": "boom"}], "invested_value": 100, "position_count": 5}
        self.assertTrue(valuehist.snapshot_incomplete(d, None)[0])

    def test_sharp_drop_with_missing_positions_is_rejected_but_a_real_selloff_is_not(self):
        prev = row("2026-09-05", 1000.0, 30)
        half = {"by_account": [], "invested_value": 480.0, "position_count": 12}
        self.assertTrue(valuehist.snapshot_incomplete(half, prev)[0])
        crash = {"by_account": [], "invested_value": 600.0, "position_count": 30}          # -40% but every position is still there
        self.assertFalse(valuehist.snapshot_incomplete(crash, prev)[0])
        small = {"by_account": [], "invested_value": 950.0, "position_count": 29}
        self.assertFalse(valuehist.snapshot_incomplete(small, prev)[0])
        self.assertFalse(valuehist.snapshot_incomplete(half, None)[0])                        # nothing to compare with

    def test_outliers_are_one_day_spikes_not_step_changes(self):
        rows = [row(f"2026-09-0{i}", v) for i, v in enumerate([100, 101, 49, 102, 103], start=1)]
        self.assertEqual(valuehist.detect_outliers(rows), ["2026-09-03"])
        step = [row(f"2026-09-0{i}", v) for i, v in enumerate([100, 101, 130, 131, 132], start=1)]   # a new account: stays up
        self.assertEqual(valuehist.detect_outliers(step), [])
        run2 = [row(f"2026-09-0{i}", v) for i, v in enumerate([115, 115, 49, 49, 114, 113], start=1)]   # the real 09-06/09-07 shape: a two-day dip
        self.assertEqual(valuehist.detect_outliers(run2), ["2026-09-03", "2026-09-04"])
        long_dip = [row(f"2026-09-0{i}", v) for i, v in enumerate([115, 115, 60, 60, 60, 60, 115, 115], start=1)]   # four days: not a "bad snapshot", leave it
        self.assertEqual(valuehist.detect_outliers(long_dip), [])
        spike_up = [row(f"2026-09-0{i}", v) for i, v in enumerate([100, 100, 200, 100, 100], start=1)]
        self.assertEqual(valuehist.detect_outliers(spike_up), ["2026-09-03"])
        self.assertEqual(valuehist.detect_outliers([row("2026-09-01", 100)]), [])

    def test_cost_series_needs_both_numbers(self):
        rows = [{"date": "a", "total_cost": 10.0, "cost_covered_value": 12.0}, {"date": "b", "total_cost": None, "cost_covered_value": 5.0},
                {"date": "c", "total_cost": 0, "cost_covered_value": 5.0}]
        self.assertEqual(valuehist.cost_series(rows), [("a", 12.0, 10.0)])


class TestStore(unittest.TestCase):
    def setUp(self):
        store.init_db()
        with store._conn() as c:
            c.execute("DELETE FROM value_history")

    def test_new_columns_round_trip_encrypted_and_exclusion_is_reversible(self):
        store.value_history_upsert_today("2026-10-01", 100.0, 80.0, 500.0, 60.0, 70.0, 12)
        store.value_history_upsert_today("2026-10-02", 10.0, 8.0, 501.0)                       # old-style call still works
        with store._conn() as c:
            raw = c.execute("SELECT total_cost FROM value_history WHERE date='2026-10-01'").fetchone()["total_cost"]
        self.assertNotIn("60", str(raw))                                                      # ciphertext, not the number
        h = store.value_history_read_all()
        self.assertEqual((h[0]["total_cost"], h[0]["cost_covered_value"], h[0]["positions"]), (60.0, 70.0, 12.0))
        self.assertIsNone(h[1]["total_cost"])
        store.value_history_exclude(["2026-10-02"])
        self.assertEqual([r["date"] for r in store.value_history_read_all()], ["2026-10-01"])
        self.assertEqual(len(store.value_history_read_all(include_excluded=True)), 2)           # never deleted
        store.value_history_exclude(["2026-10-02"], flag=0)
        self.assertEqual(len(store.value_history_read_all()), 2)

    def test_snapshot_skips_a_half_loaded_portfolio_and_records_cost_basis_for_a_good_one(self):
        today = datetime.date.today().isoformat()
        yday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        store.value_history_upsert_today(yday, 1000.0, 1000.0, 500.0, None, None, 30)
        good = {"net_worth": 1100.0, "invested_value": 1100.0, "position_count": 30, "total_cost": 800.0, "by_account": [],
                "positions": [{"value": 600.0, "cost_total": 500.0}, {"value": 300.0, "cost_total": 300.0, "suspect": True}, {"value": 200.0, "cost_total": None}]}
        with mock.patch.object(appmod, "_price", lambda t: 501.0):
            appmod._snapshot_value(good)
        t = next(r for r in store.value_history_read_all() if r["date"] == today)
        self.assertEqual((t["invested_value"], t["total_cost"], t["cost_covered_value"], t["positions"]), (1100.0, 800.0, 600.0, 30.0))
        half = dict(good, invested_value=400.0, position_count=10)
        with mock.patch.object(appmod, "_price", lambda t: 501.0):
            appmod._snapshot_value(half)
        t = next(r for r in store.value_history_read_all() if r["date"] == today)
        self.assertEqual(t["invested_value"], 1100.0)                                           # the good row for today is untouched


class TestMigration(unittest.TestCase):
    def test_an_existing_value_history_table_gains_the_new_columns_without_losing_rows(self):
        import sqlite3
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE value_history(date TEXT PRIMARY KEY, net_worth REAL, invested_value REAL, spy REAL, funded_ratio REAL)")
        con.execute("INSERT INTO value_history VALUES('2026-07-23', 1.5, 1.0, 500.0, NULL)")      # legacy plaintext numbers
        con.commit(); con.close()
        with mock.patch.object(store, "STORE_DB", path):
            store.init_db(); store.init_db()                                                   # twice: idempotent
            h = store.value_history_read_all()
        self.assertEqual((h[0]["date"], h[0]["invested_value"], h[0]["total_cost"], h[0]["excluded"]), ("2026-07-23", 1.0, None, False))


class TestReturnSeries(unittest.TestCase):
    PRICES = {"AAA": [["2026-10-01", 10.0], ["2026-10-02", 11.0], ["2026-10-03", 11.0], ["2026-10-04", 12.1]],
              "SPY": [["2026-10-01", 100.0], ["2026-10-02", 101.0], ["2026-10-03", 102.0], ["2026-10-04", 103.0]]}

    def run_it(self, rows, drop=()):
        return performance.holdings_return_series(rows, lambda t: self.PRICES.get(t, []), self.PRICES["SPY"], drop_dates=drop)

    def test_a_contribution_is_not_performance(self):
        # day 2: AAA +10% on 100 shares; before day 3 the investor buys 900 more shares (a big deposit) -- value jumps but return must not
        rows = [["2026-10-01", {"AAA": 100}], ["2026-10-02", {"AAA": 100}], ["2026-10-03", {"AAA": 1000}], ["2026-10-04", {"AAA": 1000}]]
        s = self.run_it(rows)["series"]
        self.assertEqual([r["port"] for r in s], [100.0, 110.0, 110.0, 121.0])               # +10%, flat, +10%: the deposit added nothing
        self.assertEqual(s[-1]["spy"], 103.0)

    def test_uncovered_tickers_are_reported_and_skipped(self):
        rows = [["2026-10-01", {"AAA": 100, "CIT": 50}], ["2026-10-02", {"AAA": 100, "CIT": 50}]]
        r = self.run_it(rows)
        self.assertEqual((r["covered_tickers"], r["uncovered_tickers"]), (["AAA"], ["CIT"]))
        self.assertEqual(r["series"][-1]["port"], 110.0)

    def test_dropped_days_and_glitch_days_and_short_input(self):
        rows = [["2026-10-01", {"AAA": 100}], ["2026-10-02", {"AAA": 1}], ["2026-10-03", {"AAA": 100}], ["2026-10-04", {"AAA": 100}]]
        r = self.run_it(rows, drop=["2026-10-02"])
        self.assertEqual([x["date"] for x in r["series"]], ["2026-10-01", "2026-10-03", "2026-10-04"])
        self.PRICES["AAA"][2][1] = 40.0                                                       # a 3.6x one-day "move": bad data, ignored
        try:
            self.assertEqual(self.run_it([["2026-10-02", {"AAA": 100}], ["2026-10-03", {"AAA": 100}]])["glitch_days"], 1)
        finally:
            self.PRICES["AAA"][2][1] = 11.0
        self.assertEqual(self.run_it([["2026-10-01", {"AAA": 1}]])["series"], [])

    def test_share_log_is_kept_for_years_and_the_endpoint_serves_a_cached_build(self):
        src = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
        self.assertIn("washsale.append_snapshot(_read_sharelog(), q, keep_days=3650)", src)
        with mock.patch.object(appmod.store, "read_json_strict", lambda p: {"series": [], "generated_at": 10**10, "coverage_pct": 90}):
            r = appmod.get_portfolio_return()
        self.assertEqual((r["cached"], r["refreshing"]), (True, False))
        with mock.patch.object(appmod.store, "read_json_strict", side_effect=FileNotFoundError()), mock.patch.object(appmod.threading, "Thread", lambda **k: mock.Mock(start=lambda: None)):
            self.assertTrue(appmod.get_portfolio_return()["computing"])


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestChartHelpers(unittest.TestCase):
    def fn(self, name):
        i = HTML.index(f"function {name}(")
        d, k = 0, HTML.index("{", i)
        while True:
            d += HTML[k] == "{"; d -= HTML[k] == "}"; k += 1
            if d == 0: return HTML[i:k]

    def js(self, body):
        src = ("const _isoMinus=(iso,days)=>{ const d=new Date(iso+'T00:00:00Z'); d.setUTCDate(d.getUTCDate()-days); return d.toISOString().slice(0,10); };\n"
               + self.fn("chartWindowRange") + self.fn("sliceRebase") + self.fn("bucketRows") + body)
        r = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_windows(self):
        out = self.js("""const f='2023-10-01', l='2026-10-31';
          console.log(JSON.stringify(['1m','3m','6m','1y','3y','all'].map(w=>chartWindowRange(w,'','',f,l)).concat([chartWindowRange('custom','2026-01-01','2026-02-01',f,l), chartWindowRange('custom','','',f,l),
            chartWindowRange('1y','','','2026-09-01',l), chartWindowRange('custom','2020-01-01','2030-01-01',f,l)])));""")
        self.assertEqual(out[0], ["2026-10-01", "2026-10-31"]); self.assertEqual(out[1], ["2026-08-01", "2026-10-31"])      # 91 days back
        self.assertEqual(out[3], ["2025-10-31", "2026-10-31"]); self.assertEqual(out[5], ["2023-10-01", "2026-10-31"])
        self.assertEqual(out[6], ["2026-01-01", "2026-02-01"]); self.assertEqual(out[7], ["2023-10-01", "2026-10-31"])
        self.assertEqual(out[8], ["2026-09-01", "2026-10-31"])                              # a window longer than the data is clamped to the data
        self.assertEqual(out[9], ["2023-10-01", "2026-10-31"])

    def test_rebase_to_100_at_the_window_start(self):
        out = self.js("""const rows=[{date:'2026-01-01',port:50,spy:200},{date:'2026-01-02',port:55,spy:210},{date:'2026-01-03',port:60,spy:190}];
          console.log(JSON.stringify([sliceRebase(rows,'2026-01-02','2026-01-03',['port','spy']), sliceRebase(rows,'2027-01-01','2027-02-01',['port','spy'])]));""")
        self.assertEqual(out[0], [{"date": "2026-01-02", "port": 100.0, "spy": 100.0}, {"date": "2026-01-03", "port": 109.1, "spy": 90.5}])
        self.assertEqual(out[1], [])

    def test_bucketing_keeps_the_last_point_per_day_week_or_month(self):
        out = self.js("""const mk=(n,start)=>Array.from({length:n},(_,i)=>{const d=new Date(Date.UTC(2026,0,1)+i*86400000).toISOString().slice(0,10); return {date:d,v:i};});
          const short=mk(60), mid=mk(200), long=mk(900);
          const b=(r)=>bucketRows(r, r[0].date, r[r.length-1].date);
          console.log(JSON.stringify([b(short).length, b(mid).length, b(long).length, b(mid)[0].date, b(long).map(x=>x.date.slice(0,7)).filter((m,i,a)=>a.indexOf(m)===i).length]));""")
        self.assertEqual(out[0], 60)                                                           # <= 100 days: daily
        self.assertTrue(26 <= out[1] <= 31)                                                    # ~200 days: weekly
        self.assertEqual(out[2], out[4])                                                       # long: one bar per month


class TestUi(unittest.TestCase):
    def test_wiring(self):
        for needle in ("const CH_WINDOWS=[['1m','1M'],['3m','3M'],['6m','6M'],['1y','1Y'],['3y','3Y'],['all','ALL'],['custom','CUSTOM']]", "function drawPerfCharts()",
                       "PORTFOLIO VALUE vs COST BASIS", "id=\"c-cost\"", "refreshReturnSeries()", "/api/portfolio-return", "W('perf-chart'", "RETURN", "VALUE"):
            self.assertIn(needle, HTML)
        self.assertNotIn("const pts=((VH&&VH.history)||[]).filter(h=>(h.invested_value||h.net_worth)&&h.spy);\n    if(pts.length>=2){", HTML)   # the old inline chart is gone


if __name__ == "__main__":
    unittest.main()
