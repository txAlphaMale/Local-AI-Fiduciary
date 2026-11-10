"""Tax status uses the provider's account subtype when it is more specific than the name (Monarch sends 'Roth IRA', 'IRA', 'HSA', ...)."""
import glob, os, re, sys, tempfile, unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="subtype-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod  # noqa: E402

S = appmod._account_tax_status
SRC = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()


class TestSubtypeWins(unittest.TestCase):
    def test_the_real_case_an_ira_named_cash_sweep_of_a_roth_ira(self):
        self.assertEqual(S("IRA BROKERAGE CASH (...1400)", "Cash", None), "deferred")           # before: name only
        self.assertEqual(S("IRA BROKERAGE CASH (...1400)", "Investments", "Roth IRA"), "roth")   # subtype is more specific
        self.assertEqual(S("IRA BROKERAGE CASH (...1400)", "Cash", "Roth IRA"), "roth")

    def test_tax_advantaged_subtypes(self):
        for sub, want in (("Roth IRA", "roth"), ("Roth 401k", "roth"), ("Tax-Free Savings Account (TFSA)", "roth"), ("HSA", "hsa"),
                          ("Health Savings Account", "hsa"), ("IRA", "deferred"), ("401k", "deferred"), ("403b", "deferred"),
                          ("Simple IRA", "deferred"), ("Simplified Employee Pension IRA (SEP IRA)", "deferred"),
                          ("Thrift Savings Plan (TSP)", "deferred"), ("Pension", "deferred"), ("Retirement", "deferred")):
            self.assertEqual(S("Some account", "Investments", sub), want, sub)

    def test_other_or_missing_subtypes_fall_back_to_the_name_rules_unchanged(self):
        for args, want in ((("Roth IRA - Vanguard", "Investments", None), "roth"), (("Voya HSA", "Investments", ""), "hsa"),
                           (("My 401(k)", "Investments", "Checking"), "deferred"), (("Individual", "Investments", "Brokerage"), "taxable"),
                           (("Savings", "Cash", "Savings"), "na"), (("Visa", "Credit Card", None), "na"),
                           (("WF IRA Cash", "Cash", "Checking"), "deferred")):
            self.assertEqual(S(*args), want, args)

    def test_two_argument_calls_still_work(self):
        self.assertEqual(S("Roth IRA", "Investments"), "roth"); self.assertEqual(S("Brokerage", "Investments"), "taxable")

    def test_every_account_level_call_passes_the_subtype(self):
        lines = [l for l in SRC.split("\n") if "_account_tax_status(" in l and ".get(\"type\")" in l and "def _account_tax_status" not in l]
        self.assertGreaterEqual(len(lines), 12)
        for l in lines:
            self.assertIn('.get("subtype")', l, l.strip())

    def test_balance_only_investment_accounts_use_it(self):
        self.assertIn('and _account_tax_status(a.get("name"), a.get("type"), a.get("subtype")) != "na"]', SRC)


class TestZeroBalanceReason(unittest.TestCase):
    def test_zero_balance_cash_account_mentions_monarchs_available_balance_setting(self):
        import cashdetect as cd
        a = {"name": "BROKERAGE CASH (...5346)", "institution": "Wells Fargo", "type": "Cash", "holdings_count": 0, "balance": 0.0}
        r = cd.classify_accounts([a, {"name": "WELLSTRADE (...9571)", "institution": "Wells Fargo", "type": "Investments", "holdings_count": 2, "balance": 100}],
                                 [{"account": "WELLSTRADE (...9571)", "institution": "Wells Fargo", "ticker": "AAPL", "type": "Stock", "value": 100}])[0]
        why = " ".join(r["reasons"])
        self.assertIn("Use available balance instead of current balance", why); self.assertIn("Wells Fargo", why)


if __name__ == "__main__":
    unittest.main()
