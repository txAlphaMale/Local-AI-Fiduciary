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


import json
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
