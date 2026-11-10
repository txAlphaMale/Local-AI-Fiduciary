"""An open tab can pair old page code with new server data after a deploy; the page now notices (build id in the page vs /health)."""
import glob, os, re, sys, tempfile, unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="newver-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
from fastapi.testclient import TestClient  # noqa: E402
import app as appmod  # noqa: E402

HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()


class TestBuildId(unittest.TestCase):
    def test_page_carries_the_same_build_id_that_health_reports(self):
        c = TestClient(appmod.app)
        page = c.get("/"); health = c.get("/health").json()
        m = re.search(r'<meta name="app-build" content="([0-9a-f]{10})">', page.text)
        self.assertTrue(m); self.assertEqual(m.group(1), health["build"])

    def test_build_id_changes_when_the_page_changes_and_not_otherwise(self):
        import hashlib
        a = appmod._index_page()[1]; b = appmod._index_page()[1]
        self.assertEqual(a, b)
        self.assertEqual(a, hashlib.sha256(HTML.encode("utf-8")).hexdigest()[:10])

    def test_the_page_is_not_cached_and_the_csp_still_matches_its_inline_script(self):
        r = TestClient(appmod.app).get("/")
        self.assertEqual(r.headers.get("cache-control"), "no-cache")
        import base64, hashlib
        csp = r.headers["content-security-policy"]
        for blk in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script\s*>", r.text, re.S | re.I):
            self.assertIn("'sha256-" + base64.b64encode(hashlib.sha256(blk.encode()).digest()).decode() + "'", csp)


class TestPage(unittest.TestCase):
    def test_banner_wiring(self):
        for needle in ("const APP_BUILD=(document.querySelector('meta[name=\"app-build\"]')", "function showNewVersionBanner()", "function checkNewVersion()",
                       "h.build!==APP_BUILD", "checkNewVersion(); setInterval(checkNewVersion, 120000);", "A new version of the dashboard is available."):
            self.assertIn(needle, HTML, needle)


if __name__ == "__main__":
    unittest.main()
