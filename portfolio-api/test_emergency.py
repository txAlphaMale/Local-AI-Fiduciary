# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
test_emergency.py -- TASK-002: emergency access (break-glass) + Emergency Kit.
Real app + real global gate against a PRIVATE copy of the auth/store/audit databases (so role swaps
and the single-admin assumption can't collide with other test modules), with an injected clock.
Run:  python -m pytest test_emergency.py -q
"""
import glob, hashlib, io, json, os, re, shutil, subprocess, sys, tarfile, tempfile, time, unittest, zipfile
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="emergency-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)

from fastapi.testclient import TestClient  # noqa: E402
import app as appmod  # noqa: E402
import audit, auth, detections, emergency, kit, notifications, store, vault, vault_api  # noqa: E402

PW = "correct-horse-battery-1"
PDF = b"%PDF-1.4\n" + os.urandom(300)
T = [1_000_000.0]
KIT_VENV = os.environ.get("KIT_VENV", "/home/user/work/venv-kit")


def _login(u):
    c = TestClient(appmod.app)
    r = c.post("/api/auth/login", json={"username": u, "password": PW})
    assert r.status_code == 200, r.text
    return c


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = (auth.AUTH_DB, store.STORE_DB, audit.AUDIT_DB, audit.AUDIT_JSONL, vault.VAULT_DIR,
                      kit.KIT_TMP_DIR, emergency.ALLOW_SHORT_WAIT)
        d = tempfile.mkdtemp(dir=_TMP)
        auth.AUTH_DB = os.path.join(d, "auth.db")
        store.STORE_DB = os.path.join(d, "store.db")
        audit.AUDIT_DB = os.path.join(d, "audit.db")
        audit.AUDIT_JSONL = os.path.join(d, "audit.jsonl")
        vault.VAULT_DIR = os.path.join(d, "vault")
        kit.KIT_TMP_DIR = os.path.join(d, "tmp")
        vault.VAULT_MAX_FILE_MB, vault.VAULT_MAX_TOTAL_GB = 5, 1
        auth.init_db(); store.init_db(); audit.init_db(); vault.init_db(); emergency.init_db(); kit.init_db()
        Base.admin_id, _ = auth.create_user("em_admin", PW, role="admin")      # on Base, so every subclass and
        Base.des_id, _ = auth.create_viewer("em_des", PW)                       # test sees the SAME current ids
        Base.oth_id, _ = auth.create_viewer("em_oth", PW)
        Base.A, Base.D, Base.O = _login("em_admin"), _login("em_des"), _login("em_oth")
        Base.anon = TestClient(appmod.app)
        emergency.ALLOW_SHORT_WAIT = True

    @classmethod
    def tearDownClass(cls):
        (auth.AUTH_DB, store.STORE_DB, audit.AUDIT_DB, audit.AUDIT_JSONL, vault.VAULT_DIR,
         kit.KIT_TMP_DIR, emergency.ALLOW_SHORT_WAIT) = cls._saved
        emergency.set_clock(None)

    def setUp(self):
        if not auth.get_user_by_id(Base.des_id):                 # a previous test deleted the designee
            Base.des_id, _ = auth.create_viewer("em_des%d" % int(time.time() * 1000) , PW)
            Base.D = _login(auth.get_user_by_id(Base.des_id)["username"])
        T[0] = time.time()
        emergency.set_clock(lambda: T[0])
        emergency.ALLOW_SHORT_WAIT = True
        with auth._conn() as c:     # canonical roles + designee MFA/recovery for takeover tests
            c.execute("UPDATE users SET role='admin' WHERE id=?", (self.admin_id,))
            c.execute("UPDATE users SET role='viewer' WHERE id IN (?,?)", (self.des_id, self.oth_id))
            c.execute("UPDATE users SET totp_enabled=1, totp_secret='x' WHERE id=?", (self.des_id,))
        with store._conn() as c:
            c.execute("DELETE FROM emergency_requests")
            c.execute("UPDATE emergency_config SET enabled=0, designee_uid=NULL, wait_seconds=604800, "
                      "grant_type='view', view_grant_days=30, last_test_ok=NULL, pending_json=NULL WHERE id=1")
            c.execute("DELETE FROM vault_items")
            c.execute("DELETE FROM vault_kit_meta")
            c.execute("INSERT INTO vault_kit_meta(id) VALUES (1)")
        self.pushes, self.emails = [], []
        self.patches = [
            mock.patch.object(notifications, "_send_push", lambda sub, t, b: self.pushes.append((sub, t, b)) or True),
            mock.patch.object(notifications, "_send_email", lambda t, b: self.emails.append((t, b)) or True),
            mock.patch.object(notifications, "smtp_configured", lambda: True),
            # every user muted everything; the admin has a device, the designee has one too
            mock.patch.object(notifications, "_read_prefs",
                              lambda: {u: {"push": False, "email": False, "categories": {"security": False}}
                                       for u in (self.admin_id, self.des_id)}),
            mock.patch.object(notifications, "_read_subs",
                              lambda: {self.admin_id: ["admin-dev1", "admin-dev2"], self.des_id: ["des-dev"]}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    # helpers
    def admin(self):
        return auth.get_user_by_id(self.admin_id)

    def des(self):
        return auth.get_user_by_id(self.des_id)

    def enable(self, wait=100, grant="view", days=30):
        emergency.update_config(self.admin(), {"enabled": True, "designee_uid": self.des_id,
                                               "wait_seconds": wait, "grant_type": grant, "view_grant_days": days})

    def vault_item(self, vis, payload=None):
        return vault.create_item(payload or (PDF + os.urandom(8)), filename="d.pdf", folder="Estate",
                                 title=f"t-{vis}", visibility=vis)["id"]

    def audit_types(self):
        return {e["event_type"] for e in audit.query_core(limit=5000)}


class TestConfigValidation(Base):
    def test_designee_must_be_an_existing_viewer(self):
        for bad in (None, "nope", self.admin_id):
            with self.assertRaises(emergency.EmergencyError, msg=str(bad)):
                emergency.update_config(self.admin(), {"enabled": True, "designee_uid": bad, "wait_seconds": 60})

    def test_takeover_requires_designee_mfa_and_recovery_key(self):
        with auth._conn() as c:
            c.execute("UPDATE users SET totp_enabled=0 WHERE id=?", (self.des_id,))
        with self.assertRaisesRegex(emergency.EmergencyError, "MFA"):
            self.enable(grant="takeover")
        with auth._conn() as c:
            c.execute("UPDATE users SET totp_enabled=1 WHERE id=?", (self.des_id,))
            c.execute("UPDATE users SET recovery_key_hash=NULL WHERE id=?", (self.des_id,))
        with self.assertRaisesRegex(emergency.EmergencyError, "recovery"):
            self.enable(grant="takeover")
        with auth._conn() as c:
            c.execute("UPDATE users SET recovery_key_hash='h' WHERE id=?", (self.des_id,))
        self.enable(grant="takeover")

    def test_wait_floor_and_short_wait_only_in_test_mode(self):
        emergency.ALLOW_SHORT_WAIT = False
        with self.assertRaisesRegex(emergency.EmergencyError, "EMERGENCY_ALLOW_SHORT_WAIT"):
            emergency.update_config(self.admin(), {"wait_seconds": 60})
        with self.assertRaisesRegex(emergency.EmergencyError, "at least 24"):
            emergency.update_config(self.admin(), {"wait_hours": 23})
        emergency.update_config(self.admin(), {"wait_hours": 48})       # disabled, so applies immediately
        self.assertEqual(emergency.get_config()["wait_hours"], 48)

    def test_notification_gate_then_cooldown_for_widening(self):
        emergency.ALLOW_SHORT_WAIT = False
        body = {"enabled": True, "designee_uid": self.des_id, "wait_hours": 72}
        with mock.patch.object(notifications, "smtp_configured", lambda: False), \
                mock.patch.object(notifications, "subscription_count", lambda uid: 0):
            with self.assertRaisesRegex(emergency.EmergencyError, "push subscription"):
                emergency.update_config(self.admin(), body)
        with self.assertRaisesRegex(emergency.EmergencyError, "test notification"):    # channels ok, no test yet
            emergency.update_config(self.admin(), body)
        emergency.mark_test_ok()
        res = emergency.update_config(self.admin(), body)
        self.assertIsNotNone(res["scheduled"])                       # widening -> 24h cool-down
        self.assertFalse(emergency.get_config()["enabled"])
        T[0] += 23 * 3600
        self.assertFalse(emergency.get_config()["enabled"])
        T[0] += 2 * 3600
        self.assertTrue(emergency.get_config()["enabled"])           # effective after cool-down
        # a stale test notification (>24h) blocks a new enable
        emergency.update_config(self.admin(), {"enabled": False})    # reduction is immediate
        self.assertFalse(emergency.get_config()["enabled"])
        T[0] += 48 * 3600
        with self.assertRaisesRegex(emergency.EmergencyError, "test notification"):
            emergency.update_config(self.admin(), body)

    def test_reductions_apply_immediately(self):
        self.enable(wait=100)
        emergency.update_config(self.admin(), {"wait_seconds": 500})
        self.assertEqual(emergency.get_config()["wait_seconds"], 500)
        emergency.update_config(self.admin(), {"enabled": False})
        self.assertFalse(emergency.get_config()["enabled"])


class TestStateMachine(Base):
    def setUp(self):
        super().setUp()
        self.enable(wait=100)
        self.pv, self.hh, self.em = (self.vault_item(v) for v in ("private", "household", "emergency"))

    def ids_for(self, client):
        return {i["id"] for i in client.get("/api/vault/items").json()["items"]}

    def test_request_then_deny(self):
        emergency.request_access(self.des())
        T[0] += 50
        emergency.deny(self.admin())
        T[0] += 500
        emergency.tick()
        self.assertFalse(emergency.emergency_granted(self.des()))
        self.assertNotIn(self.em, self.ids_for(self.D))
        self.assertEqual(emergency.history()[0]["status"], "denied")
        self.assertIn("emergency.denied", self.audit_types())
        h = emergency.status_for(self.admin())["history"][0]
        self.assertEqual(h["decided_by"], "em_admin")                       # records WHO decided

    def test_auto_grant_after_wait_only_exposes_emergency_items(self):
        emergency.request_access(self.des())
        T[0] += 99
        emergency.tick()
        self.assertFalse(emergency.emergency_granted(self.des()))
        self.assertNotIn(self.em, self.ids_for(self.D))
        T[0] += 2
        self.assertEqual(emergency.tick()["granted"], [emergency.history()[0]["id"]])
        self.assertTrue(emergency.emergency_granted(self.des()))
        self.assertEqual(emergency.status_for(self.admin())["history"][0]["decided_by"], "automatic")
        ids = self.ids_for(self.D)
        self.assertIn(self.em, ids)
        self.assertIn(self.hh, ids)
        self.assertNotIn(self.pv, ids)                                    # private stays private
        self.assertEqual(self.D.get(f"/api/vault/items/{self.pv}/download").status_code, 404)
        self.assertEqual(self.D.get(f"/api/vault/items/{self.em}/download").status_code, 200)
        self.assertNotIn(self.em, self.ids_for(self.O))                   # another viewer: still nothing
        self.assertFalse(emergency.emergency_granted(auth.get_user_by_id(self.oth_id)))

    def test_early_approve_then_revoke_bites_on_next_request(self):
        emergency.request_access(self.des())
        emergency.approve(self.admin())
        self.assertEqual(self.D.get(f"/api/vault/items/{self.em}/download").status_code, 200)
        emergency.revoke(self.admin())
        self.assertEqual(self.D.get(f"/api/vault/items/{self.em}/download").status_code, 404)
        self.assertEqual(emergency.history()[0]["status"], "revoked")

    def test_view_grant_expires(self):
        emergency.update_config(self.admin(), {"view_grant_days": 1})
        emergency.request_access(self.des())
        T[0] += 101
        emergency.tick()
        self.assertTrue(emergency.emergency_granted(self.des()))
        T[0] += 86400 + 5
        self.assertFalse(emergency.emergency_granted(self.des()))        # even before tick runs
        self.assertEqual(len(emergency.tick()["expired"]), 1)
        self.assertEqual(self.D.get(f"/api/vault/items/{self.em}/download").status_code, 404)

    def test_cancel_and_duplicate_and_wrong_caller(self):
        emergency.request_access(self.des())
        with self.assertRaises(emergency.EmergencyError) as cm:
            emergency.request_access(self.des())
        self.assertEqual(cm.exception.status, 409)
        emergency.cancel(self.des())
        T[0] += 1000
        emergency.tick()
        self.assertFalse(emergency.emergency_granted(self.des()))
        self.assertEqual(emergency.history()[0]["status"], "cancelled")
        for who in (auth.get_user_by_id(self.oth_id), self.admin()):
            with self.assertRaises(emergency.EmergencyError) as cm:
                emergency.request_access(who)
            self.assertEqual(cm.exception.status, 403)

    def test_disabled_feature_rejects_requests(self):
        emergency.update_config(self.admin(), {"enabled": False})
        with self.assertRaises(emergency.EmergencyError) as cm:
            emergency.request_access(self.des())
        self.assertEqual(cm.exception.status, 409)

    def test_state_is_in_sqlite_and_survives_reinit(self):
        emergency.request_access(self.des())
        T[0] += 101
        emergency.tick()
        emergency.init_db()                               # what a restart does
        self.assertTrue(emergency.emergency_granted(self.des()))
        with store._conn() as c:
            self.assertEqual(c.execute("SELECT status FROM emergency_requests").fetchone()[0], "granted")

    def test_reminders_fire_once_each(self):
        emergency.request_access(self.des())
        n0 = len(self.pushes)
        T[0] += 55; self.assertEqual(emergency.tick()["reminders"], ["r50"])
        T[0] += 1;  self.assertEqual(emergency.tick()["reminders"], [])
        T[0] += 36; self.assertEqual(emergency.tick()["reminders"], ["r90"])
        T[0] += 1;  self.assertEqual(emergency.tick()["reminders"], [])
        self.assertGreater(len(self.pushes), n0)

    def test_second_request_allowed_after_resolution(self):
        emergency.request_access(self.des()); emergency.deny(self.admin())
        emergency.request_access(self.des())                              # unique index only blocks OPEN rows
        self.assertEqual(len(emergency.history()), 2)


class TestTakeover(Base):
    def setUp(self):
        super().setUp()
        self.enable(wait=100, grant="takeover")

    def roles(self):
        return {u["id"]: u["role"] for u in auth.list_users()}

    def test_takeover_swaps_roles_exactly_once_and_disables_feature(self):
        emergency.request_access(self.des())
        T[0] += 101
        calls = []
        real = auth.transfer_admin_role
        with mock.patch.object(auth, "transfer_admin_role", lambda a, b: calls.append((a, b)) or real(a, b)):
            emergency.tick(); emergency.tick(); emergency.tick()
        self.assertEqual(len(calls), 1)
        r = self.roles()
        self.assertEqual((r[self.des_id], r[self.admin_id]), ("admin", "viewer"))
        self.assertEqual(sum(1 for v in r.values() if v == "admin"), 1)
        self.assertFalse(emergency.get_config()["enabled"])
        self.assertEqual(emergency.history()[0]["status"], "granted")
        self.assertIn("auth.user.role_changed", self.audit_types())

    def test_crash_between_steps_leaves_consistent_state_and_recovers(self):
        emergency.request_access(self.des())
        T[0] += 101
        with mock.patch.object(emergency, "_finish_takeover", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                emergency.tick()
        self.assertEqual(emergency.history()[0]["status"], "granting")   # claimed, swap not done
        self.assertEqual(self.roles()[self.admin_id], "admin")
        emergency.tick()                                                  # restart: finishes it
        emergency.tick()
        r = self.roles()
        self.assertEqual((r[self.des_id], r[self.admin_id]), ("admin", "viewer"))
        self.assertEqual(emergency.history()[0]["status"], "granted")

    def test_crash_after_swap_before_status_flip_does_not_swap_twice(self):
        emergency.request_access(self.des())
        T[0] += 101
        real_set = emergency._set_status
        def boom(req, new, **k):
            if new == "granted":
                raise RuntimeError("crash after swap")
            return real_set(req, new, **k)
        with mock.patch.object(emergency, "_set_status", boom):
            with self.assertRaises(RuntimeError):
                emergency.tick()
        self.assertEqual(self.roles()[self.des_id], "admin")              # swap happened
        calls = []
        with mock.patch.object(auth, "transfer_admin_role", lambda a, b: calls.append(1)):
            emergency.tick()
        self.assertEqual(calls, [])                                       # recovery doesn't swap again
        self.assertEqual(emergency.history()[0]["status"], "granted")

    def test_takeover_grant_cannot_be_revoked(self):
        emergency.request_access(self.des()); emergency.approve(self.admin())
        with self.assertRaises(emergency.EmergencyError):
            emergency.revoke(auth.get_user_by_id(self.des_id))


class TestAutoInvalidation(Base):
    def setUp(self):
        super().setUp()
        self.enable(wait=100, grant="takeover")
        emergency.request_access(self.des())

    def assert_disabled_and_cancelled(self):
        self.assertFalse(emergency.get_config()["enabled"])
        self.assertEqual(emergency.history()[0]["status"], "cancelled")
        self.assertIn("emergency.auto_disabled", self.audit_types())
        self.assertTrue(any("DISABLED" in t for _s, t, _b in self.pushes))

    def test_designee_mfa_disabled(self):
        auth.mfa_disable(self.des_id)
        emergency.revalidate("test")
        self.assert_disabled_and_cancelled()

    def test_designee_deleted(self):
        auth.admin_delete_viewer(self.des_id)
        emergency.revalidate("test")
        self.assert_disabled_and_cancelled()
        T[0] += 500
        emergency.tick()                                                   # no grant, no crash
        self.assertEqual(emergency.history()[0]["status"], "cancelled")

    def test_role_transferred_to_someone_else(self):
        r = self.A.post(f"/api/auth/users/{self.oth_id}/transfer-admin",
                        json={"password": PW, "confirm_username": "em_oth"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(emergency.get_config()["enabled"])
        self.assertEqual(emergency.history()[0]["status"], "cancelled")
        with auth._conn() as c:                                            # restore roles + the admin's session
            c.execute("UPDATE users SET role='admin' WHERE id=?", (self.admin_id,))
            c.execute("UPDATE users SET role='viewer' WHERE id=?", (self.oth_id,))
        Base.A = _login("em_admin")

    def test_via_api_delete_viewer_revalidates(self):
        r = self.A.post(f"/api/auth/users/{self.des_id}/delete")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(emergency.get_config()["enabled"])
        # (Base.setUp recreates a designee for the next test)


class TestApiRoles(Base):
    def setUp(self):
        super().setUp()
        self.enable(wait=100)

    def test_status_is_role_scoped(self):
        self.assertEqual(self.anon.get("/api/emergency/status").status_code, 401)
        a = self.A.get("/api/emergency/status").json()
        self.assertIn("config", a); self.assertIn("history", a)
        d = self.D.get("/api/emergency/status").json()
        self.assertTrue(d["is_designee"])
        for leaked in ("config", "history", "viewers", "channels", "pending_change", "designee_uid"):
            self.assertNotIn(leaked, d)
        o = self.O.get("/api/emergency/status").json()
        self.assertEqual(o, {"is_designee": False})

    def test_only_two_designee_writes_in_viewer_allowlist(self):
        em = {p for p in appmod._VIEWER_ALLOWED_WRITE_PATHS if p.startswith("/api/emergency")}
        self.assertEqual(em, {"/api/emergency/request", "/api/emergency/cancel"})
        self.assertFalse([p for p in appmod._VIEWER_ALLOWED_WRITE_PATHS if "/vault/kit" in p])

    def test_admin_only_endpoints_reject_viewers_and_anon(self):
        for method, path in (("post", "/api/emergency/config"), ("post", "/api/emergency/deny"),
                             ("post", "/api/emergency/approve"), ("post", "/api/emergency/revoke"),
                             ("post", "/api/emergency/test-notification"), ("post", "/api/vault/kit"),
                             ("get", "/api/vault/kit/status"), ("get", "/api/vault/kit/sometoken")):
            for c in (self.D, self.O):
                r = getattr(c, method)(path, **({"json": {}} if method == "post" else {}))
                self.assertIn(r.status_code, (403,), (path, r.status_code))
            r = getattr(self.anon, method)(path, **({"json": {}} if method == "post" else {}))
            self.assertEqual(r.status_code, 401, path)

    def test_request_endpoint_designee_only(self):
        self.assertEqual(self.O.post("/api/emergency/request").status_code, 403)
        self.assertEqual(self.A.post("/api/emergency/request").status_code, 403)
        self.assertEqual(self.anon.post("/api/emergency/request").status_code, 401)
        r = self.D.post("/api/emergency/request")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.D.post("/api/emergency/request").status_code, 409)
        self.assertEqual(self.O.post("/api/emergency/cancel").status_code, 404)
        self.assertEqual(self.D.post("/api/emergency/cancel").status_code, 200)

    def test_admin_actions_via_api_and_audit(self):
        self.D.post("/api/emergency/request")
        self.assertEqual(self.A.post("/api/emergency/deny").status_code, 200)
        self.D.post("/api/emergency/request")
        self.assertEqual(self.A.post("/api/emergency/approve").status_code, 200)
        self.assertEqual(self.A.post("/api/emergency/revoke").status_code, 200)
        self.assertTrue({"emergency.requested", "emergency.denied", "emergency.granted",
                         "emergency.revoked"} <= self.audit_types())

    def test_config_endpoint_validates(self):
        r = self.A.post("/api/emergency/config", json={"grant_type": "root"})
        self.assertEqual(r.status_code, 400)
        r = self.A.post("/api/emergency/config", json={"enabled": True, "designee_uid": self.oth_id,
                                                         "wait_seconds": 50, "grant_type": "view"})
        self.assertEqual(r.status_code, 200, r.text)


class TestNotifications(Base):
    def test_emergency_events_reach_admin_push_and_email_despite_muted_prefs(self):
        self.enable(wait=100)
        self.pushes.clear(); self.emails.clear()
        emergency.request_access(self.des())
        admin_pushes = [p for p in self.pushes if p[0].startswith("admin-")]
        self.assertEqual(len(admin_pushes), 2)                              # BOTH admin devices
        self.assertIn("will be granted on", admin_pushes[0][1])
        self.assertIn("unless you deny it", admin_pushes[0][1])
        self.assertEqual(len(self.emails), 1)                               # shared SMTP address
        self.assertTrue(any(p[0] == "des-dev" for p in self.pushes))        # designee acknowledgement
        self.assertNotIn("emergency", notifications.CATEGORIES)             # not mutable in prefs

    def test_deny_grant_revoke_notify(self):
        self.enable(wait=100)
        emergency.request_access(self.des()); emergency.deny(self.admin())
        emergency.request_access(self.des()); emergency.approve(self.admin()); emergency.revoke(self.admin())
        titles = " | ".join(t for _s, t, _b in self.pushes)
        for word in ("DENIED", "GRANTED", "REVOKED"):
            self.assertIn(word, titles)


class TestDetections(Base):
    def test_silent_admin_rule_fires_once(self):
        self.enable(wait=7 * 86400)
        emergency.request_access(self.des())
        self.assertEqual(detections._check_emergency_request_pending_no_admin_activity(), [])   # <24h
        T[0] += 25 * 3600
        self.assertEqual(len(detections._check_emergency_request_pending_no_admin_activity()), 1)
        self.assertEqual(detections._check_emergency_request_pending_no_admin_activity(), [])   # dedupe

    def test_silent_rule_quiet_when_admin_logged_in(self):
        self.enable(wait=7 * 86400)
        emergency.request_access(self.des())
        T[0] += 25 * 3600
        # an admin login that happens AFTER the (fake-clock) request time: put the request in the past
        with store._conn() as c:
            c.execute("UPDATE emergency_requests SET requested_at=?", (time.time() - 3600 * 30,))
        T[0] = time.time()
        _login("em_admin")
        self.assertEqual(detections._check_emergency_request_pending_no_admin_activity(), [])

    def test_bulk_download_rule(self):
        self.enable(wait=100)
        ids = [self.vault_item("emergency") for _ in range(7)]
        emergency.request_access(self.des()); T[0] += 101; emergency.tick()
        for i in ids[:5]:
            self.assertEqual(self.D.get(f"/api/vault/items/{i}/download").status_code, 200)
        self.assertEqual(detections._check_emergency_bulk_download(), [])            # 5 is not > 5
        for i in ids[5:]:
            self.D.get(f"/api/vault/items/{i}/download")
        self.assertEqual(len(detections._check_emergency_bulk_download()), 1)
        self.assertEqual(detections._check_emergency_bulk_download(), [])            # dedupe
        for i in ids:                                                                  # admin is exempt
            self.A.get(f"/api/vault/items/{i}/download")


def _venv_python():
    py = os.path.join(KIT_VENV, "bin", "python")
    if not os.path.exists(py):
        subprocess.run([sys.executable, "-m", "venv", KIT_VENV], check=True)
        subprocess.run([os.path.join(KIT_VENV, "bin", "pip"), "install", "-q", "cryptography"], check=True)
    return py


class TestKit(Base):
    def setUp(self):
        super().setUp()
        self.files = {}
        for vis in ("emergency", "emergency", "household", "private"):
            data = PDF + os.urandom(64)
            iid = self.vault_item(vis, data)
            self.files[iid] = (vis, data)
        self.em_data = {d for (v, d) in self.files.values() if v == "emergency"}

    def make(self, pw=None):
        res = kit.build({"executor_name": "Pat <b>X</b>", "contacts": [{"role": "Attorney", "name": "Lee",
                                                                        "phone": "555-1"}]}, pw)
        path, name = kit.claim(res["download_token"])
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        out = tempfile.mkdtemp(dir=_TMP)
        zipfile.ZipFile(path).extractall(out)
        return res, out

    def run_decrypt(self, outzip, pw, dest, script=None):
        fid = [f for f in os.listdir(outzip) if f.endswith(".fidkit")][0]
        env = {"KIT_PASSPHRASE": pw, "PATH": os.environ.get("PATH", "")}
        return subprocess.run([_venv_python(), "-I", script or os.path.join(outzip, "kit_decrypt.py"),
                               os.path.join(outzip, fid), dest], capture_output=True, text=True, env=env)

    def test_round_trip_in_clean_venv_contains_only_emergency_items(self):
        res, z = self.make()
        self.assertEqual(sorted(os.listdir(z)), sorted([f for f in os.listdir(z)]))
        self.assertTrue({"kit_decrypt.py", "INSTRUCTIONS.html"} <= set(os.listdir(z)))
        dest = tempfile.mkdtemp(dir=_TMP)
        r = self.run_decrypt(z, res["passphrase"], dest)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("checksums OK", r.stdout)
        got = []
        for root, _d, fs in os.walk(os.path.join(dest, "items")):
            got += [open(os.path.join(root, f), "rb").read() for f in fs]
        self.assertEqual(set(got), self.em_data)                               # exactly the emergency items
        self.assertEqual(len(got), 2)
        for vis, data in self.files.values():
            if vis != "emergency":
                self.assertNotIn(data, got)
        self.assertIn("Pat", open(os.path.join(dest, "binder.txt")).read())
        # the clean venv has ONLY cryptography (+ its deps), not the app's packages
        pk = subprocess.run([_venv_python(), "-m", "pip", "list", "--format=freeze"], capture_output=True, text=True).stdout
        self.assertNotIn("fastapi", pk)

    def test_wrong_passphrase_tamper_and_truncation_fail(self):
        res, z = self.make()
        fid = os.path.join(z, [f for f in os.listdir(z) if f.endswith(".fidkit")][0])
        bad = self.run_decrypt(z, "WRONG-WRONG-WRONG-WRONG", tempfile.mkdtemp(dir=_TMP))
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("wrong passphrase", bad.stderr)
        raw = open(fid, "rb").read()
        for label, blob in (("tamper", raw[:-40] + bytes([raw[-40] ^ 1]) + raw[-39:]),
                            ("truncate", raw[:-30])):
            open(fid, "wb").write(blob)
            r = self.run_decrypt(z, res["passphrase"], tempfile.mkdtemp(dir=_TMP))
            self.assertNotEqual(r.returncode, 0, label)
        open(fid, "wb").write(raw)
        self.assertEqual(self.run_decrypt(z, res["passphrase"], tempfile.mkdtemp(dir=_TMP)).returncode, 0)

    def test_chunk_boundary_and_large_item(self):
        big = b"%PDF-" + os.urandom(2 * 1024 * 1024 + 123)
        iid = self.vault_item("emergency", big)
        res, z = self.make()
        dest = tempfile.mkdtemp(dir=_TMP)
        r = self.run_decrypt(z, res["passphrase"], dest)
        self.assertEqual(r.returncode, 0, r.stderr)
        found = [os.path.join(rt, f) for rt, _d, fs in os.walk(os.path.join(dest, "items")) for f in fs]
        self.assertIn(hashlib.sha256(big).hexdigest(), {hashlib.sha256(open(f, "rb").read()).hexdigest() for f in found})

    def test_scripts_copy_is_identical(self):
        self.assertEqual(open(os.path.join(_HERE, "kit_decrypt.py"), "rb").read(),
                         open(os.path.join(_HERE, "..", "scripts", "kit_decrypt.py"), "rb").read())

    def test_passphrase_shape_and_custom(self):
        p = kit.generate_passphrase()
        self.assertGreaterEqual(len(p.replace("-", "")), 26)
        self.assertNotEqual(p, kit.generate_passphrase())
        with self.assertRaises(kit.KitError):
            kit.build({}, "short")
        res, z = self.make("my-own-long-passphrase-1")
        self.assertEqual(self.run_decrypt(z, "my-own-long-passphrase-1", tempfile.mkdtemp(dir=_TMP)).returncode, 0)

    def test_instructions_escape_user_text(self):
        _res, z = self.make()
        html = open(os.path.join(z, "INSTRUCTIONS.html")).read()
        self.assertIn("Pat &lt;b&gt;X&lt;/b&gt;", html)
        self.assertNotIn("<b>X</b>", html)

    def test_api_flow_single_use_token_temp_cleanup_no_secret_in_audit(self):
        r = self.A.post("/api/vault/kit", json={})
        self.assertEqual(r.status_code, 200, r.text)
        j = r.json()
        self.assertEqual(r.headers["cache-control"], "no-store")
        tok = j["download_token"]
        d = self.A.get(f"/api/vault/kit/{tok}")
        self.assertEqual(d.status_code, 200)
        self.assertEqual(d.headers["cache-control"], "no-store")
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(d.content)))
        self.assertEqual(self.A.get(f"/api/vault/kit/{tok}").status_code, 404)       # single use
        self.assertEqual([f for f in os.listdir(kit.KIT_TMP_DIR) if f.startswith("kit-")], [])   # temp removed
        blob = json.dumps(audit.read_recent(limit=500), default=str)
        self.assertNotIn(j["passphrase"], blob)
        self.assertTrue({"vault.kit_generated", "vault.kit_downloaded"} <= self.audit_types())

    def test_token_expiry(self):
        res, _z = self.make()
        res2 = kit.build({})
        with mock.patch("kit.time.time", return_value=time.time() + kit.TOKEN_TTL_S + 5):
            self.assertIsNone(kit.claim(res2["download_token"]))

    def test_staleness_flips_when_emergency_items_change(self):
        self.assertTrue(kit.status()["never_generated"])
        self.make()
        s = kit.status()
        self.assertFalse(s["stale"]); self.assertEqual(s["item_count"], 2)
        self.vault_item("emergency")
        self.assertTrue(kit.status()["stale"])
        self.make()
        self.assertFalse(kit.status()["stale"])
        # changing a non-emergency item does not stale the kit
        self.vault_item("household")
        self.assertFalse(kit.status()["stale"])
        # moving an item OUT of emergency stales it
        iid = next(i for i, (v, _d) in self.files.items() if v == "emergency")
        vault.update_item(iid, visibility="private")
        self.assertTrue(kit.status()["stale"])

    def test_stale_alert_after_seven_days(self):
        self.make()
        self.vault_item("emergency")
        self.assertIsNone(kit.stale_alert())
        self.assertIn("out of date", kit.stale_alert(time.time() + 8 * 86400))

    def test_no_emergency_items_no_kit(self):
        with store._conn() as c:
            c.execute("DELETE FROM vault_items")
        with self.assertRaises(kit.KitError):
            kit.build({})


class TestRiskRegisterMigration(Base):
    def test_new_rows_appended_to_an_existing_register_without_touching_others(self):
        old = {"rows": [{"id": "R-1", "asset": "Something old", "status": "closed", "closed_reason": "x"},
                        {"id": "R-15", "asset": "A row the user added by hand", "status": "open"}],
               "next_number": 16}
        store.write_json(appmod.RISK_REGISTER_FILE, old)
        reg = appmod._read_risk_register()
        ids = [r["id"] for r in reg["rows"]]
        self.assertEqual(ids[:2], ["R-1", "R-15"])                     # untouched, no id collision
        self.assertEqual(len(set(ids)), len(ids))
        assets = {r["asset"] for r in reg["rows"]}
        for a in ("Document vault contents (wills, trusts, IDs, insurance)", "Emergency access (break-glass) feature",
                  "Designee account", "Offline Emergency Kit", "Admin reachability during the emergency wait"):
            self.assertIn(a, assets)
        self.assertEqual(reg["rows"][0]["status"], "closed")
        n = len(appmod._read_risk_register()["rows"])                    # idempotent
        self.assertEqual(n, len(reg["rows"]))


class TestAuditChain(Base):
    def test_chain_verifies_after_the_sequence(self):
        self.enable(wait=100)
        emergency.request_access(self.des()); T[0] += 101; emergency.tick()
        self.assertTrue(audit.verify_chain()["ok"])


if __name__ == "__main__":
    unittest.main()
