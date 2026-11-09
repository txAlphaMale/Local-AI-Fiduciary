"""Recent-errors panel: admin only (viewers can call every other GET), secrets redacted, bounded, newest first."""
import glob, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="recent-errors-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod  # noqa: E402

HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()


class TestRecentErrors(unittest.TestCase):
    def setUp(self):
        appmod._RECENT_ERRORS.clear()

    def test_errref_records_newest_first_and_bounded(self):
        for i in range(60):
            try:
                raise ValueError(f"boom {i}")
            except ValueError as e:
                appmod._errref(e)
        self.assertEqual(len(appmod._RECENT_ERRORS), 50)
        self.assertEqual(appmod._RECENT_ERRORS[0]["message"], "boom 59")

    def test_secrets_in_messages_are_redacted(self):
        try:
            raise RuntimeError("GET https://x.test/q?apikey=SUPERSECRET123&a=1 failed; Authorization: Bearer abc.def.ghi password=hunter2")
        except RuntimeError as e:
            appmod._errref(e)
        rec = appmod._RECENT_ERRORS[0]
        blob = rec["message"] + rec["trace"]
        for s in ("SUPERSECRET123", "abc.def.ghi", "hunter2"):
            self.assertNotIn(s, blob)
        self.assertIn("[redacted]", rec["message"])

    def test_endpoint_is_admin_only(self):
        req = mock.Mock()
        with mock.patch.object(appmod, "_require_admin", return_value=None):
            r = appmod.get_recent_errors(req)
        self.assertEqual(r.status_code, 403)
        with mock.patch.object(appmod, "_require_admin", return_value={"id": 1}), mock.patch.object(appmod.demo_seed, "DEMO_MODE", False):
            r = appmod.get_recent_errors(req)
        self.assertIn("errors", r)

    def test_hidden_on_demo(self):
        with mock.patch.object(appmod.demo_seed, "DEMO_MODE", True), mock.patch.object(appmod, "_require_admin", return_value={"id": 1}):
            self.assertEqual(appmod.get_recent_errors(mock.Mock()).status_code, 404)

    def test_ui_panel_is_admin_only_and_escapes(self):
        self.assertIn("u.role==='admin' && !(DEMO_STATUS&&DEMO_STATUS.demo_mode)", HTML)
        self.assertIn("escHtml(e.trace)", HTML)


if __name__ == "__main__":
    unittest.main()
