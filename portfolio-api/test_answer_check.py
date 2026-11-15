import json
import unittest

import answer_check as ac
import store


class StripTests(unittest.TestCase):
    def test_invented_history_removed(self):
        txt = "QXO has very fast growth. QXO (formerly Office Depot) is a post-merger story. Net margin is thin."
        out, removed = ac.strip_ungrounded(txt, '{"ticker":"QXO","rev_growth_pct":11925}')
        self.assertEqual(len(removed), 1)
        self.assertNotIn("Office Depot", out)
        self.assertIn("very fast growth", out)
        self.assertIn("Net margin is thin", out)

    def test_claim_in_evidence_kept(self):
        txt = "The scout note says it completed a merger last year."
        out, removed = ac.strip_ungrounded(txt, "note: completed a merger with X")
        self.assertEqual(removed, [])
        self.assertEqual(out, txt)

    def test_clean_text_untouched(self):
        txt = "Growth is 40%.\n\nMargins are positive."
        out, removed = ac.strip_ungrounded(txt, "")
        self.assertEqual((out, removed), (txt, []))


class FactsTests(unittest.TestCase):
    def test_block_has_numbers_and_note(self):
        r = {"ticker": "ROL", "found": True,
             "screen": {"name": "Rollins", "sector": "Services", "price": 55.1, "rev_growth_pct": 9.2,
                        "net_margin_pct": 14.0, "insider": "net selling"},
             "scout_note": {"created_at": "2026-11-14T01:00", "note": "Steady compounder."}}
        b = ac.facts_block([r, {"ticker": "ZZZ", "found": False}])
        self.assertIn("**ROL**", b)
        self.assertIn("revenue growth 9.2%", b)
        self.assertIn("Steady compounder.", b)
        self.assertNotIn("ZZZ", b)

    def test_empty(self):
        self.assertEqual(ac.facts_block([]), "")
        self.assertEqual(ac.facts_block([{"found": False}]), "")


class ReportSourcesTests(unittest.TestCase):
    def test_note_source_bounded(self):
        import app
        log = []
        for i in range(60):
            app._note_source(log, "get_x", {"i": i}, {"v": "y" * 1000})
        self.assertEqual(len(log), 40)
        self.assertLessEqual(len(log[0]["result"]), 400)


class BasisLabelTests(unittest.TestCase):
    def test_labels(self):
        import app
        for sf, word in (("real_holdings (real basis)", "real cost basis"),
                         ("real_holdings (market basis)", "since seed"), (None, "since start")):
            m = app._paper_marked({"id": None, "name": "n", "cash": 100.0, "starting_cash": 100.0,
                                   "positions": {}, "seeded_from": sf})
            self.assertIn(word, m["basis_label"])


if __name__ == "__main__":
    unittest.main()
