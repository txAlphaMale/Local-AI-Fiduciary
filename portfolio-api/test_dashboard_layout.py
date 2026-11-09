"""Dashboard panel order + ARRANGE mode (2026-11-09): net worth first, then portfolio vs SPY, then cost basis; user-draggable."""
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


def _js(body):
    consts = re.search(r"^const DASH_DEFAULT=.*?^  gainers:'TOP GAINERS', losers:'TOP LOSERS', daymovers:'DAY MOVERS'};", HTML, re.S | re.M).group(0)
    src = "\n".join([consts, _fn("dashOrderParse"), _fn("dashMergeOrder"), body])
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestOrderLogic(unittest.TestCase):
    def parse(self, raw):
        return _js("console.log(JSON.stringify(dashOrderParse(%s)));" % json.dumps(raw))

    def test_default_puts_net_worth_first_then_spy_then_cost(self):
        d = self.parse(None)
        self.assertEqual(d["rows"], ["letter", "networth", "spy", "cost"])
        self.assertEqual(len(d["tiles"]), 8)

    def test_saved_order_is_kept_and_new_or_missing_keys_are_appended(self):
        d = self.parse('{"rows":["cost","letter"],"tiles":["gainers"]}')
        self.assertEqual(d["rows"], ["cost", "letter", "networth", "spy"])
        self.assertEqual(d["tiles"][0], "gainers"); self.assertEqual(len(d["tiles"]), 8)

    def test_garbage_unknown_duplicate_never_breaks(self):
        for raw in (None, "", "{", "[]", "null", '"x"', '{"rows":"abc"}', '{"rows":[1,2,{}]}'):
            self.assertEqual(self.parse(raw)["rows"], ["letter", "networth", "spy", "cost"], repr(raw))
        d = self.parse('{"rows":["spy","spy","bogus","cost"]}')
        self.assertEqual(d["rows"], ["spy", "cost", "letter", "networth"])

    def test_merge_keeps_hidden_panels_in_their_slots(self):
        r = _js("console.log(JSON.stringify(dashMergeOrder(['letter','networth','spy','cost'], ['cost','letter','spy'])));")
        # visible slots are 0, 2, 3 -> they take cost, letter, spy in that order; the hidden 'networth' stays in slot 1
        self.assertEqual(r, ["cost", "networth", "letter", "spy"])


class TestWiring(unittest.TestCase):
    def test_dashboard_uses_the_order_and_arrange_mode(self):
        for needle in ("DASH_ORDER.rows.forEach(k=>{", "DASH_ORDER.tiles.forEach(k=>{", "wireDashDnD(rowsBox,'rows',true)", "wireDashDnD(small,'tiles',false)",
                       "wrap.appendChild(dashToolbar());", "localStorage.setItem('fidDashOrder'", "RESET LAYOUT", "'▲'", "'▼'"):
            self.assertIn(needle, HTML, needle)
        self.assertNotIn("function valuePanel(", HTML)

    def test_accounts_stays_docked_and_non_arrange_mode_is_inert(self):
        self.assertIn("const acctCol=el('div'); acctCol.appendChild(accountsPanel(d)); body.appendChild(acctCol);", HTML)
        self.assertIn("if(!DASH_ARRANGE) return node;", HTML)          # no handles, not draggable, unless ARRANGE is on

    def test_arrange_is_not_persisted_only_the_order_is(self):
        self.assertNotIn("localStorage.setItem('fidDashArrange'", HTML)


if __name__ == "__main__":
    unittest.main()
