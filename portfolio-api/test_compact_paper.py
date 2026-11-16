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

    def test_passthrough_on_error_shape(self):
        self.assertEqual(app._compact_paper_for_agent({"error": "x"}), {"error": "x"})


if __name__ == "__main__":
    unittest.main()
