# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_env_override_warning.py -- warn when a value saved in the UI is overridden by the server's .env
(tester 2026-10-21: .env said csv and silently beat his UI choice)."""
import glob, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="envov-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod  # noqa: E402


def run(stored, secrets, boot_env, env):
    with mock.patch.object(appmod, "_read_settings", lambda: stored), \
         mock.patch.object(appmod, "_read_secrets", lambda: secrets), \
         mock.patch.object(appmod, "_ENV_SECRETS_AT_BOOT", set(boot_env)), \
         mock.patch.dict(os.environ, env):
        return appmod._env_overrides()


class TestOverrides(unittest.TestCase):
    def test_config_value_saved_but_env_differs_is_reported(self):
        r = run({"ANALYSIS_MODEL": "new-model"}, {}, {"ANALYSIS_MODEL"}, {"ANALYSIS_MODEL": "old-model"})
        self.assertEqual([(o["key"], o["saved"], o["env"]) for o in r], [("ANALYSIS_MODEL", "new-model", "old-model")])

    def test_same_value_or_nothing_saved_is_not_a_conflict(self):
        self.assertEqual(run({"ANALYSIS_MODEL": "m"}, {}, {"ANALYSIS_MODEL"}, {"ANALYSIS_MODEL": "m"}), [])
        self.assertEqual(run({}, {}, {"ANALYSIS_MODEL"}, {"ANALYSIS_MODEL": "m"}), [])

    def test_data_source_selector_is_exempt_because_the_ui_wins(self):
        self.assertEqual(run({"PORTFOLIO_PROVIDER": "snaptrade"}, {}, {"PORTFOLIO_PROVIDER"}, {"PORTFOLIO_PROVIDER": "csv"}), [])

    def test_secret_saved_and_also_in_env_is_reported_without_values(self):
        r = run({}, {"FINNHUB_API_KEY": "gAAAA-secret"}, {"FINNHUB_API_KEY"}, {"FINNHUB_API_KEY": "from-env"})
        self.assertEqual([(o["key"], o["kind"]) for o in r], [("FINNHUB_API_KEY", "secret")])
        self.assertNotIn("gAAAA-secret", repr(r)); self.assertNotIn("from-env", repr(r))

    def test_secret_only_in_ui_is_fine(self):
        self.assertEqual(run({}, {"FINNHUB_API_KEY": "x"}, set(), {}), [])


class TestWiring(unittest.TestCase):
    HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()

    def test_endpoint_admin_only_and_banner_shown_on_all_settings_subtabs(self):
        src = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
        i = src.index("def get_env_overrides")
        self.assertIn("_require_admin(request)", src[i:i + 300])
        self.assertIn("tc.appendChild(envOverrideBanner());\n    tc.appendChild(panelSubtabs('settings'", self.HTML)
        self.assertIn("/api/settings/overrides", self.HTML)


if __name__ == "__main__":
    unittest.main()
