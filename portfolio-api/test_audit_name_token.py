# SPDX-License-Identifier: AGPL-3.0-or-later
"""Attempted login names in the audit log: kept raw for admins (escaped in the UI, flagged when suspicious), shown to view-only
accounts only as a stable keyed token."""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="audit-token-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
sys.path.insert(0, _HERE)
import audit  # noqa: E402

HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()
APP = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
PAYLOAD = '<img src=x onerror="document.title=1">'


def _ev(name, actor=None, **kw):
    return dict({"seq": 1, "ts": "t", "actor": actor, "actor_name": name, "event_type": "auth.login.failure",
                 "outcome": "failure", "src_ip": "10.0.0.x", "resource": "/api/auth/login", "detail": {"reason": "x"}}, **kw)


class TestToken(unittest.TestCase):
    def test_stable_case_insensitive_and_distinct(self):
        self.assertEqual(audit.attempted_name_token("Alice"), audit.attempted_name_token(" alice "))
        self.assertNotEqual(audit.attempted_name_token("alice"), audit.attempted_name_token("bob"))
        self.assertRegex(audit.attempted_name_token("alice"), r"^[0-9a-f]{8}$")

    def test_token_does_not_contain_the_name(self):
        self.assertNotIn("alice", audit.attempted_name_token("alice"))

    def test_suspicious_detection(self):
        for bad in (PAYLOAD, "a<b", 'q"x', "back\\slash", "tab\tx", "javascript:alert(1)", "x" * 65, "%3cscript"):
            self.assertTrue(audit.name_is_suspicious(bad), bad)
        for ok in ("alice", "O'Brien", "a&b", "mary.ann@example.com", "x" * 64):
            self.assertFalse(audit.name_is_suspicious(ok), ok)


class TestPresentEvents(unittest.TestCase):
    def test_admin_sees_name_token_and_flag(self):
        out = audit.present_events([_ev(PAYLOAD), _ev("alice")], is_admin=True)
        self.assertEqual(out[0]["actor_name"], PAYLOAD)
        self.assertTrue(out[0]["suspicious"])
        self.assertEqual(out[1]["actor_name"], "alice")
        self.assertFalse(out[1]["suspicious"])
        self.assertEqual(out[0]["name_token"], audit.attempted_name_token(PAYLOAD))

    def test_viewer_never_receives_the_attempted_name(self):
        out = audit.present_events([_ev(PAYLOAD), _ev("hunter2-my-password")], is_admin=False)
        blob = json.dumps(out)
        self.assertNotIn("onerror", blob)
        self.assertNotIn("hunter2", blob)
        for e in out:
            self.assertTrue(e["actor_name_hidden"])
            self.assertIsNone(e["actor_name"])
            self.assertFalse(e["suspicious"])
            self.assertRegex(e["name_token"], r"^[0-9a-f]{8}$")
        self.assertEqual(out[0]["name_token"], audit.attempted_name_token(PAYLOAD))   # still correlatable

    def test_matched_accounts_are_left_alone_for_everyone(self):
        e = _ev("viewer1", actor="abc123", event_type="auth.login.success", outcome="success")
        for admin in (True, False):
            out = audit.present_events([e], is_admin=admin)[0]
            self.assertEqual(out["actor_name"], "viewer1")
            self.assertNotIn("name_token", out)

    def test_input_not_mutated(self):
        src = [_ev("alice")]
        audit.present_events(src, is_admin=False)
        self.assertEqual(src[0]["actor_name"], "alice")


class TestWiring(unittest.TestCase):
    def test_endpoint_uses_the_presenter(self):
        self.assertIn("audit.present_events(audit.read_recent(", APP)

    def test_ui_uses_who_helper_and_escapes(self):
        for needle in ("function _auditWho(e)", "${_auditWho(e)}", "escHtml(e.name_token)", "⚠ suspicious", "e.actor_name_hidden"):
            self.assertIn(needle, HTML, needle)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_who_helper_is_inert_in_node(self):
        esc = re.search(r"^const escHtml = .*$", HTML, re.M).group(0)
        fn = re.search(r"^function _auditWho\(e\)\{.*?^}", HTML, re.S | re.M).group(0)
        js = esc + "\n" + fn + "\n" + (
            "const a=_auditWho({actor_name:%s,name_token:'abcd1234',suspicious:true});"
            "const b=_auditWho({actor_name:null,actor_name_hidden:true,name_token:'abcd1234'});"
            "console.log(JSON.stringify([a,b]));") % json.dumps(PAYLOAD)
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        a, b = json.loads(out.stdout)
        self.assertNotIn("<img", a)
        self.assertIn("&lt;img", a)
        self.assertIn("suspicious", a)
        self.assertIn("#abcd1234", b)
        self.assertNotIn("onerror", b)


if __name__ == "__main__":
    unittest.main()
