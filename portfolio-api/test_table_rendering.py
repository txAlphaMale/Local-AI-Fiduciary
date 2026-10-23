# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_table_rendering.py -- markdown tables render the same everywhere (tester screenshot 2026-10-23: a chat table spilled past
the frame). Root cause: the global `td{white-space:nowrap}` (right for numeric data tables) leaked into prose tables; two
containers each got a private partial fix and everything else kept overflowing. Now ONE rule set follows the table itself."""
import json, os, re, shutil, subprocess, unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()


def css():
    return HTML[HTML.index("<style"):HTML.index("</style>")]


class TestCss(unittest.TestCase):
    def test_one_shared_ruleset_for_markdown_tables(self):
        c = css()
        self.assertRegex(c, r"\.md-table-wrap\{[^}]*overflow-x:auto")                       # too many columns scroll inside the box
        cell = re.search(r"\.md-table-wrap th,\.md-table-wrap td\{([^}]*)\}", c, re.S).group(1)
        for needle in ("white-space:normal", "overflow-wrap:anywhere", "text-align:left"):
            self.assertIn(needle, cell)                                                     # the exact property the chat-only fix forgot

    def test_no_container_specific_markdown_table_rules_remain(self):
        c = css()
        for old in (".msg.asst .msg-body table", "#posture-doc-viewer table", "#posture-doc-viewer td", "#posture-doc-viewer th"):
            self.assertNotIn(old, c)

    def test_global_data_table_defaults_are_unchanged(self):
        c = css()
        self.assertIn("white-space:nowrap;}\n  td.sym", c)                                  # numeric data tables keep their nowrap/right-align

    def test_tab_content_scrolls_sideways_as_a_safety_net(self):
        self.assertIn("if(TAB!=='chat') tc.style.cssText='max-width:100%;overflow-x:auto;';", HTML)


class TestRenderer(unittest.TestCase):
    def test_marked_is_only_called_through_mdToHtml(self):
        calls = [m.start() for m in re.finditer(r"marked\.parse\(", HTML)]
        start = HTML.index("function mdToHtml(")
        end = HTML.index("function chatScroll")
        self.assertTrue(calls and all(start < c < end for c in calls), "every markdown render must go through mdToHtml so tables get the wrapper")

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_mdToHtml_wraps_tables_and_degrades_to_escaped_text_without_marked(self):
        i = HTML.index("function mdToHtml(")
        j = HTML.index("function chatScroll")
        src = ("let _mdRenderer=null;\n" + HTML[i:j] + """
        global.window={};
        const plain=mdToHtml('a <b> & c\\nline2');
        function Renderer(){}
        global.marked={Renderer, parse:(t,o)=>o.renderer.table('<tr><th>H</th></tr>','<tr><td>B</td></tr>')+'|'+JSON.stringify(Object.keys(o))};
        global.window={marked:global.marked};
        const out=mdToHtml('x'), out2=mdToHtml('y');
        const empty=(()=>{ global.marked.parse=(t,o)=>o.renderer.table('<tr><th>H</th></tr>',''); return mdToHtml('z'); })();
        console.log(JSON.stringify([plain,out,out===out2,empty]));""")
        r = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        plain, out, same, empty = json.loads(r.stdout)
        self.assertEqual(plain, "a &lt;b> &amp; c<br>line2")
        self.assertTrue(out.startswith('<div class="md-table-wrap"><table><thead><tr><th>H</th></tr></thead><tbody><tr><td>B</td></tr></tbody></table></div>'))
        self.assertIn('["breaks","renderer"]', out); self.assertTrue(same)
        self.assertNotIn("<tbody>", empty)


if __name__ == "__main__":
    unittest.main()
