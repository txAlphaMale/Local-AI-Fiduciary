# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_ideas_phase1.py -- Discovered/Scanner Phase 1 (Jason 2026-11-12): discovery is gated like the scanner,
paper-ledger names are skipped, dropped names keep their reason, both tabs carry a funnel, scanner ideas say
where they came from, and the peer step uses the largest holdings in a stable order."""
import glob, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="ideas1-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod  # noqa: E402

HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()


def _screen(sym, crit):
    """Fake screen: GOOD fits 4/4, MID 3/4 over the price cap, BAD 0/4, NODATA unavailable."""
    if sym == "NODATA":
        return {"ticker": sym, "available": False}
    fit = {"GOOD": 4, "MID": 3, "BAD": 0, "PEER1": 3, "PEER2": 1}.get(sym, 2)
    price = 250.0 if sym == "MID" else 20.0
    names = ["rev_growth", "fcf_margin", "valuation", "analyst"]
    checks = [{"criterion": n, "pass": i < fit, "value": (None if i >= fit else 1.0)} for i, n in enumerate(names)]
    return {"ticker": sym, "available": True, "price": price, "fit_score": fit, "max_score": 4, "checks": checks}


class TestHelpers(unittest.TestCase):
    def test_failed_checks_lists_only_failures(self):
        self.assertEqual(appmod._failed_checks(_screen("BAD", {}))[0], "rev_growth n/a")
        self.assertEqual(appmod._failed_checks(_screen("GOOD", {})), [])

    def test_price_cap_reason(self):
        self.assertIsNone(appmod._price_cap_reason(250, {}))
        self.assertIn("above your $100 cap", appmod._price_cap_reason(250, {"max_share_price": 100}))
        self.assertIn("below", appmod._price_cap_reason(2, {"min_share_price": 5}))
        self.assertIn("unknown", appmod._price_cap_reason(None, {"max_share_price": 100}))

    def test_paper_held_skips_archived_and_flat_positions(self):
        reg = {"scenarios": {
            "a": {"status": "active", "positions": {"rkt": {"shares": 3}, "ZERO": {"shares": 0}}},
            "b": {"status": "archived", "positions": {"OLD": {"shares": 9}}}}}
        with mock.patch.object(appmod, "_read_registry", return_value=reg):
            self.assertEqual(appmod._paper_held_tickers(), {"RKT"})


class TestDiscoveryGate(unittest.TestCase):
    def _run(self, cands, held=(), paper=(), crit=None):
        import json
        cfg = {"discovery_queries": ["q"], **{}}
        funnel = {}
        arts = [{"title": "headline", "content": "x"}]
        with mock.patch.object(appmod, "_read_scanner", return_value=cfg), \
             mock.patch.object(appmod, "_tavily_results", return_value=(arts, {"fetched_at": 1, "stale": False})), \
             mock.patch.object(appmod, "_ollama_generate", return_value=(json.dumps({"candidates": cands}), 0)), \
             mock.patch.object(appmod, "_screen_one", side_effect=_screen), \
             mock.patch.object(appmod, "_paper_held_tickers", return_value=set(paper)), \
             mock.patch.object(appmod.edgar, "fundamentals", return_value={}), \
             mock.patch.object(appmod, "_valuation", return_value={}), \
             mock.patch.object(appmod, "_grounded", return_value={}), \
             mock.patch.object(appmod, "_news", return_value=[]), \
             mock.patch.object(appmod, "_llm", return_value={"rating": "Buy"}):
            d = {"consolidated": [{"ticker": t} for t in held]}
            out = appmod._discover(d, crit or {"max_share_price": 100}, _funnel_acc=funnel)
        return out, funnel

    def test_gate_keeps_only_good_names_and_records_reasons(self):
        out, fn = self._run([{"ticker": t, "reason": "r"} for t in
                             ["GOOD", "BAD", "MID", "NODATA", "HELD", "PAPER"]], held=["HELD"], paper=["PAPER"])
        self.assertEqual([x["ticker"] for x in out], ["GOOD"])
        self.assertEqual(out[0]["source"], "news discovery")
        by = {x["ticker"]: x for x in fn["dropped"]}
        self.assertEqual(set(by), {"BAD", "MID", "NODATA", "HELD", "PAPER"})
        self.assertIn("below the", " ".join(by["BAD"]["reasons"]))
        self.assertIn("above your $100 cap", " ".join(by["MID"]["reasons"]))
        self.assertIn("no public SEC", " ".join(by["NODATA"]["reasons"]))
        self.assertIn("paper ledger", " ".join(by["PAPER"]["reasons"]))
        self.assertEqual((fn["extracted"], fn["kept"], fn["held_skipped"], fn["paper_skipped"], fn["no_data"],
                          fn["below_min_fit"], fn["over_price_cap"]), (6, 1, 1, 1, 1, 1, 1))

    def test_everything_dropped_still_returns_a_funnel(self):
        out, fn = self._run([{"ticker": "BAD", "reason": "r"}])
        self.assertEqual(out, [])
        self.assertEqual(fn["kept"], 0)
        self.assertEqual(len(fn["dropped"]), 1)


class TestScannerFunnel(unittest.TestCase):
    def _run(self, peers_on=True):
        cfg = {"watchlist": ["GOOD", "NODATA"], "include_holding_peers": peers_on,
               "criteria": {}, "discovery_queries": []}
        order = []

        def fh(path, sym):
            order.append(sym)
            return ["PEER1", "PEER2"]
        data = {"consolidated": [{"ticker": t} for t in ["AAA", "BBB"]]}
        with mock.patch.object(appmod, "_read_scanner", return_value=cfg), \
             mock.patch.object(appmod, "_data", return_value=data), \
             mock.patch.object(appmod, "_finnhub", side_effect=fh), \
             mock.patch.object(appmod, "_screen_one", side_effect=_screen), \
             mock.patch.object(appmod, "_paper_held_tickers", return_value={"GOOD"}):
            return appmod._opportunities(), order

    def test_funnel_sources_and_stable_peer_order(self):
        r, order = self._run()
        self.assertEqual(order, ["AAA", "BBB"])        # largest holdings first, in value order
        f = r["funnel"]
        self.assertEqual((f["from_watchlist"], f["from_peers"], f["peer_lookups"]), (2, 2, 2))
        self.assertEqual((f["screened"], f["no_data"], f["with_data"], f["below_min_fit"], f["passed"]), (4, 1, 3, 1, 2))
        self.assertTrue(f["peers_enabled"])
        self.assertEqual(f["failed_by_criterion"].get("analyst"), 2)
        by = {i["ticker"]: i for i in r["ideas"]}
        self.assertEqual(by["GOOD"]["source"], "your watchlist")
        self.assertTrue(by["GOOD"]["paper_held"])
        self.assertEqual(by["PEER1"]["source"], "peer of AAA")
        self.assertEqual(by["GOOD"]["failed_checks"], [])

    def test_peers_off_is_reported(self):
        r, order = self._run(peers_on=False)
        self.assertEqual(order, [])
        self.assertFalse(r["funnel"]["peers_enabled"])
        self.assertEqual(r["funnel"]["from_peers"], 0)


class TestPageWiring(unittest.TestCase):
    def test_discovered_tab_survives_an_empty_result_and_shows_funnel(self):
        self.assertIn("DISCO.funnel && DISCO.funnel.articles!=null", HTML)
        self.assertIn("function discFunnelNodes", HTML)
        self.assertIn("Dropped by the screen", HTML)
        self.assertIn("Nothing from the newsfeed passed your screen this run", HTML)

    def test_scanner_shows_funnel_source_and_paper_tag(self):
        self.assertIn("const addFunnel=()=>", HTML)
        self.assertGreaterEqual(HTML.count("addFunnel();"), 2)    # empty state AND normal state
        self.assertIn("i.paper_held?", HTML)
        self.assertIn("where this candidate came from", HTML)


if __name__ == "__main__":
    unittest.main()
