# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_budget_panel.py -- SETTINGS > API BUDGETS sub-tab (Jason 2026-11-12): the page lists each provider's gate state,
Tavily credit spend by source, and edits the /api/budgets knobs."""
import os, unittest

HTML = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html"), encoding="utf-8").read()


class TestBudgetPanelWiring(unittest.TestCase):
    def test_subtab_registered(self):
        self.assertIn("{key:'budgets',label:'API BUDGETS',build:()=>budgetContent()}", HTML)
        self.assertIn("BUDGETS=null, BUDGETS_LOADING=false", HTML)

    def test_panel_reads_and_writes_the_budget_api(self):
        self.assertIn("fetch('/api/budgets')", HTML)
        self.assertIn("fetch('/api/budgets',{method:'POST'", HTML)
        self.assertIn("fetch('/api/budgets/probe'", HTML)

    def test_panel_shows_state_spend_and_knobs(self):
        for needle in ("PAUSED", "Next test in", "Credits by source", "credits left before your", "Spend cap per cycle",
                       "Idea-hunting credits per cycle", "Renewal date", "PROBE NOW", "bud-hc-", "bud-bc-", "bud-rn-"):
            self.assertIn(needle, HTML, needle)

    def test_fields_match_what_the_endpoint_accepts(self):
        for k in ("batch_daily", "hard_cap_usd", "batch_cycle_credits", "renewal"):
            self.assertIn(k, HTML)

    def test_loading_guard_prevents_refetch_loop(self):
        self.assertIn("if(BUDGETS_LOADING) return;", HTML)


if __name__ == "__main__":
    unittest.main()
