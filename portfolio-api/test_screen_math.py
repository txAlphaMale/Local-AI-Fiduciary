"""RV-001..004, 008, 009, 023: the SEC screen math must never mix fiscal years, report a multi-year change as one year,
let a quarter stand in for a year, use stale cash, or call negative equity healthy."""
import unittest

import edgar
import scavenge_screen as ss


def fy(end_year, val, tag_start=None, form="10-K", fp="FY", filed=None):
    start = tag_start or f"{end_year - 1}-12-31"
    return {"start": start, "end": f"{end_year}-12-31", "val": val, "form": form, "fp": fp,
            "filed": filed or f"{end_year + 1}-02-20"}


def inst(end, val, form="10-K", filed=None):
    return {"end": end, "val": val, "form": form, "fp": "FY", "filed": filed or end}


def facts(**tags):
    gaap = {}
    for tag, rows in tags.items():
        gaap[tag] = {"units": {"USD": rows}}
    return {"facts": {"us-gaap": gaap}}


class FiscalYearMixTests(unittest.TestCase):
    def test_stale_net_income_is_dropped_not_mixed(self):   # RV-001
        f = facts(Revenues=[fy(2024, 300), fy(2025, 400)],
                  NetIncomeLoss=[fy(2022, -200), fy(2023, -300)],
                  Assets=[inst("2024-12-31", 900), inst("2025-12-31", 1000)])
        r = edgar._metrics_from_facts("X", "1", f)
        self.assertEqual(r["metrics"]["fiscal_year_end"], "2025-12-31")
        self.assertIsNone(r["ratios"]["net_margin_pct"])
        self.assertIn("net_income", r["metrics"]["period_mismatch"])

    def test_stale_revenue_dropped_when_other_concepts_are_newer(self):
        f = facts(Revenues=[fy(2022, 100), fy(2023, 120)],
                  NetIncomeLoss=[fy(2024, 10), fy(2025, 12)],
                  Assets=[inst("2025-12-31", 1000)])
        r = edgar._metrics_from_facts("X", "1", f)
        self.assertIsNone(r["metrics"]["revenue"])
        self.assertIn("revenue", r["metrics"]["period_mismatch"])

    def test_consistent_year_unchanged(self):
        f = facts(Revenues=[fy(2024, 300), fy(2025, 400)],
                  NetIncomeLoss=[fy(2024, 30), fy(2025, 40)],
                  Assets=[inst("2025-12-31", 1000)])
        r = edgar._metrics_from_facts("X", "1", f)
        self.assertEqual(r["ratios"]["net_margin_pct"], 10.0)
        self.assertEqual(r["metrics"]["period_mismatch"], [])


class GapTests(unittest.TestCase):
    def test_growth_not_computed_across_a_missing_year(self):   # RV-002
        f = facts(Revenues=[fy(2022, 100), fy(2025, 400)])
        v, prev, end = edgar._latest(f, "revenue")
        self.assertEqual((v, prev, end), (400, None, "2025-12-31"))
        r = edgar._metrics_from_facts("X", "1", f)
        self.assertIsNone(r["metrics"]["revenue_growth_pct"])

    def test_adjacent_years_still_give_growth(self):
        f = facts(Revenues=[fy(2024, 100), fy(2025, 150)])
        r = edgar._metrics_from_facts("X", "1", f)
        self.assertEqual(r["metrics"]["revenue_growth_pct"], 50.0)

    def test_cagr_uses_elapsed_years(self):
        ser = [{"fy": "2022", "val": 100, "end": "2022-12-31"}, {"fy": "2025", "val": 800, "end": "2025-12-31"}]
        self.assertAlmostEqual(edgar._cagr(ser), 100.0, delta=0.5)      # 8x over 3 years = 100%/yr, not 800% over "1" row gap
        self.assertEqual(edgar._cagr([{"fy": "2024", "val": 100}, {"fy": "2025", "val": 150}]), 50.0)

    def test_alt_revenue_prior_must_be_adjacent(self):
        rows = [("2022-12-31", 100, "USD"), ("2025-12-31", 400, "USD")]
        self.assertIsNone(ss._prior_if_adjacent(rows))
        self.assertEqual(ss._prior_if_adjacent([("2024-12-31", 100, "USD"), ("2025-12-31", 150, "USD")]), 100)


class TtmTests(unittest.TestCase):
    def test_quarter_with_annual_end_date_cannot_be_the_year(self):   # RV-003
        q = {"start": "2025-10-01", "end": "2025-12-31", "val": 260, "form": "10-K", "fp": "FY", "filed": "2026-03-01"}
        f = facts(Revenues=[fy(2025, 1000, filed="2026-02-20"), q])
        self.assertEqual(edgar._annual(f, ["Revenues"])[-1], ("2025-12-31", 1000))
        self.assertEqual(edgar._ttm_val(f, ["Revenues"]), 1000)

    def test_ttm_ignores_a_disagreeing_segment_tag(self):
        seg = [fy(2025, 300)]                                    # one segment, not the company total
        f = facts(Revenues=[fy(2024, 900), fy(2025, 1000)], RevenueFromContractWithCustomerExcludingAssessedTax=seg)
        tags = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"]
        self.assertEqual(edgar._ttm_val(f, tags), 1000)


