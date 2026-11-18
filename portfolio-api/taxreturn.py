# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""taxreturn.py -- RV-043 / TASK-RV-043: read a filed federal return (Form 1040 and Form 8959) from the text layer of a tax-software PDF.

Rules, not a model: the amounts are found by their printed line numbers and labels, using a per-year map (taxreturn_maps.json) that was checked
against the blank IRS form for that year. A year with no map is refused. Only NUMBERS leave this module: no names, no SSN, no addresses are ever
read into the result, and a 9-digit run (an SSN) is never accepted as an amount. Arithmetic checks mark a field 'could not be read reliably' when
the figures do not add up. Nothing here is stored or used by an engine until a person confirms it (app.py).

Pure functions over 'pages' = [[(text, x, y), ...], ...], the positioned text fragments of each page (page_fragments() makes them with pypdf).
"""
import json
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
ROW_TOL = 3.0                                   # fragments within this many points vertically are on one row
NEAR_TOL = 8.0                                  # an amount whose baseline sits a little off its line marker is still taken if nothing is on the row
# An amount as tax software prints it: 1,234  (1,234)  -1,234  1,234.  (Intuit ends whole-dollar amounts with a period)  1,234.56
_NUM = re.compile(r"^\(?-?\$?\d[\d,]*(?:\.\d{0,2})?\)?-?$")
_MARK = re.compile(r"^\d{1,2}[a-z]?$")
_FOOT = re.compile(r"Form\s+1040\s*\((20\d\d)\)(?:\s+Page\s+(\d))?", re.I)
STATUS_LABELS = (("single", "Single"), ("married filing jointly", "Married filing jointly"), ("married filing separately", "Married filing separately"),
                 ("head of household", "Head of household"), ("qualifying surviving spouse", "Qualifying surviving spouse"))


def load_maps(path=None):
    with open(path or os.path.join(_HERE, "taxreturn_maps.json"), encoding="utf-8") as f:
        return json.load(f)


def supported_years(maps=None):
    return sorted((maps or load_maps())["years"])


def _widget_fragments(page):
    """Entered values kept as form fields (some tax programs write them as widgets, not page text): [(text, x, y)] at the field's left edge and middle."""
    out = []
    try:
        annots = page.get("/Annots") or []
    except Exception:
        return out
    for a in annots:
        try:
            a = a.get_object() if hasattr(a, "get_object") else a
            if str(a.get("/Subtype")) != "/Widget":
                continue
            v = a.get("/V")
            r = a.get("/Rect")
            if v is None or r is None or isinstance(v, (list, dict)):
                continue
            t = str(v).strip()
            if t:
                out.append((t, round(float(r[0]), 1), round((float(r[1]) + float(r[3])) / 2.0 - 3.0, 1)))
        except Exception:
            continue
    return out


def page_fragments(page):
    """Positioned text fragments of one pypdf page: [(text, x, y)], plus any entered form-field values. Only coordinates and text, nothing else."""
    out = []

    def visit(text, cm, tm, font_dict, font_size):
        t = (text or "").strip()
        if t:
            x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]                  # text matrix through the current transformation
            y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
            out.append((t, round(x, 1), round(y, 1)))
    page.extract_text(visitor_text=visit)
    return out + _widget_fragments(page)


def _norm(s):
    return re.sub(r"[^a-z0-9()\-\. ]+", " ", str(s).lower()).replace(" . ", " ").strip()


def _compact(s):
    return re.sub(r"\s+", " ", re.sub(r"[.…•]{2,}", " ", _norm(s))).strip()


def _to_amount(tok):
    t = tok.strip()
    if re.fullmatch(r"\d{9}", t) or re.fullmatch(r"\d{3}-\d{2}-\d{4}", t):
        return None                                           # a 9-digit run is an identification number, never an amount
    neg = t.startswith("(") or t.startswith("-") or t.startswith("$-") or t.endswith("-")
    digits = re.sub(r"[^\d.]", "", t).rstrip(".")
    if not digits:
        return None
    try:
        v = float(digits)
    except ValueError:
        return None
    return -v if neg else v


