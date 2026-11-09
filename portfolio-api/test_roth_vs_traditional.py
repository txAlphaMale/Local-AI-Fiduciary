import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import calculators as C

BASE = {"annual_election": 15000, "years_to_retirement": 20, "return_pct": 6, "rate_now_pct": 24, "rate_retire_pct": 22}
FV = ((1.06) ** 20 - 1) / 0.06


def by(rows, pct):
    return [r for r in rows if r["roth_pct"] == pct][0]


class TestRoth(unittest.TestCase):
    def test_known_numbers_traditional_ahead_when_retirement_rate_is_lower(self):
        r = C.run("roth_vs_traditional", BASE)
        eq = r["equal_take_home"]
        self.assertAlmostEqual(by(eq, 100)["after_tax_value"], 15000 * FV, delta=1)
        self.assertAlmostEqual(by(eq, 0)["gross_per_year"], 15000 / 0.76, delta=0.01)
        self.assertAlmostEqual(by(eq, 0)["after_tax_value"], 15000 / 0.76 * FV * 0.78, delta=1)
        self.assertEqual(r["best_after_tax"], 0)
        self.assertIn("Traditional is ahead", r["lean"])
        self.assertTrue(by(eq, 0)["after_tax_value"] > by(eq, 50)["after_tax_value"] > by(eq, 100)["after_tax_value"])

    def test_equal_rates_tie(self):
        r = C.run("roth_vs_traditional", dict(BASE, rate_retire_pct=24))
        v = [x["after_tax_value"] for x in r["equal_take_home"]]
        self.assertAlmostEqual(v[0], v[2], delta=1); self.assertAlmostEqual(v[1], v[2], delta=1)
        self.assertIn("tie", r["lean"])

    def test_roth_ahead_when_retirement_rate_is_higher(self):
        r = C.run("roth_vs_traditional", dict(BASE, rate_retire_pct=32))
        self.assertEqual(r["best_after_tax"], 100); self.assertIn("Roth is ahead", r["lean"])

    def test_equal_take_home_really_is_equal_cost(self):
        r = C.run("roth_vs_traditional", BASE)
        for row in r["equal_take_home"]:
            pre = row["pretax_per_year"]
            self.assertAlmostEqual(row["gross_per_year"] - pre * 0.24, 15000, delta=0.05)

    def test_same_gross_view_shows_paycheck_difference(self):
        same = C.run("roth_vs_traditional", BASE)["same_gross_election"]
        self.assertAlmostEqual(by(same, 0)["take_home_cost_per_year"], 15000 * 0.76, delta=0.01)
        self.assertAlmostEqual(by(same, 100)["take_home_cost_per_year"], 15000, delta=0.01)

    def test_salary_and_pct_and_income_route(self):
        r = C.run("roth_vs_traditional", {"salary": 150000, "election_pct": 10, "years_to_retirement": 20,
                                           "income_now": 130000, "income_retire": 60000, "filing_status": "mfj"})
        self.assertEqual(r["assumptions"]["annual_election"], 15000.0)
        self.assertEqual(r["assumptions"]["rate_now_pct"], 22.0)        # 2026 MFJ bracket at $130k taxable
        self.assertEqual(r["assumptions"]["rate_retire_pct"], 12.0)     # 2026 MFJ bracket at $60k taxable
        self.assertIn("2026 brackets", r["assumptions"]["rate_now_source"])

    def test_limit_warning_and_errors(self):
        r = C.run("roth_vs_traditional", dict(BASE, annual_election=24000, annual_limit=24500))
        self.assertTrue(r["warnings"] and by(r["equal_take_home"], 0).get("over_limit"))
        self.assertIn("error", C.run("roth_vs_traditional", {"years_to_retirement": 20, "rate_now_pct": 24, "rate_retire_pct": 22}))
        self.assertIn("missing", C.run("roth_vs_traditional", {"annual_election": 1000, "years_to_retirement": 5}))
        self.assertIn("error", C.run("roth_vs_traditional", dict(BASE, rate_now_pct=150)))

    def test_zero_return(self):
        r = C.run("roth_vs_traditional", dict(BASE, return_pct=0))
        self.assertAlmostEqual(by(r["equal_take_home"], 100)["balance_at_retirement"], 15000 * 20, delta=1)

    def test_registered_for_chat_and_ui(self):
        app = open(os.path.join(os.path.dirname(__file__), "app.py"), encoding="utf-8").read()
        html = open(os.path.join(os.path.dirname(__file__), "static", "index.html"), encoding="utf-8").read()
        self.assertIn("roth_vs_traditional: salary", app)
        self.assertIn("CALC_SPECS.roth_vs_traditional=", html); self.assertIn("CALC_RENDER.roth_vs_traditional=", html)
        self.assertIn("calcPanel('roth_vs_traditional')", html)          # reachable as a sub-tab, not just registered
        for key in C.CALCULATORS:                                          # every registered calculator has a sub-tab
            self.assertTrue(("calcPanel('%s')" % key) in html or (key == "whatif" and "whatifPanel()" in html), key)


if __name__ == "__main__":
    unittest.main()
