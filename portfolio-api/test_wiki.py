# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_wiki.py -- the Plaid WIKI entry carries the practical steps and authoritative links, and wiki text
is auto-linked safely (https only, HTML still escaped)."""
import os, re, shutil, subprocess, unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()


def entry(eid):
    i = HTML.index(f"W('{eid}',")
    j = HTML.index("\nW('", i + 5)
    return HTML[i:j]


class TestPlaidWiki(unittest.TestCase):
    def test_entry_has_steps_links_and_troubleshooting(self):
        e = entry("plaid-setup")
        for needle in ("https://dashboard.plaid.com/", "https://plaid.com/docs/quickstart/",
                       "https://plaid.com/docs/account/billing/", "https://plaid.com/docs/sandbox/test-credentials/",
                       "https://plaid.com/docs/link/hosted-link/", "user_good", "SECRETS & KEYS", "Plaid environment",
                       "Portfolio data source", "RECONNECT", "REMOVE", "no transactions", "Check YOUR Dashboard"):
            self.assertIn(needle, e)
        self.assertNotIn("http://", e)                                    # only https links

    def test_panel_links_to_the_entry_and_monarch_entry_mentions_ack(self):
        self.assertIn("wikiLink('plaid-setup')", HTML)
        self.assertIn("risk acknowledgment", entry("monarch-setup"))


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestAutolink(unittest.TestCase):
    def test_linkify_is_https_only_and_escapes_html(self):
        m = re.search(r"const nl=s=>(esc\(s\).*);[ \t]*\r?\n", HTML)
        self.assertIsNotNone(m)
        code = ("const esc=s=>(s==null?'':s).toString().replace(/&/g,'&amp;').replace(/</g,'&lt;');"
                f"const nl=s=>{m.group(1)};"
                "console.log(nl('a https://plaid.com/docs/x/, <b>x</b> http://evil.test javascript:alert(1)'));")
        out = subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout
        self.assertIn('<a href="https://plaid.com/docs/x/" target="_blank" rel="noopener noreferrer">', out)
        self.assertIn("&lt;b>", out)
        self.assertNotIn('<a href="http://evil', out)
        self.assertNotIn('<a href="javascript', out)


class TestTour(unittest.TestCase):
    def test_data_source_step_covers_all_four_paths_and_points_to_the_wiki(self):
        i = HTML.index("title:'Where your data comes from'")
        step = HTML[i:i + 1400]
        for needle in ("Plaid", "SnapTrade", "Monarch", "HOLDINGS tab", "never transactions", "WIKI entry"):
            self.assertIn(needle, step)

    def test_integrations_step_is_admin_only_general_and_targets_a_real_element(self):
        i = HTML.index("Integrations overview (2026-10-20)")
        block = HTML[i:i + 1700]
        self.assertIn("role==='admin'", block)
        self.assertIn("#integrations-panel", block)
        for needle in ("Plaid", "Monarch", "SnapTrade", "NOTIFICATIONS", "AI / LLM PROVIDER", "WIKI entry"):
            self.assertIn(needle, block)
        self.assertIn("id='integrations-panel'", HTML)            # the target exists in accountRegistryContent()

    def test_a_missing_tour_target_falls_back_to_a_centered_popover(self):
        self.assertIn("!document.querySelector(step.element)) delete step.element", HTML)


if __name__ == "__main__":
    unittest.main()
