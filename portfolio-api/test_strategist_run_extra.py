"""RETEST-037: the Strategist run log keeps which names were over the cap (with the trim target shown) and the tax-picture rows, so a reviewer can check them."""
import os, sys, tempfile, unittest

_TMP = tempfile.mkdtemp(prefix="sre-test-")
os.environ["STORE_DB"] = os.path.join(_TMP, "store.db")
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402


class RunLogExtra(unittest.TestCase):
    def rec(self, **kw):
        r = {"ts": 1, "scenario_id": "d", "scenario_name": "Default", "risk_profile": "moderate", "max_iter": 12, "iterations": 3,
             "tool_calls": {"get_paper_portfolio": 1}, "repeat_calls": 0, "trades": 1, "search_calls": 0, "search_budget": 3,
             "nudged": False, "converged": True, "ended": "report", "error": None, "duration_s": 5}
        r.update(kw)
        return r

    def test_round_trip(self):
        store.strategist_run_log(self.rec(ts=10, over_cap_seen=[{"ticker": "PLTR", "weight_pct": 61.2, "trim_to_pct": 14, "sell_about_dollars": 174000}],
                                          sale_tax_rows=[{"ticker": "PLTR", "amount_above_cap": 148873, "held_in_ira_roth_hsa": 235035, "needs_taxable_sale": 0}]))
        r = next(x for x in store.strategist_runs_read() if x["ts"] == 10)
        self.assertEqual(r["over_cap_seen"][0]["trim_to_pct"], 14)
        self.assertEqual(r["sale_tax_rows"][0]["needs_taxable_sale"], 0)

    def test_a_run_without_them_reads_back_unchanged(self):
        store.strategist_run_log(self.rec(ts=11))
        r = next(x for x in store.strategist_runs_read() if x["ts"] == 11)
        self.assertNotIn("over_cap_seen", r)
        self.assertEqual(r["tool_calls"], {"get_paper_portfolio": 1})


if __name__ == "__main__":
    unittest.main()