def _rows(frags):
    rows = []
    for t, x, y in sorted(frags, key=lambda f: (-f[2], f[1])):
        if rows and abs(rows[-1][0] - y) <= ROW_TOL:
            rows[-1][1].append((t, x))
        else:
            rows.append([y, [(t, x)]])
    for r in rows:
        r[1].sort(key=lambda f: f[1])
    return rows


def classify_pages(pages):
    """-> {'1040': {1: page_index, 2: page_index}, '8959': page_index|None, 'year': int|None}. From the printed form footers and headings."""
    out = {"1040": {}, "8959": None, "year": None}
    for i, frags in enumerate(pages):
        text = " ".join(t for t, _x, _y in frags)
        m = _FOOT.search(text)
        if m:
            out["year"] = out["year"] or int(m.group(1))
            pg = int(m.group(2)) if m.group(2) else (2 if re.search(r"\bPage\s+2\b", text) else 1)
            out["1040"].setdefault(pg, i)
        if re.search(r"Form\s+8959", text) and re.search(r"Additional\s+Medicare\s+Tax", text, re.I) and out["8959"] is None:
            out["8959"] = i
    return out


def _find(frags, line, label, ids):
    """-> (status, value, reason). Looks for the box marker `line` on a row that also carries `label`, and reads the nearest amount to its right.
    `ids` = every line number the map knows on this page: another marker to the right of ours means our line is blank. When the row holds no amount,
    an amount within NEAR_TOL points above or below the row (a baseline that sits slightly off) to the right of the marker is taken instead."""
    want = _compact(label)
    for y, row in _rows(frags):
        rowtext = _compact(" ".join(t for t, _x in row))
        if want not in rowtext:
            continue
        marks = [i for i, (t, _x) in enumerate(row) if t == line]
        for i in reversed(marks):                                  # the box marker is the right-most occurrence
            mx = row[i][1]
            for t, _x in row[i + 1:]:
                if t in ids and t != line and not re.search(r"[,.]", t):
                    break                                          # the next line's marker: this line is blank
                if _NUM.match(t.replace(" ", "")):
                    v = _to_amount(t)
                    if v is None:
                        return "unreliable", None, "a number that looks like an identification number was found there and was not read"
                    return "ok", v, None
            else:
                near = sorted(((abs(fy - y), fx, ft) for ft, fx, fy in frags if fx > mx and 0 < abs(fy - y) <= NEAR_TOL and _NUM.match(ft.replace(" ", "")) and ft not in ids),
                              key=lambda c: (c[0], c[1]))
                nxt = [x for t, x in row[i + 1:] if t in ids and t != line]
                if near and not nxt:
                    v = _to_amount(near[0][2])
                    if v is None:
                        return "unreliable", None, "a number that looks like an identification number was found there and was not read"
                    return "ok", v, None
            return "blank", 0.0, "the line is blank on the return (counted as 0)"
    return "missing", None, "the line and its label were not found on this page (a different form revision?)"


def _filing_status(frags):
    for y, row in _rows(frags):
        rowtext = _compact(" ".join(t for t, _x in row))
        for key, name in STATUS_LABELS:
            if key in rowtext:
                lab = [x for t, x in row if key.split()[0] in t.lower()]
                if not lab:
                    continue
                for t, x in row:
                    if t in ("X", "x", "☒", "✓", "✔") and 0 < (min(lab) - x) <= 45:
                        return name
    return None


def _owed(v):
    d = v["total_tax"] - v["total_payments"]
    return (d - v["amount_owed"]) if d > 0 else v["amount_owed"]


def _over(v):
    d = v["total_payments"] - v["total_tax"]
    return (d - v["overpaid"]) if d > 0 else v["overpaid"]


