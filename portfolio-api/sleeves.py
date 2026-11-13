# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""sleeves.py -- idea-scavenger "sleeves" (purposes), phase 4a. A sleeve is a name, an intent sentence, measurable rules chosen
from a fixed metric menu, a ranking, and free-text notes that only ever guide the LLM scout later (never code).

Rules are evaluated over the ticker-state table (the zero-API SEC screen). Metrics marked stage "s2" need market data
(price, market cap, yield, beta ...), which arrives with the next phase; a rule on one is kept and shown as WAITING, and the
sleeve's count says it is before that stage. The sleeve engine is pure: rows in, matches out."""
import datetime
import math
import re

OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, "<": lambda a, b: a < b}
STATES = ("new", "active", "reconsider")          # blacklist is never offered to a sleeve
MAX_RULES = 12

# key: (label, unit, stage). Stage s1 = from SEC filings (available now); s2 = market data (next phase).
METRICS = {
    "revenue": ("Revenue (last 12 months)", "$", "s1"),
    "rev_growth_pct": ("Revenue growth, year over year", "%", "s1"),
    "rev_cagr_pct": ("Revenue growth, multi-year average", "%", "s1"),
    "rev_accel_pts": ("Revenue growth acceleration (this year minus multi-year average)", "pts", "s1"),
    "gross_margin_pct": ("Gross margin", "%", "s1"),
    "net_margin_pct": ("Net profit margin", "%", "s1"),
    "fcf_margin_pct": ("Free cash flow margin", "%", "s1"),
    "debt_to_equity": ("Debt to equity (total liabilities / equity)", "x", "s1"),
    "roe_pct": ("Return on equity", "%", "s1"),
    "dividend_payout_pct": ("Dividend payout (share of profit paid out)", "%", "s1"),
    "fcf_dividend_coverage": ("Dividend coverage by free cash flow", "x", "s1"),
    "share_change_pct": ("Share count change in the last year (dilution)", "%", "s1"),
    "piotroski": ("Piotroski financial-strength score (0-9)", "", "s1"),
    "current_ratio": ("Current ratio (short-term assets / short-term bills)", "x", "s1"),
    "cash": ("Cash and short-term investments", "$", "s1"),
    "runway_months": ("Cash runway in months (999 = funds itself)", "mo", "s1"),
    "age_years": ("Years since first SEC financials", "yr", "s1"),
    "s1_score": ("Basic screen score (0-6)", "", "s1"),
    "price": ("Share price", "$", "s2"),
    "market_cap": ("Market value", "$", "s2"),
    "dividend_yield_pct": ("Dividend yield", "%", "s2"),
    "beta": ("Beta (sensitivity to the market)", "", "s2"),
    "pe": ("Price to earnings", "x", "s2"),
    "analyst_buy_pct": ("Analysts rating it a buy", "%", "s2"),
    "insider_net_buying": ("Insider net buying", "$", "s2"),
}

BUILTIN = {
    "growth": {"name": "Growth (speculative)", "builtin": True, "enabled": True, "include_states": list(STATES),
               "intent": "Fast-growing companies, including ones not yet profitable, that can fund themselves through the R&D phase.",
               "rules": [{"metric": "rev_growth_pct", "op": ">=", "value": 25}, {"metric": "share_change_pct", "op": "<=", "value": 40},
                         {"metric": "runway_months", "op": ">=", "value": 12}],
               "rank_by": "rev_growth_pct", "rank_dir": "desc",
               "notes": "Does not require profit. Looks for revenue that is growing fast and cash that lasts at least a year at the current burn."},
    "income": {"name": "Income", "builtin": True, "enabled": True, "include_states": ["active", "new"],
               "intent": "Companies that pay a dividend they can afford and are likely to keep paying.",
               "rules": [{"metric": "dividend_payout_pct", "op": ">", "value": 0}, {"metric": "dividend_payout_pct", "op": "<=", "value": 90},
                         {"metric": "fcf_dividend_coverage", "op": ">=", "value": 1.2}, {"metric": "net_margin_pct", "op": ">", "value": 0},
                         {"metric": "debt_to_equity", "op": "<=", "value": 2.5}],
               "rank_by": "fcf_dividend_coverage", "rank_dir": "desc",
               "notes": "Dividend yield needs a share price, so yield rules wait for the market-data stage. Until then this ranks by how comfortably cash flow covers the dividend."},
    "compounder": {"name": "Profitable compounder", "builtin": True, "enabled": True, "include_states": ["active", "new"],
                   "intent": "Profitable companies that keep growing without piling on debt or diluting owners.",
                   "rules": [{"metric": "rev_growth_pct", "op": ">=", "value": 10}, {"metric": "net_margin_pct", "op": ">=", "value": 10},
                             {"metric": "fcf_margin_pct", "op": ">=", "value": 8}, {"metric": "piotroski", "op": ">=", "value": 6},
                             {"metric": "share_change_pct", "op": "<=", "value": 3}, {"metric": "debt_to_equity", "op": "<=", "value": 2}],
                   "rank_by": "rev_growth_pct", "rank_dir": "desc",
                   "notes": "Proposed addition: the 'smart opportunity beyond pre-profit' lane."},
    "balance": {"name": "Balance (fills a gap)", "builtin": True, "enabled": True, "include_states": list(STATES),
                "intent": "Candidates in sectors or asset classes your portfolio is light on.",
                "rules": [], "rank_by": "s1_score", "rank_dir": "desc", "waiting": "sector classification of candidates (market-data stage)",
                "notes": "Needs each candidate's sector, which comes from market data. Your current sector mix is on the exposure view."},
}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def value_of(row, metric, today=None):
    """Metric value for a ticker-state row, or None when unknown / not available at this stage."""
    s1 = row.get("s1") or {}
    if metric == "s1_score":
        v = (row.get("scores") or {}).get("score")
        return v if (s1 and v is not None) else None
    if metric == "age_years":
        try:
            d = datetime.date.fromisoformat(str(s1.get("first_filed"))[:10])
            return round(((today or datetime.date.today()) - d).days / 365.25, 1)
        except (TypeError, ValueError):
            return None
    if METRICS.get(metric, ("", "", ""))[2] != "s1":
        return None
    v = s1.get(metric)
    return v if _num(v) else None


