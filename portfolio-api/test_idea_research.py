# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
import asyncio, datetime, glob, json, os, re, sys, tempfile, time, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="scout-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    with open(_f) as _fh:
        for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', _fh.read()):
            os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod, scout, sleeves, store, ticker_state  # noqa: E402


import json, os
ROW = {"ticker": "ABCD", "name": "Abcd Inc", "state": "active", "s1": {"revenue": 20_000_000, "rev_growth_pct": 40.0, "runway_months": 30},
       "s2": {"price": 12.5, "market_cap": 900_000_000, "sector": "Technology"}, "s3": None}


class IdeaResearchTool(unittest.TestCase):
    def _names(self, tools):
        return [t["function"]["name"] for t in tools]

    def test_both_agents_and_the_advice_run_can_call_it(self):
        self.assertIn("get_idea_research", self._names(appmod.STRATEGIST_TOOLS))
        self.assertIn("get_idea_research", self._names(appmod.STRATEGIST_ADVICE_TOOLS))
        self.assertIn("get_idea_research", self._names(appmod.FIDUCIARY_TOOLS))
        for t in (appmod.STRATEGIST_TOOLS, appmod.FIDUCIARY_TOOLS):    # read-only: no ordering, no required args
            f = next(x for x in t if x["function"]["name"] == "get_idea_research")["function"]
            self.assertNotIn("required", f["parameters"])

    def test_listing_sleeve_and_ticker_views(self):
        with mock.patch.object(ticker_state, "screened_rows", return_value=[ROW]), \
             mock.patch.object(ticker_state, "scout_briefs", return_value={}), \
             mock.patch.object(ticker_state, "get_note", return_value={"created_at": 1, "note": {"thesis": "t"}, "sleeves": ["growth"]}), \
             mock.patch.object(appmod, "_held_sector_weights", return_value={}):
            lst = appmod._idea_research()
            self.assertIn("growth", [x["id"] for x in lst["sleeves"]])
            self.assertEqual(lst["screened"], 1)
            bad = appmod._idea_research(sleeve="nope")
            self.assertEqual(bad["error"], "unknown sleeve")
            tk = appmod._idea_research(ticker="abcd")
            self.assertTrue(tk["found"])
            self.assertEqual(tk["scout_note"]["note"]["thesis"], "t")
            self.assertEqual(tk["screen"]["price"], 12.5)
            sl = appmod._idea_research(sleeve="growth", limit=999)
            self.assertLessEqual(len(sl["rows"]), 25)
            json.dumps([lst, tk, sl])                                   # must serialise for the model
        with mock.patch.object(ticker_state, "screened_rows", return_value=[]), \
             mock.patch.object(ticker_state, "scout_briefs", return_value={}), \
             mock.patch.object(ticker_state, "get_note", return_value=None):
            self.assertFalse(appmod._idea_research(ticker="ZZZZ")["found"])


if __name__ == "__main__":
    unittest.main()


class TestBalanceHeldWeights(unittest.TestCase):
    def test_semiconductors_is_not_a_gap_when_the_tech_family_is_heavy_and_funds_are_reported(self):
        cons = [{"ticker": "AAA", "type": "Stock", "value": 900.0}, {"ticker": "BBB", "type": "Stock", "value": 10.0},
                {"ticker": "FND", "type": "ETF", "value": 450.0}]
        profs = {"AAA": {"finnhubIndustry": "Technology"}, "BBB": {"finnhubIndustry": "Semiconductors"}}
        appmod._HELD_CACHE.update(at=0.0, w={}, funds_pct=None)
        with mock.patch.object(appmod, "_data", return_value={"consolidated": cons}), \
             mock.patch.object(appmod, "_profile", side_effect=lambda t: (profs[t], {})):
            w = appmod._held_sector_weights()
        self.assertGreaterEqual(w["Semiconductors"], 3)            # covered by the family, so Balance will not call it a gap
        self.assertAlmostEqual(appmod._HELD_CACHE["funds_pct"], 33.1, places=1)
        appmod._HELD_CACHE.update(at=0.0, w={}, funds_pct=None)


class TestGroundingRule(unittest.TestCase):
    def test_agent_prompts_forbid_background_from_memory(self):
        src = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
        self.assertGreaterEqual(src.count("do not add what the company does, former names, mergers"), 4)


class TestQuoteCache(unittest.TestCase):
    """RETEST-021: the paper ledger showed $0 because every position's quote was a separate gated Finnhub call."""
    def setUp(self):
        store._MEM_CACHE.pop(("finnhub_quote", "QCT"), None)

    def test_quote_is_reused_and_a_refused_call_falls_back_to_the_last_price(self):
        calls = []
        def fake(path, sym):
            calls.append(sym)
            return {"c": 12.5, "dp": 1.0} if len(calls) == 1 else None      # second ask is refused/failed
        with mock.patch.object(appmod, "_finnhub", side_effect=fake):
            self.assertEqual(appmod._price("qct"), 12.5)
            self.assertEqual(appmod._price("QCT"), 12.5)                     # within the TTL: no second call
            self.assertEqual(len(calls), 1)
            hit = store._MEM_CACHE[("finnhub_quote", "QCT")]
            store._MEM_CACHE[("finnhub_quote", "QCT")] = (hit[0] - 10_000, hit[1])   # expire the hot cache
            self.assertEqual(appmod._price("QCT"), 12.5)                     # refused now: last-known-good, not None
            self.assertEqual(len(calls), 2)

    def test_never_seen_and_failing_is_none_not_zero(self):
        with mock.patch.object(appmod, "_finnhub", return_value=None):
            self.assertIsNone(appmod._price("QCT"))

    def test_paper_total_keeps_positions_when_the_quote_call_is_refused(self):
        p = {"id": None, "cash": 100.0, "starting_cash": 100.0, "positions": {"QCT": {"shares": 10, "avg_cost": 1.0}}}
        with mock.patch.object(appmod, "_finnhub", return_value={"c": 5.0}):
            self.assertEqual(appmod._paper_marked(p)["total_value"], 150.0)
        store._MEM_CACHE[("finnhub_quote", "QCT")] = (0, store._MEM_CACHE[("finnhub_quote", "QCT")][1])   # expired
        with mock.patch.object(appmod, "_finnhub", return_value=None):
            self.assertEqual(appmod._paper_marked(p)["total_value"], 150.0)
