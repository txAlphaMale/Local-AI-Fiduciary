# SPDX-License-Identifier: AGPL-3.0-or-later
"""Demo data refresh (2026-11-09): synthetic history, liabilities, manual cost basis, planning inputs; daily reset timing."""
import datetime
import glob
import os
import re
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="demo-seed-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod  # noqa: E402
import demo_seed  # noqa: E402
import store  # noqa: E402


class TestHistory(unittest.TestCase):
    def test_two_years_of_daily_rows_ending_yesterday_and_landing_on_target(self):
        today = datetime.date(2026, 11, 9)
        rows = demo_seed.history_rows(today)
        self.assertEqual(len(rows), demo_seed.HISTORY_DAYS)
        self.assertEqual(rows[-1]["date"], "2026-11-08")
        self.assertEqual(rows[0]["date"], (today - datetime.timedelta(days=demo_seed.HISTORY_DAYS)).isoformat())
        self.assertAlmostEqual(rows[-1]["net_worth"], demo_seed.HISTORY_END_NET_WORTH, delta=1.0)
        self.assertEqual(len({r["date"] for r in rows}), len(rows))
        for r in rows:
            self.assertGreater(r["net_worth"], 0)
            self.assertGreater(r["invested_value"], 0)
            self.assertGreater(r["spy"], 0)
            self.assertGreater(r["cost_covered_value"], 0)

    def test_is_deterministic(self):
        t = datetime.date(2026, 11, 9)
        self.assertEqual(demo_seed.history_rows(t), demo_seed.history_rows(t))

    def test_it_grows_and_has_realistic_wiggle(self):
        rows = demo_seed.history_rows(datetime.date(2026, 11, 9))
        self.assertGreater(rows[-1]["net_worth"], rows[0]["net_worth"] * 1.3)
        ups = sum(1 for a, b in zip(rows, rows[1:]) if b["net_worth"] > a["net_worth"])
        self.assertTrue(0.4 < ups / (len(rows) - 1) < 0.7)


_SEED_SCRIPT = r"""
import datetime, glob, json, os, re, sys, tempfile
here = sys.argv[1]
tmp = tempfile.mkdtemp(prefix="demo-seed-iso-")
for f in glob.glob(os.path.join(here, "*.py")):
    if os.path.basename(f).startswith("test_"):
        continue
    for m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(f).read()):
        os.environ[m.group(1)] = os.path.join(tmp, m.group(2)[len("/data"):].lstrip("/"))   # ASSIGN: the parent test process already exported its own paths
os.environ["INTERNAL_SCHEDULER"] = "0"
sys.path.insert(0, here)
import app as appmod, demo_seed, store
res = demo_seed._seed_all(appmod, today=datetime.date(2026, 11, 9))
rows = store.value_history_read_all()
liab = {r["name"]: r for r in appmod._read_manual_liability_rows()}
ents = appmod._read_basis_entries()
ins = appmod._read_insurance_cfg()
ret = appmod._read_retire()
print(json.dumps({"res": res, "history": len(rows), "liab": {k: [v["balance"], v["rate_pct"], v["pay_in_full"]] for k, v in liab.items()},
                  "basis": [[e["institution"], e["account"], e["ticker"], e["avg_cost"]] for e in ents],
                  "retire": [ret.get("current_age"), ret.get("target_age")], "ins_income": ins.get("income_self"),
                  "policy_kinds": sorted({p["kind"] for p in ins.get("policies", [])}), "ef": ins.get("ef_accounts"),
                  "rates_kept": sorted((ins.get("debt_rates") or {}).keys())}))
"""


