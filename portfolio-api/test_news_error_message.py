# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_news_error_message.py -- the Tavily/mcpo failure text tells a new user what is wrong AND what to do (tester 2026-10-21)."""
import glob, os, re, sys, tempfile, unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="newsmsg-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod  # noqa: E402


class R:
    def __init__(self, code, text=""): self.status_code, self.text = code, text


class TestNewsHttpDetail(unittest.TestCase):
    def test_404_says_what_to_check_and_the_exact_commands(self):
        d = appmod._news_http_detail(R(404))
        for needle in ("no running 'tavily' helper", "SECRETS & KEYS", "docker compose up -d mcpo --force-recreate",
                       "plain restart does NOT", "docker logs --tail 50 fiduciary-mcpo"):
            self.assertIn(needle, d)

    def test_auth_rate_limit_and_other_errors_each_get_their_own_advice(self):
        self.assertIn("MCPO_API_KEY", appmod._news_http_detail(R(401)))
        self.assertIn("rate-limited", appmod._news_http_detail(R(429)))
        d = appmod._news_http_detail(R(500, "boom\nline2"))
        self.assertIn("out-of-credit", d); self.assertIn("boom line2", d); self.assertNotIn("\n", d)


if __name__ == "__main__":
    unittest.main()
