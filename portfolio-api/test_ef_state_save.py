"""The EF? dropdown sends only {ef_state}; that must actually save (it used to return ok with nothing saved)."""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as appmod  # noqa: E402

ACCTS = {"accounts": [{"name": "WellsTrade (...9571)", "institution": "Wells Fargo", "type": "investment", "subtype": "brokerage",
                       "balance": 17616.0, "is_asset": True},
                      {"name": "Visa (...1111)", "type": "credit", "is_asset": False, "balance": -500.0}]}


class Req:
    headers = {}
    client = None

    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


class EfStateSave(unittest.TestCase):
    def post(self, body, cfg):
        written = {}
        with mock.patch.object(appmod, "_data", lambda: ACCTS), mock.patch.object(appmod, "_read_insurance_cfg", lambda: dict(cfg)), \
                mock.patch.object(appmod.store, "write_json", lambda path, data: written.update({path: data})), \
                mock.patch.object(appmod, "_emit_config_changed", lambda *a, **k: None):
            r = asyncio.run(appmod.post_account_registry(Req(body)))
        return r, written.get(appmod.INSURANCE_FILE)

    def test_the_dropdown_payload_alone_saves_a_designation(self):
        r, cfg = self.post({"account": "WellsTrade (...9571)", "ef_state": "yes"}, {})
        self.assertEqual((r["ok"], r["saved"], r["rejected"]), (True, ["ef_state"], []))
        self.assertEqual(cfg["ef_accounts"]["WellsTrade (...9571)"], {"on": True, "cap": None})

    def test_answering_no_persists_and_unset_clears(self):
        r, cfg = self.post({"account": "WellsTrade (...9571)", "ef_state": "no"}, {})
        self.assertEqual(r["saved"], ["ef_state"])
        self.assertEqual(cfg["ef_accounts"]["WellsTrade (...9571)"], {"on": False, "excluded": True})
        r, cfg = self.post({"account": "WellsTrade (...9571)", "ef_state": "unset"}, cfg)
        self.assertNotIn("WellsTrade (...9571)", cfg["ef_accounts"])

    def test_a_cap_with_the_state_is_kept(self):
        r, cfg = self.post({"account": "WellsTrade (...9571)", "ef_state": "yes", "ef_cap": "10000"}, {})
        self.assertEqual(cfg["ef_accounts"]["WellsTrade (...9571)"], {"on": True, "cap": 10000.0})

    def test_the_older_boolean_payload_still_works(self):
        r, cfg = self.post({"account": "WellsTrade (...9571)", "ef": True}, {})
        self.assertEqual(r["saved"], ["ef"])
        self.assertTrue(cfg["ef_accounts"]["WellsTrade (...9571)"]["on"])


if __name__ == "__main__":
    unittest.main()