# (name, the fields it needs, difference in dollars: 0 when the figures add up)
CHECKS = [
    ("total income - adjustments = AGI", ("total_income", "adjustments", "agi"), lambda v: v["total_income"] - v["adjustments"] - v["agi"]),
    ("deduction + business-income deduction (+ any additional deductions) = total deductions", ("deduction", "total_deductions"),
     lambda v: v["deduction"] + (v.get("qbi_deduction") or 0) + (v.get("additional_deductions") or 0) - v["total_deductions"]),
    ("AGI - total deductions = taxable income (not below zero)", ("agi", "total_deductions", "taxable_income"), lambda v: max(0.0, v["agi"] - v["total_deductions"]) - v["taxable_income"]),
    ("amount owed = total tax - total payments", ("total_tax", "total_payments", "amount_owed"), _owed),
    ("amount overpaid = total payments - total tax", ("total_tax", "total_payments", "overpaid"), _over),
]


def run_checks(values):
    """-> list of {'name','ok','fields','difference'}. A check runs only when every figure it needs was read (ok = None otherwise); tolerance $1."""
    out = []
    for name, need, fn in CHECKS:
        if any(values.get(f) is None for f in need):
            out.append({"name": name, "ok": None, "fields": list(need), "difference": None})
            continue
        diff = fn(values)
        out.append({"name": name, "ok": abs(diff) <= 1.0, "fields": list(need), "difference": round(diff, 2)})
    return out


def extract(pages, year=None, maps=None):
    """Read the return. `year` (what the person said) must agree with the year printed on the form. Raises ValueError with a plain message when the
    year has no map, the form is not recognised or the years disagree. -> {'year','filing_status','fields':[...],'checks':[...],'pages':...}"""
    maps = maps or load_maps()
    info = classify_pages(pages)
    printed = info["year"]
    if not info["1040"]:
        raise ValueError("This does not look like a Form 1040 with a readable text layer (a scanned or image-only PDF cannot be read here).")
    if year is not None and printed is not None and int(year) != printed:
        raise ValueError(f"You said tax year {year}, but the form is printed for {printed}.")
    y = str(printed or year)
    if y not in maps["years"]:
        raise ValueError(f"No line map for tax year {y}. Supported years: {', '.join(sorted(maps['years']))}. Line numbers move between years, so an unmapped year is refused.")
    ymap = maps["years"][y]
    fields, values = [], {}
    ids_by_page = {}
    for ln in ymap["forms"]["1040"]["lines"]:
        ids_by_page.setdefault(ln["page"], set()).add(ln["line"])
    for form, spec in ymap["forms"].items():
        for ln in spec["lines"]:
            if form == "1040":
                idx = info["1040"].get(ln["page"])
                if idx is None:
                    st, val, why, pg = "missing", None, f"page {ln['page']} of the Form 1040 was not found", None
                else:
                    st, val, why = _find(pages[idx], ln["line"], ln["label"], ids_by_page[ln["page"]])
                    pg = idx + 1
            else:
                idx = info["8959"]
                if idx is None:
                    st, val, why, pg = "absent", None, "Form 8959 is not in this PDF (it is only filed with Additional Medicare Tax)", None
                else:
                    st, val, why = _find(pages[idx], ln["line"], ln["label"], set())
                    pg = idx + 1
            fields.append({"field": ln["field"], "form": form, "line": ln["line"], "label": ln["label"], "page": pg, "value": val, "status": st, "reason": why})
            values[ln["field"]] = val
    checks = run_checks(values)
    by = {f["field"]: f for f in fields}
    for c in checks:
        if c["ok"] is False:
            for fld in c["fields"]:
                f = by.get(fld)
                if f and f["status"] in ("ok", "blank"):
                    f["status"] = "unreliable"
                    f["reason"] = f"could not be read reliably: the check '{c['name']}' is off by {c['difference']:,.2f}"
    status = _filing_status(pages[info["1040"][1]]) if 1 in info["1040"] else None
    return {"year": int(y), "filing_status": status, "fields": fields, "checks": checks, "source": ymap["source"],
            "pages_read": {"1040": {k: v + 1 for k, v in info["1040"].items()}, "8959": (info["8959"] + 1) if info["8959"] is not None else None}}


