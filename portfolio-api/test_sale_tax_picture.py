"""The Strategist's tax note for a REAL position above the size cap (Jason 2026-11-17): the cost of a sale, split by account type."""
import unittest

try:
    import app
except Exception:
    app = None


@unittest.skipIf(app is None, "app not importable")
class SaleTaxPicture(unittest.TestCase):
    def _d(self):
        return {"accounts": [{"name": "Brokerage", "type": "Investments", "subtype": "Brokerage"},
                             {"name": "Roth IRA", "type": "Investments", "subtype": "Roth IRA"}],
                "by_account": [{"account": "Brokerage", "holdings": [{"ticker": "PLTR", "value": 500000.0, "gain": 400000.0},
                                                                     {"ticker": "VOO", "value": 100000.0, "gain": 10000.0}]},
                               {"account": "Roth IRA", "holdings": [{"ticker": "PLTR", "value": 50000.0, "gain": 30000.0},
                                                                    {"ticker": "VOO", "value": 50000.0, "gain": 5000.0}]}],
                "consolidated": [{"ticker": "PLTR", "value": 550000.0, "weight_pct": 61.1},
                                 {"ticker": "VOO", "value": 150000.0, "weight_pct": 12.0}]}

    def test_only_names_over_the_cap_are_listed_and_roth_room_is_tax_free(self):
        r = app._sale_tax_picture(15.0, d=self._d(), gross_ord=60000.0)
        self.assertEqual([x["ticker"] for x in r["positions"]], ["PLTR"])
        p = r["positions"][0]
        self.assertEqual(p["sellable_without_tax_in_ira_roth_hsa"], 50000)
        self.assertAlmostEqual(p["needs_taxable_sale"], p["amount_above_cap"] - 50000, delta=1)
        self.assertGreater(p["taxable_gain_realized"], 0)
        self.assertGreater(p["tax_if_sold_in_one_year"], 0)
        self.assertIn("staged", p)
        self.assertIn("not advice", r["assumptions"])

    def test_unknown_basis_gives_no_invented_tax(self):
        d = self._d()
        for h in d["by_account"][0]["holdings"]:
            h["gain"] = None
        p = app._sale_tax_picture(15.0, d=d, gross_ord=60000.0)["positions"][0]
        self.assertNotIn("tax_if_sold_in_one_year", p)
        self.assertIn("basis", p["note"])

    def test_nothing_over_cap(self):
        self.assertTrue(app._sale_tax_picture(70.0, d=self._d())["none_over_cap"])


if __name__ == "__main__":
    unittest.main()
