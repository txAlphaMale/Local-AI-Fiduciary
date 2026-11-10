"""HSA as a medical reserve (2026-11-10): own verdict in CASH CHECK (never cash, never unsure) and balance-only accounts counted by the tax-status planners."""
import glob, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="hsa-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import cashdetect as cd  # noqa: E402
import app as appmod  # noqa: E402


def acct(name, inst="Voya", typ="Investments", sub="HSA", bal=3400.0, n=0, **kw):
    return dict({"name": name, "institution": inst, "type": typ, "subtype": sub, "balance": bal, "holdings_count": n, "is_asset": True, "include_nw": True}, **kw)


class TestVerdict(unittest.TestCase):
    def test_holdingsless_hsa_is_its_own_verdict_by_name_or_subtype(self):
        for a in (acct("Voya HSA Cash"), acct("Voya Health Plan", sub="HSA"), acct("Medical", sub="Health Savings Account")):
            r = cd.classify_accounts([a], [])[0]
            self.assertEqual((r["verdict"], r["confidence"]), ("hsa", "high"), a["name"])

    def test_it_is_never_brokerage_cash_even_next_to_a_paired_hsa_with_holdings(self):
        accts = [acct("Voya HSA", n=1, bal=2000), acct("Voya HSA Cash", bal=1000)]
        pos = [{"institution": "Voya", "account": "Voya HSA", "ticker": "VFIAX", "type": "Mutual Fund", "value": 2000.0}]
        r = {x["name"]: x for x in cd.classify_accounts(accts, pos)}
        self.assertEqual(r["Voya HSA Cash"]["verdict"], "hsa")
        self.assertEqual(cd.cash_breakdown(accts, pos)["brokerage_cash_accounts"], 0.0)
        self.assertEqual(cd.counted_brokerage_cash_accounts(accts, pos), [])

    def test_the_persons_designation_still_wins(self):
        a = acct("Voya HSA Cash")
        r = cd.classify_accounts([a], [], {cd.desig_key("Voya", "Voya HSA Cash"): "investment"})[0]
        self.assertEqual((r["verdict"], r["designation"]), ("not_cash", "investment"))


class TestPlannerBuckets(unittest.TestCase):
    def run_bal(self, accounts, by_account, **kw):
        with mock.patch.object(appmod, "_data", return_value={"accounts": accounts, "by_account": by_account}):
            return appmod._balances_by_tax_status(**kw)

    def test_balance_only_accounts_count_in_their_own_bucket(self):
        accts = [acct("Employer 401k", inst="Fidelity", sub="401k", bal=50000), acct("Voya HSA Cash", bal=3400),
                 acct("Roth Bal", inst="Vanguard", sub="Roth IRA", bal=1000), acct("Listed IRA", inst="WF", sub="IRA", bal=900, n=1)]
        by = [{"account": "Listed IRA", "holdings": [{"value": 900.0}]}]
        b = self.run_bal(accts, by)
        self.assertEqual((b["deferred"], b["hsa"], b["roth"], b["taxable"]), (50900.0, 3400.0, 1000.0, 0.0))
        self.assertEqual(self.run_bal(accts, by, include_balance_only=False),
                         {"taxable": 0.0, "roth": 0.0, "deferred": 900.0, "hsa": 0.0})

    def test_hidden_liability_zero_and_cash_accounts_are_not_added(self):
        accts = [acct("Old 401k", sub="401k", bal=0), acct("Mortgage", typ="Loan", sub="", bal=9000, is_asset=False),
                 acct("Checking", typ="Depository", sub="Checking", bal=800), acct("Off NW 401k", sub="401k", bal=500, include_nw=False)]
        self.assertEqual(self.run_bal(accts, []), {"taxable": 0.0, "roth": 0.0, "deferred": 0.0, "hsa": 0.0})

    def test_the_hsa_is_never_added_to_the_retirement_spendable_buckets(self):
        b = self.run_bal([acct("Voya HSA Cash", bal=3400)], [])
        self.assertEqual((b["hsa"], b["deferred"], b["roth"], b["taxable"]), (3400.0, 0.0, 0.0, 0.0))

    def test_leverage_collateral_uses_listed_positions_only(self):
        src = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read().split("def _leverage():")[1].split("collateral =")[0]
        self.assertIn("include_balance_only=False", src)


if __name__ == "__main__":
    unittest.main()
