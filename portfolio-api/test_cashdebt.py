"""
test_cashdebt.py — unit tests for the cash / liquidity / debt desk (cashdebt.py).

Pure/stdlib (unittest), zero network. Run:  python3 -m unittest test_cashdebt -v

Covers the two 2026-08-08 additions and the invariants they must not break:
  · remaining_payments() — the closed-form amortization solve, checked against a brute-force
    month loop, plus every "insufficient information" branch. The point of most of these cases
    is that the function returns a *reason* instead of raising or guessing: a panel that 500s
    because one card is missing an APR is worse than one that says which field to fill in.
  · ef_sources() / emergency_fund() — explicit EF designation, per-source caps, the drawdown
    stress figure, tax-deferred ineligibility, and the fallback to auto-detected cash for a
    household that has designated nothing (which must behave exactly as it did before).
  · idle_cash() — that designated NON-cash reserve reduces how much cash is spoken for, and
    that drag is still computed on cash alone.
"""
import unittest

import cashdebt as cd


def _brute_months(balance, apr_pct, payment, cap=1200):
    """Independent month-by-month loop — deliberately the dumbest possible implementation, so it
    can't share a bug with the closed form it's checking."""
    b, r, n = balance, apr_pct / 100.0 / 12.0, 0
    while b > 0.005 and n < cap:
        b += b * r
        b -= min(payment, b)
        n += 1
    return n if n < cap else None


class TestRemainingPayments(unittest.TestCase):
    def test_matches_brute_force(self):
        for bal, apr, pmt in [(16244, 7.7, 561.6), (15752, 5.125, 300.0), (8800, 0.0, 600.0),
                              (1543, 19.49, 60.0), (20000, 29.99, 700.0), (500, 5.0, 25.0)]:
            with self.subTest(bal=bal, apr=apr, pmt=pmt):
                self.assertEqual(cd.remaining_payments(bal, apr, pmt)["months"],
                                 _brute_months(bal, apr, pmt))

    def test_zero_apr_is_balance_over_payment(self):
        r = cd.remaining_payments(8800, 0, 600)
        self.assertEqual(r["months"], 15)          # ceil(14.67) — a partial final payment counts
        self.assertEqual(r["status"], "ok")

    def test_rounds_up_never_down(self):
        # 100 / 30 = 3.33 payments; reporting 3 would say the debt clears a month early.
        self.assertEqual(cd.remaining_payments(100, 0, 30)["months"], 4)

    def test_zero_balance_is_paid_not_unknown(self):
        r = cd.remaining_payments(0, 19.49, 100)
        self.assertEqual((r["months"], r["status"]), (0, "paid"))

    def test_missing_balance_is_unknown_not_zero(self):
        # A missing balance must NOT be reported as "0 payments left / paid" — that reads as
        # good news for what is actually an absence of data.
        r = cd.remaining_payments(None, 19.49, 100)
        self.assertEqual(r["status"], "unknown")
        self.assertIn("balance", r["missing"])

    def test_missing_apr_does_not_fall_back_to_zero_interest(self):
        # Treating an unset APR as 0% would understate a 19.49% card's payoff by years — the one
        # direction where a helpful-looking guess actively misleads.
        r = cd.remaining_payments(1543, None, 60)
        self.assertIsNone(r["months"])
        self.assertEqual(r["missing"], ["APR"])

    def test_missing_payment(self):
        r = cd.remaining_payments(1543, 19.49, None)
        self.assertEqual(r["missing"], ["minimum payment"])

    def test_missing_both_names_both(self):
        self.assertEqual(cd.remaining_payments(1543, None, None)["missing"],
                         ["APR", "minimum payment"])

    def test_payment_below_interest_never_retires(self):
        # $100 against $10,000 at 24% (= $200/mo interest): the balance grows forever. This is
        # also the branch that keeps log() from ever seeing a non-positive argument.
        r = cd.remaining_payments(10000, 24.0, 100)
        self.assertEqual(r["status"], "never")
        self.assertIsNone(r["months"])

    def test_payment_exactly_equals_interest_never_retires(self):
        self.assertEqual(cd.remaining_payments(10000, 12.0, 100)["status"], "never")

    def test_payment_a_dollar_above_interest_resolves(self):
        r = cd.remaining_payments(10000, 24.0, 201)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["months"], _brute_months(10000, 24.0, 201))

    def test_pay_in_full_has_no_schedule(self):
        r = cd.remaining_payments(16, 16.49, 25, pay_in_full=True)
        self.assertEqual((r["months"], r["status"]), (None, "paid"))

    def test_garbage_input_never_raises(self):
        for args in [("abc", "xyz", "n/a"), (None, None, None), ([], {}, object())]:
            with self.subTest(args=args):
                r = cd.remaining_payments(*args)
                self.assertIn(r["status"], ("unknown", "paid", "never", "ok", "capped"))

    def test_capped_rather_than_absurd(self):
        r = cd.remaining_payments(1_000_000, 0, 100, max_months=600)
        self.assertEqual((r["months"], r["status"]), (600, "capped"))


