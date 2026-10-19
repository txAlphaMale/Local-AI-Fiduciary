"""
test_monarch_optional.py -- TASK-003: Monarch is optional, unofficial, default-off and gated by an
audited risk acknowledgment. Runs the real app against a throwaway data dir.
"""
import glob
import os
import re
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="monarch-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f) == "test_monarch_optional.py":
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)

from fastapi.testclient import TestClient  # noqa: E402
import app as appmod  # noqa: E402
import audit  # noqa: E402
import auth  # noqa: E402


def _login(u, p):
    c = TestClient(appmod.app)
    r = c.post("/api/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return c


class MonarchOptional(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for fn, args in ((auth.create_user, ("mboss", "correct-horse-battery-1")),
                         (auth.create_viewer, ("mkid", "correct-horse-battery-2"))):
            try:
                fn(*args, **({"role": "admin"} if fn is auth.create_user else {}))
            except Exception:
                pass
        cls.admin = _login("mboss", "correct-horse-battery-1")
        cls.viewer = _login("mkid", "correct-horse-battery-2")

    def setUp(self):
        audit.AUDIT_DB = os.path.join(_TMP, "audit.db")
        audit.AUDIT_JSONL = os.path.join(_TMP, "audit.jsonl")
        audit.init_db()
        self._env = os.environ.get("PORTFOLIO_PROVIDER")
        os.environ["PORTFOLIO_PROVIDER"] = "csv"
        s = appmod._read_settings()
        s.pop("MONARCH_RISK_ACK", None)
        s.pop("PORTFOLIO_PROVIDER", None)
        appmod.store.write_json(appmod.SETTINGS_FILE, s)
        appmod._cache.update(ts=0.0, data=None)
        appmod._monarch_state.update(fail_ts=0.0, err="")
        self.calls = []
        self._orig = appmod.requests.post
        appmod.requests.post = lambda *a, **k: self.calls.append(a) or (_ for _ in ()).throw(AssertionError("network call"))

    def tearDown(self):
        appmod.requests.post = self._orig
        if self._env is None:
            os.environ.pop("PORTFOLIO_PROVIDER", None)
        else:
            os.environ["PORTFOLIO_PROVIDER"] = self._env

    def test_default_provider_is_csv_not_monarch(self):
        spec = next(s for s in appmod.CONFIG_SPECS if s["key"] == "PORTFOLIO_PROVIDER")
        self.assertEqual(spec["default"], "csv")
        os.environ.pop("PORTFOLIO_PROVIDER")
        self.assertFalse(appmod._monarch_enabled())

    def test_boots_and_key_endpoints_work_with_monarch_disabled(self):
        for path in ("/health", "/api/portfolio", "/api/holdings-csv", "/api/integrations/monarch"):
            r = self.admin.get(path)
            self.assertEqual(r.status_code, 200, (path, r.text))
        self.assertEqual(self.admin.get("/api/portfolio").json().get("portfolio_provider"), "csv")
        self.assertEqual(self.calls, [], "no Monarch/mcpo call while disabled")

    def test_cashflow_and_diagnostics_do_not_call_monarch_when_disabled(self):
        self.assertEqual(appmod._cashflow_annual(), (None, None))
        r = self.admin.get("/api/raw-holdings")
        self.assertEqual(r.status_code, 409)
        self.assertIn("not the selected", r.json()["error"])
        self.assertEqual(self.calls, [])

    def test_enable_without_acknowledgment_is_refused(self):
        for body in ({}, {"acknowledge": False}, {"acknowledge": "yes"}):
            r = self.admin.post("/api/integrations/monarch/enable", json=body)
            self.assertEqual(r.status_code, 400, r.text)
        self.assertFalse(appmod._monarch_enabled())
        # the generic settings path cannot be used to bypass the gate
        r = self.admin.post("/api/settings", json={"PORTFOLIO_PROVIDER": "monarch"})
        self.assertEqual(r.status_code, 400)
        self.assertNotEqual(appmod._read_settings().get("PORTFOLIO_PROVIDER"), "monarch")

    def test_viewer_cannot_enable(self):
        r = self.viewer.post("/api/integrations/monarch/enable", json={"acknowledge": True})
        self.assertIn(r.status_code, (401, 403))
        self.assertFalse(appmod._monarch_enabled())

    def test_selected_but_unacknowledged_fails_closed_with_clear_message(self):
        os.environ["PORTFOLIO_PROVIDER"] = "monarch"      # e.g. a legacy .env value
        r = self.admin.get("/api/portfolio?refresh=1")
        self.assertEqual(r.status_code, 502)
        self.assertIn("not enabled", r.json()["error"])
        self.assertEqual(self.calls, [])

    def test_enable_with_acknowledgment_is_audited_and_disable_reverts(self):
        r = self.admin.post("/api/integrations/monarch/enable", json={"acknowledge": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(appmod._monarch_enabled())
        ev = audit.query_core(event_type="integration.monarch.enabled")
        self.assertTrue(ev, "enable must be audit-logged")
        self.assertEqual(self.admin.get("/api/integrations/monarch").json()["label"],
                         "Monarch (unofficial, at your own risk)")
        r = self.admin.post("/api/integrations/monarch/disable")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(appmod._monarch_enabled())
        self.assertTrue(audit.query_core(event_type="integration.monarch.disabled"))
        self.assertTrue(audit.verify_chain()["ok"])

    def test_failure_backs_off_instead_of_retrying(self):
        self.admin.post("/api/integrations/monarch/enable", json={"acknowledge": True})
        with self.assertRaises(AssertionError):
            appmod._post("get_accounts", {})
        n = len(self.calls)
        self.assertEqual(n, 1)
        with self.assertRaises(appmod.MonarchDisabled):
            appmod._post("get_accounts", {})
        self.assertEqual(len(self.calls), n, "cooldown: no second upstream call")


if __name__ == "__main__":
    unittest.main()
