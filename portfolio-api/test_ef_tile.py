import os
import unittest

HTML = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html"), encoding="utf-8").read()


class EmergencyFundTile(unittest.TestCase):
    def test_undesignated_tile_opens_the_account_chooser(self):
        self.assertIn("openEf:!ef.designated", HTML)                       # only when no accounts are designated yet
        self.assertIn("if(a.openEf) CD_EF_EDIT_OPEN=true;", HTML)           # the click opens the in-page editor before navigating
        self.assertIn("accountRegistryTable({fields:['ef','ef_cap']", HTML)  # ... which is the shared account chooser


if __name__ == "__main__":
    unittest.main()
