# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for scripts/security_report.py (the public security-pipeline page generator)."""
import importlib.util
import json
import os
import tempfile
import unittest

_P = os.path.join(os.path.dirname(__file__), "..", "scripts", "security_report.py")
_spec = importlib.util.spec_from_file_location("security_report", _P)
sr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sr)

TRIVY = {"Results": [{"Vulnerabilities": [
    {"VulnerabilityID": "CVE-1", "PkgName": "a", "Severity": "HIGH", "FixedVersion": "2"},
    {"VulnerabilityID": "CVE-1", "PkgName": "a", "Severity": "HIGH"},       # duplicate pair counts once
    {"VulnerabilityID": "CVE-2", "PkgName": "b", "Severity": "CRITICAL"}]}]}


class TestParsers(unittest.TestCase):
    def test_trivy_counts_unique_pairs(self):
        self.assertEqual(sr.trivy_counts(TRIVY), {"CRITICAL": 1, "HIGH": 1})
        self.assertEqual(sr.trivy_counts({}), {"CRITICAL": 0, "HIGH": 0})

    def test_pip_audit_count(self):
        self.assertEqual(sr.pip_audit_count({"dependencies": [{"vulns": [1, 2]}, {"vulns": []}]}), 2)

    def test_trivyignore_and_checkov_skips(self):
        rows = sr.parse_trivyignore("# header\n\n# reason one\nCVE-2025-1 exp:2027-02-04\n")
        self.assertEqual(rows[0][:2], ("CVE-2025-1", "2027-02-04"))
        self.assertIn("reason one", rows[0][2])
        self.assertEqual(sr.parse_checkov_skips("  - CKV_AWS_1    # why\n"), [("CKV_AWS_1", "why")])


class TestRender(unittest.TestCase):
    def test_page_has_counts_not_unfixed_package_names_and_escapes(self):
        with tempfile.TemporaryDirectory() as d:
            def w(n, o):
                p = os.path.join(d, n)
                json.dump(o, open(p, "w"))
                return p
            allp = w("a.json", TRIVY)
            fixp = w("f.json", {"Results": [{"Vulnerabilities": [TRIVY["Results"][0]["Vulnerabilities"][0]]}]})
            log = w("l.json", {"entries": [{"found": "x", "fixed": "y", "severity": "HIGH",
                                             "summary": "<script>alert(1)</script>", "ref": "r"}]})
            out = os.path.join(d, "site")
            sr.main(["--trivy-all", allp, "--trivy-fixable", fixp, "--remediation", log, "--out-dir", out])
            page = open(os.path.join(out, "index.html")).read()
            self.assertNotIn("<script>alert", page)
            self.assertIn("&lt;script&gt;", page)
            self.assertNotIn("CVE-2", page)          # unfixed findings are never named
            summ = json.load(open(os.path.join(out, "summary.json")))
            self.assertEqual(summ["trivy_image"]["fixable"]["HIGH"], 1)
            self.assertEqual(summ["trivy_image"]["unfixed"]["CRITICAL"], 1)


if __name__ == "__main__":
    unittest.main()
