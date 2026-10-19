"""
test_ghost_session.py -- TASK-001b: a session whose account was deleted (or whose password/MFA was
changed since it was minted) must stop working. Real app + real global gate, throwaway data dir.
Run:  python -m pytest test_ghost_session.py -q
"""
import glob, os, re, sys, tempfile, unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="ghost-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)

from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as appmod  # noqa: E402
import audit  # noqa: E402
import auth  # noqa: E402

PW = "correct-horse-battery-1"
WRITES = {"POST", "PUT", "PATCH", "DELETE"}
_n = [0]


def _login(username, password):
    c = TestClient(appmod.app)
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


def _cookie(client):
    return "session=" + client.cookies.get("session")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # other test modules repoint audit.AUDIT_DB at their own temp DBs in this process
        d = tempfile.mkdtemp(dir=_TMP)
        audit.AUDIT_DB = os.path.join(d, "audit.db")
        audit.AUDIT_JSONL = os.path.join(d, "audit.jsonl")
        audit.init_db()
        if not auth.get_user_by_username("ghostadmin"):
            auth.create_user("ghostadmin", PW, role="admin")
        cls.admin = _login("ghostadmin", PW)

    def new_viewer(self):
        _n[0] += 1
        name = f"gv{_n[0]}"
        r = self.admin.post("/api/auth/users", json={"username": name, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return name, r.json()["user"]["id"], _login(name, PW)


class TestDeletedUserSession(Base):
    def test_deleted_viewer_session_is_rejected_everywhere(self):
        name, uid, viewer = self.new_viewer()
        self.assertEqual(viewer.get("/api/vault/items").status_code, 200)       # alive: works
        cookie = _cookie(viewer)
        self.assertEqual(self.admin.post(f"/api/auth/users/{uid}/delete").status_code, 200)
        ghost = lambda: TestClient(appmod.app)
        for path in ("/api/portfolio", "/api/vault/items", "/api/vault/folders", "/api/auth/users", "/api/calc"):
            r = ghost().get(path, headers={"cookie": cookie})
            self.assertEqual(r.status_code, 401, path)
        self.assertEqual(ghost().post("/api/calc", json={}, headers={"cookie": cookie}).status_code, 401)
        ev = [e for e in audit.query_core(event_type="session.invalidated", limit=500) if e["actor"] == uid]
        self.assertTrue(ev, "no session.invalidated audit event for the deleted user")

    def test_every_write_route_rejects_a_deleted_user_session(self):
        name, uid, viewer = self.new_viewer()
        cookie = _cookie(viewer)
        self.admin.post(f"/api/auth/users/{uid}/delete")
        bad = []
        n = 0
        for route in appmod.app.routes:
            if not isinstance(route, APIRoute) or route.path in appmod._AUTH_PUBLIC_PATHS:
                continue
            if route.path == "/api/auth/logout":       # deliberate: 200 so the browser can drop the cookie
                continue
            path = re.sub(r"\{[^}]+\}", "x", route.path)
            for m in sorted(route.methods & WRITES):
                n += 1
                r = TestClient(appmod.app, raise_server_exceptions=False).request(m, path, headers={"cookie": cookie}, json={})
                if r.status_code != 401:
                    bad.append((m, route.path, r.status_code))
        self.assertGreater(n, 50)                     # the sweep really enumerated the app
        self.assertEqual(bad, [])

    def test_ghost_can_log_out_and_cookie_is_cleared(self):
        name, uid, viewer = self.new_viewer()
        self.admin.post(f"/api/auth/users/{uid}/delete")
        r = viewer.post("/api/auth/logout")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(viewer.get("/api/vault/items").status_code, 401)       # jar now empty/anon

    def test_live_sessions_are_unaffected(self):
        name, uid, viewer = self.new_viewer()
        self.assertEqual(self.admin.get("/api/vault/items").status_code, 200)
        self.assertEqual(viewer.get("/api/vault/items").status_code, 200)
        self.assertEqual(viewer.get("/api/auth/status").json()["authenticated"], True)
        self.assertEqual(viewer.post("/api/vault/upload", content=b"x").status_code, 403)   # viewer still view-only
        self.assertEqual(self.admin.get("/api/auth/users").status_code, 200)

    def test_require_admin_and_vault_user_fail_closed(self):
        class R:  # request stub with a session pointing at a nonexistent user
            session = {"uid": "does-not-exist"}
        import vault_api
        self.assertIsNone(appmod._require_admin(R))
        self.assertIsNone(vault_api._user(R))


class TestSessionEpoch(Base):
    def test_password_change_kills_other_sessions_but_not_the_acting_one(self):
        name, uid, a = self.new_viewer()
        b = _login(name, PW)
        stolen = _cookie(b)
        r = a.post("/api/auth/change-password", json={"current_password": PW, "new_password": "another-long-pass-2"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(a.get("/api/vault/items").status_code, 200)             # acting session survives
        self.assertEqual(b.get("/api/vault/items").status_code, 401)             # other session dies
        self.assertEqual(TestClient(appmod.app).get("/api/vault/items", headers={"cookie": stolen}).status_code, 401)
        self.assertTrue(any(e["actor"] == uid for e in audit.query_core(event_type="session.invalidated", limit=500)))
        _login(name, "another-long-pass-2")                                        # new password works

    def test_bump_session_epoch_signs_out_everywhere(self):
        name, uid, a = self.new_viewer()
        self.assertEqual(a.get("/api/vault/items").status_code, 200)
        auth.bump_session_epoch(uid)
        self.assertEqual(a.get("/api/vault/items").status_code, 401)

    def test_mfa_disable_bumps_epoch(self):
        name, uid, a = self.new_viewer()
        e0 = auth.session_epoch(uid)
        auth.mfa_disable(uid)
        self.assertEqual(auth.session_epoch(uid), e0 + 1)


if __name__ == "__main__":
    unittest.main()
