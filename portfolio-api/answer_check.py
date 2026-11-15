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
# "conviction 3/10", "score of 3 out of 10": the app's conviction scale is 1-5 only.
_BAD_SCALE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:/|out of)\s*(?!5\b)\d+\b", re.I)
# Personalised allocation / trading guidance: the app is read-only and educational, so the model's commentary may not give it.
_ADVICE = re.compile(
    r"\b(you\s+should|consider\s+(?:adding|buying|selling|trimming|a\s+position)|small\s+(?:speculative\s+)?allocation|"
    r"your\s+IPS|IPS\s+targets?|position\s+siz\w+|watch\s?list|be\s+prepared\s+to|trim\s+(?:the|your|if|it)|"
    r"(?:a|the)\s+(?:good|bad)\s+(?:buy|entry))\b", re.I)
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
        if re.search(r"conviction", sent, re.I) and _BAD_SCALE.search(sent):
            removed.append(sent.strip())
        elif _ADVICE.search(sent):
            removed.append(sent.strip())
        elif hits and any(h not in ev for h in hits):
            removed.append(sent.strip())
        else:
            kept.append(sent)
    # keep paragraph structure roughly: the splitter drops newlines, so rejoin with spaces
    # only when something was removed; otherwise return the text untouched
    return (" ".join(kept).strip() if removed else (text or "")), removed


def limit_sentences(text, n=6):
    """-> (text, shortened). Keeps the first n sentences; the model was told to be brief and this makes it so."""
    parts = [p for p in _SPLIT.split(text or "") if p.strip()]
    if len(parts) <= n:
        return text or "", False
    return " ".join(p.strip() for p in parts[:n]), True


def _note_lines(n):
    """Readable lines for a scout note (a dict: thesis, evidence, risks, disproof) or plain text."""
    body = n.get("note")
    date = str(n.get("created_at") or "")[:10]
    if not isinstance(body, dict):
        return [f"  - Scout note ({date}): " + str(body).strip().replace("\n", " ")[:700]]
    out = [f"  - Scout note ({date}): {str(body.get('thesis') or '').strip()}"]
    conv = body.get("conviction")
    if conv is not None:
        out.append(f"    - Conviction: {conv} of 5" + (" (capped: no news or filings cited)" if body.get("conviction_capped") else "")
                   + (f" · horizon {body['horizon']}" if body.get("horizon") else ""))
    for e in (body.get("evidence") or [])[:5]:
        if isinstance(e, dict):
            out.append(f"    - Evidence ({e.get('source', '?')}): {str(e.get('claim', '')).strip()}")
    for k, label in (("risks", "Risk"), ("catalysts", "Catalyst")):
        for x in (body.get(k) or [])[:3]:
            out.append(f"    - {label}: {str(x).strip()}")
    if body.get("disproof"):
        d = body["disproof"]
        d = "; ".join(str(x) for x in d) if isinstance(d, (list, tuple)) else d
        out.append(f"    - What would prove it wrong: {str(d).strip()}")
    return out


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
                s.get("runway_months") is not None and f"cash runway {_num(s['runway_months'], ' months', 0)}",
                s.get("beta") is not None and f"beta {_num(s['beta'], '', 2)}",
                s.get("dividend_yield_pct") and f"dividend yield {_num(s['dividend_yield_pct'], '%', 2)}",
                s.get("insider") and f"insiders: {s['insider']}",
                s.get("analyst") and f"analysts: {s['analyst']}"]
        line = f"- **{r.get('ticker')}**: " + " · ".join(str(b) for b in bits if b)
        also = s.get("also")
        if also:
            line += f" · sleeves: {', '.join(str(a) for a in also)}" if isinstance(also, (list, tuple)) else ""
        lines.append(line)
        n = r.get("scout_note")
        if n and n.get("note"):
            lines.extend(_note_lines(n))
    if not lines:
        return ""
    return "**From the app's research data (written by the app, not the model):**\n" + "\n".join(lines)


COMMENTARY_RULE = (
    "The app will print the research figures and the scout note itself, word for word, with your answer. "
    "So write ONLY a short commentary (at most about 6 sentences): how the numbers bear on Jason's goal, "
    "what to check next, and the main risk. Do not repeat the figures table, and add no company background "
    "(former names, mergers, products, history) that the tool did not give you. Conviction is on a 1 to 5 scale "
    "(say 'N of 5'); never express it on any other scale. "
    "Give NO advice: no allocation or position sizing, no what-to-do (buy, sell, trim, add to a watchlist), "
    "and do not refer to Jason's IPS targets. Describe what the data shows and what to check next.")

MODEL_LABEL = "**Model commentary (not checked by the app):**"


def compose(commentary, removed, facts):
    """Facts first (app-written), then the model's commentary under its own label."""
    note = f"\n\n_(Removed {removed} sentence(s) of company background that the research data does not contain.)_" if removed else ""
    if not facts:
        return (commentary or "") + note
    return facts + "\n\n---\n" + MODEL_LABEL + "\n" + (commentary or "").strip() + note
