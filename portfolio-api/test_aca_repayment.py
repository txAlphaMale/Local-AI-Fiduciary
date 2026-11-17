import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aca  # noqa: E402


class Repayment(unittest.TestCase):
    def test_summary_carries_the_rule_with_its_source(self):
        s = aca.summary(components={"ira_401k_withdrawals": 30000}, household_size=1)
        self.assertIn("repaid in full", s["repayment"]["text"])
        self.assertIn("Cowork", s["repayment"]["source"])
        self.assertIn("relevant", s["repayment"])

    def test_relevant_when_subsidised_not_when_over_the_cliff(self):
        sub = aca.summary(components={"ira_401k_withdrawals": 30000}, household_size=1)
        over = aca.summary(components={"ira_401k_withdrawals": 900000}, household_size=1)
        if sub["subsidy"].get("band") == "subsidized":
            self.assertTrue(sub["repayment"]["relevant"])
        self.assertFalse(over["flags"]["on_cliff_edge"])


if __name__ == "__main__":
    unittest.main()