class TestAvalancheRows(unittest.TestCase):
    def test_rows_carry_payment_counts(self):
        av = cd.avalanche([{"name": "Loan", "balance": 16244, "rate_pct": 7.7, "min_payment": 561.6},
                           {"name": "NoRate", "balance": 21}], 7.5)
        by = {r["name"]: r for r in av["rows"]}
        self.assertEqual(by["Loan"]["remaining_payments"], 33)
        self.assertEqual(by["NoRate"]["remaining_payments_status"], "unknown")


class TestPayoffSimulator(unittest.TestCase):
    DEBTS = [{"name": "A", "balance": 16244, "rate_pct": 7.7, "min_payment": 561.6},
             {"name": "B", "balance": 15752, "rate_pct": 5.125, "min_payment": 300.0}]

    def test_retired_at_is_reported_per_debt(self):
        po = cd.payoff_simulator(self.DEBTS)
        self.assertTrue(po["available"])
        self.assertEqual(set(po["avalanche"]["retired_at"]), {"A", "B"})

    def test_rollover_beats_standalone(self):
        # The whole argument for following a payoff plan: the low-rate debt clears far sooner in
        # the plan than it would on its own, because the first payoff's minimum rolls onto it.
        po = cd.payoff_simulator(self.DEBTS)
        standalone_b = cd.remaining_payments(15752, 5.125, 300.0)["months"]
        self.assertLess(po["avalanche"]["retired_at"]["B"], standalone_b)

    def test_avalanche_never_costs_more_interest_than_snowball(self):
        po = cd.payoff_simulator(self.DEBTS + [{"name": "C", "balance": 900, "rate_pct": 22.0,
                                                "min_payment": 40.0}])
        self.assertLessEqual(po["avalanche"]["total_interest"], po["snowball"]["total_interest"])


