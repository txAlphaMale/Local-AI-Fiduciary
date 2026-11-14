# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_annual_series.py -- edgar._annual after Cowork's live finding (2026-11-13): growth near +300% on MSA / FIS / RDN (a 10-K's
fourth-quarter figure shares the annual figure's end date) and SENEA frozen at fiscal 2018 (the company changed XBRL concept)."""
import glob, os, re, sys, tempfile, unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="annual-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    with open(_f) as _fh:
        for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', _fh.read()):
            os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import edgar  # noqa: E402


def row(start, end, val, filed, fp="FY", form="10-K"):
    r = {"end": end, "val": val, "fp": fp, "form": form, "filed": filed}
    if start:
        r["start"] = start
    return r


def facts(**tags):
    return {"facts": {"us-gaap": {t: {"units": {"USD": rows}} for t, rows in tags.items()}}}


class TestAnnual(unittest.TestCase):
    def test_a_segment_line_is_not_spliced_onto_the_company_total(self):
        """Chemed (2026-11-13 scout note): RevenueFromContract... is one segment and ends 2024; Revenues is the total and reaches 2025.
        Splicing them showed +65%; the series must come from the one line that reaches the newest year."""
        f = facts(RevenueFromContractWithCustomerExcludingAssessedTax=[
                      row("2023-01-01", "2023-12-31", 1315.0, "2024-02-20"), row("2024-01-01", "2024-12-31", 1531.0, "2025-02-20")],
                  Revenues=[row("2023-01-01", "2023-12-31", 2264.0, "2024-02-20"), row("2024-01-01", "2024-12-31", 2431.0, "2025-02-20"),
                            row("2025-01-01", "2025-12-31", 2530.0, "2026-02-20")])
        ser = edgar._annual(f, edgar.TAGS["revenue"])
        self.assertEqual(ser, [("2023-12-31", 2264.0), ("2024-12-31", 2431.0), ("2025-12-31", 2530.0)])
        val, prev, _ = edgar._latest(f, "revenue")
        self.assertAlmostEqual((val / prev - 1) * 100, 4.07, places=1)

    def test_agreeing_older_tag_still_fills_in_history(self):
        f = facts(Revenues=[row("2018-01-01", "2018-12-31", 100.0, "2019-02-01"), row("2019-01-01", "2019-12-31", 110.0, "2020-02-01")],
                  RevenueFromContractWithCustomerExcludingAssessedTax=[row("2019-01-01", "2019-12-31", 110.0, "2021-02-01"),
                                                                       row("2020-01-01", "2020-12-31", 120.0, "2021-02-01")])
        self.assertEqual([k for k, _ in edgar._annual(f, edgar.TAGS["revenue"])], ["2018-12-31", "2019-12-31", "2020-12-31"])

    def test_fourth_quarter_figure_with_the_same_end_date_does_not_replace_the_annual(self):
        f = facts(RevenueFromContractWithCustomerExcludingAssessedTax=[
            row("2025-01-01", "2025-12-31", 1874.0, "2026-02-12"),
            row("2025-10-01", "2025-12-31", 510.0, "2026-02-12"),        # Q4, tagged FY, same end date
            row("2025-07-01", "2025-09-30", 468.0, "2026-02-12"),
            row("2024-01-01", "2024-12-31", 1808.0, "2026-02-12"),
            row("2024-10-01", "2024-12-31", 480.0, "2026-02-12")])
        self.assertEqual(edgar._annual(f, edgar.TAGS["revenue"]), [("2024-12-31", 1808.0), ("2025-12-31", 1874.0)])
        val, prev, end = edgar._latest(f, "revenue")
        self.assertEqual((val, prev, end), (1874.0, 1808.0, "2025-12-31"))

    def test_a_company_that_changed_concept_gets_the_newer_years(self):
        f = facts(Revenues=[row("2017-04-01", "2018-03-31", 1314.0, "2018-06-29")],
                  RevenueFromContractWithCustomerIncludingAssessedTax=[
                      row("2023-04-01", "2024-03-31", 1458.0, "2024-06-13"), row("2024-04-01", "2025-03-31", 1578.0, "2025-06-12")])
        s = edgar._annual(f, edgar.TAGS["revenue"])
        self.assertEqual(s[-1], ("2025-03-31", 1578.0))
        self.assertEqual(edgar._latest(f, "revenue")[2], "2025-03-31")

    def test_earlier_listed_tag_wins_a_conflict_and_latest_filing_wins_within_a_tag(self):
        f = facts(RevenueFromContractWithCustomerExcludingAssessedTax=[row("2024-01-01", "2024-12-31", 100.0, "2025-02-01"),
                                                                       row("2024-01-01", "2024-12-31", 101.0, "2026-02-01")],
                  Revenues=[row("2024-01-01", "2024-12-31", 999.0, "2026-03-01")])
        self.assertEqual(edgar._annual(f, edgar.TAGS["revenue"]), [("2024-12-31", 101.0)])

    def test_balance_sheet_rows_without_a_start_date_still_count(self):
        f = facts(Assets=[row(None, "2024-12-31", 50.0, "2025-02-01"), row(None, "2025-12-31", 60.0, "2026-02-01")])
        self.assertEqual(edgar._latest(f, "total_assets"), (60.0, 50.0, "2025-12-31"))

    def test_quarterly_forms_and_odd_durations_are_ignored(self):
        f = facts(Revenues=[row("2025-01-01", "2025-06-30", 5.0, "2025-08-01", fp="Q2", form="10-Q"),
                            row("2024-01-01", "2025-12-31", 7.0, "2026-02-01"),            # 2 years: not annual
                            row("2025-01-01", "2025-12-31", 9.0, "2026-02-01")])
        self.assertEqual(edgar._annual(f, edgar.TAGS["revenue"]), [("2025-12-31", 9.0)])

    def test_growth_is_sane_for_the_msa_shape(self):
        f = facts(RevenueFromContractWithCustomerExcludingAssessedTax=[row("2025-01-01", "2025-12-31", 1874.0, "2026-02-12"),
                                                                       row("2025-10-01", "2025-12-31", 510.9, "2026-02-12")],
                  Revenues=[row("2024-01-01", "2024-12-31", 1808.0, "2025-02-14"), row("2025-01-01", "2025-12-31", 1874.0, "2026-02-12")])
        val, prev, _ = edgar._latest(f, "revenue")
        self.assertLess(abs((val - prev) / prev * 100), 10)


if __name__ == "__main__":
    unittest.main()
