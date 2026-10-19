# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_secrets_plain.py -- SMTP non-credential fields are shown to the ADMIN only; the password and
every other secret stay write-only."""
import glob, os, re, sys, tempfile, unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="secplain-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
from fastapi.testclient import TestClient  # noqa: E402
import app as appmod, audit, auth  # noqa: E402

PW = "correct-horse-battery-1"
PLAIN = {"SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "587", "SMTP_USERNAME": "me@gmail.com",
         "SMTP_FROM_ADDRESS": "me@gmail.com", "SMTP_TO_ADDRESS": "alerts@example.com", "SMTP_USE_TLS": "1"}


def _login(u):
    c = TestClient(appmod.app)
    assert c.post("/api/auth/login", json={"username": u, "password": PW}).status_code == 200
    return c


class TestPlainSecrets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = tempfile.mkdtemp(dir=_TMP)
        audit.AUDIT_DB = os.path.join(d, "audit.db"); audit.AUDIT_JSONL = os.path.join(d, "audit.jsonl"); audit.init_db()
        for n, mk in (("sp_admin", lambda n: auth.create_user(n, PW, role="admin")), ("sp_viewer", lambda n: auth.create_viewer(n, PW))):
            if not auth.get_user_by_username(n):
                mk(n)
        cls.A, cls.V = _login("sp_admin"), _login("sp_viewer")
        r = cls.A.post("/api/secrets", json={**PLAIN, "SMTP_PASSWORD": "super-secret-app-password"})
        assert r.status_code == 200, r.text

    def by_key(self, client):
        return {s["key"]: s for s in client.get("/api/secrets").json()["secrets"]}

    def test_admin_sees_plain_fields_but_never_the_password(self):
        k = self.by_key(self.A)
        for key, val in PLAIN.items():
            self.assertTrue(k[key]["plain"]); self.assertEqual(k[key]["value"], val)
        self.assertTrue(k["SMTP_PASSWORD"]["set"])
        self.assertNotIn("value", k["SMTP_PASSWORD"]); self.assertNotIn("plain", k["SMTP_PASSWORD"])
        self.assertNotIn("super-secret-app-password", self.A.get("/api/secrets").text)

    def test_viewer_gets_no_values(self):
        r = self.V.get("/api/secrets")
        self.assertEqual(r.status_code, 200)
        for s in r.json()["secrets"]:
            self.assertIsNone(s.get("value"))
        for val in (PLAIN["SMTP_USERNAME"], PLAIN["SMTP_TO_ADDRESS"], "super-secret-app-password"):
            self.assertNotIn(val, r.text)

    def test_no_other_secret_has_a_value_field(self):
        for s in self.by_key(self.A).values():
            if not s.get("plain"):
                self.assertNotIn("value", s, s["key"])

    def test_clearing_a_plain_field_works(self):
        self.A.post("/api/secrets", json={"SMTP_FROM_ADDRESS": ""})
        self.assertIsNone(self.by_key(self.A)["SMTP_FROM_ADDRESS"]["value"])
        self.A.post("/api/secrets", json={"SMTP_FROM_ADDRESS": PLAIN["SMTP_FROM_ADDRESS"]})


if __name__ == "__main__":
    unittest.main()
