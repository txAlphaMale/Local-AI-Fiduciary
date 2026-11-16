# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""numcheck.py -- are the FIGURES in a piece of model-written text found in the data the model was given?

Pure functions, no network or model. A "figure" is a percentage (12%), a dollar amount ($12.6, $1.2B, $3 million), a multiple (3x)
or a scaled quantity (12M shares). Plain numbers (years, counts, "5 insiders") are not checked: too many harmless ones.

A figure counts as SUPPORTED when it equals some number in the evidence after normalising units (percent as a fraction, thousands/
millions/billions) and allowing for rounding to the precision the writer used (half a unit in the last digit shown, plus a 0.5% slack).
Signs are ignored ("-4.1%" and "a 4.1% loss" match the same number): this check is about invented figures, not about direction.
"""
import re

_FIG = re.compile(
    r"(?<![\w.])(?P<sign>[-−–+]?)\s?(?P<cur>\$)?\s?(?P<num>\d[\d,]*(?:\.\d+)?)"
    r"(?:\s?(?P<suf>%|×|x(?![a-z])|k(?![a-z])|m(?![a-z])|b(?![a-z])|t(?![a-z])|thousand|million|billion|trillion|bn|mm))?", re.I)
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9, "t": 1e12, "trillion": 1e12}
_ANY_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Sentences that talk about a THRESHOLD or a hypothetical ("if growth falls below 25%", "a raise of at least $50M would...") state a
# figure of the writer's own on purpose: it is not a claim about the data, so it is not checked.
_HYPOTHETICAL = re.compile(
    r"\b(if|unless|whether|would|could|should|might|may\s+need|threshold|trigger\w*|at\s+least|at\s+most|watch\s+for|look\s+for|"
    r"need\s+to|needs\s+to|must|required?)\b", re.I)


def figures(text):
    """-> [{raw, value, tol, kind}] for each checkable figure in `text`. value is in raw units (a $1.2B figure is 1.2e9, a % stays 12.0)."""
    out = []
    for m in _FIG.finditer(text or ""):
        suf = (m.group("suf") or "").lower()
        cur = bool(m.group("cur"))
        if suf == "%":
            kind = "pct"
        elif suf in ("x", "×"):
            kind = "mult"
        elif cur or suf in _SCALE:
            kind = "usd" if cur else "qty"
        else:
            continue                       # a plain number: not checked
        txt = m.group("num").replace(",", "")
        try:
            v = float(txt)
        except ValueError:
            continue
        dec = len(txt.split(".")[1]) if "." in txt else 0
        scale = _SCALE.get(suf, 1.0) if kind in ("usd", "qty") else 1.0
        out.append({"raw": m.group(0).strip(), "value": abs(v) * scale, "tol": 0.5 * (10 ** -dec) * scale, "kind": kind})
    return out


def evidence_values(evidence):
    """Every number the evidence contains, in a form a figure can be compared with: the number itself, as a fraction (x100), and
    with the thousand/million/billion scalings. -> sorted list of floats."""
    vals = set()
    text = evidence or ""
    for m in _ANY_NUM.finditer(text.replace(",", "")):
        try:
            vals.add(abs(float(m.group(0))))
        except ValueError:
            continue
    for f in figures(text):                # "$1.2B" written out in the evidence text itself
        vals.add(f["value"])
    expanded = set()
    for v in vals:
        expanded.update((v, v * 100.0, v / 1e3, v / 1e6, v / 1e9))
    return sorted(expanded)


def _supported(fig, vals):
    x = fig["value"]
    tol = max(fig["tol"], 0.005 * x)
    import bisect
    i = bisect.bisect_left(vals, x - tol)
    return i < len(vals) and vals[i] <= x + tol


def unsupported(text, evidence, vals=None):
    """The figures in `text` that the evidence does not contain (raw strings). Pass `vals` (evidence_values) to reuse it."""
    vals = vals if vals is not None else evidence_values(evidence)
    return [f["raw"] for f in figures(text) if not _supported(f, vals)]


def sentence_is_hypothetical(sentence):
    return bool(_HYPOTHETICAL.search(sentence or ""))
