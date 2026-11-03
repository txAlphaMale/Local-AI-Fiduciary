# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_edgar_ua.py -- the SEC User-Agent saved in Settings (DB) must reach EDGAR requests, not the import-time placeholder."""
import os, sys, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import edgar  # noqa: E402


class TestRefreshUA(unittest.TestCase):
    def tearDown(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SEC_USER_AGENT", None)
        edgar.refresh_ua()

    def test_refresh_picks_up_env_set_after_import(self):
        with mock.patch.dict(os.environ, {"SEC_USER_AGENT": "Jane Doe jane@realdomain.org"}):
            self.assertEqual(edgar.refresh_ua(), "Jane Doe jane@realdomain.org")
            self.assertEqual(edgar._headers()["User-Agent"], "Jane Doe jane@realdomain.org")
            self.assertFalse(edgar.ua_is_placeholder())

    def test_blank_falls_back_to_placeholder(self):
        with mock.patch.dict(os.environ, {"SEC_USER_AGENT": ""}):
            self.assertTrue(edgar.ua_is_placeholder(edgar.refresh_ua()))

    def test_app_refreshes_after_secrets_merge(self):
        src = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
        self.assertLess(src.index("_boot_merge(SECRETS_FILE"), src.index("edgar.refresh_ua()"))


if __name__ == "__main__":
    unittest.main()