def validate(sl):
    """-> (clean sleeve, None) or (None, error text). Free text is stored as text and never evaluated."""
    if not isinstance(sl, dict):
        return None, "sleeve must be an object"
    name = str(sl.get("name") or "").strip()
    if not 1 <= len(name) <= 60:
        return None, "name must be 1 to 60 characters"
    rules = sl.get("rules") or []
    if not isinstance(rules, list) or len(rules) > MAX_RULES:
        return None, f"at most {MAX_RULES} rules"
    clean = []
    for r in rules:
        if not isinstance(r, dict) or r.get("metric") not in METRICS:
            return None, "unknown metric in a rule"
        op = r.get("op")
        if op not in OPS:
            return None, "operator must be one of >= <= > <"
        if not _num(r.get("value")):
            return None, f"{METRICS[r['metric']][0]}: the threshold must be a number"
        clean.append({"metric": r["metric"], "op": op, "value": r["value"]})
    rank_by = sl.get("rank_by") or "s1_score"
    if rank_by not in METRICS or METRICS[rank_by][2] != "s1":
        return None, "rank by one of the SEC-data metrics (market-data ranking comes later)"
    states = [s for s in (sl.get("include_states") or list(STATES)) if s in STATES]
    if not states:
        return None, "include at least one of: new, active, reconsider"
    return {"name": name, "intent": str(sl.get("intent") or "").strip()[:300], "notes": str(sl.get("notes") or "").strip()[:1000],
            "rules": clean, "rank_by": rank_by, "rank_dir": "asc" if sl.get("rank_dir") == "asc" else "desc",
            "include_states": states, "enabled": sl.get("enabled", True) is not False}, None


def describe_rule(r):
    label, unit, stage = METRICS[r["metric"]]
    v = r["value"]
    shown = f"{v:g}" + (unit if unit in ("%", "x") else (" " + unit if unit else ""))
    return f"{label} {r['op']} {shown}" + (" (waits for market data)" if stage == "s2" else "")


def evaluate(sleeve, rows, today=None):
    """-> {'matches': [...], 'considered': n, 'pending_rules': [...]}. Unknown values fail a rule (a name we cannot measure does not qualify)."""
    live = [r for r in sleeve.get("rules") or [] if METRICS[r["metric"]][2] == "s1"]
    pending = [describe_rule(r) for r in sleeve.get("rules") or [] if METRICS[r["metric"]][2] != "s1"]
    states = set(sleeve.get("include_states") or STATES)
    out, considered = [], 0
    for row in rows:
        if row.get("state") not in states or not row.get("s1"):
            continue
        considered += 1
        ok = True
        for r in live:
            v = value_of(row, r["metric"], today)
            if v is None or not OPS[r["op"]](v, r["value"]):
                ok = False
                break
        if ok and (live or not sleeve.get("waiting")):
            out.append(row)
    key, rev = sleeve.get("rank_by") or "s1_score", (sleeve.get("rank_dir") != "asc")
    out.sort(key=lambda r: (value_of(r, key, today) is None, -(value_of(r, key, today) or 0) if rev else (value_of(r, key, today) or 0)))
    return {"matches": out, "considered": considered, "pending_rules": pending}


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "sleeve"
