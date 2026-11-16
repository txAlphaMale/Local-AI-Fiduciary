"""RV-016: the insider window is paged (and says how far back it reaches); 10% holders who are not officers/directors are reported
separately instead of counting as insiders buying."""
import datetime
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import edgar, insider, signals  # noqa: E402


def xml(owners, code="P", shares=1000, price=10.0):
    own = "".join(
        f"<reportingOwner><reportingOwnerId><rptOwnerName>{n}</rptOwnerName></reportingOwnerId><reportingOwnerRelationship>"
        f"<isOfficer>{int(o)}</isOfficer><isDirector>{int(d)}</isDirector><isTenPercentOwner>{int(t)}</isTenPercentOwner>"
        f"</reportingOwnerRelationship></reportingOwner>" for n, o, d, t in owners)
    return (f"<ownershipDocument><issuer><issuerCik>0000000001</issuerCik><issuerName>X</issuerName><issuerTradingSymbol>X</issuerTradingSymbol></issuer>"
            f"<periodOfReport>2026-11-01</periodOfReport>{own}<nonDerivativeTable><nonDerivativeTransaction>"
            f"<transactionDate><value>2026-11-01</value></transactionDate><transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>"
            f"<transactionAmounts><transactionShares><value>{shares}</value></transactionShares><transactionPricePerShare><value>{price}</value></transactionPricePerShare>"
            f"<transactionAcquiredDisposedCode><value>{'A' if code == 'P' else 'D'}</value></transactionAcquiredDisposedCode></transactionAmounts>"
            f"</nonDerivativeTransaction></nonDerivativeTable></ownershipDocument>")


class Aggregate(unittest.TestCase):
    def test_a_fund_that_is_only_a_ten_percent_holder_is_not_insiders_buying(self):
        fund = insider.parse_form4(xml([("Big Fund LP", 0, 0, 1)]))
        ceo = insider.parse_form4(xml([("Jane CEO", 1, 0, 0)], shares=100))
        r = insider.aggregate([fund, ceo], "X", 120)
        self.assertEqual((r["buy"]["insiders"], r["buy"]["count"], r["buy"]["value"]), (1, 1, 1000.0))
        self.assertEqual(r["signal"], "net buying")                                   # not "cluster buying"
        self.assertEqual(r["holders_not_insiders"]["buy"], {"count": 1, "holders": 1, "value": 10000.0})

    def test_only_a_fund_buying_leaves_no_insider_signal(self):
        r = insider.aggregate([insider.parse_form4(xml([("Big Fund LP", 0, 0, 1)]))], "X", 120)
        self.assertEqual((r["signal"], r["buy"]["insiders"]), ("no open-market activity", 0))
        self.assertEqual(r["holders_not_insiders"]["buy"]["holders"], 1)

    def test_joint_filing_counts_the_officer_even_when_listed_second(self):
        f = insider.parse_form4(xml([("Parent Holdings", 0, 0, 1), ("Sam Director", 0, 1, 0)]))
        self.assertEqual(len(f["owners"]), 2)
        r = insider.aggregate([f], "X", 120)
        self.assertEqual((r["buy"]["insiders"], r["holders_not_insiders"]["buy"]["count"]), (1, 0))

    def test_older_records_without_relationship_data_still_count(self):
        legacy = {"owner": "Old Name", "transactions": [{"code": "P", "value": 5.0, "shares": 1.0, "date": "2026-01-01"}]}
        self.assertEqual(insider.aggregate([legacy], "X", 120)["buy"]["insiders"], 1)


class Paging(unittest.TestCase):
    """The filing list comes from SEC's submissions API (newest first); the window, the cap and a failed lookup behave as documented."""

    def submissions(self, forms_dates):
        return {"filings": {"recent": {"form": [f for f, _ in forms_dates], "filingDate": [d for _, d in forms_dates],
                                       "accessionNumber": [f"0000000001-26-{i:06d}" for i in range(len(forms_dates))]}}}

    def run_ex(self, forms_dates, limit=500, days=120, status=200, boom=False):
        class R:
            def __init__(self, code, body):
                self.status_code, self._b = code, body

            def json(self):
                return self._b

        def fake_get(url, params=None, headers=None, timeout=None):
            if boom:
                raise RuntimeError("timeout")
            return R(status, self.submissions(forms_dates))
        with mock.patch.object(edgar, "get_cik", return_value="0000000001"), mock.patch.object(edgar, "_get", side_effect=fake_get), \
                mock.patch.object(edgar, "_headers", return_value={}), \
                mock.patch.object(edgar, "_fetch_form4_xml", side_effect=lambda c, a: "<issuerCik>1</issuerCik>"):
            return edgar.recent_form4_ex("X", days=days, limit=limit)

    def days_ago(self, n):
        return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()

    def test_reads_back_to_the_cutoff_and_reports_a_complete_window(self):
        fd = [("4", self.days_ago(i)) for i in range(0, 60)] + [("8-K", self.days_ago(5))] + [("4", self.days_ago(400))] * 5
        res = self.run_ex(fd)
        self.assertEqual(len(res["filings"]), 60)                # the 8-K is ignored; the 400-day-old ones are outside the window
        self.assertTrue(res["window_complete"])
        self.assertFalse(res["error"])

    def test_stops_at_the_filing_cap_and_says_how_far_back_it_got(self):
        fd = [("4", self.days_ago(i % 100)) for i in range(200)]
        res = self.run_ex(fd, limit=30)
        self.assertEqual(len(res["filings"]), 30)
        self.assertFalse(res["window_complete"])
        self.assertEqual(res["covered_from"], res["filings"][-1]["date"])

    def test_a_short_list_inside_the_window_is_complete(self):
        res = self.run_ex([("4", self.days_ago(1)), ("4", self.days_ago(2)), ("4", self.days_ago(3))])
        self.assertEqual((res["window_complete"], len(res["filings"])), (True, 3))

    def test_a_failed_sec_request_is_an_error_not_no_filings(self):
        for kw in ({"status": 503}, {"boom": True}):
            res = self.run_ex([("4", self.days_ago(1))], **kw)
            self.assertTrue(res["error"], kw)
            self.assertEqual(res["filings"], [])

    def test_the_app_does_not_cache_a_failed_lookup_as_empty(self):
        import app as appmod
        with mock.patch.object(edgar, "recent_form4_ex", lambda *a, **k: {"filings": [], "window_complete": True, "covered_from": None,
                                                                          "listed": 0, "error": True}):
            res = appmod._insider_signal("ZZERR")
        self.assertTrue(res.get("error"))
        self.assertFalse(res.get("available"))
        self.assertIn("retried", res["reason"])


class Records(unittest.TestCase):
    def test_signal_record_carries_coverage_and_the_version_is_bumped(self):
        self.assertGreaterEqual(signals.INSIDER_VERSION, 4)
        rec = signals.build_insider({"available": True, "net_value": 500.0, "signal": "net buying", "buy": {"insiders": 1, "value": 500.0},
                                     "sell": {}, "window_complete": False, "covered_from": "2026-10-20",
                                     "holders_not_insiders": {"buy": {"count": 2}}}, now=1)
        self.assertEqual((rec["window_complete"], rec["covered_from"]), (False, "2026-10-20"))
        self.assertIn("only back to 2026-10-20", signals.display({"insider": rec})[0])


if __name__ == "__main__":
    unittest.main()
