# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_manual_merge.py -- manual HOLDINGS list used ALONGSIDE a live provider (decision 2026-10-22)."""
import glob, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="manualmerge-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod  # noqa: E402

HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()


def pos(t, val, acct, inst, typ="Stock", cost=None):
    return {"ticker": t, "name": t, "type": typ, "quantity": 1.0, "value": val, "cost_total": cost, "price": val, "day_pct": None,
            "gain": (val - cost) if cost is not None else None, "gain_pct": None, "suspect": cost is None, "account": acct, "institution": inst}


def live(extra_positions=None):
    """A derived portfolio like SnapTrade/Plaid/Monarch produce: one brokerage account with AAA 1000 + BBB 500, plus a loan."""
    accts = [{"id": "snaptrade:1", "name": "Individual (...1234)", "institution": "Robinhood", "type": "Investments", "balance": 1500.0,
              "holdings_count": 2, "is_asset": True, "include_nw": True},
             {"id": "loan", "name": "Car loan", "institution": "Bank", "type": "Auto Loan", "balance": 5000.0, "holdings_count": 0,
              "is_asset": False, "include_nw": True}]
    ps = [pos("AAA", 1000.0, "Individual (...1234)", "Robinhood", cost=800.0), pos("BBB", 500.0, "Individual (...1234)", "Robinhood", cost=400.0)]
    by = [{"account": "Individual (...1234)", "institution": "Robinhood", "value": 1500.0, "balance": 1500.0, "holdings": ps}]
    d = appmod._derive(accts, ps, by, [accts[0]], 1500.0 - 5000.0, 1500.0, 5000.0)
    d["snaptrade_mode"] = "personal"
    return d


ROWS = [{"ticker": "AAA", "name": "Aaa", "quantity": "2", "cost_basis": "100", "account": "Old 401k", "institution": "Fidelity", "type": "Stock"},
        {"ticker": "CCC", "name": "Ccc", "quantity": "10", "cost_basis": "", "account": "Brokerage", "institution": "", "type": "Stock"}]


def merged(rows=ROWS, base=None, price=10.0):
    with mock.patch.object(appmod, "_read_holdings_csv_rows", lambda: rows), \
         mock.patch.object(appmod, "_finnhub", lambda kind, t: {"c": price, "dp": 1.0}):
        return appmod._merge_manual_holdings(base or live())


class TestMerge(unittest.TestCase):
    def test_totals_consolidation_and_provider_extras(self):
        d = merged()
        manual_value = 2 * 10.0 + 10 * 10.0                                      # AAA 2 @10, CCC 10 @10
        self.assertEqual(d["manual_holdings"]["value"], manual_value)
        self.assertEqual(d["total_assets"], 1500.0 + manual_value)
        self.assertEqual(d["net_worth"], (1500.0 - 5000.0) + manual_value)
        self.assertEqual(d["invested_value"], 1500.0 + manual_value)
        cons = {c["ticker"]: c for c in d["consolidated"]}
        self.assertEqual(cons["AAA"]["value"], 1000.0 + 20.0)                    # the same ticker in both sources consolidates
        self.assertEqual(sorted(cons["AAA"]["accounts"]), ["Individual (...1234)", "Old 401k"])
        self.assertIn("CCC", cons); self.assertEqual(d["snaptrade_mode"], "personal")      # provider extras survive
        self.assertEqual(d["position_count"], 4); self.assertEqual(d["investment_account_count"], 1 + 2)
        self.assertEqual(round(sum(c["weight_pct"] for c in d["consolidated"])), 100)

    def test_manual_accounts_are_flagged_and_given_an_institution(self):
        d = merged()
        man = {a["name"]: a for a in d["accounts"] if a.get("manual")}
        self.assertEqual(set(man), {"Old 401k", "Brokerage"})
        self.assertEqual(man["Brokerage"]["institution"], "Manual")              # blank institution is labelled, not "?"
        self.assertTrue(all(p.get("manual") for p in d["positions"] if p["account"] in man))
        self.assertTrue(any(b.get("manual") for b in d["by_account"]))

    def test_name_collision_with_a_live_account_is_disambiguated(self):
        rows = [{"ticker": "ZZZ", "name": "", "quantity": "1", "cost_basis": "", "account": "individual (...1234)", "institution": "Fidelity", "type": "Stock"}]
        d = merged(rows)
        names = [a["name"] for a in d["accounts"]]
        self.assertIn("Individual (...1234)", names); self.assertIn("individual (...1234) (manual)", names)

    def test_possible_double_counting_is_reported(self):
        rows = [{"ticker": "AAA", "name": "", "quantity": "1", "cost_basis": "", "account": "Mine", "institution": "Robinhood Markets", "type": "Stock"},
                {"ticker": "BBB", "name": "", "quantity": "1", "cost_basis": "", "account": "Other", "institution": "Vanguard", "type": "Stock"}]
        self.assertEqual(merged(rows)["manual_holdings"]["possible_overlap"], ["Robinhood Markets"])
        self.assertEqual(merged()["manual_holdings"]["possible_overlap"], [])

    def test_empty_list_and_failures_leave_the_live_portfolio_intact(self):
        base = live(); before = (base["total_assets"], base["position_count"])
        d = merged([], base)
        self.assertEqual((d["total_assets"], d["position_count"]), before); self.assertEqual(d["manual_holdings"]["positions"], 0)
        base = live()
        with mock.patch.object(appmod, "_read_holdings_csv_rows", side_effect=OSError("boom")):
            d = appmod._merge_manual_holdings(base)
        self.assertEqual(d["total_assets"], before[0]); self.assertIn("OSError", d["manual_holdings"]["error"])

    def test_liabilities_overlay_still_applies_after_the_merge(self):
        d = merged()
        with mock.patch.object(appmod, "_read_manual_liability_rows", lambda: [{"name": "Visa", "balance": 1000, "institution": "Bank", "type": "Credit Card"}]):
            d2 = appmod._apply_manual_liabilities(d)
        self.assertEqual(d2["total_liabilities"], 6000.0)


