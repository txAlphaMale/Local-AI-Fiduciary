# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""Grounding for chat answers that use the idea-research tool.

Two pieces, both plain functions with no network or model:

* facts_block(results): the app writes the company facts itself, straight from what the
  research tool returned, so the numbers and labels never pass through the model.
* strip_ungrounded(text, evidence): drops sentences that make a company-history claim
  (former name, merger, acquisition, spin-off, founding...) whose key word does not appear
  anywhere in the evidence the model was given. The model keeps the commentary; invented
  background is removed and the reader is told.
"""
import re

# Words that signal "background about the company" rather than a reading of the numbers.
_CLAIM = re.compile(
    r"\b(formerly|fmr\.?|previously\s+known|rebrand\w*|renamed|re-?named|merg(?:er|ed|ing)|"
    r"post-?merger|acquir\w+|acquisition|spun[\s-]?off|spin-?off|founded|headquartered|"
    r"went\s+public|ipo\w*|known\s+for|best\s+known)\b", re.I)
_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9*_#\-])|\n+")


_STEMS = ("merg", "acquir", "acquisition", "rebrand", "renam", "founded", "spun", "spin", "ipo",
          "formerly", "fmr", "headquarter", "went public", "known")


def _norm(w):
    w = re.sub(r"\s+", " ", w.lower().rstrip(".")).replace("-", "")
    return next((st for st in _STEMS if st in w), w)


def strip_ungrounded(text, evidence):
    """Return (clean_text, removed_sentences). A flagged sentence is removed only when the claim
    word's stem is absent from `evidence`, so a claim the tool itself made is left alone."""
    ev = (evidence or "").lower()
    kept, removed = [], []
    for sent in _SPLIT.split(text or ""):
        if not sent.strip():
            continue
        hits = [_norm(m.group(0)) for m in _CLAIM.finditer(sent)]
        if hits and any(h not in ev for h in hits):
            removed.append(sent.strip())
        else:
            kept.append(sent)
    # keep paragraph structure roughly: the splitter drops newlines, so rejoin with spaces
    # only when something was removed; otherwise return the text untouched
    return (" ".join(kept).strip() if removed else (text or "")), removed


def _num(v, suffix="", nd=1):
    return None if v is None else f"{round(v, nd):g}{suffix}" if isinstance(v, (int, float)) else str(v)


def facts_block(results):
    """results: tool-result dicts from get_idea_research(ticker=...). '' when nothing usable."""
    lines = []
    for r in results or []:
        if not isinstance(r, dict) or not r.get("found"):
            continue
        s = r.get("screen") or {}
        bits = [s.get("name"), s.get("sector"),
                _num(s.get("price"), "", 2) and "price $" + _num(s.get("price"), "", 2),
                s.get("market_cap") and "mkt cap $" + format(round(s["market_cap"]), ","),
                s.get("rev_growth_pct") is not None and f"revenue growth {_num(s['rev_growth_pct'], '%')}",
                s.get("net_margin_pct") is not None and f"net margin {_num(s['net_margin_pct'], '%')}",
                s.get("fcf_margin_pct") is not None and f"FCF margin {_num(s['fcf_margin_pct'], '%')}",
                s.get("share_change_pct") is not None and f"share count change {_num(s['share_change_pct'], '%')}",
                s.get("insider") and f"insiders: {s['insider']}",
                s.get("analyst") and f"analysts: {s['analyst']}"]
        line = f"- **{r.get('ticker')}**: " + " · ".join(str(b) for b in bits if b)
        also = s.get("also")
        if also:
            line += f" · sleeves: {', '.join(str(a) for a in also)}" if isinstance(also, (list, tuple)) else ""
        lines.append(line)
        n = r.get("scout_note")
        if n and n.get("note"):
            lines.append(f"  - Scout note ({str(n.get('created_at') or '')[:10]}): " + str(n["note"]).strip().replace("\n", " ")[:700])
    if not lines:
        return ""
    return "**From the app's research data (written by the app, not the model):**\n" + "\n".join(lines)


COMMENTARY_RULE = (
    "The app will print the research figures and the scout note itself, word for word, with your answer. "
    "So write ONLY a short commentary (at most about 6 sentences): how the numbers bear on Jason's goal, "
    "what to check next, and the main risk. Do not repeat the figures table, and add no company background "
    "(former names, mergers, products, history) that the tool did not give you.")


def compose(commentary, removed, facts):
    """Facts first (app-written), then the model's commentary under its own label."""
    note = f"\n\n_(Removed {removed} sentence(s) of company background that the research data does not contain.)_" if removed else ""
    if not facts:
        return (commentary or "") + note
    return facts + "\n\n---\n**Model commentary (not checked by the app):**\n" + (commentary or "").strip() + note
