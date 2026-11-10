"""/api/widget-trace: admin-only, read-only reconciliation of provider balances to what the donuts were drawn from."""
import glob, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="widgettrace-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod  # noqa: E402

HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()


def acct(name, inst, typ, bal, hc=0, sub=None, **kw):
    return dict({"name": name, "institution": inst, "type": typ, "subtype": sub, "holdings_count": hc, "balance": bal, "include_nw": True, "is_asset": True}, **kw)


def pos(a, inst, tkr, typ, val, price=100.0, name=""):
    return {"ticker": tkr, "name": name or tkr, "type": typ, "value": val, "cost_total": val, "gain": 0.0, "gain_pct": 0.0, "quantity": 1.0, "price": price,
            "day_pct": 0.0, "account": a, "institution": inst, "suspect": False, "is_cit": False, "price_source": None}


class TestTrace(unittest.TestCase):
    def snapshot(self):
        accts = [acct("MC VG IRA (...0439)", "Vanguard", "Investments", 50500.0, hc=2), acct("IRA BROKERAGE CASH (...1400)", "Wells Fargo", "Investments", 113.0, sub="Roth IRA"),
                 acct("WELLSTRADE IRA (...6140)", "Wells Fargo", "Investments", 5000.0, hc=1), acct("EVERYDAY CHECKING (...3913)", "Wells Fargo", "Cash", 4026.0, sub="Checking"),
                 acct("Blue Cash Everyday (...3003)", "American Express", "Credit Card", 300.0, is_asset=False)]
        ps = [pos("MC VG IRA (...0439)", "Vanguard", "VFIAX", "Mutual Fund", 40000.0, price=480.0), pos("MC VG IRA (...0439)", "Vanguard", "VMFXX", "Mutual Fund", 10000.0, price=1.0, name="Vanguard Federal Money Market Fund"),
              pos("WELLSTRADE IRA (...6140)", "Wells Fargo", "VTI", "ETF", 5000.0, price=250.0)]
        return appmod._derive(accts, ps, [], [accts[0], accts[2]], 60000.0, 60500.0, 300.0, balance_only=[accts[1]])

    def trace(self):
        with mock.patch.object(appmod, "_require_admin", return_value={"id": 1}), mock.patch.object(appmod, "_data", return_value=self.snapshot()):
            return appmod.get_widget_trace(mock.Mock())

    def bank_only_where(self):
        accts = [acct("Emergency Fund", "Ally", "Investments", 22000.0, hc=1), acct("Brokerage", "Fidelity", "Investments", 100.0, hc=1)]
        ps = [pos("Emergency Fund", "Ally", "CASH", "Cash", 22000.0, price=1.0), pos("Brokerage", "Fidelity", "AAPL", "Stock", 100.0)]
        snap = appmod._derive(accts, ps, [], accts, 22100.0, 22100.0, 0.0)
        with mock.patch.object(appmod, "_require_admin", return_value={"id": 1}), mock.patch.object(appmod, "_data", return_value=snap):
            return {x["account"]: x for x in appmod.get_widget_trace(mock.Mock())["accounts"]}["Emergency Fund"]["where"]

    def test_admin_only(self):
        with mock.patch.object(appmod, "_require_admin", return_value=None):
            self.assertEqual(appmod.get_widget_trace(mock.Mock()).status_code, 403)

    def test_every_position_says_which_slice_it_feeds(self):
        by = {p["ticker"]: p for p in self.trace()["positions"]}
        self.assertEqual((by["VFIAX"]["counted_as"], by["VFIAX"]["asset_type_slice"]), ("investment", "Mutual Fund"))
        self.assertEqual((by["VMFXX"]["counted_as"], by["VMFXX"]["asset_type_slice"]), ("money-market fund counted as cash", "Cash (in brokerage accounts)"))
        self.assertEqual(by["VTI"]["asset_type_slice"], "ETF")

    def test_accounts_reconcile_provider_balance_to_holdings_and_say_where_each_lands(self):
        a = {x["account"]: x for x in self.trace()["accounts"]}
        v = a["MC VG IRA (...0439)"]
        self.assertEqual((v["provider_balance"], v["holdings_value"], v["delta"]), (50500.0, 50000.0, 500.0))   # 500 of the balance is not a listed holding
        self.assertEqual(v["where"], "holdings feed every donut")
        self.assertIn("counted in the cash slice", a["IRA BROKERAGE CASH (...1400)"]["where"])
        self.assertIn("not in any donut", a["EVERYDAY CHECKING (...3913)"]["where"])
        self.assertEqual(self.bank_only_where(), "bank cash only: left out of every donut")
        self.assertEqual(a["Blue Cash Everyday (...3003)"]["where"], "liability: not in any donut")

    def test_totals_and_slices_match_what_the_widgets_use(self):
        t = self.trace()
        self.assertEqual(t["totals"]["brokerage_cash"], 10113.0)
        types = {r["label"]: r["value"] for r in t["slices"]["asset_type"]}
        self.assertEqual(types, {"Mutual Fund": 40000.0, "ETF": 5000.0, "Cash (in brokerage accounts)": 10113.0})
        self.assertEqual(t["totals"]["asset_type_donut_total"], sum(types.values()))
        self.assertIn("by_type", t["engine_fields"])

    def test_read_only(self):
        src = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read().split("def get_widget_trace(")[1].split('@app.get("/api/benefits/limits")')[0]
        for bad in ("store.", "atomic_write", "open(", "requests."):
            self.assertNotIn(bad, src)


class TestPanelWiring(unittest.TestCase):
    def test_the_panel_and_copy_report_include_the_trace(self):
        for needle in ("DONUT TRACE · WHAT EACH DONUT WAS DRAWN FROM", "function donutSlices()", "fetch('/api/widget-trace')", "DONUT TRACE: slices in dollars",
                       "DONUT TRACE: accounts (provider balance | holdings value | delta | where it lands)", "WHERE IT LANDS"):
            self.assertIn(needle, HTML, needle)


if __name__ == "__main__":
    unittest.main()
