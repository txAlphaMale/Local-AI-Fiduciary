# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_growth_experiments.py -- the two Growth experiments (Jason 2026-11-14): a small-base marker for growth above 1,000%, and a lower
dilution cap, each with a change log and one-step revert so a bad idea can be backed out."""
import asyncio, datetime, glob, json, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="gexp-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    with open(_f) as _fh:
        for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', _fh.read()):
            os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod, sleeves, store, ticker_state  # noqa: E402


def row(t, share_change, growth=60.0, first="2012-01-01"):
    return {"ticker": t, "name": t, "state": "active", "scores": {"score": 50},
            "s1": {"revenue": 5e7, "rev_growth_pct": growth, "share_change_pct": share_change, "runway_months": 40, "first_filed": first,
                   "net_margin_pct": 1, "fcf_margin_pct": 1}, "s2": {"price": 10.0}, "s3": None}


ROWS = [row("LOW", 5), row("MID", 217), row("HIGH", 280), row("NEWCO", 440, first="2025-09-22"), row("BIG", 20, growth=11925.0)]


class FakeReq:
    def __init__(self, body): self._b = body
    async def json(self): return self._b
    headers = {}
    client = None
    state = type("S", (), {})()


class TestExperiments(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(_TMP, "growth-exp-%d.json" % id(self))
        self.p = [mock.patch.object(appmod, "GROWTH_EXPERIMENTS_FILE", self.path),
                  mock.patch.object(ticker_state, "screened_rows", return_value=ROWS),
                  mock.patch.object(appmod, "_emit_config_changed")]
        for x in self.p: x.start()

    def tearDown(self):
        for x in self.p: x.stop()
        if os.path.exists(self.path): os.remove(self.path)

    def post(self, body):
        return asyncio.run(appmod.post_sleeve_experiments(FakeReq(body)))

    def test_defaults_change_nothing(self):
        d = appmod.get_sleeve_experiments()
        self.assertEqual((d["current_cap"], d["growth_now"], d["growth_baseline"]), (300, 5, 5))      # NEWCO passes: exempt as a new listing
        self.assertTrue(d["flags"]["small_base_flag"])
        self.assertIsNone(d["flags"]["dilution_cap"])

    def test_preview_shows_who_each_cap_would_drop_before_anything_is_turned_on(self):
        d = appmod.get_sleeve_experiments()
        by = {p["cap"]: p for p in d["preview"]}
        self.assertEqual(by[300]["dropped"], [])
        self.assertEqual(by[200]["dropped"], ["HIGH", "MID"])
        self.assertEqual(by[100]["dropped"], ["HIGH", "MID"])
        self.assertNotIn("NEWCO", by[75]["dropped"])                      # the new-listing exemption still applies
        self.assertEqual(appmod.get_sleeve_experiments()["growth_now"], 5)  # previewing changed nothing

    def test_setting_a_cap_changes_growth_logs_it_and_revert_restores_it(self):
        r = self.post({"dilution_cap": 200})
        self.assertEqual((r["growth_before"], r["growth_after"]), (5, 3))
        names = [x["id"] for x in appmod._sleeve_list() if x["id"] == "growth"]
        g = next(x for x in appmod._sleeve_list() if x["id"] == "growth")
        self.assertTrue(any(ru["metric"] == "share_change_pct" and ru["value"] == 200 for ru in g["rules"]))
        d = appmod.get_sleeve_experiments()
        self.assertEqual((d["current_cap"], d["growth_now"], d["growth_baseline"]), (200, 3, 5))
        self.assertEqual(d["log"][-1]["dropped"], ["HIGH", "MID"])
        back = self.post({"revert": True})
        self.assertEqual((back["growth_before"], back["growth_after"]), (3, 5))
        self.assertEqual(appmod.get_sleeve_experiments()["current_cap"], 300)
        again = self.post({"revert": True})                               # reverting the revert redoes it
        self.assertEqual(again["growth_after"], 3)
        self.assertIn("revert", [e["change"] for e in appmod.get_sleeve_experiments()["log"]])

    def test_null_goes_back_to_the_builtin_rule_and_bad_values_are_refused(self):
        self.post({"dilution_cap": 100})
        self.post({"dilution_cap": None})
        self.assertEqual(appmod.get_sleeve_experiments()["current_cap"], 300)
        for bad in ({"dilution_cap": 5}, {"dilution_cap": "lots"}, {"dilution_cap": True}, {"small_base_flag": "yes"}, {}):
            self.assertEqual(self.post(bad).status_code, 400)
        self.assertEqual(self.post({"revert": True}).__class__.__name__ in ("dict",), True)

    def test_revert_with_nothing_logged_is_refused(self):
        self.assertEqual(self.post({"revert": True}).status_code, 400)

    def test_small_base_marker_follows_the_flag_and_only_marks_growth_above_1000(self):
        today = datetime.date(2026, 11, 14)
        flagged = {r["ticker"]: appmod._sleeve_row(r, today)["small_base"] for r in ROWS}
        self.assertEqual([t for t, v in flagged.items() if v], ["BIG"])
        self.post({"small_base_flag": False})
        self.assertFalse(appmod._sleeve_row(ROWS[4], today)["small_base"])
        self.assertEqual(self.post({"small_base_flag": True})["flags"]["small_base_flag"], True)

    def test_preview_and_toggle_do_not_alter_the_builtin_definition(self):
        before = json.dumps(sleeves.BUILTIN["growth"], sort_keys=True)
        self.post({"dilution_cap": 100}); appmod.get_sleeve_experiments(); self.post({"revert": True})
        self.assertEqual(json.dumps(sleeves.BUILTIN["growth"], sort_keys=True), before)


if __name__ == "__main__":
    unittest.main()