class CashTests(unittest.TestCase):
    def test_newest_cash_tag_wins_over_old_one(self):   # RV-004
        f = facts(CashAndCashEquivalentsAtCarryingValue=[inst("2018-12-31", 5_000_000)],
                  CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents=[inst("2025-12-31", 90_000_000)],
                  Assets=[inst("2025-12-31", 1)])
        self.assertEqual(ss.cash_position_ex(f), (90_000_000, "2025-12-31"))

    def test_old_cash_is_unknown_not_used(self):
        f = facts(CashAndCashEquivalentsAtCarryingValue=[inst("2023-12-31", 5_000_000)], Assets=[inst("2025-12-31", 1)])
        self.assertEqual(ss.cash_position_ex(f), (None, None))

    def test_investments_use_largest_not_sum(self):     # RV-023
        f = facts(CashAndCashEquivalentsAtCarryingValue=[inst("2025-12-31", 10)],
                  ShortTermInvestments=[inst("2025-12-31", 40)],
                  MarketableSecuritiesCurrent=[inst("2025-12-31", 30)])
        self.assertEqual(ss.cash_position_ex(f)[0], 50)


class EquityTests(unittest.TestCase):
    def test_negative_equity_has_no_debt_ratio_or_roe(self):   # RV-009
        f = facts(Revenues=[fy(2024, 100), fy(2025, 110)], NetIncomeLoss=[fy(2024, -30), fy(2025, -40)],
                  Assets=[inst("2025-12-31", 700)], Liabilities=[inst("2025-12-31", 900)],
                  StockholdersEquity=[inst("2025-12-31", -200)])
        r = edgar._metrics_from_facts("X", "1", f)
        self.assertIsNone(r["ratios"]["debt_to_equity"])
        self.assertIsNone(r["ratios"]["return_on_equity_pct"])
        self.assertTrue(r["ratios"]["negative_equity"])


class NetMarginFallbackTests(unittest.TestCase):
    def test_annual_margin_used_when_ttm_missing(self):   # RV-008
        res = {"metrics": {"ttm": {"revenue": None, "net_margin_pct": None}, "revenue": 1000, "fiscal_year_end": "2025-12-31"},
               "ratios": {"net_margin_pct": 10.0}, "scores": {}}
        s1 = ss.compact(res, facts())
        self.assertEqual(s1["net_margin_pct"], 10.0)


if __name__ == "__main__":
    unittest.main()


class OperatingMarginTests(unittest.TestCase):
    def _res(self, op, nm):
        return {"metrics": {"ttm": {"revenue": 1000, "net_margin_pct": nm}, "revenue": 1000, "fiscal_year_end": "2025-12-31"},
                "ratios": {"net_margin_pct": nm, "operating_margin_pct": op}, "scores": {}}

    def test_operating_margin_carried(self):
        s1 = ss.compact(self._res(-12.0, 40.0), facts())
        self.assertEqual((s1["operating_margin_pct"], s1["net_margin_pct"]), (-12.0, 40.0))


class TtmGrowthTests(unittest.TestCase):
    """RV-B8: trailing-twelve-month revenue growth, bridged the same way as the TTM value itself."""
    def q(self, year, start, end, val, form="10-Q"):
        return {"start": f"{year}-{start}", "end": f"{year}-{end}", "val": val, "form": form, "fp": "Q3", "filed": f"{year}-{end}"}

    def test_interim_ttm_growth(self):
        # FY2023 = 80, FY2024 = 100; nine months: 2023 = 45, 2024 = 60, 2025 = 180.
        # TTM = 100 + 180 - 60 = 220; the TTM a year earlier = 80 + 60 - 45 = 95.
        f = facts(Revenues=[fy(2023, 80), fy(2024, 100), self.q(2023, "01-01", "09-30", 45),
                            self.q(2024, "01-01", "09-30", 60), self.q(2025, "01-01", "09-30", 180)])
        self.assertEqual(edgar.ttm_revenue_growth(f), round((220 - 95) / 95 * 100, 2))

    def test_no_interim_uses_annual_growth(self):
        f = facts(Revenues=[fy(2024, 100), fy(2025, 150)])
        self.assertEqual(edgar.ttm_revenue_growth(f), 50.0)

    def test_unbridgeable_prior_year_is_none_not_a_guess(self):
        f = facts(Revenues=[fy(2025, 200), self.q(2025, "01-01", "09-30", 180)])
        self.assertIsNone(edgar.ttm_revenue_growth(f))
