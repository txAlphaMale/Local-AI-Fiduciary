# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""RETEST-046: an asset-location chat answer prints the app's own facts first, then a short labelled model comment."""
import os, sys, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("DEMO_MODE", "0")
try:
    import app as appmod
except Exception:                                            # pragma: no cover
    appmod = None
import asset_location as al


@unittest.skipIf(appmod is None, "app not importable")
class LocationChat(unittest.TestCase):
    def run_chat(self, question, commentary):
        h = lambda t, b, v: {"ticker": t, "name": t, "bucket": b, "value": v, "gain": 0.0, "yield_pct": 0.0, "yield_basis": "x",
                             "type": "Stock", "mix": None, "industry": None, "is_stock": True}
        view = al.build([h("AAA", "taxable", 50000.0), h("ZZZ", "roth", 50000.0)],
                        {"taxable": 50000.0, "pretax": 0.0, "roth": 50000.0, "hsa": 0.0}, 0.24, 0.15)
        calls = []

        def fake(messages, model, **k):
            calls.append(messages)
            if len(calls) == 1:
                yield {"content": None, "thinking": None, "done": True, "error": None, "cancelled": False,
                       "tool_calls": [{"function": {"name": "get_asset_location", "arguments": {}}}]}
            else:
                yield {"content": commentary, "thinking": None, "tool_calls": None, "done": True, "error": None, "cancelled": False}
        saved = []
        with mock.patch.object(appmod.llm, "_llm_chat", side_effect=fake), mock.patch.object(appmod, "_chat_summarise_old"), \
                mock.patch.object(appmod, "_asset_location_view", return_value=view), \
                mock.patch.object(appmod, "_chat_add_message", side_effect=lambda sid, role, c, tool_json=None: saved.append((role, c))):
            out = "".join(appmod._chat_stream("fiduciary", None, question, req_id="loc1"))
        return out, saved, calls

    def test_facts_first_label_then_commentary_and_the_rule_is_given_to_the_model(self):
        out, saved, calls = self.run_chat("where should I hold my bond fund?", "Nothing taxable moves because every yield is 0.")
        final = [c for r, c in saved if r == "assistant"][-1]
        self.assertTrue(final.startswith("**From the app's asset-location data"))
        self.assertIn("difference $0", final)
        self.assertIn("PLAN > ASSET LOCATION", final)
        self.assertLess(final.index("Full grid"), final.index("Model commentary"))
        self.assertTrue(final.rstrip().endswith("every yield is 0."))
        self.assertTrue(any(m.get("content") == al.COMMENTARY_RULE for m in calls[1]))

    def test_a_long_or_invented_figure_commentary_is_trimmed_and_figures_not_in_the_data_removed(self):
        text = "First. Second. Third. Fourth. Fifth. Sixth. Seventh. The tax is $987,654 a year."
        out, saved, _ = self.run_chat("why does ZZZ get less Roth than AAA?", text)
        final = [c for r, c in saved if r == "assistant"][-1]
        self.assertNotIn("987,654", final)
        self.assertNotIn("Seventh", final)


if __name__ == "__main__":
    unittest.main()
