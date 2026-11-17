import json
import unittest

try:
    import app
except Exception:          # app deps missing on this interpreter
    app = None


@unittest.skipIf(app is None, "app not importable")
class CompactPaper(unittest.TestCase):
    def _book(self, n=25):
        pos = [{"ticker": f"T{i}", "shares": 10.123456, "avg_cost": 5.0, "price": 9.0, "value": 100.0 + i,
                "cost": 50.0, "gain": 50.0, "gain_pct": 100.0} for i in range(n)]
        pos[0]["value"] = 5000.0
        total = sum(x["value"] for x in pos) + 200
        return {"id": "d", "name": "Default", "goal": {"max_position_pct": 15}, "cash": 200.0,
                "holdings_value": total - 200, "total_value": total, "return_pct": 3.2,
                "basis_label": "since seed", "positions": pos,
                "transactions": [{"ts": i, "action": "buy", "ticker": "X", "shares": 1, "price": 2, "value": 2,
                                  "recurring": False} for i in range(40)],
                "reports": [{"summary": "x" * 3000} for _ in range(12)], "note": "HYPOTHETICAL"}

    def test_fits_and_keeps_every_position(self):
        out = app._compact_paper_for_agent(self._book())
        self.assertEqual(len(out["positions"]), 25)
        self.assertLess(len(json.dumps({"tool": "get_paper_portfolio", "result": out})), 8000)
        self.assertNotIn("reports", out)
        self.assertEqual(len(out["recent_transactions"]), 10)

    def test_weights_and_over_cap(self):
        out = app._compact_paper_for_agent(self._book())
        self.assertEqual(out["positions"][0]["ticker"], "T0")
        self.assertGreater(out["positions"][0]["weight_pct"], 50)
        self.assertEqual([x["ticker"] for x in out["over_cap"]], ["T0"])
        self.assertEqual(out["position_cap_pct"], 15)

    def test_over_cap_carries_a_trim_target_under_the_cap(self):      # RETEST-027 (d)
        o = app._compact_paper_for_agent(self._book())["over_cap"][0]
        self.assertEqual(o["trim_to_pct"], 14)
        self.assertGreater(o["sell_about_dollars"], 0)

    def test_a_name_just_over_the_cap_is_inside_the_band(self):       # RKLB 15.04% was flagged after selling down to the cap
        book = {"id": "d", "name": "D", "goal": {"max_position_pct": 15}, "cash": 0.0, "holdings_value": 10000.0, "total_value": 10000.0,
                "positions": [{"ticker": "A", "shares": 1, "price": 1, "value": 1504.0, "gain_pct": 0},
                              {"ticker": "B", "shares": 1, "price": 1, "value": 8496.0, "gain_pct": 0}]}
        self.assertEqual([x["ticker"] for x in app._compact_paper_for_agent(book)["over_cap"]], ["B"])

    def test_churn_guard_refuses_sell_then_buy_and_buy_then_sell(self):   # RETEST-027 (a)
        sides = {}
        self.assertIsNone(app._churn_block(sides, "paper_sell", {"ticker": "msft"}))
        app._churn_note(sides, "paper_sell", {"ticker": "msft"}, {"ok": True})
        self.assertIn("already sold", app._churn_block(sides, "paper_buy", {"ticker": "MSFT"})["error"])
        self.assertIsNone(app._churn_block(sides, "paper_sell", {"ticker": "MSFT"}))      # a second sell is not a reversal
        app._churn_note(sides, "paper_buy", {"ticker": "VOO"}, {"ok": True})
        self.assertIn("already bought", app._churn_block(sides, "paper_sell", {"ticker": "voo"})["error"])

    def test_a_failed_trade_does_not_lock_the_name(self):
        sides = {}
        app._churn_note(sides, "paper_sell", {"ticker": "MSFT"}, {"error": "no such position"})
        self.assertIsNone(app._churn_block(sides, "paper_buy", {"ticker": "MSFT"}))

    def test_passthrough_on_error_shape(self):
        self.assertEqual(app._compact_paper_for_agent({"error": "x"}), {"error": "x"})


if __name__ == "__main__":
    unittest.main()
