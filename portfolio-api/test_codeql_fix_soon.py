# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the CodeQL 'fix soon' batch: SRI on CDN assets, bounded name cleaning (ReDoS), no raw exception text
in server-failure replies, and ACME host-name validation."""
import os
import re
import time
import unittest

import traceback
import unittest.mock
import uuid

import tls   # does not touch /data at import time (unlike app.py, whose import binds data paths from the environment)

_HERE = os.path.dirname(__file__)
HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()
APP_SRC = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()


def _load(*names):
    """Pull specific top-level definitions out of app.py without importing the whole app (its import has side effects)."""
    ns = {"re": re, "uuid": uuid, "traceback": traceback}
    for n in names:
        m = re.search(r"^(def %s\(.*?)(?=^\S)" % n, APP_SRC, re.M | re.S) if n.startswith("_") and n[1].islower() and not n.endswith("_RE") \
            else re.search(r"^(%s = .*?)\n(?=\S)" % n, APP_SRC, re.M)
        exec(m.group(1), ns)
    return ns


class TestSri(unittest.TestCase):
    def test_every_cdn_asset_is_pinned_and_hashed(self):
        tags = re.findall(r'<(?:script|link)\b[^>]*(?:src|href)="(https://(?:cdn\.jsdelivr\.net|cdnjs\.cloudflare\.com)[^"]+)"[^>]*>', HTML)
        full = re.findall(r'<(?:script|link)\b[^>]*(?:src|href)="https://(?:cdn\.jsdelivr\.net|cdnjs\.cloudflare\.com)[^"]+"[^>]*>', HTML)
        self.assertGreaterEqual(len(tags), 4)
        for url, tag in zip(tags, full):
            self.assertRegex(tag, r'integrity="sha384-[A-Za-z0-9+/=]{64}"', url)
            self.assertIn('crossorigin="anonymous"', tag, url)
            self.assertNotRegex(url, r"@\d+/", "floating major tag: " + url)


class TestBoundedNames(unittest.TestCase):
    def test_clean_name_is_fast_on_hostile_input(self):
        t = time.time()
        out = _load('_NAME_JUNK_RE', '_NAME_STATE_RE', '_clean_name')['_clean_name'](" " * 200000 + "x")
        self.assertLess(time.time() - t, 1.0)
        self.assertIsInstance(out, str)

    def test_clean_name_still_works(self):
        f = _load("_NAME_JUNK_RE", "_NAME_STATE_RE", "_clean_name")["_clean_name"]
        self.assertEqual(f("Acme Corp Isin CA92X Sedol BL9"), "Acme Corp")
        self.assertEqual(f("Foo Inc /DE/"), "Foo Inc")


class TestCleanNameEquivalence(unittest.TestCase):
    """The linear rewrite must give the same answers as the original regex version on single-line names. (Only difference: a
    name containing a line break after the label, where the old '.*$' stopped matching; the new code cuts there, which is the
    intended behaviour and cannot occur in a real security name.)"""
    @staticmethod
    def _old(n):
        n = re.sub(r"\s+(isin|sedol|cusip|figi)\b.*$", "", (n or "").strip()[:200], flags=re.I)
        return re.sub(r"\s*/[A-Za-z]{1,5}/?$", "", n).strip()

    def test_same_results_on_many_inputs(self):
        import random
        f = _load("_NAME_JUNK_RE", "_NAME_STATE_RE", "_clean_name")["_clean_name"]
        rnd = random.Random(7)
        words = ["Acme", "Corp", "Inc", "Isin", "ISIN", "Sedol", "Cusip", "Figi", "CA92X", "/DE", "/NEW/", "/", "  ", "\t", "x", "isinx", ""]
        for _ in range(4000):
            s = "".join(rnd.choice(words) + rnd.choice(["", " ", "  ", "\t"]) for _ in range(rnd.randint(0, 7)))
            self.assertEqual(f(s), self._old(s), repr(s))
        for s in (None, "", "Isin only", "Foo /DE/", "Foo Inc Isin US1 Sedol B", "A  /NEW/  "):
            self.assertEqual(f(s), self._old(s), repr(s))


class TestErrRef(unittest.TestCase):
    def test_no_raw_exception_text_returned(self):
        msg = _load('_errref')['_errref'](RuntimeError("secret path /data/x.db and token=abc"))
        self.assertNotIn("secret", msg)
        self.assertNotIn("/data", msg)
        self.assertRegex(msg, r"^internal error \(ref [0-9a-f]{6}\)$")

    def test_errmsg_keeps_deliberate_messages_and_masks_the_rest(self):
        ns = _load("_errref", "_errmsg")
        self.assertEqual(ns["_errmsg"](RuntimeError("Monarch is not enabled")), "Monarch is not enabled")
        self.assertEqual(ns["_errmsg"](ValueError("bad input")), "bad input")
        self.assertRegex(ns["_errmsg"](OSError("connection to /var/x failed")), r"^internal error \(ref [0-9a-f]{6}\)$")

    def test_server_failure_sites_use_it(self):
        # the migrated handlers must not interpolate the raw exception any more
        for needle in ('"error": f"tax audit failed: {_errref(e)}"', '"error": f"tax engine failed: {_errref(e)}"',
                       '"error": f"research failed: {_errref(e)}"', '"error": f"reindex failed: {_errref(e)}"'):
            self.assertIn(needle, APP_SRC)
        self.assertNotIn('f"tax audit failed: {e}"', APP_SRC)


class TestAcmeHostname(unittest.TestCase):
    def test_valid_and_invalid(self):
        for ok in ("example.com", "a.b.example.co.uk", "*.example.com", "localhost"):
            self.assertTrue(tls._HOSTNAME_RE.match(ok), ok)
        for bad in ("../../etc/x", "a/b.com", "-bad.com", "a..b.com", "ex ample.com", "x" * 64 + ".com", ""):
            self.assertFalse(tls._HOSTNAME_RE.match(bad), bad)

    def test_enroll_rejects_path_like_domains_before_touching_disk(self):
        with unittest.mock.patch.object(tls, "acme_available", return_value=True):
            with self.assertRaises(ValueError):
                tls.acme_enroll("../../etc/passwd")
            with self.assertRaises(ValueError):
                tls.acme_enroll("ok.example.com", extra_domains=["a/b"])


if __name__ == "__main__":
    unittest.main()
