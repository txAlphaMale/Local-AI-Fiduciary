"""RV-T3 / TASK-RV-043: reading a filed return by rule. Fixtures are PDFs built here with INVENTED figures, laid out like the IRS Form 1040 / 8959;
no real return is used or committed. Only numbers may come out: never a name, an SSN or an address."""
import io, json, os, re, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import taxreturn as tr  # noqa: E402
from pypdf import PdfReader  # noqa: E402

MAPS = tr.load_maps()


# ---- a tiny PDF writer: pages of positioned text (Helvetica), enough for pypdf to read back ----
def make_pdf(pages):
    objs = []
    def add(b):
        objs.append(b); return len(objs)
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    kids, page_objs = [], []
    pages_id = len(objs) + 1 + 2 * len(pages)
    for frags in pages:
        stream = "\n".join(f"BT /F1 8 Tf 1 0 0 1 {x} {y} Tm ({t.replace(chr(92), chr(92) * 2).replace('(', chr(92) + '(').replace(')', chr(92) + ')')}) Tj ET" for (x, y, t) in frags).encode("latin-1")
        c = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        p = add(f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] /Contents {c} 0 R /Resources << /Font << /F1 {font} 0 R >> >> >>".encode())
        kids.append(p)
    add(f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode())
    cat = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())
    out = b"%PDF-1.4\n"; offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out)); out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    x = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + "".join(f"{o:010d} 00000 n \n" for o in offs).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root {cat} 0 R >>\nstartxref\n{x}\n%%EOF\n".encode()
    return out


def money(v):
    return f"{v:,.0f}"


def layout(year, vals, with_8959=None, extra_p1=()):
    """Pages (x, y, text) for a Form 1040 of `year` from invented `vals` (field -> number; a missing field is a blank line), optionally Form 8959."""
    spec = MAPS["years"][str(year)]["forms"]["1040"]["lines"]
    pages = {1: [(40, 760, f"Form 1040 U.S. Individual Income Tax Return {year}"), (40, 745, "Your first name Jason Q Last name Public"), (400, 745, "123-45-6789"),
                 (40, 730, "Home address 1 Main Street Anytown TX 75000"), (400, 30, f"Form 1040 ({year}) Created 1/1/26")] + list(extra_p1),
             2: [(40, 770, f"Form 1040 ({year}) Page 2")]}
    ys = {1: 700, 2: 740}
    paired = {"2a": ("2b", "Tax-exempt interest", "Taxable interest"), "3a": ("3b", "Qualified dividends", "Ordinary dividends"), "4a": ("4b", "IRA distributions", "Taxable amount"),
              "5a": ("5b", "Pensions and annuities", "Taxable amount"), "6a": ("6b", "Social security benefits", "Taxable amount")}
    done = set()
    for ln in spec:
        if ln["line"] in done:
            continue
        pg, y = ln["page"], ys[ln["page"]]
        ys[pg] -= 22
        if ln["line"] in paired:
            b, la, lb = paired[ln["line"]]
            fa, fb = next(l for l in spec if l["line"] == ln["line"]), next(l for l in spec if l["line"] == b)
            row = [(40, y, f"{ln['line']} {la} . . ."), (230, y, ln["line"]), (40, y, ln["line"]), (300, y, f"b {lb} . . ."), (430, y, b)]
            if fa["field"] in vals: row.append((260, y, money(vals[fa["field"]])))
            if fb["field"] in vals: row.append((470, y, money(vals[fb["field"]])))
            pages[pg] += row; done |= {ln["line"], b}
        else:
            row = [(40, y, f"{ln['line']} {ln['label'].capitalize()} . . . . ."), (30, y, ln["line"]), (420, y, ln["line"])]
            if ln["field"] in vals: row.append((470, y, money(vals[ln["field"]])))
            pages[pg] += row
    out = [pages[1], pages[2]]
    if with_8959 is not None:
        p = [(40, 770, f"Form 8959 {year} Additional Medicare Tax"), (40, 740, "18 Add lines 7, 13, and 17. . . . ."), (420, 740, "18")]
        p.append((470, 740, money(with_8959)))
        out.append(p)
    return out


