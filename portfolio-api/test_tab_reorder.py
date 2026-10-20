# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_tab_reorder.py -- live drag-reorder of tab bars (reports 2026-10-21: moving right showed the stop icon; the drop
marker looked glued to the word; "tabs should rearrange while I'm still holding the mouse, like browser tabs").
Runs the real wireTabBarDnD from index.html under node against a tiny fake DOM."""
import json, os, shutil, subprocess, unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()


def _fn(name):
    i = HTML.index(f"function {name}(")
    depth, k = 0, HTML.index("{", i)
    while True:
        depth += HTML[k] == "{"
        depth -= HTML[k] == "}"
        k += 1
        if depth == 0:
            return HTML[i:k]


FAKE_DOM = r"""
class N {
  constructor(kind, key){ this.kind=kind; this.dataset={}; if(kind==='tab') this.dataset.dkey=key; if(kind==='sep') this.dataset.sep='1';
    this.style={}; this.children=[]; this.parentNode=null; }
  get nextSibling(){ const s=this.parentNode.children; return s[s.indexOf(this)+1]||null; }
  remove(){ if(this.parentNode){ const s=this.parentNode.children; s.splice(s.indexOf(this),1); this.parentNode=null; } }
  insertBefore(n, ref){ n.remove(); const s=this.children; const i=ref?s.indexOf(ref):s.length; s.splice(i<0?s.length:i,0,n); n.parentNode=this; return n; }
  cloneNode(){ return new N(this.kind, this.dataset.dkey); }
  querySelectorAll(sel){ const a=sel.includes('dkey')?'dkey':'sep'; return this.children.filter(c=>c.dataset[a]!==undefined); }
  querySelector(sel){ return this.querySelectorAll(sel)[0]||null; }
  closest(sel){ const a=sel.includes('dkey')?'dkey':'sep'; return this.dataset[a]!==undefined?this:null; }
  getBoundingClientRect(){ let x=0; for(const c of this.parentNode.children){ const w=c.kind==='tab'?100:20; if(c===this) return {left:x,right:x+w,width:w,top:0,height:20}; x+=w; } }
}
function makeBar(keys){ const bar=new N('bar'); keys.forEach((k,i)=>{ if(i) bar.insertBefore(new N('sep'),null); bar.insertBefore(new N('tab',k),null); }); return bar; }
const order=bar=>bar.querySelectorAll('[data-dkey]').map(t=>t.dataset.dkey).join('');
const seps=bar=>bar.querySelectorAll('[data-sep]').length;
const tabOf=(bar,k)=>bar.querySelectorAll('[data-dkey]').find(t=>t.dataset.dkey===k);
const mkEv=(x,t)=>({clientX:x,clientY:10,target:t,preventDefault(){},dataTransfer:{}});
global.setTimeout=f=>f();
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestLiveReorder(unittest.TestCase):
    def run_js(self, body):
        src = FAKE_DOM + _fn("wireTabBarDnD") + "\n" + body
        r = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_tabs_move_while_the_mouse_is_still_down_and_commit_on_drop(self):
        out = self.run_js("""
        const bar=makeBar(['a','b','c','d']); let committed=null, reverted=0;
        wireTabBarDnD(bar, ks=>committed=ks, ()=>reverted++);
        const a=tabOf(bar,'a'); bar.ondragstart(mkEv(50,a));
        const log=[];
        bar.ondragover(mkEv(120,a));  log.push(order(bar));   // a is at x 0-100; pointer in the gap, not past b's midpoint (190)
        bar.ondragover(mkEv(200,a));  log.push(order(bar));   // past b's midpoint -> a moves after b, live
        bar.ondragover(mkEv(330,a));  log.push(order(bar));   // past c's midpoint -> after c
        bar.ondragover(mkEv(900,a));  log.push(order(bar));   // far right of the bar -> last slot
        bar.ondrop(mkEv(900,a)); bar.ondragend({});
        console.log(JSON.stringify([log, committed, reverted, seps(bar)]));""")
        self.assertEqual(out[0], ["abcd", "bacd", "bcad", "bcda"])
        self.assertEqual(out[1], ["b", "c", "d", "a"])
        self.assertEqual(out[2], 0)
        self.assertEqual(out[3], 3)                 # still exactly one separator between each pair

    def test_moving_left_works_and_does_not_jitter_around_the_midpoint(self):
        out = self.run_js("""
        const bar=makeBar(['a','b','c','d']); let committed=null;
        wireTabBarDnD(bar, ks=>committed=ks, ()=>{});
        const d=tabOf(bar,'d'); bar.ondragstart(mkEv(500,d));
        const log=[];
        bar.ondragover(mkEv(270,d)); log.push(order(bar));    // c is 240-340 (mid 290): pointer left of it -> d before c
        bar.ondragover(mkEv(270,d)); log.push(order(bar));    // same position again: no flip-flop
        bar.ondragover(mkEv(5,d));   log.push(order(bar));    // far left -> first slot
        bar.ondrop(mkEv(5,d)); bar.ondragend({});
        console.log(JSON.stringify([log, committed]));""")
        self.assertEqual(out[0], ["abdc", "abdc", "dabc"])
        self.assertEqual(out[1], ["d", "a", "b", "c"])

    def test_cancelled_drag_reverts_and_unmoved_drop_saves_nothing(self):
        out = self.run_js("""
        let bar=makeBar(['a','b','c']), committed=null, reverted=0;
        wireTabBarDnD(bar, ks=>committed=ks, ()=>reverted++);
        let a=tabOf(bar,'a'); bar.ondragstart(mkEv(50,a)); bar.ondragover(mkEv(300,a)); bar.ondragend({});   // no drop event
        const r1=[committed, reverted];
        bar=makeBar(['a','b','c']); committed=null; reverted=0; wireTabBarDnD(bar, ks=>committed=ks, ()=>reverted++);
        a=tabOf(bar,'a'); bar.ondragstart(mkEv(50,a)); bar.ondragover(mkEv(60,a)); bar.ondrop(mkEv(60,a)); bar.ondragend({});
        console.log(JSON.stringify([r1,[committed,reverted]]));""")
        self.assertEqual(out[0], [None, 1])
        self.assertEqual(out[1], [None, 0])

    def test_dragged_tab_is_dimmed_and_restored(self):
        out = self.run_js("""
        const bar=makeBar(['a','b']); wireTabBarDnD(bar, ()=>{}, ()=>{});
        const a=tabOf(bar,'a'); bar.ondragstart(mkEv(50,a)); const during=a.style.opacity; bar.ondragend({});
        console.log(JSON.stringify([during, a.style.opacity]));""")
        self.assertEqual(out, ["0.35", ""])

    def test_both_bars_use_it_and_the_old_per_tab_handlers_are_gone(self):
        self.assertEqual(HTML.count("wireTabBarDnD("), 3)                 # definition + sub-tab bar + top bar
        self.assertNotIn("reorderKeys", HTML)
        self.assertNotIn("t.ondrop=", HTML)
        self.assertEqual(HTML.count("sep.dataset.sep='1'"), 2)            # both bars mark their separators


if __name__ == "__main__":
    unittest.main()
