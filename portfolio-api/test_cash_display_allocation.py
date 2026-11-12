"""Display allocation: cash-only accounts (savings/checking) are not investments; cash inside an account that also holds securities
is kept as one slice. Engines keep their own by_type / by_institution."""
import glob, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="cashdisp-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod  # noqa: E402

_ROWS = [
    {"ticker": "VTI", "quantity": 10, "cost_basis": 1000, "account": "Brokerage", "institution": "Fidelity", "type": "ETF"},
    {"ticker": "CASH", "quantity": 500, "cost_basis": 500, "account": "Brokerage", "institution": "Fidelity", "type": "Cash"},
    {"ticker": "CASH", "quantity": 20000, "cost_basis": 20000, "account": "Emergency Fund", "institution": "Demo Bank", "type": "Cash"},
    {"ticker": "CASH", "quantity": 3000, "cost_basis": 3000, "account": "Checking", "institution": "Demo Bank", "type": "Cash"},
]


ROWS = [{k: (str(v) if isinstance(v, (int, float)) else v) for k, v in r.items()} for r in _ROWS]


class TestCashDisplay(unittest.TestCase):
    def setUp(self):
        with mock.patch.object(appmod, "_finnhub", return_value={"c": 100.0, "dp": 0.0}):
            accounts, positions, by_account, nw = appmod._csv_raw(ROWS)
            self.d = appmod._derive(accounts, positions, by_account, accounts, nw, nw, 0.0)

    def test_cash_is_split_into_brokerage_and_bank(self):
        self.assertEqual(self.d["brokerage_cash"], 500.0)
        self.assertEqual(self.d["bank_cash"], 23000.0)
        self.assertEqual(self.d["cash_value"], 23500.0)

    def test_display_widgets_drop_bank_cash_and_keep_the_brokerage_cash_slice(self):
        ad = self.d["alloc_display"]
        inst = {r["label"]: r["value"] for r in ad["by_institution"]}
        self.assertEqual(inst, {"Fidelity": 1500.0})                       # no 'Demo Bank' slice
        types = {r["label"]: r["value"] for r in ad["by_type"]}
        self.assertEqual(types, {"ETF": 1000.0, "Cash (in brokerage accounts)": 500.0})
        self.assertEqual(ad["total"], 1500.0); self.assertEqual(ad["type_total"], 1500.0)

    def test_engines_still_see_all_cash(self):
        self.assertEqual({r["label"]: r["value"] for r in self.d["by_type"]}["Cash"], 23500.0)
        self.assertEqual(self.d["invested_value"], 24500.0)

    def test_no_cash_anywhere(self):
        with mock.patch.object(appmod, "_finnhub", return_value={"c": 100.0, "dp": 0.0}):
            a, p, b, nw = appmod._csv_raw(ROWS[:1])
            d = appmod._derive(a, p, b, a, nw, nw, 0.0)
        self.assertEqual((d["brokerage_cash"], d["bank_cash"]), (0.0, 0.0))
        self.assertEqual([r["label"] for r in d["alloc_display"]["by_type"]], ["ETF"])


