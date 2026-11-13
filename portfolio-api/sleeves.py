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

# key: (label, unit, stage). Stage s1 = from SEC filings; s2 = market data (Finnhub: price, size, yield, beta, P/E); s3 = signals
# (analyst ratings from Finnhub, insider buying from SEC Form 4, short interest from FINRA when credentials are saved).
# Short-interest rules stay "pending" until at least one name has FINRA data.
METRICS = {
    "revenue": ("Revenue (last 12 months)", "$", "s1"),
    "rev_growth_pct": ("Revenue growth, year over year", "%", "s1"),
    "growth_capped": ("Revenue growth, capped at 150% (so a tiny base cannot dominate a ranking)", "%", "s1"),
    "rev_cagr_pct": ("Revenue growth, multi-year average", "%", "s1"),
    "rev_accel_pts": ("Revenue growth acceleration (this year minus multi-year average)", "pts", "s1"),
    "gross_margin_pct": ("Gross margin", "%", "s1"),
    "net_margin_pct": ("Net profit margin", "%", "s1"),
    "fcf_margin_pct": ("Free cash flow margin", "%", "s1"),
    "debt_to_equity": ("Debt to equity (total liabilities / equity)", "x", "s1"),
    "roe_pct": ("Return on equity", "%", "s1"),
    "dividend_payout_pct": ("Dividend payout (share of profit paid out)", "%", "s1"),
    "fcf_dividend_coverage": ("Dividend coverage by free cash flow", "x", "s1"),
    "share_change_pct": ("Share count change in the last year (dilution; ignored in the first 2 years after listing)", "%", "s1"),
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
    "analyst_buy_pct": ("Analysts rating it a buy (share of ratings)", "%", "s3"),
    "analyst_count": ("Number of analyst ratings", "", "s3"),
    "insider_net_buying": ("Insider net open-market buying, last 120 days (buys minus sales)", "$", "s3"),
    "insider_buyers": ("Insiders who bought on the open market, last 120 days", "", "s3"),
    "short_days_to_cover": ("Short interest: days to cover", "d", "s3"),
    "short_interest_change_pct": ("Short interest change since the previous report", "%", "s3"),
}

S3_GROUP = {"analyst_buy_pct": "analyst", "analyst_count": "analyst", "insider_net_buying": "insider", "insider_buyers": "insider",
            "short_days_to_cover": "short", "short_interest_change_pct": "short"}

BUILTIN = {
    "growth": {"name": "Growth (speculative)", "builtin": True, "enabled": True, "include_states": list(STATES),
               "intent": "Fast-growing companies, including ones not yet profitable, that can fund themselves through the R&D phase.",
               "rules": [{"metric": "revenue", "op": ">=", "value": 10_000_000}, {"metric": "rev_growth_pct", "op": ">=", "value": 25},
                         {"metric": "share_change_pct", "op": "<=", "value": 300},
                         {"metric": "runway_months", "op": ">=", "value": 12}],
               "rank_by": "growth_capped", "rank_dir": "desc",
               "notes": "Does not require profit. Ranked by revenue growth capped at 150% (ties go to the larger business), so enormous percentages on small bases do not crowd out real companies. Revenue must be at least $10M, because growth percentages on tiny revenue are noise. Looks for revenue growing fast and cash that lasts at least a year at the current burn. The dilution cap (300%) only screens out extreme share issuance; it is ignored in the first 2 years after listing, when the share count jumps because of the IPO."},
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
                   "notes": "For opportunities that are not early-stage bets: companies that are already profitable and still growing."},
    "insiders": {"name": "Insiders are buying", "builtin": True, "enabled": True, "include_states": list(STATES),
                 "intent": "Companies where several insiders recently bought shares with their own money.",
                 "rules": [{"metric": "revenue", "op": ">=", "value": 10_000_000}, {"metric": "insider_buyers", "op": ">=", "value": 2},
                           {"metric": "insider_net_buying", "op": ">=", "value": 50000}],
                 "rank_by": "insider_net_buying", "rank_dir": "desc",
                 "notes": "Open-market purchases by officers and directors (SEC Form 4, codes P and S only). Cluster buying is a stronger signal than one insider; sales are noisy and not scored here. Context, never a buy signal on its own."},
    "balance": {"name": "Balance (fills a gap)", "builtin": True, "enabled": True, "include_states": list(STATES),
                "intent": "Candidates in sectors or asset classes your portfolio is light on.",
                "rules": [{"metric": "s1_score", "op": ">=", "value": 5}], "rank_by": "s1_score", "rank_dir": "desc",
                "fill_gap": True, "gap_below_pct": 3,
                "notes": "Healthy companies (basic screen score 5+) in a sector where you hold under 3% of your portfolio. Sector comes from market data, so a candidate shows as waiting until its market data is fetched."},
}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def value_of(row, metric, today=None):
    """Metric value for a ticker-state row, or None when unknown / not available at this stage."""
    s1 = row.get("s1") or {}
    if metric == "s1_score":
        v = (row.get("scores") or {}).get("score")
        return v if (s1 and v is not None) else None
    if metric == "growth_capped":
        g = s1.get("rev_growth_pct")
        return min(g, 150.0) if _num(g) else None
    if metric == "age_years":
        try:
            d = datetime.date.fromisoformat(str(s1.get("first_filed"))[:10])
            return round(((today or datetime.date.today()) - d).days / 365.25, 1)
        except (TypeError, ValueError):
            return None
    stage = METRICS.get(metric, ("", "", ""))[2]
    if metric == "share_change_pct" and s1.get("share_change_pct") is not None:
        age = value_of(row, "age_years", today)
        if age is not None and age < 2:
            return 0.0                    # the first year or two after an IPO compares a pre-listing share count: not real dilution
    if stage == "s3":
        g = ((row.get("s3") or {}).get(S3_GROUP.get(metric, "")) or {})
        if metric == "analyst_buy_pct":
            return g.get("buy_pct") if g.get("total") else None
        if metric == "analyst_count":
            return g.get("total") if g.get("total") is not None else None
        if metric == "insider_net_buying":
            return g.get("net_value") if g.get("available") else None
        if metric == "insider_buyers":
            return g.get("buyers") if g.get("available") else None
        if metric == "short_days_to_cover":
            return g.get("days_to_cover")
        if metric == "short_interest_change_pct":
            return g.get("change_pct")
        return None
    if stage == "s2":
        s2 = row.get("s2") or {}
        v = s2.get(metric)
        return v if (s2.get("available") and _num(v)) else None
    if stage != "s1":
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
    if not rules:
        return None, "add at least one rule (a sleeve with no rules would match every stock)"
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
    if rank_by not in METRICS:
        return None, "unknown metric to rank by"
    states = [s for s in (sl.get("include_states") or list(STATES)) if s in STATES]
    if not states:
        return None, "include at least one of: new, active, reconsider"
    return {"name": name, "intent": str(sl.get("intent") or "").strip()[:300], "notes": str(sl.get("notes") or "").strip()[:1000],
            "rules": clean, "rank_by": rank_by, "rank_dir": "asc" if sl.get("rank_dir") == "asc" else "desc",
            "include_states": states, "enabled": sl.get("enabled", True) is not False}, None