class TestSeedAll(unittest.TestCase):
    """Runs in its OWN process with its own data directory: seeding writes history, debts and plan inputs, which must never
    leak into the other tests that share this process's store."""
    @classmethod
    def setUpClass(cls):
        import json
        import subprocess
        out = subprocess.run([sys.executable, "-c", _SEED_SCRIPT, _HERE], capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr[-2000:]
        cls.r = json.loads(out.stdout.strip().splitlines()[-1])

    def test_history_is_stored(self):
        self.assertEqual(self.r["history"], demo_seed.HISTORY_DAYS)
        self.assertEqual(self.r["res"]["history_days"], demo_seed.HISTORY_DAYS)

    def test_liabilities_are_stored_with_rates(self):
        liab = self.r["liab"]
        self.assertEqual(set(liab), {"Demo Visa", "Demo Auto Loan", "Demo Student Loan"})
        self.assertEqual(liab["Demo Auto Loan"][1], 5.1)
        self.assertTrue(liab["Demo Visa"][2])
        self.assertEqual(round(sum(v[0] for v in liab.values()), 2), demo_seed.DEMO_LIABILITIES_TOTAL)

    def test_manual_cost_basis_entry_matches_the_no_basis_holding(self):
        self.assertEqual(len(self.r["basis"]), 1)
        inst, acct, tkr, avg = self.r["basis"][0]
        row = next(h for h in demo_seed.SYNTHETIC_HOLDINGS if h["ticker"] == tkr and h["account"] == acct)
        self.assertEqual(row["cost_basis"], "")                       # no provider basis, so the entry applies
        self.assertEqual((inst, acct), (row["institution"], row["account"]))
        other = next(h for h in demo_seed.SYNTHETIC_HOLDINGS if h["ticker"] == "FXAIX" and h["account"] == "Roth IRA")
        self.assertEqual(other["cost_basis"], "")                     # second line stays unentered: "known for 1 of 2"

    def test_planning_inputs_are_stored(self):
        self.assertEqual(self.r["retire"], [41, 60])
        self.assertEqual(self.r["ins_income"], 142000.0)
        self.assertEqual(self.r["policy_kinds"], ["disability", "life", "umbrella"])
        self.assertEqual(self.r["ef"], {"Emergency Fund": {"state": "yes"}})
        self.assertIn("Demo Auto Loan", self.r["rates_kept"])         # the liabilities' rates survived the planning write

    def test_no_vault_documents_are_seeded(self):
        body = open(os.path.join(_HERE, "demo_seed.py")).read().split("def _seed_all(")[1].split("def _extras_current")[0].lower()
        self.assertNotIn("vault", body)


_ANCHOR_SCRIPT = r"""
import datetime, glob, json, os, re, sys, tempfile
here = sys.argv[1]
tmp = tempfile.mkdtemp(prefix="demo-anchor-iso-")
for f in glob.glob(os.path.join(here, "*.py")):
    if os.path.basename(f).startswith("test_"):
        continue
    for m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(f).read()):
        os.environ[m.group(1)] = os.path.join(tmp, m.group(2)[len("/data"):].lstrip("/"))
os.environ["INTERNAL_SCHEDULER"] = "0"
sys.path.insert(0, here)
import app as appmod, demo_seed, store
demo_seed.DEMO_MODE = True
demo_seed.EXTRAS_MARKER_FILE = os.path.join(tmp, "extras")
demo_seed.ANCHOR_MARKER_FILE = os.path.join(tmp, "anchored")
open(demo_seed.EXTRAS_MARKER_FILE, "w").write(demo_seed.EXTRAS_VERSION + "\n")
today = datetime.date(2026, 11, 9)
demo_seed._seed_history(today)
before = store.value_history_read_all()
first1, last1 = dict(before[0]), dict(before[-1])
did1 = demo_seed.anchor_history("2026-11-09", 250000.0, 280000.0, 700.0, 150000.0, 260000.0)
after = store.value_history_read_all()
first2, last2 = dict(after[0]), dict(after[-1])
did2 = demo_seed.anchor_history("2026-11-09", 1.0, 1.0, 1.0, 1.0, 1.0)        # second call: marker present, no change
again = store.value_history_read_all()[-1]
os.remove(demo_seed.ANCHOR_MARKER_FILE)                                          # a reset re-arms it
did3 = demo_seed.anchor_history("2026-11-09", 500000.0, None, 700.0, 150000.0, 260000.0)   # None is skipped, not crashed on
final = store.value_history_read_all()[-1]
print(json.dumps({"did": [did1, did2, did3], "rows": [len(before), len(after)], "last_after": last2,
                  "ratio_first": [first2["net_worth"] / first1["net_worth"], last2["net_worth"] / last1["net_worth"]],
                  "unchanged_on_second": again == last2, "final_nw": final["net_worth"], "final_inv_same": final["invested_value"] == last2["invested_value"]}))
"""


class TestAnchor(unittest.TestCase):
    """The synthetic history must join the first REAL snapshot exactly, whatever prices the demo sees (isolated process)."""
    @classmethod
    def setUpClass(cls):
        import json
        import subprocess
        out = subprocess.run([sys.executable, "-c", _ANCHOR_SCRIPT, _HERE], capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr[-2000:]
        cls.r = json.loads(out.stdout.strip().splitlines()[-1])

    def test_last_synthetic_day_equals_the_real_snapshot(self):
        la = self.r["last_after"]
        self.assertAlmostEqual(la["net_worth"], 250000.0, delta=0.05)
        self.assertAlmostEqual(la["invested_value"], 280000.0, delta=0.05)
        self.assertAlmostEqual(la["spy"], 700.0, delta=0.05)
        self.assertAlmostEqual(la["total_cost"], 150000.0, delta=0.05)
        self.assertAlmostEqual(la["cost_covered_value"], 260000.0, delta=0.05)

    def test_it_rescales_the_whole_series_by_one_factor_so_the_shape_is_kept(self):
        a, b = self.r["ratio_first"]
        self.assertAlmostEqual(a, b, delta=0.002)
        self.assertEqual(self.r["rows"][0], self.r["rows"][1])          # no rows added or lost

    def test_runs_once_per_seed_and_survives_missing_values(self):
        self.assertEqual(self.r["did"], [True, False, True])
        self.assertTrue(self.r["unchanged_on_second"])
        self.assertAlmostEqual(self.r["final_nw"], 500000.0, delta=0.05)  # net worth re-anchored
        self.assertTrue(self.r["final_inv_same"])                          # the None invested value was skipped, not zeroed


class TestCostBasisTotals(unittest.TestCase):
    def test_cost_basis_is_the_total_cost_of_the_line_not_per_share(self):
        expect = {("VTI", "Brokerage"): 25200, ("VXUS", "Brokerage"): 11600, ("BND", "Brokerage"): 10800, ("AAPL", "Brokerage"): 6000,
                  ("MSFT", "Brokerage"): 7000, ("VOO", "Roth IRA"): 22800, ("QQQ", "Roth IRA"): 9900, ("VTI", "401(k)"): 15600,
                  ("VBTLX", "401(k)"): 5100, ("SCHD", "HSA"): 6120}
        got = {(h["ticker"], h["account"]): h["cost_basis"] for h in demo_seed.SYNTHETIC_HOLDINGS if h["cost_basis"] not in ("", None) and h["type"] != "Cash"}
        self.assertEqual(got, expect)
        for h in demo_seed.SYNTHETIC_HOLDINGS:
            if h["type"] != "Cash" and h["cost_basis"] not in ("", None):
                self.assertGreater(h["cost_basis"] / h["quantity"], 5)          # per-share values here would be tiny totals


class TestStaleReleaseStartsFresh(unittest.TestCase):
    def setUp(self):
        self.saved = {k: getattr(demo_seed, k) for k in ("DEMO_MODE", "_APP", "_FRESH", "DEMO_MARKER_FILE", "EXTRAS_MARKER_FILE", "reset_and_reseed", "_seed_all")}
        demo_seed.DEMO_MODE = True
        demo_seed._APP = object()
        demo_seed.DEMO_MARKER_FILE = os.path.join(_TMP, "stale-demo-marker")
        demo_seed.EXTRAS_MARKER_FILE = os.path.join(_TMP, "stale-extras-marker")
        open(demo_seed.DEMO_MARKER_FILE, "w").write("x")
        if os.path.exists(demo_seed.EXTRAS_MARKER_FILE):
            os.remove(demo_seed.EXTRAS_MARKER_FILE)
        self.calls = []

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(demo_seed, k, v)
        for f in (os.path.join(_TMP, "stale-demo-marker"), os.path.join(_TMP, "stale-extras-marker")):
            if os.path.exists(f):
                os.remove(f)

    def test_an_older_release_demo_is_reset_not_layered(self):
        demo_seed._FRESH = False
        demo_seed.reset_and_reseed = lambda: self.calls.append("reset") or {"extras": {"seeded": True}}
        demo_seed._seed_all = lambda app: self.calls.append("seed") or {}
        self.assertEqual(demo_seed.seed_extras(), {"seeded": True})
        self.assertEqual(self.calls, ["reset"])                     # reset_and_reseed seeds the extras itself

    def test_a_freshly_created_demo_just_seeds_the_extras(self):
        demo_seed._FRESH = True
        demo_seed.reset_and_reseed = lambda: self.calls.append("reset") or {}
        demo_seed._seed_all = lambda app: self.calls.append("seed") or {"history_days": 1, "liabilities": 0}
        res = demo_seed.seed_extras()
        self.assertTrue(res["seeded"])
        self.assertEqual(self.calls, ["seed"])


class TestHoldings(unittest.TestCase):
    def test_csv_rows_parse_with_blank_basis_and_cash(self):
        import io
        import csv
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=demo_seed.CSV_FIELDS)
        w.writeheader()
        for r in demo_seed.SYNTHETIC_HOLDINGS:
            w.writerow(r)
        rows = list(csv.DictReader(io.StringIO(buf.getvalue())))
        accounts, positions, by_account, nw = appmod._csv_raw(rows)
        names = {a["name"] for a in accounts}
        self.assertTrue({"Emergency Fund", "Checking", "401(k)", "Roth IRA", "Brokerage", "HSA"} <= names)
        cash = [p for p in positions if p["type"] == "Cash"]
        self.assertEqual(sum(p["value"] for p in cash), 22000 + 6400)
        nobasis = [p for p in positions if p["ticker"] == "FXAIX"]
        self.assertEqual(len(nobasis), 2)
        self.assertTrue(all(p["cost_total"] is None for p in nobasis))


class TestDailyReset(unittest.TestCase):
    def test_default_interval_is_daily(self):
        self.assertEqual(demo_seed.DEMO_RESET_INTERVAL_S, 86400)

    def test_seconds_until_reset_counts_from_the_last_seed_not_from_boot(self):
        import time
        marker = os.path.join(_TMP, "marker-test")
        old = demo_seed.DEMO_MARKER_FILE
        demo_seed.DEMO_MARKER_FILE = marker
        try:
            open(marker, "w").write("x")
            os.utime(marker, (time.time() - 20 * 3600, time.time() - 20 * 3600))      # seeded 20 h ago
            self.assertTrue(3 * 3600 < demo_seed.seconds_until_reset(86400) < 4.2 * 3600)
            os.utime(marker, (time.time() - 30 * 3600, time.time() - 30 * 3600))      # overdue: the minimum wait only
            self.assertEqual(demo_seed.seconds_until_reset(86400), 60)
        finally:
            demo_seed.DEMO_MARKER_FILE = old
            if os.path.exists(marker):
                os.remove(marker)

    def test_extras_refuse_without_demo_mode_or_marker(self):
        res = demo_seed.seed_extras()
        self.assertFalse(res["seeded"])


if __name__ == "__main__":
    unittest.main()
