# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
test_calculators.py — unit tests for the CALCULATORS tab engine (calculators.py).

Pure/stdlib (unittest), zero network. Run:  python3 -m unittest test_calculators -v

Strategy: every closed-form or solver result is checked against something it can't share a bug
with — a brute-force period loop, a published reference value, or the inverse solve (TVM round
trips: solve each of N/I/PV/PMT/FV from the other four and land back on the original).
"""
import math
import unittest

import calculators as c


def _brute(balance, apr, payment, extra=0.0, lump=0.0, ppy=12, cap=2000):
    b, i, n, tot = balance, apr / 100.0 / ppy, 0, 0.0
    while b > 0.005 and n < cap:
        n += 1
        intr = b * i
        tot += intr
        b += intr
        b -= min(payment + extra + (lump if n == 1 else 0.0), b)
    return (n, tot) if n < cap else (None, None)


class TestAmortization(unittest.TestCase):
    def test_reference_payments(self):
        # Widely published reference values
        self.assertAlmostEqual(c.amort_payment(320000, 0.065 / 12, 360), 2022.62, places=2)
        self.assertAlmostEqual(c.amort_payment(30000, 0.07 / 12, 60), 594.04, places=2)
        self.assertAlmostEqual(c.amort_payment(12000, 0.0, 48), 250.0, places=6)

    def test_totals_match_brute_force(self):
        for bal, apr, n in [(320000, 6.5, 360), (25000, 9.9, 72), (8000, 0.0, 24), (1500, 29.99, 18)]:
            with self.subTest(bal=bal, apr=apr):
                r = c.amortize(bal, apr / 1200.0, n)
                bn, bi = _brute(bal, apr, r["scheduled_payment"])
                self.assertEqual(r["periods"], bn)
                self.assertAlmostEqual(r["total_interest"], bi, places=4)
                self.assertAlmostEqual(r["total_paid"] - r["total_interest"], bal, places=4)

    def test_balloon_equals_remaining_balance(self):
        bal, i, n, term = 80000, 0.085 / 12, 240, 60
        r = c.amortize(bal, i, n, term_periods=term)
        p = c.amort_payment(bal, i, n)
        expected = bal * (1 + i) ** term - p * ((1 + i) ** term - 1) / i
        self.assertAlmostEqual(r["balloon"], expected, places=4)
        self.assertEqual(len(r["rows"]), term)

    def test_interest_only_then_amortizes_over_remainder(self):
        bal, i = 100000, 0.06 / 12
        r = c.amortize(bal, i, 360, io_periods=120)
        self.assertAlmostEqual(r["rows"][0]["payment"], 500.0, places=6)
        self.assertAlmostEqual(r["rows"][119]["balance"], bal, places=6)
        self.assertAlmostEqual(r["rows"][120]["payment"], c.amort_payment(bal, i, 240), places=6)
        self.assertEqual(r["periods"], 360)

    def test_payment_below_interest_never_resolves(self):
        r = c.amortize(5000, 0.24 / 12, 1, payment=90)
        self.assertFalse(r["resolved"])
        self.assertIsNone(r["periods"])


class TestLoan(unittest.TestCase):
    def test_basic(self):
        r = c.loan({"amount": 30000, "apr": 7, "term_months": 60})
        self.assertEqual(r["payment"], 594.04)
        self.assertEqual(r["periods"], 60)
        self.assertAlmostEqual(r["total_interest"], 5642.16, places=1)

    def test_extra_payment_saves(self):
        r = c.loan({"amount": 30000, "apr": 7, "term_months": 60, "extra": 100})
        self.assertLess(r["periods"], 60)
        self.assertGreater(r["interest_saved"], 0)
        bn, bi = _brute(30000, 7, 594.0398, extra=100)
        self.assertEqual(r["periods"], bn)

    def test_fee_raises_effective_apr(self):
        r = c.loan({"amount": 30000, "apr": 7, "term_months": 60, "origination_fee": 900})
        self.assertGreater(r["effective_apr"], 7.0)
        # 3% fee on a 5-yr loan is worth roughly 1.2 APR points
        self.assertTrue(8.0 < r["effective_apr"] < 8.5, r["effective_apr"])

    def test_auto_purchase_build(self):
        r = c.loan({"price": 35000, "down_payment": 5000, "trade_in": 8000, "trade_in_owed": 10000,
                    "sales_tax_pct": 6.25, "fees": 500, "apr": 6, "term_years": 5})
        # taxed after trade: (35000-8000)*6.25% = 1687.50; amount = 35000-5000-8000+10000+1687.5+500
        self.assertAlmostEqual(r["amount_financed"], 34187.50, places=2)
        self.assertEqual(r["purchase"]["negative_equity_rolled_in"], 2000.0)

    def test_missing_apr_is_reported_not_zeroed(self):
        r = c.loan({"amount": 1000, "term_months": 12})
        self.assertIn("apr", r.get("missing", []))


class TestTVM(unittest.TestCase):
    @staticmethod
    def _loop_fv(n, apr, pv, pmt, begin=False):
        """Independent month loop for the future value of a deposit plan (signs: money in −)."""
        b, i = -pv, apr / 1200.0
        for _ in range(n):
            if begin:
                b += -pmt
            b *= 1 + i
            if not begin:
                b += -pmt
        return b

    CASES = [
        dict(n=360, rate=6.5, pv=320000, pmt=-c.amort_payment(320000, 0.065 / 12, 360), fv=0),
        dict(n=120, rate=7.0, pv=-10000, pmt=-500, fv=None),                     # savings plan
        dict(n=300, rate=5.0, pv=-1000000, pmt=c.amort_payment(1e6, 0.05 / 12, 300), fv=0),
        dict(n=60, rate=0.0, pv=12000, pmt=-200, fv=0),                          # 0% loan
        dict(n=48, rate=4.0, pv=-5000, pmt=-100, fv=None, timing="begin"),       # annuity due
    ]

    def setUp(self):
        for case in self.CASES:
            if case["fv"] is None or case.get("_fv_from_loop"):
                case["_fv_from_loop"] = True
                case["fv"] = self._loop_fv(case["n"], case["rate"], case["pv"], case["pmt"],
                                           case.get("timing") == "begin")

    def test_round_trip_each_field(self):
        for case in self.CASES:
            # recompute exact fv/pmt from the case to avoid rounding in the literal
            base = {k: v for k, v in case.items() if not k.startswith("_")}
            for unknown in c.TVM_FIELDS:
                with self.subTest(case=case, unknown=unknown):
                    inp = dict(base)
                    inp[unknown] = None
                    r = c.tvm(inp)
                    self.assertNotIn("error", r, r.get("error"))
                    tol = 1e-3 if unknown == "rate" else max(0.02, abs(case[unknown]) * 1e-6)
                    self.assertAlmostEqual(r["value"], case[unknown], delta=tol)

    def test_needs_exactly_one_blank(self):
        self.assertIn("error", c.tvm({"n": 10, "rate": 5}))
        self.assertIn("error", c.tvm({"n": 10, "rate": 5, "pv": 1, "pmt": -1, "fv": 0}))

    def test_sign_slip_is_auto_corrected(self):
        r = c.tvm({"n": None, "rate": 6.5, "pv": 320000, "pmt": 2022.6177, "fv": 0})
        self.assertAlmostEqual(r["value"], 360, delta=0.01)
        self.assertTrue(r["notes"])

    def test_compounding_differs_from_payments(self):
        # Canadian-style: semiannual compounding, monthly payments
        i = (1 + 0.05 / 2) ** (2 / 12) - 1
        expected = c.amort_payment(300000, i, 300)
        r = c.tvm({"n": 300, "rate": 5, "pv": 300000, "pmt": None, "fv": 0,
                   "periods_per_year": 12, "compounds_per_year": 2})
        self.assertAlmostEqual(-r["value"], expected, places=3)

    def test_net_interest_sign(self):
        loan = c.tvm({"n": 360, "rate": 6.5, "pv": 320000, "pmt": None, "fv": 0})
        self.assertLess(loan["net_interest"], 0)
        self.assertEqual(loan["balance_meaning"], "amount owed")
        sav = c.tvm({"n": 120, "rate": 7, "pv": -10000, "pmt": -500, "fv": None})
        self.assertGreater(sav["net_interest"], 0)
        self.assertEqual(sav["balance_meaning"], "account value")
        self.assertAlmostEqual(sav["schedule"][-1]["balance"], sav["value"], places=1)


class TestWhatIf(unittest.TestCase):
    def test_extra_matches_brute_force(self):
        r = c.whatif({"balance": 320000, "apr": 6.5, "payment": 2022.62, "extra": 50})
        bn, bi = _brute(320000, 6.5, 2022.62)
        sn, si = _brute(320000, 6.5, 2022.62, extra=50)
        self.assertEqual(r["baseline"]["periods"], bn)
        self.assertEqual(r["scenario"]["periods"], sn)
        self.assertEqual(r["periods_saved"], bn - sn)
        self.assertAlmostEqual(r["interest_saved"], bi - si, places=1)

    def test_lump_sum(self):
        r = c.whatif({"balance": 16244, "apr": 7.7, "payment": 561.6, "lump_sum": 2000})
        bn, _ = _brute(16244, 7.7, 561.6)
        sn, _ = _brute(16244, 7.7, 561.6, lump=2000)
        self.assertEqual(r["periods_saved"], bn - sn)
        self.assertGreater(r["interest_saved"], 0)

    def test_missing_fields_named(self):
        r = c.whatif({"balance": 5000})
        self.assertEqual(sorted(r["missing"]), ["apr", "payment"])

    def test_zero_apr_no_interest_saved(self):
        r = c.whatif({"balance": 8800, "apr": 0, "payment": 600, "extra": 100})
        self.assertNotIn("interest_saved", r)
        self.assertEqual(r["baseline"]["periods"], 15)
        self.assertEqual(r["scenario"]["periods"], 13)

    def test_never_to_resolved(self):
        r = c.whatif({"balance": 5000, "apr": 24, "payment": 90, "extra": 50})
        self.assertFalse(r["baseline"]["resolved"])
        self.assertTrue(r["scenario"]["resolved"])

    def test_target_payment(self):
        r = c.whatif({"balance": 10000, "apr": 12, "payment": 200, "target_months": 24})
        self.assertAlmostEqual(r["target"]["required_payment"], 470.73, places=2)
        self.assertAlmostEqual(r["target"]["extra_needed"], 270.73, places=2)


class TestMortgage(unittest.TestCase):
    BASE = {"price": 400000, "down_pct": 20, "apr": 6.5, "term_years": 30,
            "property_tax_pct": 1.8, "insurance_annual": 2400,
            "tax_growth_pct": 0, "insurance_growth_pct": 0}

    def test_totals(self):
        r = c.mortgage(self.BASE)
        self.assertEqual(r["monthly_first"]["principal_interest"], 2022.62)
        self.assertAlmostEqual(r["totals"]["principal"], 320000, places=1)
        self.assertAlmostEqual(r["totals"]["property_tax"], 7200 * 30, places=1)
        self.assertAlmostEqual(r["total_cost"], 80000 + 320000 + r["totals"]["interest"] + 7200 * 30 + 2400 * 30, places=0)

    def test_pmi_warning_when_unpriced(self):
        r = c.mortgage(dict(self.BASE, down_pct=10))
        self.assertEqual(r["totals"]["pmi"], 0)
        self.assertTrue(any("PMI" in w for w in r["warnings"]))

    def test_pmi_rules(self):
        inp = dict(self.BASE, down_pct=5, pmi_pct=0.6, extra=500)
        auto = c.mortgage(dict(inp, pmi_rule="auto78"))
        req = c.mortgage(dict(inp, pmi_rule="request80"))
        self.assertGreater(auto["pmi_months"], 0)
        self.assertLess(req["pmi_months"], auto["pmi_months"])   # extra payments help only on request
        # auto78 is on the ORIGINAL schedule: independent check
        sched = c.amortize(380000, 0.065 / 12, 360)
        m78 = next(r["period"] for r in sched["rows"] if r["balance"] <= 0.78 * 400000)
        self.assertEqual(auto["pmi_months"], m78)

    def test_net_cost_with_appreciation(self):
        r = c.mortgage(dict(self.BASE, appreciation_pct=3, horizon_years=10))
        h = r["at_horizon"]
        self.assertAlmostEqual(h["home_value"], 400000 * 1.03 ** 10, places=0)
        self.assertAlmostEqual(h["net_cost_of_ownership"], r["total_cost"] - h["net_proceeds"], places=1)


class TestLand(unittest.TestCase):
    def test_balloon_and_guidance(self):
        r = c.land_loan({"price": 100000, "down_pct": 20, "apr": 8.5, "amort_years": 20,
                         "balloon_years": 5, "land_type": "raw"})
        self.assertEqual(r["balloon_month"], 60)
        self.assertGreater(r["balloon"], 60000)
        self.assertTrue(any("35%" in w for w in r["warnings"]))

    def test_io(self):
        r = c.land_loan({"price": 100000, "down_pct": 30, "apr": 9, "amort_years": 15, "io_years": 2})
        self.assertAlmostEqual(r["io_payment"], 70000 * 0.09 / 12, places=2)
        self.assertAlmostEqual(r["payment"], round(c.amort_payment(70000, 0.0075, 156), 2), places=2)


class TestDrip(unittest.TestCase):
    def test_no_growth_no_yield_is_flat(self):
        r = c.drip({"initial_investment": 10000, "share_price": 50, "dividend_yield_pct": 0, "years": 10})
        self.assertAlmostEqual(r["final_value"], 10000, places=2)
        self.assertEqual(r["drip_advantage"], 0)

    def test_flat_price_compounding(self):
        # 4% yield, flat price, annual pay, no tax: shares grow 4%/yr exactly
        r = c.drip({"initial_investment": 10000, "share_price": 100, "dividend_yield_pct": 4,
                    "years": 10, "frequency": 1})
        self.assertAlmostEqual(r["final_value"], 10000 * 1.04 ** 10, places=2)
        self.assertAlmostEqual(r["without_drip"]["final_value"], 14000, places=2)

    def test_tax_and_discount(self):
        a = c.drip({"initial_investment": 10000, "share_price": 100, "dividend_yield_pct": 4, "years": 10})
        t = c.drip({"initial_investment": 10000, "share_price": 100, "dividend_yield_pct": 4, "years": 10, "tax_rate_pct": 15})
        d = c.drip({"initial_investment": 10000, "share_price": 100, "dividend_yield_pct": 4, "years": 10, "drip_discount_pct": 5})
        self.assertLess(t["final_value"], a["final_value"])
        self.assertGreater(d["final_value"], a["final_value"])


class TestDripInputs(unittest.TestCase):
    def test_price_derived_from_value_and_shares(self):
        r = c.drip({"initial_investment": 34185, "shares": 1000, "dividend_yield_pct": 3, "years": 5})
        self.assertNotIn("error", r)
        self.assertAlmostEqual(r["share_price_used"], 34.19, places=2)

    def test_missing_lists_everything(self):
        r = c.drip({"years": 5})
        self.assertEqual(sorted(r["missing"]), ["dividend_per_share", "initial_investment", "share_price"])

    def test_dps_instead_of_yield(self):
        r = c.drip({"shares": 100, "share_price": 50, "dividend_per_share": 2, "years": 1, "frequency": 1})
        self.assertAlmostEqual(r["dividend_yield_used_pct"], 4.0, places=6)


class TestDripSanity(unittest.TestCase):
    MFA = {"initial_investment": 10000, "share_price": 6.43, "dividend_per_share": 1.44,
           "price_growth_pct": -3.64, "years": 20}

    def test_flat_dps_with_falling_price_gets_cut_not_exploded(self):
        r = c.drip(dict(self.MFA))
        self.assertTrue(any("Dividend cut applied" in w for w in r["warnings"]))
        self.assertLessEqual(r["end_yield_pct"], r["max_yield_pct_used"] + 0.01)
        self.assertLess(r["dividend_per_share_end"], 1.44)

    def test_dps_is_anchor_not_yield(self):
        # Price triples, DPS flat: yield falls to a third, income per original share unchanged.
        r = c.drip({"shares": 100, "share_price": 50, "dividend_per_share": 2, "price_growth_pct": 0,
                    "years": 1, "frequency": 1})
        r2 = c.drip({"shares": 100, "share_price": 50, "dividend_per_share": 2, "price_growth_pct": 20,
                     "years": 1, "frequency": 1})
        self.assertAlmostEqual(r["without_drip"]["cash_dividends_collected"], 200.0, places=2)
        self.assertAlmostEqual(r2["without_drip"]["cash_dividends_collected"], 200.0, places=2)
        self.assertLess(r2["end_yield_pct"], r["end_yield_pct"])

    def test_dps_beats_conflicting_yield(self):
        r = c.drip({"shares": 100, "share_price": 50, "dividend_per_share": 2, "dividend_yield_pct": 9,
                    "years": 1, "frequency": 1})
        self.assertAlmostEqual(r["dividend_yield_used_pct"], 4.0, places=6)

    def test_income_tile_matches_final_shares_times_end_dps(self):
        r = c.drip({"shares": 100, "share_price": 50, "dividend_per_share": 2, "years": 3})
        self.assertAlmostEqual(r["annual_income_at_end"], r["final_shares"] * r["dividend_per_share_end"], delta=0.05)
        self.assertLess(r["without_drip"]["annual_income_at_end"], r["annual_income_at_end"])


class TestDividendProfile(unittest.TestCase):
    @staticmethod
    def _ts(y, m, d=15):
        import datetime as dt
        return int(dt.datetime(y, m, d, tzinfo=dt.timezone.utc).timestamp())

    def test_quarterly_growth_and_yield(self):
        now = self._ts(2026, 10, 19)
        divs = []
        for y in range(2015, 2027):
            for m in (3, 6, 9, 12):
                if (y, m) <= (2026, 9):
                    divs.append((self._ts(y, m), 0.20 * 1.05 ** (y - 2015)))
        closes = [(self._ts(2016, 10, 1), 20.0), (self._ts(2021, 10, 1), 30.0)]
        p = c.dividend_profile(40.0, divs, closes, now=now)
        self.assertEqual(p["frequency"], 4)
        self.assertAlmostEqual(p["dividend_growth_5y_pct"], 5.0, places=2)
        ttm = 0.20 * 1.05 ** 10 + 3 * 0.20 * 1.05 ** 11      # Dec-25 + Mar/Jun/Sep-26
        self.assertAlmostEqual(p["dividend_yield_pct"], round(100 * ttm / 40, 3), places=3)
        self.assertAlmostEqual(p["price_cagr_5y_pct"], round(((40 / 30) ** 0.2 - 1) * 100, 2), places=2)
        self.assertAlmostEqual(p["price_cagr_10y_pct"], round(((40 / 20) ** 0.1 - 1) * 100, 2), places=2)

    def test_slipped_payment_does_not_fake_growth(self):
        now = self._ts(2026, 10, 19)
        divs = [(self._ts(y, m), 0.25) for y in range(2019, 2027) for m in (3, 6, 9, 12) if (y, m) <= (2026, 9)]
        divs = [d for d in divs if d[0] != self._ts(2024, 12)] + [(self._ts(2025, 1, 2), 0.25)]
        p = c.dividend_profile(30.0, divs, now=now)
        self.assertAlmostEqual(p["dividend_growth_5y_pct"], 0.0, places=6)

    def test_non_payer(self):
        p = c.dividend_profile(100.0, [], now=self._ts(2026, 10, 19))
        self.assertFalse(p["pays_dividend"])


class TestOthers(unittest.TestCase):
    def test_points(self):
        r = c.points({"loan_amount": 320000, "apr_base": 6.5, "apr_points": 6.25, "points": 1})
        self.assertAlmostEqual(r["simple_breakeven_months"], 3200 / (2022.62 - 1970.30), delta=0.2)
        self.assertLess(r["true_breakeven_months"], r["simple_breakeven_months"])

    def test_affordability_binds_and_hits_cap(self):
        r = c.affordability({"annual_income": 120000, "monthly_debts": 1500, "down_payment": 60000,
                             "apr": 6.5, "property_tax_pct": 1.8, "insurance_annual": 2400})
        self.assertEqual(r["binding_constraint"], "back-end (total debt)")
        self.assertAlmostEqual(r["payment"]["total"], 120000 / 12 * 0.36 - 1500, places=1)

    def test_yields(self):
        r = c.yields({"fed_rate_pct": 24, "state_rate_pct": 5, "muni_instate_yield": 3.4})
        self.assertAlmostEqual(r["rows"][0]["taxable_equivalent_pct"], 3.4 / 0.71, places=3)
        r = c.yields({"apr_pct": 5, "compounds_per_year": 12})
        self.assertAlmostEqual(r["apy_from_apr"], 5.1162, places=4)

    def test_inflation_fisher(self):
        r = c.inflation({"amount": 100, "years": 1, "inflation_pct": 3, "nominal_return_pct": 7})
        self.assertAlmostEqual(r["real_return_pct"], (1.07 / 1.03 - 1) * 100, places=3)

    def test_annuity_implied_rate_equalizes(self):
        r = c.lump_vs_annuity({"lump_sum": 300000, "payment": 1800, "years": 25})
        r2 = c.lump_vs_annuity({"lump_sum": 300000, "payment": 1800, "years": 25,
                                "discount_rate_pct": r["implied_return_pct"]})
        self.assertAlmostEqual(r2["annuity_present_value"], 300000, delta=50)

    def test_npv_irr(self):
        r = c.npv_irr({"initial_investment": 10000, "cash_flows": "3000,3000,3000,3000",
                       "discount_rate_pct": 8})
        self.assertAlmostEqual(r["npv"], -63.62, places=2)
        self.assertAlmostEqual(r["irr_pct"], 7.714, places=2)

    def test_rent_vs_buy_runs(self):
        r = c.rent_vs_buy({"price": 400000, "apr": 6.5, "rent_monthly": 2200, "horizon_years": 15})
        self.assertEqual(len(r["yearly"]), 15)
        self.assertIn("comes out ahead", r["verdict"])


class TestRun(unittest.TestCase):
    def test_never_raises(self):
        self.assertIn("error", c.run("nope", {}))
        self.assertIn("error", c.run("loan", "garbage"))
        for name in c.CALCULATORS:
            with self.subTest(name=name):
                r = c.run(name, {})
                self.assertIsInstance(r, dict)
                r = c.run(name, {"apr": "abc", "price": "-5"})
                self.assertIsInstance(r, dict)

    def test_strips_schedule(self):
        r = c.run("loan", {"amount": 1000, "apr": 5, "term_months": 12}, include_schedule=False)
        self.assertNotIn("schedule", r)


if __name__ == "__main__":
    unittest.main()