class TestBrokerageCashAccounts(unittest.TestCase):
    """Monarch lists a brokerage's cash sweep as its OWN account (no holdings); high-confidence ones feed the 'Cash (in brokerage accounts)' slice."""
    def acct(self, name, inst, typ, bal, hc=0, sub=None, **kw):
        return dict({"name": name, "institution": inst, "type": typ, "subtype": sub, "holdings_count": hc, "balance": bal, "include_nw": True, "is_asset": True}, **kw)

    def derive(self, extra=None):
        accts = [self.acct("WELLSTRADE IRA (...6140)", "Wells Fargo", "Investments", 1000.0, hc=1),
                 self.acct("IRA BROKERAGE CASH (...1400)", "Wells Fargo", "Investments", 113.0, sub="Roth IRA"),
                 self.acct("BROKERAGE CASH (...5346)", "Wells Fargo", "Cash", 5.0, sub="Checking", hidden=True),
                 self.acct("WAY2SAVE SAVINGS (...4524)", "Wells Fargo", "Cash", 1862.0, sub="Savings"),
                 self.acct("Health Savings Account (...8095)", "Voya", "Investments", 3418.0, sub="Other"),
                 self.acct("PayPal", "PayPal", "Cash", 8.0, sub="Mobile Payment System"),
                 self.acct("Blue Cash Everyday (...3003)", "American Express", "Credit Card", 300.0, is_asset=False)] + (extra or [])
        pos = [{"ticker": "VTI", "name": "Vanguard Total Market", "type": "ETF", "value": 1000.0, "cost_total": 800.0, "gain": 200.0, "gain_pct": 25.0,
                "quantity": 5.0, "price": 200.0, "day_pct": 0.1, "account": "WELLSTRADE IRA (...6140)", "institution": "Wells Fargo",
                "suspect": False, "is_cit": False, "price_source": None}]
        bo = [a for a in accts if a["holdings_count"] == 0 and a["type"] == "Investments" and a["balance"]]
        return appmod._derive(accts, pos, [{"account": "WELLSTRADE IRA (...6140)", "institution": "Wells Fargo", "value": 1000.0, "holdings": pos}], [accts[0]],
                              6000.0, 6500.0, 300.0, balance_only=bo)

    def test_only_high_confidence_cash_accounts_are_counted(self):
        d = self.derive()
        self.assertEqual(d["brokerage_cash_accounts"], 118.0)                       # 113 (Roth IRA cash sweep) + 5 (hidden brokerage cash)
        self.assertEqual(d["brokerage_cash"], 118.0)
        self.assertEqual(d["bank_cash"], 0.0)                                       # savings/wallet are neither counted nor positions

    def test_the_asset_type_donut_gets_the_cash_slice_and_stays_honest(self):
        t = {r["label"]: r["value"] for r in self.derive()["alloc_display"]["by_type"]}
        self.assertEqual(t, {"ETF": 1000.0, "Cash (in brokerage accounts)": 118.0, "Medical reserve (HSA)": 3418.0})   # the balance-only HSA, plainly named

    def test_institution_donut_counts_each_account_once(self):
        i = {r["label"]: r["value"] for r in self.derive()["alloc_display"]["by_institution"]}
        self.assertEqual(i["Wells Fargo"], 1000.0 + 113.0 + 5.0)                    # 1400 already arrives as a balance-only account; 5346 is added once
        self.assertEqual(i["Voya"], 3418.0)                                         # the unsure HSA keeps its existing treatment
        self.assertNotIn("PayPal", i); self.assertNotIn("American Express", i)

    def test_medium_confidence_and_deactivated_accounts_are_not_counted(self):
        d = self.derive([self.acct("Mystery Cash", "Elsewhere", "Cash", 500.0),                       # unpaired: unsure/low
                         self.acct("Old IRA Cash (...1)", "Wells Fargo", "Cash", 700.0, deactivated=True)])
        self.assertEqual(d["brokerage_cash_accounts"], 118.0)

    def test_engines_see_no_change_in_invested_value_or_by_type(self):
        d = self.derive()
        self.assertEqual(d["invested_value"], 1000.0)
        self.assertEqual({r["label"]: r["value"] for r in d["by_type"]}, {"ETF": 1000.0})

    def test_the_cash_check_report_marks_which_accounts_feed_the_widgets(self):
        import cashdetect as cd
        accts = [self.acct("WELLSTRADE IRA (...6140)", "Wells Fargo", "Investments", 1000.0, hc=1), self.acct("IRA BROKERAGE CASH (...1400)", "Wells Fargo", "Investments", 113.0),
                 self.acct("WAY2SAVE SAVINGS (...4524)", "Wells Fargo", "Cash", 1862.0)]
        rep = cd.report(accts, [{"account": "WELLSTRADE IRA (...6140)", "institution": "Wells Fargo", "ticker": "VTI", "type": "ETF", "value": 1000.0}], 0, 0)
        by = {r["name"]: r for r in rep["accounts"]}
        self.assertTrue(by["IRA BROKERAGE CASH (...1400)"]["counted_in_widgets"])
        self.assertFalse(by["WAY2SAVE SAVINGS (...4524)"]["counted_in_widgets"])
        self.assertIn("counted in the dashboard", " ".join(by["IRA BROKERAGE CASH (...1400)"]["reasons"]))