def valid(year):
    v = {"wages": 120000, "tax_exempt_interest": 300, "taxable_interest": 1200, "qualified_dividends": 2000, "ordinary_dividends": 2500, "capital_gain": 8000,
         "total_income": 131700, "adjustments": 5000, "agi": 126700, "deduction": 29200, "total_deductions": 29200, "taxable_income": 97500, "tax": 11000,
         "total_tax": 11000, "withholding": 20000, "total_payments": 20000, "overpaid": 9000, "refund_requested": 9000}
    return v


def read(year, vals=None, **kw):
    pdf = make_pdf(layout(year, vals if vals is not None else valid(year), **kw))
    pages = [tr.page_fragments(p) for p in PdfReader(io.BytesIO(pdf)).pages]
    return tr.extract(pages, year)


def by(res):
    return {f["field"]: f for f in res["fields"]}


class EveryYear(unittest.TestCase):
    def test_each_supported_year_reads_every_invented_value_with_a_page_reference(self):
        self.assertEqual(tr.supported_years(), ["2023", "2024", "2025"])
        for y in (2023, 2024, 2025):
            res = read(y)
            f = by(res)
            self.assertEqual(res["year"], y)
            for fld, want in valid(y).items():
                self.assertEqual(f[fld]["value"], want, (y, fld))
                self.assertEqual(f[fld]["status"], "ok", (y, fld, f[fld]["reason"]))
                self.assertIn(f[fld]["page"], (1, 2))
            self.assertTrue(all(c["ok"] in (True, None) for c in res["checks"]))
            self.assertEqual(sum(1 for c in res["checks"] if c["ok"] is True), 5)         # all five arithmetic checks run and pass

    def test_2025_line_numbers_differ_from_2024(self):
        m25 = {l["field"]: l["line"] for l in MAPS["years"]["2025"]["forms"]["1040"]["lines"]}
        m24 = {l["field"]: l["line"] for l in MAPS["years"]["2024"]["forms"]["1040"]["lines"]}
        self.assertEqual((m25["agi"], m25["capital_gain"], m25["deduction"]), ("11a", "7a", "12e"))
        self.assertEqual((m24["agi"], m24["capital_gain"], m24["deduction"]), ("11", "7", "12"))

    def test_blank_lines_are_zero_and_say_so(self):
        f = by(read(2024))
        self.assertEqual((f["ira_gross"]["value"], f["ira_gross"]["status"]), (0.0, "blank"))
        self.assertEqual(f["amount_owed"]["status"], "blank")


class Checks(unittest.TestCase):
    def test_one_altered_digit_marks_the_related_fields_unreliable(self):
        v = valid(2024); v["agi"] = 127700                                         # 1,000 off: breaks AGI = total income - adjustments
        res = read(2024, v)
        f = by(res)
        for fld in ("total_income", "adjustments", "agi"):
            self.assertEqual(f[fld]["status"], "unreliable", fld)
            self.assertIn("could not be read reliably", f[fld]["reason"])
        self.assertEqual(f["wages"]["status"], "ok")                               # unrelated fields stay fine
        self.assertFalse(next(c for c in res["checks"] if c["name"].startswith("total income")) ["ok"])

    def test_a_dollar_of_rounding_is_tolerated(self):
        v = valid(2025); v["agi"] = 126701
        self.assertEqual(by(read(2025, v))["agi"]["status"], "ok")

    def test_amount_owed_year(self):
        v = valid(2024); v.update(total_payments=8000, overpaid=None, refund_requested=None, amount_owed=3000)
        v = {k: x for k, x in v.items() if x is not None}
        res = read(2024, v)
        self.assertTrue(next(c for c in res["checks"] if c["name"].startswith("amount owed"))["ok"])


