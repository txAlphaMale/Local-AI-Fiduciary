"""RV-013: figures in model commentary must be in the evidence; thresholds and hypotheticals are allowed."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import answer_check as ac  # noqa: E402
import numcheck  # noqa: E402

RESULT = {"found": True, "ticker": "QXO", "screen": {"name": "QXO Inc", "price": 12.6, "market_cap": 8_650_000_000, "rev_growth_pct": 11925.3,
                                                      "net_margin_pct": -4.1, "fcf_margin_pct": -0.8, "runway_months": 64.8, "beta": 1.41,
                                                      "share_change_pct": 200.0, "revenue": 560_000_000}}
EVID = json.dumps(RESULT) + "\nTell me about QXO"


class Figures(unittest.TestCase):
    def test_kinds_and_scaling(self):
        f = {x["raw"]: x for x in numcheck.figures("Up 12.5%, a $1.2B market value, $3 million cash, 3x sales, 12M shares, in 2026 with 5 insiders.")}
        self.assertEqual(f["12.5%"]["value"], 12.5)
        self.assertEqual(f["$1.2B"]["value"], 1.2e9)
        self.assertEqual(f["$3 million"]["value"], 3e6)
        self.assertEqual(f["3x"]["kind"], "mult")
        self.assertEqual(f["12M"]["value"], 12e6)
        self.assertEqual(len(f), 5)                       # the year and the count of insiders are not checked

    def test_rounding_and_units_match(self):
        for text in ("Revenue growth of 11,925% is a base effect.", "Net margin is -4.1%.", "It trades at $12.60.", "Market value is about $8.7B.",
                     "Market value is $8.65 billion.", "Runway is 65 months, 64.8 to be exact", "A 4.1% loss on sales.", "Beta of 1.41x."):
            self.assertEqual(numcheck.unsupported(text, EVID), [], text)

    def test_invented_figures_are_found(self):
        self.assertEqual(numcheck.unsupported("The company has $2.1B of backlog and 38% gross margins.", EVID), ["$2.1B", "38%"])
        self.assertEqual(numcheck.unsupported("Market value is $9.9B.", EVID), ["$9.9B"])

    def test_plain_numbers_are_ignored(self):
        self.assertEqual(numcheck.unsupported("It reported in 2026 and has 7 offices and 3 of 5 conviction.", EVID), [])


class Commentary(unittest.TestCase):
    def test_sentences_with_unsupported_figures_are_removed_and_counted(self):
        text = ("Growth is a base effect, not organic. The firm won $2.1B of new contracts. Net margin is -4.1%, so profits are not there yet. "
                "Check cash runway of 65 months against spending.")
        clean, removed = ac.strip_unsupported_numbers(text, EVID)
        self.assertEqual(len(removed), 1)
        self.assertIn("$2.1B", removed[0])
        self.assertIn("Net margin is -4.1%", clean)
        self.assertNotIn("2.1B", clean)

    def test_thresholds_and_hypotheticals_keep_their_own_figures(self):
        text = "If revenue growth falls below 25% the thesis weakens. A raise of at least $200M would be a warning."
        clean, removed = ac.strip_unsupported_numbers(text, EVID)
        self.assertEqual((removed, clean), ([], text))

    def test_figures_from_the_users_question_are_allowed(self):
        clean, removed = ac.strip_unsupported_numbers("You asked about a 15% cap; the position is far below it.", EVID + " I hold a 15% cap")
        self.assertEqual(removed, [])

    def test_untouched_text_is_returned_exactly(self):
        text = "Net margin is -4.1%.\nSecond line, price $12.60."
        self.assertEqual(ac.strip_unsupported_numbers(text, EVID), (text, []))

    def test_compose_says_how_many_sentences_had_figures_removed(self):
        out = ac.compose("Kept.", 0, "FACTS", removed_numbers=2)
        self.assertIn("Removed 2 sentence(s) containing figures", out)
        self.assertTrue(out.startswith("FACTS"))
        self.assertIn("containing figures", ac.compose("Kept.", 0, "", 1))                  # a sleeve-list answer has no facts block


if __name__ == "__main__":
    unittest.main()


class ChatLoop(unittest.TestCase):
    """End to end through _chat_stream with a fake model: the number check applies to ticker answers AND sleeve-list answers."""

    def run_chat(self, tool_args, research_result, answer):
        from unittest import mock
        import app as appmod
        n = {"i": 0}

        def fake(messages, model, **k):
            n["i"] += 1
            if n["i"] == 1:
                yield {"content": None, "thinking": None, "done": True, "error": None, "cancelled": False,
                       "tool_calls": [{"function": {"name": "get_idea_research", "arguments": tool_args}}]}
            else:
                yield {"content": answer, "thinking": None, "tool_calls": None, "done": True, "error": None, "cancelled": False}
        frames = []
        with mock.patch.object(appmod.llm, "_llm_chat", side_effect=fake), mock.patch.object(appmod, "_chat_summarise_old"), \
                mock.patch.object(appmod, "_idea_research", return_value=research_result):
            for raw in appmod._chat_stream("fiduciary", None, "question", req_id="nc" + str(id(answer))):
                frames.append(raw)
        return "".join(frames)

    def test_ticker_answer_loses_the_sentence_with_an_invented_figure(self):
        out = self.run_chat({"ticker": "QXO"}, RESULT, "Net margin is -4.1%. The firm has $2.1B of backlog.")
        self.assertIn("Net margin is -4.1%", out)
        self.assertIn("Removed 1 sentence(s) containing figures", out)
        self.assertNotIn("2.1B of backlog", out.split("replace")[-1])
        self.assertIn('"numbers_removed": 1', out)

    def test_sleeve_list_answer_is_checked_too(self):
        res = {"sleeve": "growth", "ideas": [{"ticker": "AAA", "rev_growth_pct": 61.0}, {"ticker": "BBB", "rev_growth_pct": 44.0}]}
        out = self.run_chat({"sleeve": "growth"}, res, "AAA grew 61%. BBB grew 77% and has $5B of cash.")
        self.assertIn("AAA grew 61%", out)
        self.assertIn("Removed 1 sentence(s) containing figures", out)
        self.assertNotIn("BBB grew 77%", out.split("replace")[-1])


if __name__ == "__main__":
    unittest.main()
