# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""scavenge_screen.py -- S1 of the idea scavenger: a quant screen of every stock in the universe, with NO API calls.

Input is SEC's nightly bulk file (companyfacts.zip, one entry per company). Each company's facts go through the same
numbers the app already computes for a holding (edgar._metrics_from_facts), are boiled down to a compact record, and
classified into a ticker state with a plain-English reason. The rules here are fixed numbers on purpose: they decide only
what is worth SPENDING quota on later (Finnhub, Tavily, the LLM). Judgment about upside belongs to the scout stage."""
import datetime
import json
import zipfile

import edgar

STALE_FILING_DAYS = 550          # no filing in ~18 months: dormant, delisted or deregistered
NEW_IPO_DAYS = 365               # first filing inside a year: a first look regardless of the screen
MIN_REVENUE = 1_000_000          # below this there is no operating business to screen yet


def _filed_span(facts):
    """(earliest, latest) filing date seen on the balance-sheet rows, as ISO strings, or (None, None)."""
    try:
        rows = edgar._usd_rows(facts, edgar.TAGS["total_assets"])
    except Exception:
        rows = []
    dates = sorted(str(e.get("filed"))[:10] for e in rows if e.get("filed"))
    return (dates[0], dates[-1]) if dates else (None, None)


def compact(result, facts):
    """Boil edgar's full result down to what the screen and later sleeves need. None when no usable financials."""
    if not result or result.get("available") is False:
        return None
    m, r = result.get("metrics") or {}, result.get("ratios") or {}
    ttm = m.get("ttm") or {}
    tr = m.get("trends") or {}
    sc = result.get("scores") or {}
    pio, alt = sc.get("piotroski_f") or {}, sc.get("altman_z") or {}
    sh, sh_p, _ = edgar._latest(facts, "shares")
    dil = round((sh - sh_p) / sh_p * 100, 2) if (sh is not None and sh_p) else None
    first, last = _filed_span(facts)
    g, cagr = m.get("revenue_growth_pct"), tr.get("revenue_cagr_pct")
    return {
        "revenue": ttm.get("revenue") if ttm.get("revenue") is not None else m.get("revenue"),
        "rev_growth_pct": g, "rev_cagr_pct": cagr,
        "rev_accel_pts": round(g - cagr, 2) if (g is not None and cagr is not None) else None,
        "gross_margin_pct": r.get("gross_margin_pct"), "net_margin_pct": ttm.get("net_margin_pct", r.get("net_margin_pct")),
        "fcf_margin_pct": ttm.get("fcf_margin_pct") if ttm.get("fcf_margin_pct") is not None else r.get("fcf_margin_pct"),
        "debt_to_equity": r.get("debt_to_equity"), "roe_pct": r.get("return_on_equity_pct"),
        "dividend_payout_pct": r.get("dividend_payout_pct"), "fcf_dividend_coverage": r.get("fcf_dividend_coverage"),
        "share_change_pct": dil, "piotroski": pio.get("score"), "piotroski_of": pio.get("of"),
        "altman_zone": alt.get("zone"), "first_filed": first, "last_filed": last,
        "fiscal_year_end": m.get("fiscal_year_end"),
    }


def flags(s1):
    """Sleeve-neutral pass/fail marks on a compact record (None = no data). s1_score counts the passes."""
    def f(v, test):
        return None if v is None else bool(test(v))
    out = {"growth": f(s1.get("rev_growth_pct"), lambda v: v >= 15),
           "cash_positive": f(s1.get("fcf_margin_pct"), lambda v: v >= 0),
           "profitable": f(s1.get("net_margin_pct"), lambda v: v > 0),
           "low_dilution": f(s1.get("share_change_pct"), lambda v: v <= 3),
           "solvent": f(s1.get("debt_to_equity"), lambda v: v <= 2),
           "strong_financials": f(s1.get("piotroski"), lambda v: v >= 6)}
    out["score"] = sum(1 for k, v in out.items() if v)
    return out


def _plus(today, days):
    return (today + datetime.timedelta(days=days)).isoformat()