def describe_rule(r, note=""):
    label, unit, stage = METRICS[r["metric"]]
    v = r["value"]
    if unit == "$":
        shown = "$" + (f"{v / 1e9:g}B" if abs(v) >= 1e9 else f"{v / 1e6:g}M" if abs(v) >= 1e6 else f"{v / 1e3:g}K" if abs(v) >= 1e3 else f"{v:g}")
    else:
        shown = f"{v:g}" + (unit if unit in ("%", "x") else (" " + unit if unit else ""))
    return f"{label} {r['op']} {shown}" + (f" ({note})" if note else "")


def evaluate(sleeve, rows, today=None, held=None):
    """-> {'matches', 'awaiting', 'considered', 'pending_rules', 'gap_sectors'}.
    SEC-data rules apply to every row. Rules on market-data or signals metrics (and a Balance sleeve's sector test) apply once a row
    has that data; a row that passes the SEC rules but has none yet is counted as `awaiting`, not as a failure. Short-interest rules
    stay in `pending_rules` until at least one name has FINRA data. An unknown value fails a rule: a name we cannot measure does not
    qualify. `held` = {sector: portfolio weight %} for the Balance sleeve."""
    rules = sleeve.get("rules") or []
    stage = lambda r: METRICS[r["metric"]][2]
    any_short = any(((r.get("s3") or {}).get("short")) for r in rows)
    pending, live = [], []
    for r in rules:
        if S3_GROUP.get(r["metric"]) == "short" and not any_short:
            pending.append(describe_rule(r, "needs FINRA short-interest credentials"))
        else:
            live.append(r)
    s1_rules = [r for r in live if stage(r) == "s1"]
    later = [r for r in live if stage(r) in ("s2", "s3")]
    gap, gap_below = bool(sleeve.get("fill_gap")), float(sleeve.get("gap_below_pct", 3))
    held = held or {}
    states = set(sleeve.get("include_states") or STATES)

    def has_data(row, r):
        if stage(r) == "s2":
            return bool(row.get("s2"))
        return bool(((row.get("s3") or {}).get(S3_GROUP.get(r["metric"], ""))))
    ok_rule = lambda row, r: (lambda v: v is not None and OPS[r["op"]](v, r["value"]))(value_of(row, r["metric"], today))
    out, awaiting, considered, gap_sectors = [], 0, 0, {}
    for row in rows:
        if row.get("state") not in states or not row.get("s1"):
            continue
        considered += 1
        if not all(ok_rule(row, r) for r in s1_rules):
            continue
        s2 = row.get("s2")
        if (gap and not s2) or any(not has_data(row, r) for r in later):
            awaiting += 1
            continue
        if not all(ok_rule(row, r) for r in later):
            continue
        if gap:
            sector = (s2 or {}).get("sector")
            if not sector or held.get(sector, 0) >= gap_below:
                continue
            gap_sectors[sector] = gap_sectors.get(sector, 0) + 1
        out.append(row)
    key, rev = sleeve.get("rank_by") or "s1_score", (sleeve.get("rank_dir") != "asc")
    out.sort(key=lambda r: (value_of(r, key, today) is None, -(value_of(r, key, today) or 0) if rev else (value_of(r, key, today) or 0),
                            -((r.get("s1") or {}).get("revenue") or 0)))             # ties: the larger business first
    out = _one_per_company(out)
    return {"matches": out, "awaiting": awaiting, "considered": considered, "pending_rules": pending,
            "gap_sectors": sorted(gap_sectors.items(), key=lambda kv: -kv[1])}


def _one_per_company(rows):
    """Share classes of one company (SENEA/SENEB, FWONA/FWONK) carry identical SEC numbers: keep the best-ranked, note the others."""
    seen, out = {}, []
    for r in rows:
        key = r.get("cik")
        if key and key in seen:
            seen[key].setdefault("also", []).append(r["ticker"])
            continue
        r = dict(r)
        if key:
            seen[key] = r
        out.append(r)
    return out


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "sleeve"