class TestCashBreakdown(unittest.TestCase):
    """Cash-only investment accounts and money-market FUNDS count as brokerage cash; bank accounts do not."""
    def acct(self, name, inst, typ, bal, hc=0, sub=None):
        return {"name": name, "institution": inst, "type": typ, "subtype": sub, "holdings_count": hc, "balance": bal, "include_nw": True, "is_asset": True}

    def pos(self, acct, inst, tkr, typ, val, price=100.0, name="", day=0.4):
        return {"ticker": tkr, "name": name or tkr, "type": typ, "value": val, "cost_total": val, "gain": 0.0, "gain_pct": 0.0, "quantity": 1.0, "price": price,
                "day_pct": day, "account": acct, "institution": inst, "suspect": False, "is_cit": False, "price_source": None}

    def setUp(self):
        self.accts = [self.acct("Individual Brokerage -2317 (...2317)", "E*TRADE", "Investments", 1292.0, hc=1),
                      self.acct("MC VG IRA (...0439)", "Vanguard", "Investments", 50000.0, hc=2, sub="Roth IRA"),
                      self.acct("WELLSTRADE IRA (...6140)", "Wells Fargo", "Investments", 5000.0, hc=1),
                      self.acct("Emergency Fund", "Ally", "Investments", 22000.0, hc=1)]
        self.pos_ = [self.pos("Individual Brokerage -2317 (...2317)", "E*TRADE", "CASH", "Cash", 1292.0, price=1.0, day=0.0),   # an account that holds ONLY cash for now
                     self.pos("MC VG IRA (...0439)", "Vanguard", "VFIAX", "Mutual Fund", 40000.0, price=480.0, name="Vanguard 500 Index"),
                     self.pos("MC VG IRA (...0439)", "Vanguard", "VMFXX", "Mutual Fund", 10000.0, price=1.0, name="Vanguard Federal Money Market Fund", day=0.0),
                     self.pos("WELLSTRADE IRA (...6140)", "Wells Fargo", "VTI", "ETF", 5000.0, price=250.0),
                     self.pos("Emergency Fund", "Ally", "CASH", "Cash", 22000.0, price=1.0, day=0.0)]

    def test_classification(self):
        import cashdetect as cd
        b = cd.cash_breakdown(self.accts, self.pos_)
        self.assertEqual([x["account"] for x in b["brokerage_lines"]], ["Individual Brokerage -2317 (...2317)"])
        self.assertEqual([x["account"] for x in b["bank_lines"]], ["Emergency Fund"])
        self.assertEqual([x["ticker"] for x in b["equivalent_lines"]], ["VMFXX"])
        self.assertEqual(b["brokerage_cash"], 1292.0 + 10000.0); self.assertEqual(b["bank_cash"], 22000.0)

    def test_the_donuts_move_the_money_market_fund_from_mutual_fund_to_cash_and_drop_bank_cash(self):
        d = appmod._derive(self.accts, self.pos_, [], self.accts, 100000.0, 100000.0, 0.0)
        t = {r["label"]: r["value"] for r in d["alloc_display"]["by_type"]}
        self.assertEqual(t, {"Mutual Fund": 40000.0, "ETF": 5000.0, "Cash (in brokerage accounts)": 11292.0})
        self.assertEqual((d["brokerage_cash"], d["bank_cash"], d["cash_equiv_value"], d["cash_equiv_tickers"]), (11292.0, 22000.0, 10000.0, ["VMFXX"]))
        i = {r["label"]: r["value"] for r in d["alloc_display"]["by_institution"]}
        self.assertNotIn("Ally", i); self.assertEqual(i["Vanguard"], 50000.0)       # the fund stays in its institution's slice

    def test_engines_still_see_the_original_types(self):
        d = appmod._derive(self.accts, self.pos_, [], self.accts, 100000.0, 100000.0, 0.0)
        bt = {r["label"]: r["value"] for r in d["by_type"]}
        self.assertEqual(bt["Mutual Fund"], 50000.0); self.assertEqual(bt["Cash"], 23292.0)

    def test_report_lists_exactly_what_the_widgets_use(self):
        import cashdetect as cd
        w = cd.report(self.accts, self.pos_, 0, 0)["widget_cash"]
        self.assertEqual(w["brokerage_cash"], 11292.0); self.assertEqual({x["ticker"] for x in w["equivalent_lines"]}, {"VMFXX"})


if __name__ == "__main__":
    unittest.main()
