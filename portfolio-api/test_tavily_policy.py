"""Tavily results: the content of a result is used in memory and never written to disk or into a stored record; links, titles, sites and dates may be kept."""
import json, os, sys, tempfile, unittest

_TMP = tempfile.mkdtemp(prefix="tav-test-")
os.environ["STORE_DB"] = os.path.join(_TMP, "store.db")
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store, tavily_policy as tp  # noqa: E402

ART = [{"title": "Rocket Lab wins contract", "url": "https://www.example.com/a?x=1", "content": "SECRET ARTICLE BODY " * 10, "published_date": "2026-11-01", "score": 0.9},
       {"title": "No link here", "url": "javascript:alert(1)", "content": "more body"}]


class Refs(unittest.TestCase):
    def test_references_only(self):
        r = tp.refs(ART)
        self.assertEqual(r[0], {"title": "Rocket Lab wins contract", "url": "https://www.example.com/a?x=1", "domain": "example.com", "published_date": "2026-11-01"})
        self.assertIsNone(r[1]["url"])                                  # not a web address: not kept
        self.assertNotIn("SECRET", json.dumps(r))
        self.assertNotIn("score", r[0])

    def test_news_blob_keeps_titles_only(self):
        blob = "Title: A\nURL: u\nPublished: d\nContent: BODY ONE\n\nTitle: B\nURL: v\nContent: BODY TWO\n"
        out = tp.clean_stored_news_text(blob)
        self.assertEqual(out, "- A\n- B")
        self.assertEqual(tp.clean_stored_news_text("No recent articles found."), "No recent articles found.")

    def test_strategist_sources_keep_links_not_text(self):
        res = tp.sources_for_report("web_search", {"query": "q"}, {"results": ART})
        self.assertNotIn("SECRET", json.dumps(res))
        self.assertEqual(res["results"][0]["domain"], "example.com")
        other = {"cash": 5}
        self.assertEqual(tp.sources_for_report("get_paper_portfolio", {}, other), other)      # other tools are untouched


class Store(unittest.TestCase):
    def test_disk_copy_holds_references_while_memory_holds_the_full_result(self):
        full, meta = store.cached_fetch("tavily", "k1", 3600, lambda: ART, is_ok=lambda r: bool(r), persist_fn=tp.refs)
        self.assertIn("SECRET", json.dumps(full))                       # this process still gets the content for the request in front of it
        disk = store.ext_cache_get("tavily", "k1")[1]
        self.assertNotIn("SECRET", json.dumps(disk))
        self.assertEqual(disk[0]["domain"], "example.com")

    def test_outage_fallback_serves_references_only(self):
        store._MEM_CACHE.clear()
        store.ext_cache_set("tavily", "k2", tp.refs(ART))
        val, meta = store.cached_fetch("tavily", "k2", 0, lambda: [], is_ok=lambda r: bool(r), persist_fn=tp.refs)
        self.assertTrue(meta["fallback"])
        self.assertNotIn("content", val[0])

    def test_clean_up_of_entries_written_before_the_rule(self):
        store.ext_cache_set("tavily", "old", ART)
        store.ext_cache_set("tavily_news_raw", "oldnews", "Title: T\nURL: u\nContent: BODY BODY\n")
        n = store.ext_cache_rewrite("tavily", tp.clean_stored_articles) + store.ext_cache_rewrite("tavily_news_raw", tp.clean_stored_news_text)
        self.assertGreaterEqual(n, 2)
        self.assertNotIn("SECRET", json.dumps(store.ext_cache_get("tavily", "old")[1]))
        self.assertEqual(store.ext_cache_get("tavily_news_raw", "oldnews")[1], "- T")
        self.assertEqual(store.ext_cache_rewrite("tavily", tp.clean_stored_articles), 0)       # idempotent


try:
    import app
except Exception:
    app = None


@unittest.skipIf(app is None, "app not importable")
class Report(unittest.TestCase):
    def test_strategist_source_log_records_a_search_as_links(self):
        log = []
        app._note_source(log, "web_search", {"query": "rklb"}, {"results": ART})
        self.assertNotIn("SECRET", log[0]["result"])
        self.assertIn("example.com", log[0]["result"])


if __name__ == "__main__":
    unittest.main()
