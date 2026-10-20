# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_attention_links.py -- ATTENTION items link to real, correctly-named places (tester feedback 2026-10-20:
'insights' isn't a tab the user can see, and the destination wasn't clickable)."""
import ast, glob, os, re, sys, tempfile, unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="attn-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod  # noqa: E402

HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()
SRC = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
D = appmod._ALERT_DESTINATIONS


def subtab_keys(tab):
    """The sub-tab keys the UI really defines for a top-level tab (from its panelSubtabs('<tab>', [...]) block)."""
    i = HTML.index(f"panelSubtabs('{tab}',[")
    j = HTML.index("]));", i)
    return set(re.findall(r"\{key:'([a-z0-9_]+)'", HTML[i:j]))


class TestDestinations(unittest.TestCase):
    def test_every_destination_is_a_real_tab_and_subtab(self):
        for key, d in D.items():
            self.assertIn(d["subtab"], subtab_keys(d["tab"]), key)

    def test_labels_use_names_a_user_can_see_on_screen(self):
        tab_label = {"insights": "RISK · EXPOSURE", "research": "RESEARCH · IDEAS", "audits": "AUDITS · TAX·FEES", "plan": "PLAN · FI"}
        for key, d in D.items():
            self.assertNotIn("INSIGHTS", d["label"], key)
            if d["tab"] in tab_label:
                self.assertTrue(d["label"].startswith(tab_label[d["tab"]] + " → "), d["label"])
                self.assertIn(f"'{tab_label[d['tab']]}'", HTML)                  # that top-level label really exists in the UI
        self.assertTrue(D["settings.accounts"]["label"].startswith("⚙ SETTINGS"))

    def test_every_add_call_in_the_digest_uses_a_known_destination(self):
        fn = next(n for n in ast.walk(ast.parse(SRC)) if isinstance(n, ast.FunctionDef) and n.name == "_alerts_digest")
        used = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "add":
                self.assertIsInstance(n.args[3], ast.Constant, ast.get_source_segment(SRC, n))
                used.add(n.args[3].value)
        self.assertGreaterEqual(len(used), 10)
        self.assertEqual(used - set(D), set())                                    # nothing falls back to raw text
        self.assertEqual(set(D) - used, set())                                    # no dead entries

    def test_digest_items_carry_tab_subtab_and_a_readable_label(self):
        with unittest.mock.patch.object(appmod, "_read_exp", lambda: {"concentration": {"flags": ["Technology 47% (> 35%)"]}}):
            items = [a for a in appmod._alerts_digest()["alerts"] if a["category"] == "Concentration"]
        self.assertTrue(items)
        a = items[0]
        self.assertEqual((a["tab"], a["subtab"], a["where"]), ("insights", "exposure", "RISK · EXPOSURE → EXPOSURE"))


class TestUi(unittest.TestCase):
    def test_attention_destination_is_a_link_using_the_cockpit_jump(self):
        i = HTML.index("The destination is a real link")
        block = HTML[i:i + 700]
        for needle in ("a.tab", "a.subtab", "setSubtab(a.tab,a.subtab)", "lnk.onclick"):
            self.assertIn(needle, block)

    def test_no_user_visible_text_still_names_the_retired_insights_tab(self):
        for m in re.finditer(r"INSIGHTS\s*→", HTML):
            self.fail("stale INSIGHTS → in index.html: " + HTML[max(0, m.start() - 40):m.end() + 30])
        for f in ["../NEW-USER-SETUP-GUIDE.md"] + glob.glob(os.path.join(_HERE, "..", "knowledge", "**", "*.md"), recursive=True):
            p = os.path.join(_HERE, f) if not os.path.isabs(f) else f
            self.assertNotRegex(open(p, encoding="utf-8").read(), r"INSIGHTS\s*→", p)


class TestCockpitChips(unittest.TestCase):
    """Tester feedback 2026-10-20: ASSET LOC chip landed on AUDITS -> ACCOUNTS (a DRIP checkbox editor) because
    the chip named a tab but no sub-tab. Every chip must name a real sub-tab of its tab."""
    def test_every_chip_names_a_real_subtab(self):
        i = HTML.index("function cockpitPanel")
        block = HTML[i:i + 20000]
        chips = re.findall(r"A\.push\(\{[^\n]*?tab:'([a-z]+)'[^\n]*", block)
        self.assertGreaterEqual(len(chips), 14)
        for line in re.findall(r"A\.push\(\{[^\n]*", block):
            tab = re.search(r"tab:'([a-z]+)'", line)
            if not tab or tab.group(1) not in ("insights", "audits", "research", "plan"):
                continue
            sub = re.search(r"subtab:'([a-z0-9_]+)'", line)
            self.assertTrue(sub, "chip without subtab: " + line[:90])
            self.assertIn(sub.group(1), subtab_keys(tab.group(1)), line[:90])

    def test_asset_location_and_tax_loss_chips_open_tax_tlh(self):
        for cat in ("ASSET LOC", "TAX-LOSS"):
            line = next(l for l in HTML.splitlines() if f"cat:'{cat}'" in l)
            self.assertIn("tab:'audits',subtab:'tax'", line)


import unittest.mock  # noqa: E402,F401

if __name__ == "__main__":
    unittest.main()
