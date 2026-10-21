# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_tavily_direct.py -- Tavily news search called directly (2026-10-21), mcpo kept only as a fallback."""
import datetime, glob, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="tavily-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import tavily_client as tc  # noqa: E402
import app as appmod  # noqa: E402

KEY = "tvly-SECRET-KEY-123"


class Resp:
    def __init__(self, code=200, js=None, text=""):
        self.status_code, self._js, self.text = code, js, text or ("" if js is None else "x")
    def json(self):
        if self._js is None: raise ValueError("no json")
        return self._js


OK = {"query": "q", "results": [{"title": "Acme beats", "url": "https://x/1", "content": "c" * 900, "published_date": "2026-10-20", "score": 0.9},
                                {"title": "", "url": "", "content": "junk"}]}


class TestClient(unittest.TestCase):
    def setUp(self):
        tc.configure(lambda: KEY)

    def test_request_shape_and_key_in_header_only(self):
        with mock.patch.object(tc.requests, "post", return_value=Resp(200, OK)) as p:
            r = tc.search("ACME stock news", days=14, max_results=5)
        kw = p.call_args.kwargs
        self.assertEqual(p.call_args.args[0], "https://api.tavily.com/search")
        self.assertEqual(kw["headers"]["Authorization"], "Bearer " + KEY)
        self.assertEqual(kw["json"]["topic"], "news"); self.assertEqual(kw["json"]["max_results"], 5)
        self.assertEqual(kw["json"]["start_date"], (datetime.date.today() - datetime.timedelta(days=14)).isoformat())
        self.assertNotIn(KEY, repr(kw["json"])); self.assertNotIn(KEY, repr(r))
        self.assertTrue(r["ok"]); self.assertEqual(len(r["articles"]), 1)               # the empty junk row is dropped
        self.assertEqual(r["articles"][0]["published_date"], "2026-10-20")

    def test_400_retries_once_with_a_relative_window(self):
        with mock.patch.object(tc.requests, "post", side_effect=[Resp(400, {"detail": "bad"}, "bad"), Resp(200, OK)]) as p:
            r = tc.search("q", days=10)
        self.assertTrue(r["ok"]); self.assertEqual(p.call_count, 2)
        self.assertEqual(p.call_args.kwargs["json"]["time_range"], "month"); self.assertNotIn("start_date", p.call_args.kwargs["json"])

    def test_each_failure_class_gets_its_own_actionable_message(self):
        msgs = {c: tc.error_detail(c, "boom") for c in (401, 429, 432, 433, 400, 502, 418)}
        self.assertIn("SECRETS & KEYS", msgs[401]); self.assertIn("no restart", msgs[401])
        self.assertIn("rate-limiting", msgs[429]); self.assertIn("credit limit", msgs[432]); self.assertIn("pay-as-you-go", msgs[433])
        self.assertIn("boom", msgs[400]); self.assertIn("problem", msgs[502]); self.assertIn("HTTP 418", msgs[418])
        with mock.patch.object(tc.requests, "post", return_value=Resp(432, {}, "limit")):
            self.assertIn("credit limit", tc.search("q")["detail"])

    def test_no_key_and_network_failure_never_raise_or_leak(self):
        tc.configure(lambda: "")
        self.assertIn("No Tavily API key", tc.search("q")["detail"])
        tc.configure(lambda: KEY)
        with mock.patch.object(tc.requests, "post", side_effect=tc.requests.ConnectionError("dns " + KEY)):
            r = tc.search("q")
        self.assertFalse(r["ok"]); self.assertNotIn(KEY, repr(r)); self.assertIn("internet", r["detail"])
        with mock.patch.object(tc.requests, "post", return_value=Resp(200, None, "<html>")):
            self.assertIn("not JSON", tc.search("q")["detail"])

    def test_time_range_mapping_and_text_blob(self):
        self.assertEqual([tc.time_range_for(d) for d in (1, 3, 7, 14, 40)], ["day", "week", "week", "month", "year"])
        t = tc.as_text([{"title": "T", "url": "U", "content": "C", "published_date": "2026-10-20"}])
        self.assertTrue(t.startswith("Title: T\nURL: U\nPublished: 2026-10-20\nContent: C"))
        self.assertLessEqual(len(tc.as_text([{"title": "x", "content": "y" * 900}] * 20)), 2500)


