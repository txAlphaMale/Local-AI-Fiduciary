# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_brand_home_link.py -- the 'PORTFOLIO TERMINAL' title is a live link to the dashboard (tester request 2026-10-21)."""
import json, os, re, shutil, subprocess, unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()


def _handler_src():
    i = HTML.index("(function(){ const a=document.getElementById('brand-home');")
    return HTML[i:HTML.index("})();", i) + 5]


class TestBrandLink(unittest.TestCase):
    def test_title_is_a_real_anchor_keyboard_focusable_and_openable_in_a_new_tab(self):
        self.assertRegex(HTML, r'<div class="brand"><a id="brand-home" href="\./" title="[^"]+">PORTFOLIO TERMINAL</a>')
        self.assertIn(".brand a:focus-visible", HTML)          # visible focus ring for keyboard users
        self.assertIn(".brand a:hover{text-decoration:underline", HTML)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_click_goes_to_the_dashboard_from_any_tab_but_modified_clicks_are_left_to_the_browser(self):
        src = """
        let TAB='settings', renders=0, scrolled=0; const render=()=>{renders++;};
        let handler=null; const a={addEventListener:(t,f)=>{handler=f;}};
        global.document={getElementById:id=>id==='brand-home'?a:null}; global.window={scrollTo:()=>{scrolled++;}};
        """ + _handler_src() + """
        const ev=(o)=>Object.assign({ctrlKey:false,metaKey:false,shiftKey:false,altKey:false,button:0,defaultPrevented:false,preventDefault(){this.defaultPrevented=true;}},o||{});
        const plain=ev(); handler(plain); const after=[TAB, renders, plain.defaultPrevented, scrolled];
        TAB='wiki'; const ctrl=ev({ctrlKey:true}); handler(ctrl); const mid=ev({button:1}); handler(mid);
        console.log(JSON.stringify([after, TAB, ctrl.defaultPrevented, mid.defaultPrevented, renders]));"""
        r = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), [["dashboard", 1, True, 1], "wiki", False, False, 1])


if __name__ == "__main__":
    unittest.main()
