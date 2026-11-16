import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as appmod  # noqa: E402


class Prune(unittest.TestCase):
    def run_prune(self, stored):
        written = {}
        with mock.patch.object(appmod.store, "write_json", lambda path, data: written.update(data=data)):
            removed = appmod._prune_default_settings(stored)
        return sorted(removed), written.get("data")

    def test_values_equal_to_their_default_are_dropped_and_real_changes_kept(self):
        removed, left = self.run_prune({"mc_runs": 3000, "tlh_min_loss": "50", "ef_target_months": 9, "rb_lower_band": 70,
                                        "LLM_API_DIALECT": "ollama", "LLM_BASE_URL": "http://other:11434", "ANTHROPIC_MODEL": "claude-sonnet-5"})
        self.assertEqual(removed, ["ANTHROPIC_MODEL", "LLM_API_DIALECT", "mc_runs", "rb_lower_band", "tlh_min_loss"])
        self.assertEqual(left, {"ef_target_months": 9, "LLM_BASE_URL": "http://other:11434"})

    def test_data_source_choice_is_never_pruned(self):
        removed, left = self.run_prune({"PORTFOLIO_PROVIDER": "csv", "mc_runs": 3000})
        self.assertEqual(removed, ["mc_runs"])
        self.assertEqual(left, {"PORTFOLIO_PROVIDER": "csv"})

    def test_nothing_to_prune_writes_nothing(self):
        removed, left = self.run_prune({"ef_target_months": 9})
        self.assertEqual((removed, left), ([], None))

    def test_garbage_values_are_left_alone(self):
        removed, left = self.run_prune({"mc_runs": "lots"})
        self.assertEqual((removed, left), ([], None))


if __name__ == "__main__":
    unittest.main()
