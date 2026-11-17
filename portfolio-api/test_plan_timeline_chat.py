"""RV-032 phase 3: the chat tool get_plan_timeline returns the table's own figures as short rows, and is read-only by construction."""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plan_timeline  # noqa: E402
from test_plan_timeline import base  # noqa: E402

try:
    import app
except Exception:
    app = None


@unittest.skipIf(app is None, "app not importable")
class ChatTimeline(unittest.TestCase):
    def setUp(self):
        self.res = plan_timeline.build(base(conversions={2042: 30000.0}))
        self.res["notes"], self.res["tax_year"] = ["n"], 2026

    def test_short_rows_with_the_tables_own_numbers(self):
        out = app._timeline_summary_for_chat(self.res, 5)
        self.assertEqual(out["rows_shown"], 5)
        self.assertEqual(out["rows_total"], len(self.res["rows"]))
        self.assertEqual(out["summary"], self.res["summary"])
        for short, full in zip(out["rows"], self.res["rows"]):
            self.assertEqual((short["year"], short["end_total"], short["shortfall"]), (full["year"], full["end_total"], full["shortfall"]))
            self.assertNotIn("start", short)                       # no bucket detail in chat
            self.assertNotIn("growth", short)

    def test_years_is_clamped_and_defaults_to_ten(self):
        self.assertEqual(app._timeline_summary_for_chat(self.res)["rows_shown"], 10)
        self.assertEqual(app._timeline_summary_for_chat(self.res, 0)["rows_shown"], 10)
        self.assertEqual(app._timeline_summary_for_chat(self.res, 9999)["rows_shown"], min(70, len(self.res["rows"])))

    def test_a_conversion_year_carries_the_conversion(self):
        out = app._timeline_summary_for_chat(self.res, 70)
        self.assertEqual(next(r for r in out["rows"] if r["year"] == 2042)["conversion"], 30000.0)

    def test_tool_is_registered_read_only_and_dispatched(self):
        names = [t["function"]["name"] for t in app.FIDUCIARY_TOOLS]
        self.assertIn("get_plan_timeline", names)
        self.assertNotIn("get_plan_timeline", [t["function"]["name"] for t in app.STRATEGIST_TOOLS])   # chat only
        with unittest.mock.patch.object(app, "_timeline_inputs", return_value=({"start_age": None}, [])):
            self.assertIn("error", app._fiduciary_dispatch("get_plan_timeline", {}))


if __name__ == "__main__":
    import unittest.mock
    unittest.main()
