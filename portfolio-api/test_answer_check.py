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


class V3Tests(unittest.TestCase):
    def test_bad_conviction_scale_removed(self):
        out, rem = ac.strip_ungrounded("It has a low conviction score of 3/10. Runway is long.", "")
        self.assertEqual(len(rem), 1)
        self.assertIn("Runway is long", out)
        out2, rem2 = ac.strip_ungrounded("Conviction is 3/5. Bought 3/4/2024.", "")
        self.assertEqual(rem2, [])

    def test_limit_sentences(self):
        txt = " ".join(f"Sentence {i}." for i in range(10))
        out, cut = ac.limit_sentences(txt, 6)
        self.assertTrue(cut)
        self.assertEqual(out.count("Sentence"), 6)
        self.assertEqual(ac.limit_sentences("One. Two.", 6), ("One. Two.", False))

    def test_note_dict_rendered_readable_with_scale(self):
        r = {"ticker": "QXO", "found": True, "screen": {"name": "QXO", "runway_months": 65, "beta": 2.36},
             "scout_note": {"created_at": "2026-11-14", "note": {
                 "thesis": "Roll-up story.", "conviction": 3, "conviction_capped": False, "horizon": "1-3 years",
                 "evidence": [{"claim": "Revenue up", "source": "sec"}], "risks": ["Debt"], "disproof": ["Growth stalls"]}}}
        b = ac.facts_block([r])
        self.assertNotIn("{'", b)
        self.assertIn("Conviction: 3 of 5", b)
        self.assertIn("Evidence (sec): Revenue up", b)
        self.assertIn("What would prove it wrong: Growth stalls", b)
        self.assertIn("cash runway 65 months", b)
        self.assertIn("beta 2.36", b)

    def test_rule_names_scale(self):
        self.assertIn("1 to 5", ac.COMMENTARY_RULE)


class ComposeTests(unittest.TestCase):
    def test_facts_first_commentary_labelled(self):
        out = ac.compose("It fits.", 2, "FACTS")
        self.assertTrue(out.startswith("FACTS"))
        self.assertIn("Model commentary", out)
        self.assertIn("Removed 2 sentence", out)
        self.assertLess(out.index("FACTS"), out.index("It fits."))

    def test_no_facts_returns_commentary_unchanged(self):
        self.assertEqual(ac.compose("Hi.", 0, ""), "Hi.")
        self.assertIn("Removed 1", ac.compose("Hi.", 1, ""))

    def test_rule_forbids_background(self):
        self.assertIn("no company background", ac.COMMENTARY_RULE)


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
