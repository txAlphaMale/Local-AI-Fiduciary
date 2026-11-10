"""CASH CHECK diagnostic: guesses (with reasons) about brokerage cash vs bank cash, modelled on real naming patterns. Read-only."""
import glob, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="cashdetect-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import cashdetect as cd  # noqa: E402

HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()


def acct(name, inst, typ="Investments", hc=0, bal=0, sub=None):
    return {"name": name, "institution": inst, "type": typ, "subtype": sub, "holdings_count": hc, "balance": bal}


def pos(acct_name, inst, tkr, typ, val, price=100.0, name="", day=0.5):
    return {"account": acct_name, "institution": inst, "ticker": tkr, "type": typ, "value": val, "price": price, "name": name, "day_pct": day}


class TestStem(unittest.TestCase):
    def test_masks_prefixes_and_cash_words_are_removed(self):
        self.assertEqual(cd.stem("WF IRA Cash - XX2053"), "wf ira")
        self.assertEqual(cd.stem("MC WF IRA Roth Cash - XX3595"), "wf ira roth")
        self.assertEqual(cd.stem("WF Investing Cash - XX5346"), "wf")
        self.assertEqual(cd.stem("WF Investment - XX9571"), "wf")
        self.assertEqual(cd.stem("Voya HSA Cash"), "voya hsa")


class TestAccounts(unittest.TestCase):
    ACCTS = [acct("WF IRA - XX6140", "Wells Fargo", hc=5, bal=236157), acct("WF IRA Cash - XX2053", "Wells Fargo", "Cash", 0, 4),
             acct("WF IRA Roth - XX3916", "Wells Fargo", hc=3, bal=85545), acct("MC WF IRA Roth Cash - XX3595", "Wells Fargo", "Cash", 0, 0.5),
             acct("WF Investment - XX9571", "Wells Fargo", hc=2, bal=17316), acct("WF Investing Cash - XX5346", "Wells Fargo", "Cash", 0, 5),
             acct("Voya HSA", "Voya", hc=1, bal=2769), acct("Voya HSA Cash", "Voya", "Cash", 0, 1278),
             acct("Ally Savings", "Ally", "Cash", 0, 20000), acct("Mystery Cash", "Somewhere", "Cash", 0, 90)]
    POS = [pos("WF IRA - XX6140", "Wells Fargo", "VTI", "ETF", 1000), pos("WF IRA Roth - XX3916", "Wells Fargo", "VTI", "ETF", 500),
           pos("WF Investment - XX9571", "Wells Fargo", "AAPL", "Stock", 500), pos("Voya HSA", "Voya", "VFIAX", "Mutual Fund", 900)]

    def by(self):
        return {a["name"]: a for a in cd.classify_accounts(self.ACCTS, self.POS)}

    def test_wells_fargo_style_cash_accounts_pair_with_their_brokerage(self):
        b = self.by()
        for n, p in (("WF IRA Cash - XX2053", "WF IRA - XX6140"), ("MC WF IRA Roth Cash - XX3595", "WF IRA Roth - XX3916"),
                     ("WF Investing Cash - XX5346", "WF Investment - XX9571"), ("Voya HSA Cash", "Voya HSA")):
            self.assertEqual((b[n]["verdict"], b[n]["paired_with"]), ("brokerage_cash", p), n)
            self.assertEqual(b[n]["confidence"], "high")

    def test_investment_accounts_are_not_cash(self):
        b = self.by()
        self.assertEqual(b["WF IRA - XX6140"]["verdict"], "not_cash")
        self.assertEqual(b["Voya HSA"]["verdict"], "not_cash")

    def test_a_savings_account_is_bank_cash_and_an_unpaired_cash_account_is_unsure(self):
        b = self.by()
        self.assertEqual(b["Ally Savings"]["verdict"], "bank_cash")
        self.assertEqual((b["Mystery Cash"]["verdict"], b["Mystery Cash"]["confidence"]), ("unsure", "low"))

    def test_pairing_does_not_cross_institutions(self):
        a = [acct("Cash - XX1", "Bank A", "Cash", 0, 5), acct("Brokerage", "Bank B", hc=2, bal=100)]
        self.assertEqual(cd.classify_accounts(a, [])[0]["verdict"], "unsure")

    def test_provider_type_and_subtype_are_used(self):
        a = [acct("Core", "Fidelity", "Investments", 0, 3, sub="Cash Management"), acct("Brokerage", "Fidelity", hc=1, bal=10)]
        r = cd.classify_accounts(a, [])
        self.assertIn("provider type/subtype", " ".join(r[0]["reasons"]))