class Refusals(unittest.TestCase):
    def pages(self, y, vals=None):
        return [tr.page_fragments(p) for p in PdfReader(io.BytesIO(make_pdf(layout(y, vals or valid(y))))).pages]

    def test_a_year_without_a_map_is_refused_with_a_plain_message(self):
        pages = self.pages(2024)
        pages[1] = [(t.replace("2024", "2022"), x, y) for t, x, y in pages[1]]
        pages[0] = [(t.replace("2024", "2022"), x, y) for t, x, y in pages[0]]
        with self.assertRaises(ValueError) as cm:
            tr.extract(pages, 2022)
        self.assertIn("No line map for tax year 2022", str(cm.exception))
        self.assertIn("2023, 2024, 2025", str(cm.exception))

    def test_the_year_the_person_gave_must_match_the_form(self):
        with self.assertRaises(ValueError) as cm:
            tr.extract(self.pages(2024), 2025)
        self.assertIn("printed for 2024", str(cm.exception))

    def test_an_image_only_pdf_has_no_text_to_read(self):
        with self.assertRaises(ValueError) as cm:
            tr.extract([[], []], 2024)
        self.assertIn("scanned", str(cm.exception))

    def test_a_changed_form_revision_is_caught_not_misread(self):
        pages = self.pages(2024)
        pages[0] = [(t.replace("Total amount from", "Something unrelated to"), x, y) for t, x, y in pages[0]]
        res = tr.extract(pages, 2024)
        w = {f["field"]: f for f in res["fields"]}["wages"]
        self.assertEqual(w["status"], "missing")
        self.assertIsNone(w["value"])


class Privacy(unittest.TestCase):
    def test_no_name_no_ssn_no_address_in_the_output_and_no_nine_digit_run(self):
        res = read(2025)
        blob = json.dumps(res)
        for secret in ("Jason", "Public", "123-45-6789", "123456789", "Main Street", "Anytown", "75000"):
            self.assertNotIn(secret, blob)
        self.assertIsNone(re.search(r"(?<![\d.])\d{9}(?![\d.])", blob))
        self.assertIsNone(re.search(r"\b\d{3}-\d{2}-\d{4}\b", blob))

    def test_an_ssn_where_an_amount_belongs_is_never_read(self):
        extra = [(470, 700 - 0, "123456789")]
        v = valid(2024); v.pop("wages")                                            # 1a is blank, so the only number to its right is the SSN-like run
        res = read(2024, v, extra_p1=[(470, 700, "123456789")])
        w = by(res)["wages"]
        self.assertEqual(w["status"], "unreliable")
        self.assertIsNone(w["value"])
        self.assertNotIn("123456789", json.dumps(res))


class Form8959AndStatus(unittest.TestCase):
    def test_form_8959_line_18_is_read_when_present_and_absent_otherwise(self):
        r = read(2025, with_8959=412)
        self.assertEqual(by(r)["additional_medicare_tax"]["value"], 412.0)
        self.assertEqual(by(r)["additional_medicare_tax"]["page"], 3)
        self.assertEqual(by(read(2025))["additional_medicare_tax"]["status"], "absent")

    def test_filing_status_from_the_checked_box_else_none(self):
        pages = [tr.page_fragments(p) for p in PdfReader(io.BytesIO(make_pdf(layout(2025, valid(2025), extra_p1=[(40, 600, "X"), (60, 600, "Married filing jointly (even if only one had income)"), (40, 580, "Single")])))).pages]
        self.assertEqual(tr.extract(pages, 2025)["filing_status"], "Married filing jointly")
        pages2 = [tr.page_fragments(p) for p in PdfReader(io.BytesIO(make_pdf(layout(2025, valid(2025))))).pages]
        self.assertIsNone(tr.extract(pages2, 2025)["filing_status"])


class Card(unittest.TestCase):
    def test_card_figures_and_effective_rate(self):
        s = tr.summary({"agi": 126700, "taxable_income": 97500, "total_tax": 11000, "capital_gain": 8000, "ordinary_dividends": 2500})
        self.assertEqual((s["agi"], s["total_tax"], s["effective_rate_pct"]), (126700, 11000, 8.7))


if __name__ == "__main__":
    unittest.main()
