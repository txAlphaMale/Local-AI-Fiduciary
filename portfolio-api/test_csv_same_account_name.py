# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_csv_same_account_name.py -- two brokerages with the same account name stay two accounts
(tester report 2026-10-21: both shown as one 'Robinhood' account with the combined total)."""
import glob, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="csvacct-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod  # noqa: E402


def build(csv_text):
    p = os.path.join(_TMP, "h.csv")
    open(p, "w").write(csv_text)
    with mock.patch.dict(os.environ, {"HOLDINGS_CSV": p}), \
         mock.patch.object(appmod, "_finnhub", lambda kind, t: {"c": 10.0, "dp": 0.0}):
        return appmod._build_csv()


class TestSameNameTwoBrokers(unittest.TestCase):
    def test_same_account_name_at_two_institutions_is_two_accounts(self):
        d = build("ticker,quantity,cost_basis,account,institution,type\n"
                  "AAA,10,,Brokerage,Robinhood,Stock\nBBB,20,,Brokerage,Fidelity,Stock\n")
        accts = {a["name"]: a for a in d["accounts"]}
        self.assertEqual(set(accts), {"Brokerage · Robinhood", "Brokerage · Fidelity"})
        self.assertEqual(accts["Brokerage · Robinhood"]["balance"], 100.0)
        self.assertEqual(accts["Brokerage · Fidelity"]["institution"], "Fidelity")
        self.assertEqual(accts["Brokerage · Fidelity"]["balance"], 200.0)

    def test_distinct_names_and_single_institution_are_unchanged(self):
        d = build("ticker,quantity,cost_basis,account,institution,type\n"
                  "AAA,10,,Roth IRA,Fidelity,Stock\nBBB,20,,Roth IRA,Fidelity,Stock\nCCC,1,,Taxable,Robinhood,Stock\n")
        self.assertEqual({a["name"] for a in d["accounts"]}, {"Roth IRA", "Taxable"})


if __name__ == "__main__":
    unittest.main()
