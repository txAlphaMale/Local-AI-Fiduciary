# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_whatif_liabilities.py -- which debts the WHAT-IF tool lists (app._whatif_liabilities)."""
import glob, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="whatif-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod  # noqa: E402


def acct(name, bal, **kw):
    return dict(name=name, balance=bal, is_asset=False, type="Credit Card", institution="X", **kw)


class TestWhatIfList(unittest.TestCase):
    def listed(self, accts, cfg):
        with mock.patch.object(appmod, "_data", lambda: {"accounts": accts}), \
                mock.patch.object(appmod, "_read_insurance_cfg", lambda: cfg):
            return [x["name"] for x in appmod._whatif_liabilities()]

    def test_zero_apr_excluded_even_with_a_payment(self):
        cfg = {"debt_rates": {"Promo": 0, "Card": 19.9, "Loan": 5.5}, "debt_min_payments": {"Promo": 90, "Card": 50, "Loan": 300}}
        self.assertEqual(sorted(self.listed([acct("Promo", 352), acct("Card", 2827), acct("Loan", 15000)], cfg)),
                         ["Card", "Loan"])

    def test_paid_in_full_flag_excluded(self):
        cfg = {"debt_rates": {"Visa": 22.0, "Rewards": 19.0}, "debt_min_payments": {"Visa": 25, "Rewards": 25},
               "pay_in_full_overrides": {"Visa": True}}
        self.assertEqual(self.listed([acct("Visa", 900), acct("Rewards", 900)], cfg), ["Rewards"])

    def test_zero_and_tiny_balances_excluded_unknown_balance_kept(self):
        cfg = {"debt_rates": {"A": 20, "B": 20, "C": 20}, "debt_min_payments": {"A": 10, "B": 10, "C": 10}}
        self.assertEqual(sorted(self.listed([acct("A", 0), acct("B", 0.001), acct("C", None)], cfg)), ["C"])

    def test_unknown_apr_is_not_zero(self):
        self.assertEqual(self.listed([acct("NoRate", 500)], {}), ["NoRate"])


if __name__ == "__main__":
    unittest.main()
