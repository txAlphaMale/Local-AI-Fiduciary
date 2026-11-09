"""Regression: broad `except Exception` handlers must not put raw exception text into an API reply.
Every logged-in user (viewers included) can call the GETs, and raw text can carry paths, library internals or upstream
bodies. Use _errref (log + reference) or _errmsg (keeps only the message of exceptions we raise on purpose)."""
import os
import re
import unittest

SRC = open(os.path.join(os.path.dirname(__file__), "app.py"), encoding="utf-8").read().split("\n")
RESPONSE_HINT = re.compile(r'JSONResponse\(|"error"\s*:|_sse\(|"note"\s*:|"reason"\s*:')


class TestNoRawExceptionText(unittest.TestCase):
    def test_broad_handlers_do_not_interpolate_the_exception(self):
        bad = []
        in_broad = False
        for i, line in enumerate(SRC, 1):
            m = re.match(r"(\s*)except\b(.*)", line)
            if m:
                in_broad = "Exception as e" in m.group(2)
                continue
            if in_broad and RESPONSE_HINT.search(line) and re.search(r"\{e\}|str\(e\)", line) \
                    and "audit.emit" not in line and "detail=" not in line and "print(" not in line:
                bad.append(f"app.py:{i}: {line.strip()[:100]}")
        self.assertEqual(bad, [], "raw exception text in a response:\n" + "\n".join(bad))


if __name__ == "__main__":
    unittest.main()