def classify(row, s1, today, facts_status):
    """-> dict(state, reason, reason_kind, next_check). `facts_status`: 'ok' | 'no_cik' | 'no_facts' | 'no_gaap'."""
    def out(state, kind, reason, days=None):
        return {"state": state, "reason_kind": kind, "reason": reason, "next_check": _plus(today, days) if days else None}
    if row.get("fin_status") not in (None, "", "N"):
        return out("blacklist", "exchange_flag",
                   f"The exchange flags this listing (status {row['fin_status']}: deficient, delinquent or bankrupt).", 90)
    if facts_status == "no_cik":
        return out("reconsider", "no_sec_cik", "No SEC registration found (foreign ordinary share or not an SEC filer).", 180)
    if facts_status == "no_facts":
        return out("reconsider", "no_financials", "SEC has no company financials on file for this CIK yet.", 90)
    if facts_status == "no_gaap":
        return out("reconsider", "not_us_gaap", "Reports under a taxonomy this app does not read yet (for example IFRS).", 180)
    if s1 is None:
        return out("reconsider", "no_financials", "No usable financial statements in the SEC data.", 90)
    last = s1.get("last_filed")
    if last:
        try:
            if (today - datetime.date.fromisoformat(last)).days > STALE_FILING_DAYS:
                return out("blacklist", "dormant", f"No SEC filing since {last}: dormant, delisted or deregistered.", 180)
        except ValueError:
            pass
    first = s1.get("first_filed")
    if first:
        try:
            if (today - datetime.date.fromisoformat(first)).days <= NEW_IPO_DAYS:
                return out("new", "recent_ipo", f"First SEC financials filed {first}: a recent listing that gets a first look.", 30)
        except ValueError:
            pass
    rev = s1.get("revenue")
    if rev is None or rev < MIN_REVENUE:
        return out("reconsider", "pre_revenue", "No meaningful revenue yet (under $1M); watch for the first real sales.", 90)
    g, fcf, nm = s1.get("rev_growth_pct"), s1.get("fcf_margin_pct"), s1.get("net_margin_pct")
    if s1.get("altman_zone") == "distress" and g is not None and g < 0 and fcf is not None and fcf < 0:
        return out("blacklist", "distressed", "Financial distress score, shrinking revenue and burning cash.", 180)
    if g is not None and g < -15 and nm is not None and nm < 0:
        return out("reconsider", "shrinking", f"Revenue down {abs(g):.0f}% and unprofitable; re-check after the next filings.", 90)
    return out("active", "screened", "Passed the basic screen.")


def screen_zip(zip_path, universe, today, progress=None):
    """Stream the bulk file; classify every universe row. Returns (rows_for_ticker_state, stats).
    Rows from a company sharing one CIK (share classes) each get the same financials."""
    by_cik = {}
    for r in universe:
        if r.get("cik"):
            by_cik.setdefault(r["cik"], []).append(r)
    stats = {"companies_in_file": 0, "matched": 0, "parse_errors": 0, "no_gaap": 0}
    results = {}                                  # cik -> (s1, flags, status)
    errored = set()                               # a company we could not read: its row is left untouched, not downgraded
    with zipfile.ZipFile(zip_path) as z:
        for i, name in enumerate(z.namelist()):
            if not name.startswith("CIK") or not name.endswith(".json"):
                continue
            stats["companies_in_file"] += 1
            cik = name[3:13]
            if cik not in by_cik:
                continue
            stats["matched"] += 1
            try:
                with z.open(name) as fh:
                    facts = json.load(fh)
                res = edgar._metrics_from_facts(by_cik[cik][0]["ticker"], cik, facts)
                if res.get("available") is False:
                    results[cik] = (None, None, "no_gaap" if res.get("sec_filer") else "no_facts")
                    stats["no_gaap"] += 1 if res.get("sec_filer") else 0
                else:
                    s1 = compact(res, facts)
                    results[cik] = (s1, flags(s1) if s1 else None, "ok")
            except Exception:
                stats["parse_errors"] += 1
                errored.add(cik)
            if progress and i % 500 == 0:
                progress(i)
    rows = []
    for r in universe:
        if r.get("cik") in errored:
            continue                              # only a successful read may change a ticker's state
        if not r.get("cik"):
            s1, fl, status = None, None, "no_cik"
        elif r["cik"] not in results:
            s1, fl, status = None, None, "no_facts"
        else:
            s1, fl, status = results[r["cik"]]
        c = classify(r, s1, today, status)
        rows.append({**r, **c, "check_set_by": "rule", "s1": s1, "scores": fl})
    return rows, stats