class TestWiringAndSetting(unittest.TestCase):
    def test_csv_provider_output_is_unchanged_by_the_refactor(self):
        with mock.patch.object(appmod, "_finnhub", lambda kind, t: {"c": 10.0, "dp": 0.0}):
            accounts, positions, by_account, nw = appmod._csv_raw(ROWS)
        self.assertEqual(nw, 120.0); self.assertEqual(len(accounts), 2); self.assertFalse(any(a.get("manual") for a in accounts))

    def test_build_merges_only_for_a_live_provider_and_only_when_enabled(self):
        src = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
        self.assertIn('if p != "csv" and _include_manual():\n        d = _merge_manual_holdings(d)\n    d = _apply_manual_liabilities(d)', src)
        with mock.patch.object(appmod, "_read_settings", lambda: {}):
            self.assertFalse(appmod._include_manual())                              # default OFF: existing installs are unchanged
        with mock.patch.object(appmod, "_read_settings", lambda: {"INCLUDE_MANUAL_HOLDINGS": True}):
            self.assertTrue(appmod._include_manual())

    def test_build_end_to_end_with_a_fake_live_provider(self):
        def run(enabled):
            with mock.patch.dict(os.environ, {"PORTFOLIO_PROVIDER": "snaptrade"}), mock.patch.object(appmod, "_build_snaptrade", lambda: live()), \
                 mock.patch.object(appmod, "_include_manual", lambda: enabled), mock.patch.object(appmod, "_read_holdings_csv_rows", lambda: ROWS), \
                 mock.patch.object(appmod, "_finnhub", lambda kind, t: {"c": 10.0, "dp": 0.0}), mock.patch.object(appmod, "_read_manual_liability_rows", lambda: []), \
                 mock.patch.object(appmod, "_snapshot_value", lambda d: None), mock.patch.object(appmod, "_snapshot_shares", lambda d: None), \
                 mock.patch.object(appmod.notifications, "record_integration_result", lambda *a, **k: None):
                return appmod.build()
        off, on = run(False), run(True)
        self.assertNotIn("manual_holdings", off); self.assertEqual(off["total_assets"], 1500.0)          # default: nothing changes
        self.assertEqual(on["total_assets"], 1620.0); self.assertEqual(on["portfolio_provider"], "snaptrade")
        self.assertEqual(on["manual_holdings"]["positions"], 2)

    def test_toggle_endpoint_persists_and_clears_the_cache(self):
        import asyncio
        saved = {}
        class FR:
            def __init__(s, b): s._b = b; s.session = {}; s.headers = {}; s.client = None
            async def json(s): return s._b
        appmod._cache["data"] = {"x": 1}
        with mock.patch.object(appmod, "_require_admin", lambda r: {"id": 1, "username": "a"}), mock.patch.object(appmod, "_read_settings", lambda: dict(saved)), \
             mock.patch.object(appmod.store, "write_json", lambda p, d: saved.update(d)), mock.patch.object(appmod.audit, "emit", lambda *a, **k: None):
            r = asyncio.run(appmod.post_provider(FR({"include_manual": True})))
        self.assertEqual((r["include_manual"], saved["INCLUDE_MANUAL_HOLDINGS"], appmod._cache["data"]), (True, True, None))

    def test_ui(self):
        for needle in ("Also include my manual HOLDINGS list", "include_manual:cb.checked", "Possible double counting", "This manual list is INCLUDED in the dashboard alongside"):
            self.assertIn(needle, HTML)


if __name__ == "__main__":
    unittest.main()
