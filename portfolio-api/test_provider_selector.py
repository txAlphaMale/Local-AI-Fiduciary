# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_provider_selector.py -- the Portfolio data source selector lives in SETTINGS -> ACCOUNTS and applies live;
SnapTrade keys saved in the UI apply without a restart (tester feedback 2026-10-21)."""
import asyncio, glob, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="prov-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod  # noqa: E402

HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()


class FakeReq:
    def __init__(self, body): self._b = body; self.session = {}; self.headers = {}; self.client = None
    async def json(self): return self._b


class TestSnapTradeKeysLive(unittest.TestCase):
    def test_keys_saved_in_the_ui_apply_without_a_restart(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in ("SNAPTRADE_CLIENT_ID", "SNAPTRADE_CONSUMER_KEY", "SNAPTRADE_USER_ID", "SNAPTRADE_USER_SECRET"):
                os.environ.pop(k, None)
            with mock.patch.object(appmod, "_read_secrets", lambda: {"SNAPTRADE_CLIENT_ID": "PERS-X", "SNAPTRADE_CONSUMER_KEY": "k"}):
                self.assertEqual(appmod._snaptrade_env(), ("PERS-X", "k", None, None))

    def test_real_env_still_wins(self):
        with mock.patch.dict(os.environ, {"SNAPTRADE_CLIENT_ID": "PERS-ENV"}), \
             mock.patch.object(appmod, "_read_secrets", lambda: {"SNAPTRADE_CLIENT_ID": "PERS-UI"}):
            self.assertEqual(appmod._snaptrade_env()[0], "PERS-ENV")


class TestProviderEndpoint(unittest.TestCase):
    def post(self, body, ack=None):
        saved = {}
        with mock.patch.object(appmod, "_require_admin", lambda r: {"id": 1, "username": "a"}), \
             mock.patch.object(appmod, "_monarch_ack", lambda: ack), \
             mock.patch.object(appmod, "_read_settings", lambda: dict(saved)), \
             mock.patch.object(appmod.store, "write_json", lambda p, d: saved.update(d)), \
             mock.patch.object(appmod.audit, "emit", lambda *a, **k: None), \
             mock.patch.dict(os.environ, {"PORTFOLIO_PROVIDER": "csv"}):
            r = asyncio.run(appmod.post_provider(FakeReq(body)))
            return r, saved, os.environ["PORTFOLIO_PROVIDER"]

    def test_switch_applies_live_and_persists(self):
        r, saved, env = self.post({"provider": "snaptrade"})
        self.assertEqual((r["active"], saved["PORTFOLIO_PROVIDER"], env), ("snaptrade", "snaptrade", "snaptrade"))

    def test_unknown_and_unacknowledged_monarch_are_refused(self):
        self.assertEqual(self.post({"provider": "bogus"})[0].status_code, 400)
        self.assertEqual(self.post({"provider": "monarch"}, ack=None)[0].status_code, 400)
        self.assertEqual(self.post({"provider": "monarch"}, ack={"at": "x"})[0]["active"], "monarch")

    def test_non_admin_refused(self):
        with mock.patch.object(appmod, "_require_admin", lambda r: None):
            self.assertEqual(asyncio.run(appmod.post_provider(FakeReq({"provider": "csv"}))).status_code, 403)


class TestUi(unittest.TestCase):
    def test_selector_is_above_the_integrations_and_gone_from_advanced_knobs(self):
        i = HTML.index("_integ.id='integrations-panel'")
        line = HTML[i:i + 400]
        self.assertLess(line.index("providerPanel()"), line.index("plaidPanel()"))
        self.assertIn("it.key==='PORTFOLIO_PROVIDER') return;", HTML)

    def test_no_text_still_sends_users_to_advanced_knobs_for_the_data_source(self):
        for f in [os.path.join(_HERE, "static", "index.html")] + glob.glob(os.path.join(_HERE, "..", "*.md")):
            t = open(f, encoding="utf-8").read()
            self.assertNotRegex(t, r"ADVANCED KNOBS\s*→\s*\"?Portfolio data source", f)

    def test_boot_override_keeps_the_saved_choice_over_the_compose_default(self):
        self.assertIn('os.environ["PORTFOLIO_PROVIDER"] = _pp', open(os.path.join(_HERE, "app.py"), encoding="utf-8").read())



class _Fake:
    """Minimal fake SnapTrade client for health()."""
    def __init__(self, accounts, conns, boom=False):
        outer = self
        class AI:
            def list_all_user_accounts(s, **k):
                if boom: raise RuntimeError("401 unauthorized")
                return {"results": accounts}
        class CN:
            def list_brokerage_authorizations(s, **k): return conns
        self.account_information, self.connections = AI(), CN()


class TestSnapTradeHealth(unittest.TestCase):
    ACC = [{"id": "a1", "display_name": "Individual", "masked_account_number": "1234", "connection_id": "c1",
            "net_value": {"amount": 1500.0}, "raw_type": "Margin"}]

    def test_healthy_connection(self):
        import snaptrade_integration as si
        h = si.health(_Fake(self.ACC, [{"id": "c1", "brokerage": {"name": "Robinhood"}, "disabled": False}]), {})
        self.assertTrue(h["ok"]); self.assertEqual(h["n_accounts"], 1)
        self.assertEqual(h["accounts"][0]["institution"], "Robinhood"); self.assertEqual(h["problems"], [])

    def test_no_accounts_and_disabled_connection_are_reported(self):
        import snaptrade_integration as si
        h = si.health(_Fake([], []), {})
        self.assertFalse(h["ok"]); self.assertIn("no brokerage accounts", h["problems"][0])
        h = si.health(_Fake(self.ACC, [{"id": "c1", "brokerage": {"name": "Robinhood"}, "disabled": True}]), {})
        self.assertFalse(h["ok"]); self.assertIn("disabled", h["problems"][0])

    def test_endpoint_reports_bad_keys_without_leaking_them(self):
        import snaptrade_integration as si
        fake = _Fake([], [], boom=True)
        with mock.patch.object(appmod, "_require_admin", lambda r: {"id": 1, "username": "a"}), \
             mock.patch.object(appmod, "_snaptrade_env", lambda: ("PERS-X", "SECRETKEY", None, None)), \
             mock.patch.object(si, "make_client", lambda *a: (fake, {}, "personal")):
            r = asyncio.run(appmod.post_snaptrade_test(FakeReq({})))
        self.assertEqual(r.status_code, 502)
        self.assertNotIn(b"SECRETKEY", r.body)

    def test_failed_portfolio_load_is_recorded_for_the_panel(self):
        import snaptrade_integration as si
        with mock.patch.object(appmod, "_snaptrade_env", lambda: ("PERS-X", "k", None, None)), \
             mock.patch.object(si, "build_portfolio", mock.Mock(side_effect=RuntimeError("SnapTrade returned no accounts yet"))):
            with self.assertRaises(RuntimeError):
                appmod._build_snaptrade()
        self.assertIn("no accounts yet", appmod._snaptrade_state["last_err"])

    def test_ui_has_test_button_and_clear_holdings_banner(self):
        for needle in ("TEST CONNECTION", "/api/integrations/snaptrade/test", "Last dashboard load",
                       "This is your MANUAL holdings list, and it is not being used right now"):
            self.assertIn(needle, HTML)



class TestSecretsPopup(unittest.TestCase):
    """Tester 2026-10-21: saving SnapTrade creds popped 'applies on the next docker compose up --build' although
    they apply at once. The save response now says which keys are live and the popup only warns about the rest."""
    def test_response_marks_snaptrade_and_plaid_keys_live(self):
        saved = {}
        with mock.patch.object(appmod, "_read_secrets", lambda: dict(saved)), \
             mock.patch.object(appmod, "_write_secrets", lambda d: saved.update(d)), \
             mock.patch.object(appmod.audit, "emit", lambda *a, **k: None):
            r = asyncio.run(appmod.post_secrets(FakeReq({"SNAPTRADE_CLIENT_ID": "PERS-X", "SNAPTRADE_CONSUMER_KEY": "k"})))
        self.assertEqual(sorted(r["applied_live"]), ["SNAPTRADE_CLIENT_ID", "SNAPTRADE_CONSUMER_KEY"])
        self.assertFalse(r["restart_required"])

    def test_popup_only_warns_about_keys_that_are_not_live(self):
        for needle in ("applied_live", "Applied immediately — no restart or rebuild needed", "!live.has(k)"):
            self.assertIn(needle, HTML)


if __name__ == "__main__":
    unittest.main()
