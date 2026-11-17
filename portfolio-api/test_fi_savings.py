"""FI annual savings = cash left over + payroll 401(k) + after-tax 401(k) + employer match (override wins); and the expense-category breakdown."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as appmod  # noqa: E402

BEN = {"facts": {"salary": 192520, "aftertax_pct": 0}, "sections": {"k401": {"deferral_dollars": 19250.0, "match": {"earned_match_dollars": 7700.0}}}}


class Savings(unittest.TestCase):
    def test_payroll_401k_and_match_are_added_to_the_cash_left_over(self):
        sb = appmod._savings_breakdown(200000.0, 191613.0, BEN)
        self.assertEqual((sb["left_over"], sb["k401_employee"], sb["employer_match"], sb["source"]), (8387.0, 19250.0, 7700.0, "estimate"))
        self.assertEqual(sb["total"], 8387.0 + 19250.0 + 7700.0)

    def test_a_typed_total_replaces_the_estimate_and_is_never_added_to(self):
        sb = appmod._savings_breakdown(200000.0, 191613.0, BEN, override=30000.0)
        self.assertEqual((sb["total"], sb["source"]), (30000.0, "override"))
        self.assertIsNone(sb["k401_employee"])

    def test_after_tax_401k_counts_and_hsa_does_not(self):
        b = {"facts": {"salary": 100000, "aftertax_pct": 5, "hsa_contribution": 4000}, "sections": {"k401": {"deferral_dollars": 10000.0, "match": {"earned_match_dollars": 3000.0}}}}
        sb = appmod._savings_breakdown(90000.0, 80000.0, b)
        self.assertEqual(sb["k401_after_tax"], 5000.0)
        self.assertEqual(sb["total"], 10000.0 + 5000.0 + 10000.0 + 3000.0)
        self.assertTrue([n for n in sb["notes"] if "HSA" in n])

    def test_without_benefits_data_it_says_so_and_uses_only_the_cash_left_over(self):
        for b in (None, {"setup_needed": True}, {"facts": {"salary": None}}):
            sb = appmod._savings_breakdown(100000.0, 90000.0, b)
            self.assertEqual(sb["total"], 10000.0)
            self.assertTrue([n for n in sb["notes"] if "NOT included" in n])

    def test_without_cash_flow_the_payroll_part_still_counts_and_the_gap_is_stated(self):
        sb = appmod._savings_breakdown(None, None, BEN)
        self.assertIsNone(sb["left_over"])
        self.assertEqual(sb["total"], 19250.0 + 7700.0)
        self.assertTrue([n for n in sb["notes"] if "cash left over is unknown" in n])
        self.assertIsNone(appmod._savings_breakdown(None, None, None)["total"])

    def test_negative_cash_left_over_is_kept(self):
        self.assertEqual(appmod._savings_breakdown(80000.0, 95000.0, BEN)["total"], -15000.0 + 19250.0 + 7700.0)


CF = {"cashflow": {"byCategory": [
    {"groupBy": {"category": {"name": "Loan Repayment", "group": {"name": "Financial", "type": "expense"}}}, "summary": {"sum": -9600.0}},
    {"groupBy": {"category": {"name": "Mortgage", "group": {"name": "Housing", "type": "expense"}}}, "summary": {"sum": -24000.0}},
    {"groupBy": {"category": {"name": "Groceries", "group": {"name": "Food", "type": "expense"}}}, "summary": {"sum": -12000.0}},
    {"groupBy": {"category": {"name": "Paychecks", "group": {"name": "Income", "type": "income"}}}, "summary": {"sum": 150000.0}},
    {"groupBy": {"category": {"name": "Credit Card Payment", "group": {"name": "Transfers", "type": "transfer"}}}, "summary": {"sum": -20000.0}},
    {"groupBy": {"category": {"group": {"type": "expense"}}}, "summary": {"sum": -1.0}},             # no name: ignored
    {"groupBy": {"category": {"name": "Odd", "group": {"type": "expense"}}}, "summary": {}}]}}        # no total: ignored


class Breakdown(unittest.TestCase):
    def setUp(self):
        appmod._CF_CAT_MEMO.update(at=0.0, rows=None)

    def test_categories_are_parsed_sorted_and_debt_like_ones_marked(self):
        rows = appmod._categories_from_cashflow(CF)
        self.assertEqual([r["category"] for r in rows], ["Paychecks", "Mortgage", "Credit Card Payment", "Groceries", "Loan Repayment"])      # largest first
        by = {r["category"]: r for r in rows}
        self.assertTrue(by["Loan Repayment"]["debt_like"] and by["Mortgage"]["debt_like"])
        self.assertFalse(by["Groceries"]["debt_like"])
        self.assertEqual(len(rows), 5)
        self.assertEqual(appmod._categories_from_cashflow({}), [])
        self.assertEqual(appmod._categories_from_cashflow({"cashflow": {"byCategory": "nonsense"}}), [])

    def test_endpoint_splits_expenses_income_and_transfers(self):
        with mock.patch.object(appmod, "_post", lambda *a, **k: CF):
            r = appmod.get_cashflow_breakdown()
        self.assertEqual((r["expense_total"], r["income_total"], r["transfers_total"], r["debt_like_total"]), (45600.0, 150000.0, 20000.0, 33600.0))
        self.assertEqual([x["category"] for x in r["debt_like"]], ["Mortgage", "Loan Repayment"])
        self.assertIn("Monarch decides", r["note"])

    def test_a_monarch_failure_is_reported_not_raised(self):
        with mock.patch.object(appmod, "_post", side_effect=RuntimeError("down")):
            self.assertFalse(appmod.get_cashflow_breakdown()["available"])

    def test_the_call_uses_an_allowlisted_monarch_operation(self):
        self.assertIn("get_cashflow", appmod.MONARCH_READ_OPS)


class Page(unittest.TestCase):
    def test_the_assumptions_page_shows_the_breakdown_and_the_expense_categories(self):
        html = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html"), encoding="utf-8").read()
        for needle in ("function savBreakHtml(sb)", "function loadExpenseBreakdown(box)", "fetch('/api/cashflow-breakdown')", "id=\"ret-sav-break\"",
                       "What is in my expenses figure?", "That changes the FI target only"):
            self.assertIn(needle, html, needle)


if __name__ == "__main__":
    unittest.main()
