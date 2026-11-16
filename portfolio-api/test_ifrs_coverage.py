"""RV-010: foreign (IFRS) filers get a share-count change, dollar rules do not compare foreign-currency revenue with dollar
thresholds, and each sleeve says how many names could not be evaluated."""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scavenge_screen as ss, sleeves  # noqa: E402

TODAY = datetime.date(2026, 11, 16)


def rows(tax, tag, pairs, form="20-F"):
    return {tax: {tag: {"units": {"shares": [{"end": e, "val": v, "form": form, "filed": e[:4] + "-12-31"} for e, v in pairs]}}}}


class ShareChange(unittest.TestCase):
    def test_uses_ifrs_shares_outstanding_when_reported(self):
        f = {"facts": rows("ifrs-full", "NumberOfSharesOutstanding", [("2024-12-31", 1000), ("2025-12-31", 1100)])}
        self.assertEqual(ss.ifrs_share_change(f), (10.0, "IFRS shares outstanding", None))

    def test_falls_back_to_the_cover_page_count(self):
        f = {"facts": rows("dei", "EntityCommonStockSharesOutstanding", [("2024-12-31", 100), ("2025-12-31", 140)])}
        self.assertEqual(ss.ifrs_share_change(f), (40.0, "cover-page share count", None))

    def test_a_fall_of_more_than_80_percent_is_not_called_no_dilution(self):
        f = {"facts": rows("dei", "EntityCommonStockSharesOutstanding", [("2024-12-31", 1000), ("2025-12-31", 100)])}
        pct, basis, why = ss.ifrs_share_change(f)
        self.assertIsNone(pct)
        self.assertIn("split or unit change", why)

    def test_non_adjacent_years_are_not_compared(self):
        f = {"facts": rows("dei", "EntityCommonStockSharesOutstanding", [("2022-12-31", 100), ("2025-12-31", 140)])}
        self.assertEqual(ss.ifrs_share_change(f)[0], None)

    def test_ifrs_compact_carries_it(self):
        facts = {"facts": {"ifrs-full": {
            "Revenue": {"units": {"CAD": [{"start": "2024-01-01", "end": "2024-12-31", "val": 5e6, "form": "20-F", "filed": "2025-03-01"},
                                          {"start": "2025-01-01", "end": "2025-12-31", "val": 8e6, "form": "20-F", "filed": "2026-03-01"}]}}},
            "dei": rows("dei", "EntityCommonStockSharesOutstanding", [("2024-12-31", 100), ("2025-12-31", 150)])["dei"]}}
        c = ss.ifrs_compact(facts)
        self.assertEqual((c["currency"], c["share_change_pct"], c["share_change_basis"]), ("CAD", 50.0, "cover-page share count"))


def row(t, **s1):
    base = {"revenue": 50e6, "currency": "USD", "rev_growth_pct": 40.0, "share_change_pct": 5.0, "first_filed": "2015-01-01"}
    base.update(s1)
    return {"ticker": t, "cik": t, "name": t, "state": "active", "s1": base, "scores": {"score": 4}}


class Sleeve(unittest.TestCase):
    sl = {"id": "x", "name": "x", "rules": [{"metric": "revenue", "op": ">=", "value": 10_000_000},
                                           {"metric": "rev_growth_pct", "op": ">=", "value": 25},
                                           {"metric": "share_change_pct", "op": "<=", "value": 300}], "rank_by": "s1_score"}

    def test_foreign_currency_revenue_is_not_compared_with_a_dollar_threshold(self):
        self.assertIsNone(sleeves.value_of(row("J", revenue=50e6, currency="JPY"), "revenue", TODAY))
        self.assertEqual(sleeves.value_of(row("U"), "revenue", TODAY), 50e6)
        self.assertEqual(sleeves.value_of(row("J", currency="JPY"), "rev_growth_pct", TODAY), 40.0)     # a percentage is currency-neutral

    def test_cards_count_names_blocked_only_by_missing_data(self):
        rs = [row("A"), row("B", share_change_pct=None), row("C", currency="JPY"), row("D", share_change_pct=None, currency="EUR"),
              row("E", rev_growth_pct=5.0, share_change_pct=None)]            # E fails a rule it CAN be measured on: its gap changes nothing
        ev = sleeves.evaluate(self.sl, rs, TODAY)
        self.assertEqual([m["ticker"] for m in ev["matches"]], ["A"])
        self.assertEqual((ev["considered"], ev["not_evaluated"]), (5, 3))
        self.assertEqual(ev["not_evaluated_by_metric"], {"share_change_pct": 2, "revenue": 2})

    def test_nothing_missing_means_zero(self):
        self.assertEqual(sleeves.evaluate(self.sl, [row("A")], TODAY)["not_evaluated"], 0)


if __name__ == "__main__":
    unittest.main()
