# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_push_errors.py -- push delivery failures: contact address, which errors drop a subscription, error reporting."""
import os, sys, tempfile, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("NOTIFY_SUBS_FILE", os.path.join(tempfile.mkdtemp(prefix="pusherr-"), "subs.json"))
import notifications as n  # noqa: E402
from pywebpush import WebPushException  # noqa: E402


class Resp:
    def __init__(self, code, text=""): self.status_code, self.text = code, text


def boom(code, text=""):
    def f(*a, **k): raise WebPushException("Push failed", response=Resp(code, text))
    return f


class TestPush(unittest.TestCase):
    def test_vapid_contact_prefers_real_email_not_localhost(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(n, "_read_json", lambda *a, **k: {}), \
                mock.patch.object(n, "_read_app_secrets", lambda: {"SMTP_TO_ADDRESS": "me@gmail.com"}):
            os.environ.pop("NOTIFY_VAPID_CONTACT_EMAIL", None)
            self.assertEqual(n._vapid_claim_email(), "me@gmail.com")
        with mock.patch.object(n, "_read_json", lambda *a, **k: {}), mock.patch.object(n, "_read_app_secrets", lambda: {}):
            os.environ.pop("NOTIFY_VAPID_CONTACT_EMAIL", None)
            self.assertNotIn("localhost", n._vapid_claim_email())
            self.assertIn(".", n._vapid_claim_email().split("@")[1])
        with mock.patch.dict(os.environ, {"NOTIFY_VAPID_CONTACT_EMAIL": "x@y.org"}):
            self.assertEqual(n._vapid_claim_email(), "x@y.org")

    def test_403_is_reported_and_not_gone(self):
        import pywebpush
        with mock.patch.object(pywebpush, "webpush", boom(403, '{"reason":"BadJwtToken"}')):
            self.assertFalse(n._send_push({"endpoint": "https://web.push.apple.com/x"}, "t", "b"))
        e = n.last_push_error()
        self.assertEqual(e["status"], 403); self.assertIn("BadJwtToken", e["reason"]); self.assertFalse(e["gone"])

    def test_410_is_gone(self):
        import pywebpush
        with mock.patch.object(pywebpush, "webpush", boom(410)):
            n._send_push({"endpoint": "e"}, "t", "b")
        self.assertTrue(n.last_push_error()["gone"])

    def test_dispatch_keeps_a_subscription_on_403_but_drops_on_410(self):
        import pywebpush
        subs = {"u1": [{"endpoint": "keep"}], "u2": [{"endpoint": "drop"}]}
        written = {}
        with mock.patch.object(n, "_read_subs", lambda: dict((k, list(v)) for k, v in subs.items())), \
                mock.patch.object(n, "_write_subs", lambda d: written.update(d)), \
                mock.patch.object(n, "_read_prefs", lambda: {}), mock.patch.object(n, "smtp_configured", lambda: False):
            def fake(subscription_info=None, **k):
                raise WebPushException("x", response=Resp(403 if subscription_info["endpoint"] == "keep" else 410))
            with mock.patch.object(pywebpush, "webpush", fake):
                n._dispatch("security", "auth.user.created", "success", "a", "r", {})
        self.assertEqual(written["u1"], [{"endpoint": "keep"}])
        self.assertEqual(written["u2"], [])

    def test_send_test_surfaces_the_push_service_reason(self):
        import pywebpush
        with mock.patch.object(n, "_read_subs", lambda: {"u1": [{"endpoint": "e"}]}), \
                mock.patch.object(n, "get_prefs", lambda uid: {"push": True, "email": False}), \
                mock.patch.object(pywebpush, "webpush", boom(403, "BadJwtToken")):
            r = n.send_test("u1", False)
        self.assertEqual(r["push_sent"], 0); self.assertIn("403", r["push_error"]); self.assertIn("BadJwtToken", r["push_error"])


if __name__ == "__main__":
    unittest.main()
