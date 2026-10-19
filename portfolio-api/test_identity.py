# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_identity.py -- the brief must describe the SEC registrant for a ticker, not whatever the
holdings source labelled it (VOYG: Monarch said 'Voyager Digital Ltd ...', SEC says Voyager Technologies)."""
import glob, os, re, sys, tempfile, unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="ident-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod  # noqa: E402

VOYG_MONARCH = "Voyager Digital Ltd Isin Ca92919V1085 Sedol Bl9Y6T0"
SEC = {"available": True, "entity": "Voyager Technologies, Inc./DE"}


class TestIdentity(unittest.TestCase):
    def test_voyg_sec_entity_wins_and_mismatch_is_flagged(self):
        name, note = appmod._company_identity("VOYG", VOYG_MONARCH, SEC)
        self.assertEqual(name, "Voyager Technologies, Inc.")
        self.assertIn("Voyager Digital Ltd", note)
        self.assertIn("DIFFERENT company", note)

    def test_agreeing_names_no_note(self):
        for held in ("Apple Inc", "Apple", "APPLE INC. COMMON STOCK"):
            name, note = appmod._company_identity("AAPL", held, {"available": True, "entity": "Apple Inc."})
            self.assertEqual(name, "Apple Inc.")
            self.assertEqual(note, "", held)

    def test_on_demand_bare_ticker_uses_sec_name(self):
        name, note = appmod._company_identity("VOYG", "VOYG", SEC)
        self.assertEqual((name, note), ("Voyager Technologies, Inc.", ""))

    def test_no_sec_data_falls_back_to_cleaned_holdings_name(self):
        self.assertEqual(appmod._company_identity("VOYG", VOYG_MONARCH, {"available": False}),
                         ("Voyager Digital Ltd", ""))
        self.assertEqual(appmod._company_identity("VTI", "VTI", None), ("VTI", ""))

    def test_prompt_carries_identity(self):
        seen = {}
        orig = appmod._ollama_generate
        appmod._ollama_generate = lambda prompt, *a, **k: (seen.setdefault("p", prompt) and None, "no model in test")
        try:
            appmod._llm("VOYG", VOYG_MONARCH, 1.0, 0.0, {}, "", SEC, {})
        finally:
            appmod._ollama_generate = orig
        self.assertIn("Ticker: VOYG (Voyager Technologies, Inc.)", seen["p"])
        self.assertIn("IDENTITY: VOYG is Voyager Technologies, Inc.", seen["p"])
        self.assertIn("DIFFERENT company", seen["p"])


if __name__ == "__main__":
    unittest.main()