class TestHoldings(unittest.TestCase):
    def test_money_market_ticker_is_brokerage_cash_even_when_typed_as_a_fund(self):
        c = cd.classify_holding(pos("MC VG IRA", "Vanguard", "VMFXX", "Mutual Fund", 5000, price=1.0, name="Vanguard Federal Money Market Fund", day=0.0), True)
        self.assertEqual((c["verdict"], c["confidence"]), ("brokerage_cash", "high"))
        self.assertTrue(len(c["reasons"]) >= 2)

    def test_name_only_and_nav_only_are_weaker(self):
        by_name = cd.classify_holding(pos("A", "X", "ZZZXX", "Mutual Fund", 10, price=1.0, name="Acme Government Money Market"), True)
        self.assertEqual(by_name["verdict"], "brokerage_cash")
        by_nav = cd.classify_holding(pos("A", "X", "QQQXX", "Mutual Fund", 10, price=1.0, name="Acme Stable Fund", day=0.0), True)
        self.assertEqual((by_nav["verdict"], by_nav["confidence"]), ("brokerage_cash", "low"))

    def test_a_normal_fund_or_stock_is_an_investment(self):
        for p in (pos("A", "X", "VFIAX", "Mutual Fund", 5000, price=480.0, name="Vanguard 500 Index"), pos("A", "X", "AAPL", "Stock", 100, price=190.0)):
            self.assertEqual(cd.classify_holding(p, True)["verdict"], "investment")

    def test_typed_cash_depends_on_the_account(self):
        self.assertEqual(cd.classify_holding(pos("A", "X", "CASH", "Cash", 5, price=1.0), True)["verdict"], "brokerage_cash")
        self.assertEqual(cd.classify_holding(pos("A", "X", "CASH", "Cash", 5, price=1.0), False)["verdict"], "bank_cash")


class TestReport(unittest.TestCase):
    def test_roll_up_counts_holding_cash_and_cashless_account_balances(self):
        accts = TestAccounts.ACCTS + [acct("MC VG IRA", "Vanguard", hc=2, bal=45000)]
        poss = TestAccounts.POS + [pos("MC VG IRA", "Vanguard", "VFIAX", "Mutual Fund", 40000, price=480.0),
                                   pos("MC VG IRA", "Vanguard", "VMFXX", "Mutual Fund", 5000, price=1.0, name="Money Market", day=0.0)]
        r = cd.report(accts, poss, 0.0, 0.0)
        self.assertEqual(r["guessed_totals"]["brokerage_cash"], round(5000 + 4 + 0.5 + 5 + 1278, 2))
        self.assertEqual(r["guessed_totals"]["bank_cash"], 20000.0)
        self.assertEqual(r["guessed_totals"]["unsure"], 90.0)
        self.assertEqual([h["ticker"] for h in r["cash_holdings"]], ["VMFXX"])


class TestWiring(unittest.TestCase):
    def test_endpoint_is_admin_only_and_read_only(self):
        import app as appmod
        with mock.patch.object(appmod, "_require_admin", return_value=None):
            self.assertEqual(appmod.get_cash_check(mock.Mock()).status_code, 403)
        fake = {"accounts": TestAccounts.ACCTS, "positions": TestAccounts.POS, "brokerage_cash": 0.0, "bank_cash": 0.0}
        with mock.patch.object(appmod, "_require_admin", return_value={"id": 1}), mock.patch.object(appmod, "_data", return_value=fake):
            r = appmod.get_cash_check(mock.Mock())
        self.assertIn("guessed_totals", r); self.assertTrue(r["accounts"])
        src = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read().split("def get_cash_check(")[1].split('@app.get("/api/benefits/limits")')[0]
        for bad in ("store.", "write", "atomic_write", ".update(d"):
            self.assertNotIn(bad, src)

    def test_provider_type_and_subtype_names_are_captured_and_the_panel_is_admin_only(self):
        src = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
        self.assertIn('"subtype_name": (a.get("subtype") or {}).get("name")', src)
        self.assertIn("_isAdminUser() && {key:'cashcheck',label:'CASH CHECK'", HTML)
        self.assertIn("COPY REPORT (account numbers masked)", HTML)


if __name__ == "__main__":
    unittest.main()