class TestEFSources(unittest.TestCase):
    SRC = [{"name": "Checking", "balance": 8081, "tier": "cash"},
           {"name": "Brokerage", "balance": 62000, "tier": "taxable", "cap": 25000},
           {"name": "Roth", "balance": 90000, "tier": "roth_basis", "cap": 34000},
           {"name": "401k", "balance": 400000, "tier": "deferred"}]

    def test_cap_limits_what_counts(self):
        r = cd.ef_sources(self.SRC, 20.0)
        self.assertEqual(r["total"], 8081 + 25000 + 34000)

    def test_deferred_counts_zero_but_is_still_reported(self):
        r = cd.ef_sources(self.SRC, 20.0)
        k = next(x for x in r["rows"] if x["name"] == "401k")
        self.assertEqual(k["counted"], 0.0)
        self.assertFalse(k["eligible"])
        self.assertIn("401k", r["ineligible"])     # reported, not silently dropped

    def test_haircut_applies_to_balance_not_to_the_cap(self):
        # $25k earmarked inside a $62k brokerage is still fully there after a 20% drop ($49.6k
        # remains). Haircutting the cap itself would double-count the loss.
        r = cd.ef_sources([{"name": "B", "balance": 62000, "tier": "taxable", "cap": 25000}], 20.0)
        self.assertEqual(r["rows"][0]["counted_stress"], 25000.0)
        # ...but once the drawdown takes the balance below the cap, the cap stops binding.
        r2 = cd.ef_sources([{"name": "B", "balance": 30000, "tier": "taxable", "cap": 28000}], 20.0)
        self.assertEqual(r2["rows"][0]["counted_stress"], 24000.0)

    def test_cash_takes_no_haircut(self):
        r = cd.ef_sources([{"name": "C", "balance": 10000, "tier": "cash"}], 50.0)
        self.assertEqual(r["rows"][0]["counted_stress"], 10000.0)

    def test_unknown_tier_degrades_to_cash_at_par(self):
        r = cd.ef_sources([{"name": "?", "balance": 500, "tier": "not-a-tier"}], 20.0)
        self.assertEqual(r["rows"][0]["tier"], "cash")
        self.assertEqual(r["rows"][0]["counted"], 500.0)

    def test_malformed_rows_do_not_raise(self):
        r = cd.ef_sources([{"name": "x", "balance": None, "cap": "abc"}, {}, None] or [], 20.0)
        self.assertEqual(r["total"], 0.0)


class TestEmergencyFund(unittest.TestCase):
    def test_fallback_to_auto_cash_is_unchanged(self):
        ef = cd.emergency_fund(11081, 6288, 6.0)
        self.assertFalse(ef["designated"])
        self.assertEqual(ef["basis"], "auto-cash")
        self.assertEqual(ef["months_covered"], 1.8)
        self.assertEqual(ef["status"], "critical")

    def test_designation_overrides_auto_cash(self):
        src = cd.ef_sources([{"name": "C", "balance": 8081, "tier": "cash"},
                             {"name": "B", "balance": 62000, "tier": "taxable"}], 20.0)
        ef = cd.emergency_fund(11081, 6288, 6.0, sources=src, stress_drawdown_pct=20.0)
        self.assertTrue(ef["designated"])
        self.assertEqual(ef["ef_total"], 70081.0)          # not the 11081 cash figure
        self.assertLess(ef["months_covered_stress"], ef["months_covered"])

    def test_empty_designation_falls_back(self):
        ef = cd.emergency_fund(11081, 6288, 6.0, sources=cd.ef_sources([]))
        self.assertFalse(ef["designated"])

    def test_undesignated_cash_is_surfaced_once_anything_is_designated(self):
        # Designating one brokerage silently drops checking/savings OUT of a figure they used to
        # sit inside (designation is all-or-nothing by design). That omission must be visible, or
        # it reads as the reserve having shrunk for no reason.
        src = cd.ef_sources([{"name": "B", "balance": 17482, "tier": "taxable"}], 20.0)
        ef = cd.emergency_fund(11081, 6288, 6.0, sources=src)
        self.assertEqual(ef["undesignated_cash"], 11081.0)
        self.assertIn("hasn't been ruled on", ef["note"].lower())

    def test_no_undesignated_figure_while_still_on_the_guess(self):
        ef = cd.emergency_fund(11081, 6288, 6.0)
        self.assertEqual(ef["undesignated_cash"], 0.0)   # the guess already counts that cash

    def test_ruled_out_cash_stops_being_reported(self):
        # Jason's case: the checking balances are committed to monthly bills and were deliberately
        # excluded. Once ruled on, the caller passes unreviewed_cash=0 and the prompt goes silent —
        # a decided question must not keep being asked.
        src = cd.ef_sources([{"name": "B", "balance": 19051, "tier": "taxable"}], 20.0)
        ef = cd.emergency_fund(8470, 6288, 6.0, sources=src, unreviewed_cash=0)
        self.assertEqual(ef["undesignated_cash"], 0.0)
        self.assertNotIn("ruled on", ef["note"].lower())
        self.assertEqual(ef["ef_total"], 19051.0)   # the reserve figure itself is unaffected

    def test_partially_ruled_cash_reports_only_the_remainder(self):
        src = cd.ef_sources([{"name": "B", "balance": 19051, "tier": "taxable"}], 20.0)
        ef = cd.emergency_fund(8470, 6288, 6.0, sources=src, unreviewed_cash=632)
        self.assertEqual(ef["undesignated_cash"], 632.0)

    def test_unreviewed_cash_defaults_to_cash_total_when_not_supplied(self):
        # Back-compat: a caller that doesn't know about the three-state model still gets the old
        # behavior rather than a silent zero.
        src = cd.ef_sources([{"name": "B", "balance": 19051, "tier": "taxable"}], 20.0)
        self.assertEqual(cd.emergency_fund(8470, 6288, 6.0, sources=src)["undesignated_cash"], 8470.0)

    def test_no_essential_spend_yields_no_verdict_rather_than_a_crash(self):
        ef = cd.emergency_fund(11081, None, 6.0)
        self.assertIsNone(ef["months_covered"])
        self.assertIsNone(ef["status"])
        self.assertIsNone(ef["gap_dollars"])