def summary(values):
    """The 'Last filed year' card from CONFIRMED values: AGI, taxable income, total tax, effective rate, realized gains, dividends, Additional Medicare Tax."""
    v = values or {}
    agi, tt = v.get("agi"), v.get("total_tax")
    return {"agi": agi, "taxable_income": v.get("taxable_income"), "total_tax": tt,
            "effective_rate_pct": round(tt / agi * 100, 1) if (agi and tt is not None and agi > 0) else None,
            "capital_gain": v.get("capital_gain"), "ordinary_dividends": v.get("ordinary_dividends"), "qualified_dividends": v.get("qualified_dividends"),
            "additional_medicare_tax": v.get("additional_medicare_tax"), "wages": v.get("wages")}


def diagnose(pages):
    """Structure only, NO values: what the PDF's text layer looks like, so a layout that will not read can be understood without sharing any amount.
    -> per page: fragment counts, how many look like amounts (and in which style), form-field values, and for each mapped line whether its marker and label were
    found, whether an amount sits on the same row, and how far (in points) the nearest amount is."""
    out = []
    info = classify_pages(pages)
    for i, frags in enumerate(pages):
        nums = [t for t, _x, _y in frags if _NUM.match(t.replace(" ", ""))]
        out.append({"page": i + 1, "fragments": len(frags), "amount_like": len(nums),
                    "amount_styles": {"with_comma": sum(1 for t in nums if "," in t), "trailing_period": sum(1 for t in nums if t.rstrip(")-").endswith(".")),
                                      "with_cents": sum(1 for t in nums if re.search(r"\.\d\d$", t)), "parenthesised_or_negative": sum(1 for t in nums if t.startswith(("(", "-")))},
                    "ssn_like_runs": sum(1 for t, _x, _y in frags if re.fullmatch(r"\d{9}|\d{3}-\d{2}-\d{4}", t.strip()))})
    return {"year": info["year"], "pages": out, "form_pages": {"1040": {k: v + 1 for k, v in info["1040"].items()}, "8959": (info["8959"] + 1) if info["8959"] is not None else None}}


def diagnose_lines(pages, year, maps=None):
    maps = maps or load_maps()
    info = classify_pages(pages)
    ymap = (maps["years"].get(str(year or info["year"])) or {}).get("forms", {}).get("1040")
    if not ymap:
        return []
    ids_by_page = {}
    for ln in ymap["lines"]:
        ids_by_page.setdefault(ln["page"], set()).add(ln["line"])
    out = []
    for ln in ymap["lines"]:
        idx = info["1040"].get(ln["page"])
        if idx is None:
            out.append({"line": ln["line"], "found": False}); continue
        frags = pages[idx]
        want = _compact(ln["label"])
        row_hit = None
        for y, row in _rows(frags):
            if want in _compact(" ".join(t for t, _x in row)) and any(t == ln["line"] for t, _x in row):
                row_hit = (y, row); break
        if not row_hit:
            out.append({"line": ln["line"], "found": False}); continue
        y, row = row_hit
        mx = max(x for t, x in row if t == ln["line"])
        same = [t for t, x in row if x > mx and _NUM.match(t.replace(" ", ""))]
        near = sorted((round(abs(fy - y), 1) for ft, fx, fy in frags if fx > mx and _NUM.match(ft.replace(" ", ""))))
        out.append({"line": ln["line"], "found": True, "amount_on_row": bool(same), "nearest_amount_right_dy": near[0] if near else None,
                    "row_texts_right_of_marker": len([1 for t, x in row if x > mx])})
    return out
