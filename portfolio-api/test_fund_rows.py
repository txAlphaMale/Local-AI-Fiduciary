# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_fund_rows.py -- TASK-007: fund rows in the HOLDINGS tables show what we know and explain every blank."""
import glob, json, os, re, shutil, subprocess, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="fundrows-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod  # noqa: E402
import edgar  # noqa: E402

HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()


def raw(qty=10, val=100.0, cur="x", sec_close="x", hold_close="x", basis=0):
    s = {"ticker": "BTEKX", "name": "Fund", "typeDisplay": "Mutual Fund"}
    if cur != "x": s["currentPrice"] = cur
    if sec_close != "x": s["closingPrice"] = sec_close
    h = {"quantity": qty, "totalValue": val, "basis": basis, "security": s, "holdings": [{}]}
    if hold_close != "x": h["holdings"] = [{"closingPrice": hold_close}]
    return h


class TestPriceFallback(unittest.TestCase):
    def test_order_current_then_closing_then_derived(self):
        self.assertEqual(appmod._hold_price(raw(cur=12.5, sec_close=11.0)), (12.5, "current"))
        self.assertEqual(appmod._hold_price(raw(cur=None, sec_close=11.0, hold_close=10.0)), (11.0, "closing"))
        self.assertEqual(appmod._hold_price(raw(cur=None, sec_close=None, hold_close=10.0)), (10.0, "closing"))   # UPOZ / SMID shape
        self.assertEqual(appmod._hold_price(raw(cur=None, sec_close=None, hold_close=None, qty=4, val=100.0)), (25.0, "derived"))

    def test_nothing_to_derive_from_stays_unknown(self):
        for q, v in ((0, 100.0), (10, 0), (None, 100.0), (10, None)):
            self.assertEqual(appmod._hold_price(raw(cur=None, sec_close=None, hold_close=None, qty=q, val=v)), (None, None), (q, v))

    def test_hold_carries_price_and_source_and_a_zero_basis_is_not_reported(self):
        h = appmod._hold(raw(cur=None, sec_close=None, hold_close=9.5, qty=10, val=95.0, basis=0))
        self.assertEqual((h["price"], h["price_source"], h["cost_total"], h["gain"]), (9.5, "closing", None, None))


class TestMix(unittest.TestCase):
    def fh(self, rows, total=None, resolved=True):
        return {"resolved": resolved, "holdings": rows, "total_pct": total if total is not None else sum(r["pct"] for r in rows),
                "as_of": "2026-08-31", "series_name": "Test Index Fund"}

    def test_label_from_the_cached_nport_and_scaled_when_over_100(self):
        rows = [{"asset_cat": "EC", "pct": 98.4}, {"asset_cat": "EC", "pct": 0.6}, {"asset_cat": "STIV", "pct": 1.0}]
        with mock.patch.object(edgar, "fund_holdings_cached", lambda t: self.fh(rows)):
            m = appmod._fund_mix("FXAIX")
        self.assertEqual(m["label"], "Equity 99% · Cash & equivalents 1%"); self.assertEqual(m["as_of"], "2026-08-31")
        with mock.patch.object(edgar, "fund_holdings_cached", lambda t: self.fh([{"asset_cat": "EC", "pct": 120.0}, {"asset_cat": "DBT", "pct": 30.0}], total=150.0)):
            m = appmod._fund_mix("X")
        self.assertEqual(m["label"], "Equity 80% · Bonds 20%")

    def test_unresolved_or_missing_cache_gives_none_so_the_table_says_unavailable(self):
        for val in (None, self.fh([], resolved=True), self.fh([{"asset_cat": "EC", "pct": 5}], resolved=False)):
            with mock.patch.object(edgar, "fund_holdings_cached", lambda t, v=val: v):
                self.assertIsNone(appmod._fund_mix("ZZZ"))

    def test_cache_peek_never_touches_the_network(self):
        with mock.patch.object(edgar.store, "ext_cache_get", return_value=(123, {"resolved": True, "holdings": []})), \
             mock.patch.object(edgar.requests, "get", side_effect=AssertionError("network!")), \
             mock.patch.object(edgar.requests, "post", side_effect=AssertionError("network!")):
            self.assertEqual(edgar.fund_holdings_cached("fxaix")["_cache_fetched_at"], 123)
        with mock.patch.object(edgar.store, "ext_cache_get", return_value=None):
            self.assertIsNone(edgar.fund_holdings_cached("FXAIX"))

    def test_exposure_rows_for_funds_carry_mix_and_expense_ratio_and_stocks_do_not(self):
        cons = [{"ticker": "FZAHX", "type": "Mutual Fund", "value": 100.0, "name": "Fid"},
                {"ticker": "AAA", "type": "Stock", "value": 50.0, "name": "Aaa"}]
        with mock.patch.object(appmod, "_data", lambda: {"consolidated": cons}), mock.patch.object(appmod, "_valuation", lambda t, **k: {}), \
             mock.patch.object(appmod, "_profile", lambda t: ({"finnhubIndustry": "Tech"}, {})), \
             mock.patch.object(appmod, "_fund_mix", lambda t: {"label": "Equity 100%", "as_of": None, "series": None, "mix": {}}), \
             mock.patch.object(appmod, "_read_lt", lambda: None):
            ex = appmod._exposure()
        rows = {r["ticker"]: r for r in ex["positions"]}
        self.assertEqual(rows["FZAHX"]["expense_ratio_pct"], 0.60); self.assertEqual(rows["FZAHX"]["fund_mix"]["label"], "Equity 100%")
        self.assertEqual(rows["FZAHX"]["sector"], "Funds (no look-through)")                   # the sector allocation panel is unchanged
        self.assertNotIn("fund_mix", rows["AAA"])