class TestAppWiring(unittest.TestCase):
    def test_key_precedence_env_then_store(self):
        with mock.patch.object(appmod, "_read_secrets", lambda: {"TAVILY_API_KEY": "from-store"}):
            with mock.patch.object(appmod, "_ENV_SECRETS_AT_BOOT", set()), mock.patch.dict(os.environ, {"TAVILY_API_KEY": "stale-boot"}):
                self.assertEqual(appmod._tavily_key(), "from-store")             # a key saved in the UI is read fresh
            with mock.patch.object(appmod, "_ENV_SECRETS_AT_BOOT", {"TAVILY_API_KEY"}), mock.patch.dict(os.environ, {"TAVILY_API_KEY": "from-env"}):
                self.assertEqual(appmod._tavily_key(), "from-env")               # a real .env value still wins

    def test_direct_when_a_key_is_visible_and_mcpo_only_otherwise(self):
        with mock.patch.object(tc, "configured", lambda: True), mock.patch.object(tc, "search", return_value={"ok": True, "articles": [{"title": "A", "url": "u", "content": "c"}], "http": 200, "detail": ""}), \
             mock.patch.object(appmod, "_tavily_via_mcpo") as legacy:
            r = appmod._tavily_call("q", 7, 3)
        self.assertEqual((r["via"], r["ok"]), ("direct", True)); legacy.assert_not_called(); self.assertIn("Title: A", r["text"])
        with mock.patch.object(tc, "configured", lambda: False), mock.patch.object(appmod, "_tavily_via_mcpo", return_value={"ok": True, "via": "mcpo", "articles": []}) as legacy:
            self.assertEqual(appmod._tavily_call("q", 7, 3)["via"], "mcpo")
        legacy.assert_called_once()

    def test_direct_failure_falls_back_to_mcpo_except_for_key_and_plan_errors(self):
        legacy_ok = {"ok": True, "articles": [{"title": "L"}], "text": "Title: L\n", "http": 200, "detail": "", "via": "mcpo"}
        for http in (400, 502, None):
            bad = {"ok": False, "articles": [], "http": http, "detail": "direct broke"}
            with mock.patch.object(tc, "configured", lambda: True), mock.patch.object(tc, "search", return_value=bad), \
                 mock.patch.object(appmod, "_tavily_via_mcpo", return_value=dict(legacy_ok)):
                r = appmod._tavily_call("q", 7, 3)
            self.assertEqual((r["ok"], r["via"], r["direct_detail"]), (True, "mcpo-fallback", "direct broke"), http)
        for http in (401, 429, 432, 433):
            bad = {"ok": False, "articles": [], "http": http, "detail": "key/plan"}
            with mock.patch.object(tc, "configured", lambda: True), mock.patch.object(tc, "search", return_value=bad), \
                 mock.patch.object(appmod, "_tavily_via_mcpo") as legacy:
                r = appmod._tavily_call("q", 7, 3)
            self.assertEqual((r["ok"], r["via"]), (False, "direct")); legacy.assert_not_called()
        bad = {"ok": False, "articles": [], "http": 400, "detail": "direct broke"}
        with mock.patch.object(tc, "configured", lambda: True), mock.patch.object(tc, "search", return_value=bad), \
             mock.patch.object(appmod, "_tavily_via_mcpo", return_value={"ok": False, "articles": [], "http": 404, "detail": "mcpo 404", "via": "mcpo"}):
            r = appmod._tavily_call("q", 7, 3)
        self.assertEqual((r["ok"], r["detail"]), (False, "direct broke"))        # both failed: report the DIRECT error

    def test_legacy_mcpo_path_still_parses_text_and_reports_404_clearly(self):
        text = "Title: Hello\nURL: http://x\nContent: body\n"
        with mock.patch.object(appmod.requests, "post", return_value=Resp(200, None, text)):
            r = appmod._tavily_via_mcpo("q", 7, 3)
        self.assertEqual(r["articles"][0]["title"], "Hello")
        with mock.patch.object(appmod.requests, "post", return_value=Resp(404, None, "nf")):
            r = appmod._tavily_via_mcpo("q", 7, 3)
        self.assertIn("no running 'tavily' helper", r["detail"])

    def test_results_and_brief_news_use_the_unified_call(self):
        good = {"ok": True, "articles": [{"title": "A"}], "text": "Title: A\n", "http": 200, "detail": "", "via": "direct"}
        with mock.patch.object(appmod, "_tavily_call", return_value=good), mock.patch.object(appmod, "_news_note"):
            with mock.patch.object(appmod.store, "cached_fetch", lambda src, k, ttl, fn, **kw: (fn(), {})):
                self.assertEqual(appmod._tavily_results("q", 7, 3)[0]["title"], "A")
                self.assertEqual(appmod._news("ACME", "Acme"), "Title: A\n")
        quiet = dict(good, articles=[], text="")
        with mock.patch.object(appmod, "_tavily_call", return_value=quiet), mock.patch.object(appmod, "_news_note"), \
             mock.patch.object(appmod.store, "cached_fetch", lambda src, k, ttl, fn, **kw: (fn(), {})):
            self.assertTrue(appmod._news("ACME", "Acme"))        # a quiet week is a non-empty SUCCESS, not a failure
        bad = {"ok": False, "articles": [], "text": "", "http": 432, "detail": "limit", "via": "direct"}
        notes = []
        with mock.patch.object(appmod, "_tavily_call", return_value=bad), mock.patch.object(appmod, "_news_note", lambda ok, d=None, w="": notes.append((ok, d))), \
             mock.patch.object(appmod.store, "cached_fetch", lambda src, k, ttl, fn, **kw: (fn(), {})):
            self.assertEqual(appmod._tavily_results("q"), [])
        self.assertEqual(notes, [(False, "limit")])

    def test_tavily_key_applies_live_and_probe_is_admin_only(self):
        saved = {}
        class FR:
            def __init__(s): s.session = {}; s.headers = {}; s.client = None
            async def json(s): return {"TAVILY_API_KEY": "tvly-x"}
        import asyncio
        with mock.patch.object(appmod, "_read_secrets", lambda: dict(saved)), mock.patch.object(appmod, "_write_secrets", lambda d: saved.update(d)), \
             mock.patch.object(appmod, "_write_mcpo_secrets_file", lambda: None), mock.patch.object(appmod.audit, "emit", lambda *a, **k: None):
            r = asyncio.run(appmod.post_secrets(FR()))
        self.assertEqual(r["applied_live"], ["TAVILY_API_KEY"]); self.assertFalse(r["restart_required"])
        with mock.patch.object(appmod, "_require_admin", lambda req: None):
            resp = appmod.get_news_status(FR(), probe=1)
        self.assertEqual(resp.status_code, 403)

    def test_ui_has_test_button_and_no_stale_mcpo_advice_for_tavily(self):
        html = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()
        for needle in ("TEST TAVILY NEWS SEARCH", "/api/news-status?probe=1", "applies immediately — the app now calls Tavily directly",
                       "scope==='mcpo-file'&&!(d.applied_live||[]).includes(k)"):
            self.assertIn(needle, html)


if __name__ == "__main__":
    unittest.main()
