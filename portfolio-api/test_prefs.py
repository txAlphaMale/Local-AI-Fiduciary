"""Server-side interface preferences: per account, whitelisted, encrypted, size-capped, off on the demo."""
import glob, json, os, re, sqlite3, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="prefs-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import prefs  # noqa: E402
import app as appmod  # noqa: E402

HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()


class FakeReq:
    def __init__(self, uid, body=None):
        self.session = {"uid": uid} if uid else {}
        self._b = body or {}

    async def json(self):
        return self._b


def run(coro):
    import asyncio
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class TestStore(unittest.TestCase):
    def test_round_trip_is_per_user_and_merges(self):
        prefs.update("u1", set_={"fidDashOrder": '{"rows":["cost"]}'})
        prefs.update("u1", set_={"fidGraphs": '{"spy":false}'})
        prefs.update("u2", set_={"fidTheme": "dark"})
        self.assertEqual(prefs.get("u1"), {"fidDashOrder": '{"rows":["cost"]}', "fidGraphs": '{"spy":false}'})
        self.assertEqual(prefs.get("u2"), {"fidTheme": "dark"})
        prefs.update("u1", remove=["fidGraphs"])
        self.assertEqual(set(prefs.get("u1")), {"fidDashOrder"})

    def test_encrypted_at_rest(self):
        prefs.update("u3", set_={"fidCalcIn": '{"salary":"230000"}'})
        raw = sqlite3.connect(prefs.PREFS_DB).execute("SELECT blob FROM prefs WHERE uid='u3'").fetchone()[0]
        self.assertNotIn("230000", raw)

    def test_only_whitelisted_keys_and_text_values_within_limits(self):
        for bad in ({"notAKey": "x"}, {"fidSubtab": "x"}, {"fidTheme": 5}, {"fidTheme": "x" * (prefs.MAX_VALUE + 1)}, {"../../etc": "x"}):
            with self.assertRaises(ValueError, msg=str(bad)):
                prefs.update("u4", set_=bad)
        big = {k: "y" * 59000 for k in list(prefs.SYNC_KEYS)[:5]}
        with self.assertRaises(ValueError):
            prefs.update("u5", set_=big)

    def test_unreadable_stored_blob_is_an_empty_set_not_an_error(self):
        with prefs._conn() as c:
            c.execute("INSERT OR REPLACE INTO prefs(uid, blob, updated) VALUES('u6','not-encrypted-garbage',0)")
        self.assertEqual(prefs.get("u6"), {})

    def test_delete_user(self):
        prefs.update("u7", set_={"fidTheme": "t"}); prefs.delete_user("u7"); self.assertEqual(prefs.get("u7"), {})

    def test_sync_keys_match_the_page(self):
        m = re.search(r"const PREFS_KEYS=\[(.*?)\];", HTML)
        page = set(re.findall(r"'(fid[A-Za-z0-9]+)'", m.group(1)))
        self.assertEqual(page, set(prefs.SYNC_KEYS))


class TestEndpoints(unittest.TestCase):
    def test_get_and_post_for_a_signed_in_user(self):
        with mock.patch.object(appmod.demo_seed, "DEMO_MODE", False):
            r = run(appmod.prefs_set(FakeReq("ua", {"set": {"fidTabOrder": "[1,2]"}})))
            self.assertTrue(r["ok"]); self.assertEqual(appmod.prefs_get(FakeReq("ua"))["prefs"], {"fidTabOrder": "[1,2]"})
            self.assertEqual(appmod.prefs_get(FakeReq("ub"))["prefs"], {})                      # another account sees nothing of it

    def test_bad_key_is_a_400_and_signed_out_is_a_401(self):
        with mock.patch.object(appmod.demo_seed, "DEMO_MODE", False):
            self.assertEqual(run(appmod.prefs_set(FakeReq("ua", {"set": {"evil": "x"}}))).status_code, 400)
            self.assertEqual(run(appmod.prefs_set(FakeReq(None, {"set": {"fidTheme": "x"}}))).status_code, 401)

    def test_disabled_on_the_demo(self):
        with mock.patch.object(appmod.demo_seed, "DEMO_MODE", True):
            self.assertFalse(appmod.prefs_get(FakeReq("demo"))["enabled"])
            r = run(appmod.prefs_set(FakeReq("demo", {"set": {"fidTheme": "x"}})))
            self.assertFalse(r["enabled"]); self.assertEqual(prefs.get("demo"), {})

    def test_viewers_may_save_their_own_layout(self):
        self.assertIn("/api/prefs", appmod._VIEWER_ALLOWED_WRITE_PATHS)

    def test_account_deletion_clears_prefs(self):
        src = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
        self.assertGreaterEqual(src.count("prefs.delete_user(uid)"), 2)


class TestPage(unittest.TestCase):
    def test_sync_is_wired_into_sign_in_and_storage_writes(self):
        for needle in ("async function prefsPull()", "Storage.prototype.setItem=function", "function authInitAppReal()",
                       "prefsPull().then(reloading=>", "navigator.sendBeacon('/api/prefs'", "sessionStorage.setItem('fidPrefsReloaded'"):
            self.assertIn(needle, HTML, needle)


if __name__ == "__main__":
    unittest.main()
