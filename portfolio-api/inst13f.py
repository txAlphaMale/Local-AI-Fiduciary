# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
inst13f.py — SEC Form 13F institutional-holdings signal (Fable G15, forward whale-tracking). Context-only.

Institutional managers over $100M file Form 13F-HR quarterly, disclosing their long US-equity book
in an XML *information table* (issuer, CUSIP, market value, shares). This module answers the FORWARD
question — "of the managers I respect, which ones hold each of MY names, and did they add / trim /
exit last quarter?" — which is cheap (one filing per watched manager) and directly serves exit
discipline: respected long-term holders quietly exiting a concentrated position is a real context
signal (feeds G14). It is NOT the reverse question ("every institution holding X"), which needs a
full quarterly CUSIP index and is deferred (see the G15 institutional/short eval).

Pure logic + parsing: the app supplies the raw 13F-HR information-table XML (via edgar); this module
parses it, matches rows to the user's holdings (by CUSIP when known, else by normalized issuer
name), and diffs two consecutive filings. QoQ change is keyed on SHARES (unit-agnostic — sidesteps
the pre-/post-2023 thousands-vs-dollars value convention). Fully offline-testable.

CONTEXT-ONLY, never a standalone buy/sell signal: 13F is filed up to 45 days after quarter-end,
is long-only, and omits shorts/options — a conviction/turnover lens, not a real-time trade.
"""

import re
import xml.etree.ElementTree as ET

# Boilerplate tokens dropped when normalizing an issuer name for matching.
_NAME_STOP = {"THE", "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "COM", "CL",
              "CLASS", "LTD", "LIMITED", "PLC", "LP", "LLC", "HOLDINGS", "HOLDING", "GROUP",
              "TRUST", "FUND", "SA", "NV", "AG", "ADR", "ADS", "NEW", "COMMON", "STOCK", "SHARES",
              "ORD", "ORDINARY", "PAR", "VALUE", "SPONSORED", "REPRESENTING", "A", "B", "C"}


def _ln(tag):
    """Local (namespace-stripped) tag name."""
    return tag.rsplit("}", 1)[-1]


def _first_text(elem, name):
    if elem is None:
        return None
    for e in elem.iter():
        if _ln(e.tag) == name and (e.text or "").strip():
            return e.text.strip()
    return None


def _f(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


def norm_name(s):
    """Normalized significant-token key of an issuer name for fuzzy matching (order-independent set)."""
    toks = [t for t in re.sub(r"[^A-Z0-9 ]", " ", (s or "").upper()).split()
            if len(t) > 1 and t not in _NAME_STOP]
    return frozenset(toks)


def norm_cusip(c):
    """Normalize a CUSIP to compare 13F rows against holdings (uppercase, alnum; 8→9 leaves as-is).
    13F reports 9-char CUSIPs; a holding may carry 8 (no check digit) — compare on the first 8."""
    c = re.sub(r"[^A-Za-z0-9]", "", (c or "")).upper()
    return c[:8] if len(c) >= 8 else (c or None)


def parse_infotable(xml):
    """Parse a 13F-HR information-table XML into a list of positions:
    [{issuer, cusip, cusip8, class, value, shares, name_key}]. Returns [] on unparseable input.
    `value` is left as-reported (thousands pre-2023 / dollars after) — do NOT compare across the
    convention change; use `shares` for quantity diffs."""
    if not xml:
        return []
    try:
        root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    except Exception:
        return []
    out = []
    for it in root.iter():
        if _ln(it.tag) != "infoTable":
            continue
        issuer = _first_text(it, "nameOfIssuer")
        cusip = _first_text(it, "cusip")
        klass = _first_text(it, "titleOfClass")
        value = _f(_first_text(it, "value"))
        shares = _f(_first_text(it, "sshPrnamt"))
        if issuer is None and cusip is None:
            continue
        out.append({"issuer": issuer, "cusip": cusip, "cusip8": norm_cusip(cusip),
                    "class": klass, "value": value, "shares": shares,
                    "name_key": norm_name(issuer)})
    return out


def _aggregate_by_security(rows):
    """Collapse an information table to one entry per security (a manager can list several lots /
    share-classes of the same issuer). Keyed by cusip8 when present, else the name key.
    Returns {key: {issuer, cusip8, name_key, shares, value}}."""
    agg = {}
    for r in rows:
        key = r.get("cusip8") or (tuple(sorted(r.get("name_key") or ())) or r.get("issuer"))
        if not key:
            continue
        a = agg.get(key)
        if a is None:
            a = {"issuer": r.get("issuer"), "cusip8": r.get("cusip8"),
                 "name_key": r.get("name_key") or frozenset(), "shares": 0.0, "value": 0.0}
            agg[key] = a
        a["shares"] += r.get("shares") or 0.0
        a["value"] += r.get("value") or 0.0
    return agg


def _match_key(holding, agg):
    """Return the aggregation key in `agg` that matches a holding {ticker, name, cusip?}, or None.
    Prefers CUSIP; falls back to significant-token name subset match (either set ⊆ the other)."""
    hc = norm_cusip(holding.get("cusip")) if holding.get("cusip") else None
    if hc and hc in agg:
        return hc
    hk = norm_name(holding.get("name") or holding.get("ticker"))
    if not hk:
        return None
    best = None
    for key, a in agg.items():
        ak = a.get("name_key") or frozenset()
        if not ak:
            continue
        if hk and (hk <= ak or ak <= hk):     # one significant-token set contained in the other
            # prefer the tighter match (fewer extra tokens)
            score = len(ak ^ hk)
            if best is None or score < best[0]:
                best = (score, key)
    return best[1] if best else None


def classify_change(prev_shares, cur_shares, tol=0.02):
    """QoQ position change from share counts. new / added / trimmed / exited / unchanged."""
    p = prev_shares or 0.0
    c = cur_shares or 0.0
    if p <= 0 and c > 0:
        return "new"
    if p > 0 and c <= 0:
        return "exited"
    if p <= 0 and c <= 0:
        return "none"
    if c > p * (1 + tol):
        return "added"
    if c < p * (1 - tol):
        return "trimmed"
    return "unchanged"


def manager_holdings_of(cur_xml, prev_xml, holdings, manager=None):
    """For ONE watched manager, which of the user's holdings they report + the QoQ change.
    cur_xml/prev_xml are 13F-HR information tables (prev may be None). Returns
    {manager, as_of_positions, matches:[{ticker, issuer, shares, value, prev_shares, change,
    pct_change, matched_by}]}."""
    cur = _aggregate_by_security(parse_infotable(cur_xml))
    prev = _aggregate_by_security(parse_infotable(prev_xml)) if prev_xml else {}
    matches = []
    for h in holdings or []:
        tk = (h.get("ticker") or "").upper()
        key = _match_key(h, cur)
        if not key:
            continue
        a = cur[key]
        matched_by = "cusip" if (h.get("cusip") and norm_cusip(h["cusip"]) == a.get("cusip8")) else "name"
        # find the same security in the prior filing (by cusip8 first, else name-key)
        pa = prev.get(key)
        if pa is None and a.get("name_key"):
            for pk, pv in prev.items():
                if pv.get("cusip8") and pv["cusip8"] == a.get("cusip8"):
                    pa = pv
                    break
                if pv.get("name_key") and pv["name_key"] == a["name_key"]:
                    pa = pv
                    break
        prev_sh = (pa or {}).get("shares")
        change = classify_change(prev_sh, a.get("shares")) if prev_xml else "held"
        pct = (round((a["shares"] - prev_sh) / prev_sh * 100, 1)
               if (prev_sh and prev_sh > 0 and a.get("shares") is not None) else None)
        matches.append({"ticker": tk, "issuer": a.get("issuer"),
                        "shares": round(a.get("shares") or 0.0, 0),
                        "value": round(a.get("value") or 0.0, 0),
                        "prev_shares": (round(prev_sh, 0) if prev_sh is not None else None),
                        "change": change, "pct_change": pct, "matched_by": matched_by})
    matches.sort(key=lambda m: -(m["value"] or 0))
    return {"manager": manager, "as_of_positions": len(cur), "matches": matches}


def aggregate_holdings(manager_views, holdings):
    """Invert per-manager views into a per-HOLDING smart-money picture:
    {ticker: {holders:[{manager, shares, change, pct_change}], holder_count, buying, selling}}.
    `buying`/`selling` = # watched managers that added-or-opened / trimmed-or-exited last quarter."""
    by_tk = {}
    for h in holdings or []:
        tk = (h.get("ticker") or "").upper()
        if tk:
            by_tk[tk] = {"ticker": tk, "holders": [], "holder_count": 0, "buying": 0, "selling": 0}
    for mv in manager_views or []:
        mgr = mv.get("manager")
        for m in mv.get("matches") or []:
            tk = m.get("ticker")
            slot = by_tk.get(tk)
            if slot is None:
                slot = {"ticker": tk, "holders": [], "holder_count": 0, "buying": 0, "selling": 0}
                by_tk[tk] = slot
            slot["holders"].append({"manager": mgr, "shares": m.get("shares"),
                                    "value": m.get("value"), "change": m.get("change"),
                                    "pct_change": m.get("pct_change")})
            slot["holder_count"] += 1
            if m.get("change") in ("new", "added"):
                slot["buying"] += 1
            elif m.get("change") in ("trimmed", "exited"):
                slot["selling"] += 1
    for slot in by_tk.values():
        slot["holders"].sort(key=lambda x: -(x["value"] or 0))
    return by_tk


# A small seed of widely-followed managers (CIK → label). User-editable in the app; these are
# defaults, not endorsements. CIKs are the SEC filer numbers for each manager's 13F.
DEFAULT_MANAGERS = {
    "0001067983": "Berkshire Hathaway (Buffett)",
    "0001350694": "Bridgewater Associates",
    "0001649339": "Scion Asset Mgmt (Burry)",
    "0001037389": "Renaissance Technologies",
    "0001656456": "Duquesne Family Office (Druckenmiller)",
}
