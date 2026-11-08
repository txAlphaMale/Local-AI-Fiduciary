# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the 2026-11 stored-XSS finding: an unauthenticated failed login stores its attempted username
verbatim in the audit log, a viewer can rename themselves, and the admin UI rendered both unescaped."""
import os
import re
import shutil
import subprocess
import unittest

import auth

_HERE = os.path.dirname(__file__)
HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()
AUDIT = open(os.path.join(_HERE, "audit.py"), encoding="utf-8").read()


class TestUsernameValidation(unittest.TestCase):
    def test_markup_and_control_characters_refused(self):
        for bad in ("<img src=x onerror=alert(1)>", "a<b", 'q"x', "back\\slash", "tab\tname", "a" * 65):
            with self.assertRaises(ValueError, msg=bad):
                auth._check_username(bad)

    def test_ordinary_names_allowed(self):
        for ok in ("alice", "a.b-c_d@example.com", "Mary Ann", "O'Brien", "a&b", "x" * 64):
            auth._check_username(ok)


class TestUiEscaping(unittest.TestCase):
    def test_no_unescaped_attacker_controlled_fields(self):
        for raw in ("${a.username}", "${e.actor_name||'—'}", "${e.resource||''}", "${e.event_type||''}", "${u.username}",
                    "${_pendingLoginCreds.username}", "onclick=\"accountsDeleteUser(", "onclick=\"accountsTransferAdmin("):
            self.assertNotIn(raw, HTML, raw)

    def test_escaped_forms_present(self):
        for needle in ("const escHtml =", "${escHtml(a.username)}", "${escHtml(e.actor_name)||'—'}", "${escHtml(detailShort)}",
                       "class=\"acct-del\"", "b.dataset.un"):
            self.assertIn(needle, HTML, needle)

    def test_audit_actor_name_is_bounded(self):
        self.assertIn("actor_name = str(actor_name)[:128]", AUDIT)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_escHtml_makes_payloads_inert(self):
        src = re.search(r"^const escHtml = .*$", HTML, re.M).group(0)
        js = src + "\nconsole.log(JSON.stringify([escHtml('<img src=x onerror=alert(1)>'), escHtml('\"><script>'), escHtml(null), escHtml(\"a'b&c\")]));"
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        import json
        a, b, c, d = json.loads(out.stdout)
        self.assertNotIn("<", a)
        self.assertNotIn("<", b)
        self.assertNotIn('"', b)
        self.assertEqual(c, "")
        self.assertEqual(d, "a&#39;b&amp;c")


if __name__ == "__main__":
    unittest.main()
