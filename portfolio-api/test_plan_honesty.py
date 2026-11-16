"""RV-B7: retirement odds under the real book's volatility, the senior deduction, and the RMD age from the birth year."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as appmod  # noqa: E402
import roth  # noqa: E402
import tax  # noqa: E402


class SeniorDeduction(unittest.TestCase):
    def setUp(self):
        tax.reload_tables()

    def test_the_table_carries_the_amounts_years_and_phase_out(self):
        self.assertEqual(tax.SENIOR_BONUS["amount"], 6000)
        self.assertEqual((tax.SENIOR_BONUS["first_year"], tax.SENIOR_BONUS["last_year"]), (2025, 2028))
        self.assertEqual(tax.SENIOR_BONUS["threshold"], {"single": 75000, "mfj": 150000, "hoh": 75000})

    def test_full_amount_below_the_threshold_per_eligible_person(self):
        self.assertEqual(tax.senior_bonus("single", 66, None, magi=50000), 6000)
        self.assertEqual(tax.senior_bonus("mfj", 66, 67, magi=100000), 12000)
        self.assertEqual(tax.senior_bonus("mfj", 66, 60, magi=100000), 6000)             # only one spouse is 65+

    def test_it_phases_out_at_six_percent_above_the_threshold(self):
        self.assertEqual(tax.senior_bonus("single", 70, None, magi=75000), 6000)
        self.assertEqual(tax.senior_bonus("single", 70, None, magi=95000), 4800)           # 6000 - 6% of 20,000
        self.assertEqual(tax.senior_bonus("single", 70, None, magi=175000), 0)             # fully phased out
        self.assertEqual(tax.senior_bonus("mfj", 66, 67, magi=200000), 9000)               # 12,000 - 6% of 50,000

    def test_nothing_under_65_without_an_income_or_outside_the_years(self):
        self.assertEqual(tax.senior_bonus("single", 64, None, magi=40000), 0)
        self.assertEqual(tax.senior_bonus("single", 70, None, magi=None), 0)               # income unknown: left out, never overstated
        with mock.patch.object(tax, "TAX_YEAR", 2029):
            self.assertEqual(tax.senior_bonus("single", 70, None, magi=40000), 0)          # expired after 2028 (a carried-forward table must not extend it)
        with mock.patch.object(tax, "TAX_YEAR", 2024):
            self.assertEqual(tax.senior_bonus("single", 70, None, magi=40000), 0)

    def test_standard_deduction_includes_it_only_when_income_is_given(self):
        base = tax.standard_deduction("single", 70)
        self.assertEqual(tax.standard_deduction("single", 70, None, magi=50000), base + 6000)
        self.assertEqual(tax.standard_deduction("single", 40, None, magi=50000), tax.standard_deduction("single", 40))

    def test_summary_uses_it_and_lowers_the_tax(self):
        with_b = tax.summary(gross_ordinary=60000, ltcg=0, status="single", age=70)
        without = tax.summary(gross_ordinary=60000, ltcg=0, status="single", age=60)
        self.assertEqual(with_b["standard_deduction"] - tax.standard_deduction("single", 70), 6000)
        self.assertLess(with_b["effective"]["total_tax"], without["effective"]["total_tax"])


class RmdAge(unittest.TestCase):
    def test_by_birth_year(self):
        for y, a in ((1948, 72), (1950, 72), (1951, 73), (1959, 73), (1960, 75), (1985, 75)):
            self.assertEqual(roth.rmd_age(y), a, y)
        self.assertIsNone(roth.rmd_age(None))
        self.assertIsNone(roth.rmd_age("unknown"))

    def test_the_ladder_uses_it_unless_the_user_overrides(self):
        cfg = {"birth_year": 1982, "current_age": None}
        with mock.patch.object(appmod, "_read_retire", lambda: cfg), mock.patch.object(appmod, "_marginal_curve", lambda: {"recommended_fill": {"fill_to": 0.12}}), \
                mock.patch.object(appmod, "_balances_by_tax_status", lambda: {"deferred": 100000.0}), mock.patch.object(appmod, "_derive_age", lambda b, c: 44):
            with mock.patch.object(appmod, "_knob", lambda k: 0 if k == "roth_rmd_age" else 5.0):
                r = appmod._roth_plan()
            self.assertEqual(r["rmd_age"], 75)
            self.assertIn("birth year 1982", r["rmd_age_source"])
            with mock.patch.object(appmod, "_knob", lambda k: 73 if k == "roth_rmd_age" else 5.0):
                r = appmod._roth_plan()
            self.assertEqual((r["rmd_age"], r["rmd_age_source"]), (73, "your setting"))

    def test_default_setting_is_automatic(self):
        self.assertEqual(appmod.SETTING_SPECS["roth_rmd_age"]["default"], 0)


class HoldingsVolatility(unittest.TestCase):
    def run_odds(self, risk, res_success=60.0, headline=80.0):
        fake = lambda *a, **k: {"success_pct": res_success, "guardrails": {"success_pct": res_success + 5}}
        return appmod._holdings_volatility_odds({"success_pct": headline}, 16.0, 500000, 20000, 40000, 0.05, 10, 30, None, risk=risk, run=fake)

    def test_two_figures_with_their_inputs_named(self):
        r = self.run_odds({"status": "ready", "annualized_volatility_pct": 31.0, "days_recorded": 180, "since": "2026-05-01"})
        self.assertEqual((r["available"], r["volatility_pct"], r["assumed_pct"], r["success_pct"], r["success_gap_pts"]), (True, 31.0, 16.0, 60.0, 20.0))
        self.assertTrue(r["exceeds_assumption"])
        self.assertIn("likely optimistic", r["note"])

    def test_no_warning_when_the_book_is_close_to_the_assumption(self):
        r = self.run_odds({"status": "ready", "annualized_volatility_pct": 18.0, "days_recorded": 180, "since": "x"})
        self.assertFalse(r["exceeds_assumption"])
        self.assertNotIn("likely optimistic", r["note"])

    def test_absurd_measurements_are_capped_and_a_short_history_says_so(self):
        r = self.run_odds({"status": "ready", "annualized_volatility_pct": 400.0, "days_recorded": 40, "since": "x"})
        self.assertEqual((r["volatility_pct"], r["measured_pct"]), (80.0, 400.0))
        r = self.run_odds({"status": "building", "days_recorded": 12, "note": "Recording daily portfolio value (12/60 days)."})
        self.assertFalse(r["available"])
        self.assertIn("12/60", r["reason"])
        self.assertEqual(r["assumed_pct"], 16.0)

    def test_attention_item_when_realized_volatility_is_over_a_quarter_above_the_assumption(self):
        rk = {"status": "ready", "annualized_volatility_pct": 30.0, "days_recorded": 200}
        with mock.patch.object(appmod, "_risk_metrics", lambda: rk), mock.patch.object(appmod, "_read_retire", lambda: {"volatility_pct": None}):
            alerts = appmod._alerts_digest()["alerts"]
        a = [x for x in alerts if x["category"] == "Retirement odds"]
        self.assertEqual(len(a), 1)
        self.assertIn("30%", a[0]["message"])
        rk["annualized_volatility_pct"] = 19.0
        with mock.patch.object(appmod, "_risk_metrics", lambda: rk), mock.patch.object(appmod, "_read_retire", lambda: {"volatility_pct": None}):
            self.assertFalse([x for x in appmod._alerts_digest()["alerts"] if x["category"] == "Retirement odds"])

    def test_page_shows_both_figures(self):
        html = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html"), encoding="utf-8").read()
        self.assertIn("UNDER YOUR HOLDINGS' VOLATILITY", html)
        self.assertIn("mc.holdings_volatility", html)


if __name__ == "__main__":
    unittest.main()
