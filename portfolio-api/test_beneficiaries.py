"""Per-account beneficiary answers: stored as an answer + date (never names), judged by the estate audit, read by the LIFE MAP check."""
import asyncio
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as appmod  # noqa: E402
import estate  # noqa: E402
import lifemap  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def accts():
    return [{"name": "401k", "tax_status": "deferred", "value": 100000, "beneficiary_state": "complete", "beneficiary_checked": "2026-01-02"},
            {"name": "Roth", "tax_status": "roth", "value": 50000, "beneficiary_state": "primary_only"},
            {"name": "IRA", "tax_status": "deferred", "value": 40000},
            {"name": "Brokerage", "tax_status": "taxable", "value": 30000, "beneficiary_state": "none"},
            {"name": "Home", "tax_status": "real_estate", "value": 400000}]


class Audit(unittest.TestCase):
    def test_gap_states_and_summary(self):
        r = estate.titling_audit(accts(), state="TX")
        by = {x["account"]: x for x in r["accounts"]}
        self.assertEqual((by["401k"]["bene_gap"], by["Roth"]["bene_gap"], by["IRA"]["bene_gap"], by["Brokerage"]["bene_gap"]), ("no", "yes", "unknown", "yes"))
        self.assertIsNone(by["Home"]["bene_gap"])
        self.assertEqual(r["beneficiary_summary"], {"tracked": 4, "complete": 1, "primary_only": 1, "none": 1, "unset": 1})


class Check(unittest.TestCase):
    def _check(self, rows):
        cfg = json.load(open(os.path.join(HERE, "lifemap_default.json")))
        chk = next(c for t in cfg["topics"] for c in t.get("checks", []) if c["id"] == "H2b")
        return lifemap.run_check(chk, {"continuity": {"account_map": rows}}, {})

    def test_attention_when_a_backup_is_missing(self):
        rows = estate.titling_audit(accts(), state="TX")["accounts"]
        r = self._check(rows)
        self.assertEqual(r["state"], "attention")
        self.assertIn("2 of 4", r["reason"])

    def test_todo_when_only_unanswered(self):
        a = [x for x in accts() if x["name"] in ("401k", "IRA")]
        r = self._check(estate.titling_audit(a, state="TX")["accounts"])
        self.assertEqual(r["state"], "todo")

    def test_ok_when_all_complete_and_when_none_tracked(self):
        a = [dict(x, beneficiary_state="complete") for x in accts() if x["tax_status"] != "real_estate"]
        self.assertEqual(self._check(estate.titling_audit(a, state="TX")["accounts"])["state"], "ok")
        self.assertEqual(self._check([])["state"], "ok")


class Registry(unittest.TestCase):
    def _post(self, body, status="deferred"):
        saved = {}
        acct = {"name": "401k", "type": "Retirement", "subtype": "401k"}
        req = mock.Mock()

        async def js():
            return body
        req.json = js
        with mock.patch.object(appmod, "_data", return_value={"accounts": [acct]}), \
                mock.patch.object(appmod, "_account_tax_status", return_value=status), \
                mock.patch.object(appmod, "_read_accounts_cfg", return_value={"defaults": {}, "accounts": {}}), \
                mock.patch.object(appmod.store, "write_json", side_effect=lambda f, c: saved.update(c)), \
                mock.patch.object(appmod, "_emit_config_changed"):
            out = asyncio.run(appmod.post_account_registry(req))
        return out, saved

    def test_answer_is_saved_with_todays_date_and_no_names(self):
        out, saved = self._post({"account": "401k", "beneficiary": "complete"})
        self.assertIn("beneficiary", out["saved"])
        ent = saved["accounts"]["401k"]
        self.assertEqual(ent["beneficiary_state"], "complete")
        self.assertEqual(ent["beneficiary_checked"], appmod.datetime.date.today().isoformat())

    def test_bad_state_future_date_and_wrong_account_are_refused(self):
        out, saved = self._post({"account": "401k", "beneficiary": "everyone"})
        self.assertTrue(out["rejected"] and not saved)
        out, saved = self._post({"account": "401k", "beneficiary": "complete", "beneficiary_checked": "2999-01-01"})
        self.assertTrue(out["rejected"] and not saved)
        out, saved = self._post({"account": "401k", "beneficiary": "complete"}, status="na")
        self.assertTrue(out["rejected"] and not saved)

    def test_unset_clears_the_answer(self):
        out, saved = self._post({"account": "401k", "beneficiary": "unset"})
        self.assertNotIn("beneficiary_state", saved["accounts"]["401k"])


if __name__ == "__main__":
    unittest.main()
