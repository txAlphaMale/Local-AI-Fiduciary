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


if __name__ == "__main__":
    unittest.main()
