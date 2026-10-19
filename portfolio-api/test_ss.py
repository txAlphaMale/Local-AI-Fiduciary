# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
test_ss.py — unit tests for the Social Security engine (ss.py, Fable G2).

Pure/stdlib (unittest), zero network. Run:  python3 -m unittest test_ss -v
Covers PIA bend points, FRA-by-birth-year, claiming adjustments (62/FRA/70),
benefit taxation thresholds, the earnings test, and the household MC floor schedule.
"""
import unittest
import ss


class TestFRA(unittest.TestCase):
    def test_fra_table(self):
        self.assertEqual(ss.fra_months(1950), 66 * 12)      # <=1954 -> 66y
        self.assertEqual(ss.fra_months(1954), 66 * 12)
        self.assertEqual(ss.fra_months(1955), 66 * 12 + 2)  # +2mo/yr
        self.assertEqual(ss.fra_months(1957), 66 * 12 + 6)
        self.assertEqual(ss.fra_months(1959), 66 * 12 + 10)
        self.assertEqual(ss.fra_months(1960), 67 * 12)      # >=1960 -> 67y
        self.assertEqual(ss.fra_months(1975), 67 * 12)


class TestPIA(unittest.TestCase):
    def test_bend_points(self):
        # AIME below first bend point: 90% replacement.
        self.assertAlmostEqual(ss.pia_from_aime(1000), 900.0, places=1)
        # AIME in second band: 0.9*1286 + 0.32*(6000-1286) = 2665.88 -> trunc to dime 2665.8
        self.assertAlmostEqual(ss.pia_from_aime(6000), 2665.8, places=1)
        # Third band engaged above 7749.
        aime = 10000
        expect = 0.90 * 1286 + 0.32 * (7749 - 1286) + 0.15 * (aime - 7749)
        self.assertAlmostEqual(ss.pia_from_aime(aime), int(expect * 10) / 10.0, places=1)

    def test_pia_capped_at_max(self):
        self.assertEqual(ss.pia_from_aime(1_000_000), ss.MAX_FRA_BENEFIT_2026)


class TestClaimFactor(unittest.TestCase):
    def test_fra_is_unity(self):
        self.assertEqual(ss.claim_factor(67, 1965), 1.0)
        self.assertEqual(ss.claim_factor(66, 1950), 1.0)

    def test_early_62_fra67(self):
        # 60 months early: 36*(5/9)% + 24*(5/12)% = 20% + 10% = 30% reduction.
        self.assertAlmostEqual(ss.claim_factor(62, 1965), 0.70, places=4)

    def test_early_62_fra66(self):
        # 48 months early: 36*(5/9)% + 12*(5/12)% = 20% + 5% = 25% reduction.
        self.assertAlmostEqual(ss.claim_factor(62, 1950), 0.75, places=4)

    def test_delayed_70_fra67(self):
        # 3 years * 8% = 24% credit.
        self.assertAlmostEqual(ss.claim_factor(70, 1965), 1.24, places=4)

    def test_delayed_capped_at_70(self):
        # No credit accrues past 70.
        self.assertEqual(ss.claim_factor(72, 1965), ss.claim_factor(70, 1965))

    def test_claim_clamped_below_62(self):
        self.assertEqual(ss.claim_factor(59, 1965), ss.claim_factor(62, 1965))


class TestTaxation(unittest.TestCase):
    def test_below_base_untaxed(self):
        # MFJ base1=32k. other 10k + 0.5*30k = 25k < 32k -> 0 taxable.
        self.assertEqual(ss.taxable_portion(30000, 10000, "mfj"), 0.0)

    def test_middle_band_50pct(self):
        # single base1=25k,base2=34k. other 20k + 0.5*20k = 30k in [25k,34k].
        # taxable = min(0.5*20k=10k, 0.5*(30k-25k)=2.5k) = 2500.
        self.assertAlmostEqual(ss.taxable_portion(20000, 20000, "single"), 2500.0, places=1)

    def test_upper_band_85pct_cap(self):
        # High other income -> capped at 85% of benefits.
        self.assertAlmostEqual(ss.taxable_portion(40000, 200000, "mfj"),
                               0.85 * 40000, places=1)

    def test_mfj_worked_example(self):
        # other 40k, ss 40k: prov = 40k + 20k = 60k > 44k.
        # lesser = min(0.5*40k=20k, 0.5*(44k-32k)=6k) = 6k.
        # taxable = min(0.85*40k=34k, 0.85*(60k-44k)=13.6k + 6k) = 19.6k.
        self.assertAlmostEqual(ss.taxable_portion(40000, 40000, "mfj"), 19600.0, places=1)


class TestEarningsTest(unittest.TestCase):
    def test_under_fra_withholding(self):
        # 2026 under-FRA exempt 24,480; $1 per $2 over. Earn 34,480 -> (10,000)/2 = 5,000.
        self.assertAlmostEqual(ss.earnings_test_withholding(34480, under_fra=True), 5000.0, places=1)

    def test_fra_year_withholding(self):
        # 2026 FRA-year exempt 65,160; $1 per $3 over. Earn 68,160 -> (3,000)/3 = 1,000.
        self.assertAlmostEqual(ss.earnings_test_withholding(68160, fra_this_year=True), 1000.0, places=1)

    def test_no_withholding_below_limit(self):
        self.assertEqual(ss.earnings_test_withholding(20000, under_fra=True), 0.0)


class TestHouseholdSummary(unittest.TestCase):
    def test_schedule_and_totals(self):
        h = ss.summary(birth_year=1965, claim_age=67, status="mfj", other_income=50000,
                       retire_age=60,
                       self_kwargs={"estimated_fra_benefit": 3000},
                       spouse={"birth_year": 1966, "claim_age": 67,
                               "estimated_fra_benefit": 2000})
        self.assertEqual(h["household_annual_benefit"], 60000.0)   # (3000+2000)*12
        self.assertEqual(len(h["mc_floor_schedule"]), 2)
        # both claim at 67, retire at 60 -> start 7 years into retirement.
        self.assertTrue(all(s["start_year"] == 7 for s in h["mc_floor_schedule"]))

    def test_different_claim_ages_stagger(self):
        h = ss.summary(retire_age=62, status="mfj",
                       self_kwargs={"birth_year": 1963, "claim_age": 70,
                                    "estimated_fra_benefit": 2500},
                       spouse={"birth_year": 1965, "claim_age": 62,
                               "estimated_fra_benefit": 1500})
        starts = {s["label"]: s["start_year"] for s in h["mc_floor_schedule"]}
        self.assertEqual(starts["you"], 8)      # 70 - 62
        self.assertEqual(starts["spouse"], 0)   # 62 - 62

    def test_unavailable_without_input(self):
        h = ss.summary(birth_year=1965, retire_age=60)
        self.assertFalse(h["people"][0]["available"])


if __name__ == "__main__":
    unittest.main()
