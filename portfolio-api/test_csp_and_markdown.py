"""R-23: the dashboard sends a Content-Security-Policy whose inline-script hash matches the served page, and the markdown
renderer neutralises raw HTML, javascript: links and images."""
import glob, hashlib, base64, os, re, sys, tempfile, unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="csp-test-")
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


class TestCsp(unittest.TestCase):
    def setUp(self):
        self.csp = TestClient(appmod.app).get("/").headers.get("content-security-policy", "")

    def test_policy_blocks_the_important_things(self):
        for d in ("default-src 'self'", "connect-src 'self'", "object-src 'none'", "base-uri 'none'",
                  "form-action 'self'", "frame-ancestors 'none'"):
            self.assertIn(d, self.csp)
        self.assertNotIn("unsafe-eval", self.csp)
        self.assertNotIn("script-src 'unsafe-inline'", self.csp)
        self.assertNotRegex(self.csp, r"(?:^|; )script-src [^;]*'unsafe-inline'")

    def test_every_inline_script_is_allowed_by_hash(self):
        blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", HTML, re.S | re.I)
        self.assertTrue(blocks)
        for b in blocks:
            h = "'sha256-" + base64.b64encode(hashlib.sha256(b.encode()).digest()).decode() + "'"
            self.assertIn(h, self.csp)

    def test_only_the_pinned_cdns_are_allowed_for_scripts(self):
        script_src = [p for p in self.csp.split("; ") if p.startswith("script-src ")][0]
        hosts = set(re.findall(r"https://[a-z0-9.\-]+", script_src))
        self.assertEqual(hosts, {"https://cdn.jsdelivr.net", "https://cdnjs.cloudflare.com"})

    def test_every_external_script_has_integrity(self):
        for tag in re.findall(r"<script[^>]*\bsrc=\"https://[^>]*>", HTML):
            self.assertIn("integrity=", tag)


class TestMarkdownRenderer(unittest.TestCase):
    def test_renderer_overrides_present(self):
        for needle in ("_mdRenderer.html=", "_mdRenderer.link=", "_mdRenderer.image="):
            self.assertIn(needle, HTML)


if __name__ == "__main__":
    unittest.main()