class TestIdleCash(unittest.TestCase):
    def test_noncash_reserve_frees_cash(self):
        ic = cd.idle_cash(11081, 3.74, 0.4, 37728, "TX", ef_covered_by_noncash=59000)
        self.assertEqual(ic["ef_target_from_cash"], 0.0)
        self.assertEqual(ic["excess_over_ef"], 11081.0)

    def test_without_designation_cash_is_still_spoken_for(self):
        ic = cd.idle_cash(11081, 3.74, 0.4, 37728, "TX")
        self.assertEqual(ic["excess_over_ef"], 0.0)

    def test_drag_is_computed_on_cash_only(self):
        # Drag must never be computed on a designated brokerage reserve: it isn't sitting at a
        # deposit yield, so folding it in would invent a phantom five-figure "loss".
        a = cd.idle_cash(11081, 3.74, 0.4, 37728, "TX")
        b = cd.idle_cash(11081, 3.74, 0.4, 37728, "TX", ef_covered_by_noncash=500000)
        self.assertEqual(a["drag_annual"], b["drag_annual"])


class TestRunIntegration(unittest.TestCase):
    def test_plan_months_are_stamped_onto_avalanche_rows(self):
        res = cd.run({"cash_total": 11081, "annual_expenses": 116086, "needs_pct": 65,
                      "tbill_pct": 3.74, "expected_real_pct": 5.0, "state": "TX",
                      "debts": [{"name": "A", "balance": 16244, "rate_pct": 7.7,
                                 "min_payment": 561.6},
                                {"name": "B", "balance": 15752, "rate_pct": 5.125,
                                 "min_payment": 300.0}]})
        by = {r["name"]: r for r in res["avalanche"]["rows"]}
        self.assertIsNotNone(by["A"]["plan_months"])
        self.assertLess(by["B"]["plan_months"], by["B"]["remaining_payments"])

    def test_undesignated_household_gets_told_the_number_is_a_guess(self):
        res = cd.run({"cash_total": 11081, "annual_expenses": 116086, "needs_pct": 65,
                      "tbill_pct": 3.74, "expected_real_pct": 5.0, "state": "TX", "debts": []})
        msgs = " ".join(a["msg"] for a in res["alerts"])
        self.assertIn("designated", msgs)

    def test_deferred_designation_raises_an_alert(self):
        res = cd.run({"cash_total": 0, "annual_expenses": 116086, "needs_pct": 65,
                      "tbill_pct": 3.74, "expected_real_pct": 5.0, "state": "TX", "debts": [],
                      "ef_sources": [{"name": "401k", "balance": 400000, "tier": "deferred"}]})
        self.assertTrue(any("tax-deferred" in a["msg"] for a in res["alerts"]))


if __name__ == "__main__":
    unittest.main()
