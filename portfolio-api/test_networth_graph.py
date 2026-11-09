# SPDX-License-Identifier: AGPL-3.0-or-later
"""Net-worth bar graph + per-graph show/hide toggles (2026-11-08): bucketing, preference parsing, wiring."""
import datetime
import json
import os
import re
import shutil
import subprocess
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()


def _fn(name):
    m = re.search(r"^function %s\(.*?^}" % re.escape(name), HTML, re.S | re.M)
    assert m, name
    return m.group(0)


def _line(prefix):
    m = re.search(r"^%s.*$" % re.escape(prefix), HTML, re.M)
    assert m, prefix
    return m.group(0)


def _js(body):
    src = "\n".join([
        _line("const _isoMinus="), _fn("chartWindowRange"), _fn("bucketRows"), _fn("bucketMode"), _fn("netWorthRows"),
        _line("const GRAPH_KEYS="), _fn("graphPrefsParse"), body])
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _hist(days, start="2025-01-01", f=lambda i: 100000 + i * 10):
    d0 = datetime.date.fromisoformat(start)
    return [{"date": (d0 + datetime.timedelta(days=i)).isoformat(), "net_worth": f(i)} for i in range(days)]


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestNetWorthBuckets(unittest.TestCase):
    def rows(self, hist, win="all"):
        return _js("console.log(JSON.stringify(netWorthRows(%s, %s, '', '')));" % (json.dumps(hist), json.dumps(win)))

    def test_short_history_is_daily(self):
        r = self.rows(_hist(60))
        self.assertEqual(r["mode"], "daily")
        self.assertEqual(len(r["rows"]), 60)

    def test_medium_history_compresses_to_weekly_keeping_the_last_value_of_each_week(self):
        h = _hist(200)
        r = self.rows(h)
        self.assertEqual(r["mode"], "weekly")
        self.assertTrue(25 <= len(r["rows"]) <= 31, len(r["rows"]))
        self.assertEqual(r["rows"][-1]["nw"], h[-1]["net_worth"])
        self.assertEqual(r["rows"][-1]["date"], h[-1]["date"])

    def test_long_history_compresses_to_monthly(self):
        r = self.rows(_hist(900))
        self.assertEqual(r["mode"], "monthly")
        self.assertTrue(28 <= len(r["rows"]) <= 32, len(r["rows"]))

    def test_a_short_window_on_a_long_history_goes_back_to_daily(self):
        r = self.rows(_hist(900), win="1m")
        self.assertEqual(r["mode"], "daily")
        self.assertTrue(25 <= len(r["rows"]) <= 32, len(r["rows"]))

    def test_bad_days_are_skipped_and_negative_net_worth_is_kept(self):
        h = _hist(10, f=lambda i: -5000 + i)
        h[3]["net_worth"] = 0
        h[4]["net_worth"] = None
        r = self.rows(h)
        self.assertEqual(len(r["rows"]), 8)
        self.assertTrue(all(x["nw"] < 0 for x in r["rows"]))

    def test_needs_two_days(self):
        self.assertEqual(self.rows(_hist(1))["rows"], [])
        self.assertEqual(self.rows([])["rows"], [])


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestGraphPrefs(unittest.TestCase):
    def parse(self, raw):
        return _js("console.log(JSON.stringify(graphPrefsParse(%s)));" % json.dumps(raw))

    def test_defaults_show_everything(self):
        allon = {"spy": True, "cost": True, "networth": True, "pies": True}
        for raw in (None, "", "garbage{", "null", "[]", "5"):
            self.assertEqual(self.parse(raw), allon, repr(raw))

    def test_only_an_explicit_false_hides_a_graph(self):
        self.assertEqual(self.parse('{"networth":false,"spy":"no","cost":0}'),
                         {"spy": True, "cost": True, "networth": False, "pies": True})


class TestWiring(unittest.TestCase):
    def test_panels_and_toggles_are_wired(self):
        for needle in ("function spyPanel(withBar)", "function costPanel(withBar)", "function netWorthPanel(withBar)", "function graphToggleBar()",
                       "else if(k==='networth' && graphOn('networth')) n=netWorthPanel(takeBar());", "if(k.indexOf('alloc-')===0 || k.indexOf('mix-')===0){ if(!graphOn('pies')) return; }",
                       "document.getElementById('c-networth')", "localStorage.setItem('fidGraphs'", "'<canvas id=\"c-networth\"></canvas>'"):
            self.assertIn(needle, HTML, needle)

    def test_net_worth_is_a_quiet_line_not_bars(self):
        block = HTML.split("document.getElementById('c-networth')")[1].split("// fetch the holdings-based return series")[0]
        self.assertIn("type:'line'", block)
        self.assertNotIn("type:'bar'", block)
        self.assertIn("pointRadius:0", block)
        self.assertIn("Line: your net worth", HTML)

    def test_window_picker_survives_hiding_the_first_chart(self):
        self.assertIn("const takeBar=()=>{ const b=needBar; needBar=false; return b; };", HTML)
        self.assertIn("if(withBar) p.appendChild(perfToolbar());", HTML)


if __name__ == "__main__":
    unittest.main()