class TestMonarchConnections(unittest.TestCase):
    def build(self, accounts, holdings):
        def post(op, payload):
            if op == "get_accounts": return {"accounts": accounts}
            return {"holdings": {"portfolio": {"aggregateHoldings": {"edges": [{"node": n} for n in holdings]}}}}
        with mock.patch.object(appmod, "_post", post), mock.patch.object(appmod, "_monarch_guard", lambda: None):
            return appmod._build_monarch()

    def acct(self, id, name, inst, update_required=False, holdings=1, **kw):
        a = {"id": id, "displayName": name, "institution": {"name": inst}, "type": {"display": "Investments"}, "displayBalance": 100.0,
             "holdingsCount": holdings, "isAsset": True, "includeBalanceInNetWorth": True, "dataProvider": "PLAID",
             "credential": {"updateRequired": update_required, "dataProvider": "PLAID"}}
        a.update(kw); return a

    def test_reauth_needed_is_reported_per_institution_and_skips_disabled_accounts(self):
        accts = [self.acct("1", "401k", "Principal", True), self.acct("2", "IRA", "Principal", True), self.acct("3", "Brokerage", "Fidelity", False),
                 self.acct("4", "Old", "OldBank", True, deactivatedAt="2026-01-01")]
        d = self.build(accts, [])
        self.assertEqual(d["connections_needing_login"], [{"institution": "Principal", "accounts": 2}])
        self.assertTrue(any(a["needs_reauth"] for a in d["accounts"]))

    def test_positions_get_the_price_fallback_end_to_end(self):
        node = raw(cur=None, sec_close=None, hold_close=9.5, qty=10, val=95.0, basis=0)
        d = self.build([self.acct("1", "401k", "Principal")], [node])
        p = d["positions"][0]
        self.assertEqual((p["price"], p["price_source"], p["cost_total"]), (9.5, "closing", None))
        self.assertEqual(d["consolidated"][0]["institutions"], ["Principal"]); self.assertEqual(d["consolidated"][0]["price_source"], "closing")

    def test_digest_raises_an_attention_item_for_it(self):
        with mock.patch.object(appmod, "_data", lambda: {"connections_needing_login": [{"institution": "Principal", "accounts": 2}]}):
            alerts = appmod._alerts_digest()["alerts"]
        a = [x for x in alerts if x["category"] == "Monarch"]
        self.assertEqual(len(a), 1); self.assertIn("Principal needs you to log in again in Monarch", a[0]["message"])
        self.assertEqual((a[0]["tab"], a[0]["subtab"]), ("settings", "accounts"))


class TestUi(unittest.TestCase):
    def test_wiring(self):
        for needle in ("look-through unavailable", "SECTOR · MIX", "ANALYST · ER", "function noBasisCell", "not reported by ", "n/r",
                       "closing price — the provider has no live price", "derived: value ÷ quantity", "fundInfoMap()"):
            self.assertIn(needle, HTML)
        self.assertNotIn("Funds (no look-through)</td>", HTML)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_cell_helpers_under_node(self):
        def fn(name):
            i = HTML.index(f"function {name}(")
            d, k = 0, HTML.index("{", i)
            while True:
                d += HTML[k] == "{"; d -= HTML[k] == "}"; k += 1
                if d == 0: return HTML[i:k]
        src = ("const attrEsc = x => (x==null?'':String(x)).replace(/\"/g,'&quot;').replace(/</g,'&lt;');\n"
               + "const isViewer=()=>false;\n" + fn("fundSectorCell") + fn("fundAnalystCell") + fn("basisPen") + fn("noBasisCell") + """
        const FI={FXAIX:{fund_mix:{label:'Equity 99% · Cash & equivalents 1%',as_of:'2026-08-31'},expense_ratio_pct:0.015},NOPE:{}};
        console.log(JSON.stringify([fundSectorCell({ticker:'FXAIX'},FI), fundSectorCell({ticker:'NOPE'},FI), fundAnalystCell({ticker:'FXAIX'},FI),
          fundAnalystCell({ticker:'NOPE'},FI), fundSectorCell({ticker:'LATE'},FI), noBasisCell({type:'Mutual Fund',institutions:['Principal','Fidelity']},'monarch'),
          noBasisCell({type:'Cash'},'monarch'), noBasisCell({type:'Stock'},'')]));""")
        r = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        sec_ok, sec_no, an_ok, an_no, late, nb, cash, nb2 = json.loads(r.stdout)
        self.assertIn("Equity 99% · Cash &amp; equivalents 1%".replace("&amp;", "&"), sec_ok); self.assertIn("as of 2026-08-31", sec_ok)
        self.assertIn("look-through unavailable", sec_no); self.assertIn("ER 0.015%", an_ok)         # ratios under 0.1% keep 3 decimals (0.015 must not read 0.01)
        self.assertIn(">n/a<", an_no); self.assertIn("still being computed", late); self.assertIn("not reported by Principal / Fidelity via Monarch", nb)
        self.assertEqual(cash, '<span class="muted">—</span>'); self.assertIn("not reported by your provider", nb2)


if __name__ == "__main__":
    unittest.main()
