"""Regression (2026-11-09): an end-of-module `import sys as _sys` rebound the prompt helper `_sys`, so the weekly
State-of-the-Wealth brief and every other caller failed with "'module' object is not callable"."""
import os, re, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()


class TestSysHelper(unittest.TestCase):
    def test_no_module_alias_named__sys(self):
        self.assertIsNone(re.search(r"^\s*import\s+\w+\s+as\s+_sys\s*(#.*)?$", SRC, re.M),
                          "`_sys` is the prompt helper; do not alias a module to it")

    def test_helper_is_the_only_binding(self):
        self.assertEqual(len(re.findall(r"^def _sys\(", SRC, re.M)), 1)
        self.assertIsNone(re.search(r"^_sys\s*=", SRC, re.M))


if __name__ == "__main__":
    unittest.main()
