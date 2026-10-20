# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_tab_reorder.py -- drag-reorder of tab bars (reported 2026-10-21: moving a tab left worked, moving it right showed
the stop icon). Runs the real functions from index.html under node with a stub DOM."""
import json, os, re, shutil, subprocess, unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()


def _fn(name):
    i = HTML.index(f"function {name}(")
    depth, j = 0, HTML.index("{", i)
    k = j
    while True:
        depth += HTML[k] == "{"
        depth -= HTML[k] == "}"
        k += 1
        if depth == 0:
            return HTML[i:k]


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestReorder(unittest.TestCase):
    def run_js(self, body):
        src = _fn("reorderKeys") + "\n" + _fn("wireTabBarDnD") + "\n" + body
        r = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_move_right_lands_after_the_target_and_last_slot_is_reachable(self):
        out = self.run_js("""console.log(JSON.stringify([
          reorderKeys(['a','b','c','d'],'a','c',true), reorderKeys(['a','b','c','d'],'a','d',true),
          reorderKeys(['a','b','c','d'],'d','b',false), reorderKeys(['a','b','c','d'],'c','c',true),
          reorderKeys(['a','b','c','d'],'a','zz',true)]))""")
        self.assertEqual(out, [["b", "c", "a", "d"], ["b", "c", "d", "a"], ["a", "d", "b", "c"], None, None])

    def test_drop_anywhere_on_the_bar_picks_nearest_tab_and_side(self):
        # tabs at x 0-100 (a), 120-220 (b), 240-340 (c); the gaps/separators in between used to be dead zones
        out = self.run_js("""
        const mk=(k,l)=>({dataset:{dkey:k},style:{},getBoundingClientRect:()=>({left:l,width:100,top:0,height:20})});
        const tabs=[mk('a',0),mk('b',120),mk('c',240)];
        const bar={querySelectorAll:()=>tabs,contains:()=>false};
        const calls=[]; wireTabBarDnD(bar,(f,t,a)=>calls.push([f,t,a]));
        const ev=(x,y)=>({clientX:x,clientY:y,preventDefault(){},dataTransfer:{getData:()=>'a',dropEffect:''}});
        bar.ondrop(ev(110,10));   // in the gap between a and b, nearer a's side -> after a? (nearest is a or b)
        bar.ondrop(ev(400,10));   // far right of the last tab -> after c
        bar.ondrop(ev(300,10));   // right half of c -> after c
        bar.ondrop(ev(250,10));   // left half of c -> before c
        console.log(JSON.stringify(calls));""")
        self.assertEqual(out[1], ["a", "c", True])
        self.assertEqual(out[2], ["a", "c", True])
        self.assertEqual(out[3], ["a", "c", False])
        self.assertIn(out[0][1], ("a", "b"))

    def test_both_bars_use_the_shared_whole_bar_drop_zone(self):
        self.assertEqual(HTML.count("wireTabBarDnD("), 3)          # definition + sub-tab bar + top bar
        self.assertNotIn("t.ondrop=", HTML.split("function wireTabBarDnD")[1].split("function panelSubtabs")[0])
        self.assertNotRegex(HTML, r"t\.ondrop=e=>\{ e\.preventDefault\(\); let from")


if __name__ == "__main__":
    unittest.main()
