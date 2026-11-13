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


SCREEN_VERSION = 5               # bump when rules change: the job re-screens once even if SEC's file is unchanged

# Revenue under other us-gaap tags: banks, REITs and insurers do not use the revenue tags edgar looks for, which made the
# first live run call them "pre-revenue" (Cowork RESULT-018 F3). Used only by this screen, so holdings math is unchanged.
ALT_REVENUE_TAGS = ["InterestIncomeOperating", "RevenuesNetOfInterestExpense", "InterestAndDividendIncomeOperating", "InterestAndFeeIncomeLoansAndLeases",
                    "PremiumsEarnedNet", "RealEstateRevenueNet", "OperatingLeasesIncomeStatementLeaseRevenue",
                    "RevenuesExcludingInterestAndDividends", "SalesRevenueServicesNet", "SalesRevenueGoodsNet"]

# Foreign filers on IFRS (20-F / 40-F) keep their numbers under "ifrs-full" in a few currencies.
IFRS = {"revenue": ["Revenue", "RevenueFromContractsWithCustomers", "InsuranceRevenue"],
        "net_income": ["ProfitLoss", "ProfitLossAttributableToOwnersOfParent"],
        "ocf": ["CashFlowsFromUsedInOperatingActivities"],
        "capex": ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", "PurchaseOfPropertyPlantAndEquipment"],
        "assets": ["Assets"], "liabilities": ["Liabilities"], "equity": ["Equity"]}


CASH_TAGS = {"us-gaap": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
             "ifrs-full": ["CashAndCashEquivalents"]}
INVEST_TAGS = ["ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"]
_FORMS = ("10-K", "10-Q", "20-F", "40-F")


def _instant(facts, taxonomy, tag):
    """(end, value) of the newest balance-sheet row for a tag (any annual or quarterly form), else None."""
    node = ((facts.get("facts") or {}).get(taxonomy) or {}).get(tag)
    best = None
    for rows in ((node or {}).get("units") or {}).values():
        for e in rows:
            if e.get("val") is None or not e.get("end") or not str(e.get("form", "")).startswith(_FORMS):
                continue
            k = (str(e["end"]), str(e.get("filed", "")))
            if best is None or k > best[0]:
                best = (k, e["val"])
    return (best[0][0], best[1]) if best else None


def cash_position(facts, taxonomy="us-gaap"):
    """Cash plus short-term investments at the newest balance-sheet date, or None. Investments count only when dated the same day as the cash."""
    cash = None
    for tag in CASH_TAGS.get(taxonomy, []):
        cash = _instant(facts, taxonomy, tag)
        if cash:
            break
    if not cash:
        return None
    total = cash[1]
    for tag in INVEST_TAGS if taxonomy == "us-gaap" else []:
        inv = _instant(facts, taxonomy, tag)
        if inv and inv[0] == cash[0] and inv[1]:
            total += inv[1]
    return total


def runway_from_fcf(cash, fcf):
    """Months of cash at the current yearly free-cash-flow burn; 999 when not burning; None when unknowable. Works for pre-revenue companies."""
    if cash is None or fcf is None:
        return None
    return 999 if fcf >= 0 else round(cash / (-fcf) * 12, 1)


def runway_months(cash, revenue, fcf_margin_pct):
    """999 when free cash flow is not negative (funds itself); months of cash at the current burn otherwise; None if unknowable."""
    if cash is None or revenue is None or fcf_margin_pct is None:
        return None
    if fcf_margin_pct >= 0:
        return 999
    burn = -fcf_margin_pct / 100.0 * revenue
    return round(cash / burn * 12, 1) if burn > 0 else 999


def _days(a, b):
    try:
        return (datetime.date.fromisoformat(str(b)[:10]) - datetime.date.fromisoformat(str(a)[:10])).days
    except ValueError:
        return None


def _annual_rows(facts, taxonomy, tags, forms, flow=True, latest_tag=False):
    """Annual series [(end, value, currency)] oldest->newest, one per end date (latest filing wins), for the first tag that has rows
    (or, with latest_tag, the tag whose series reaches the newest year: a company that switched concepts, such as insurers moving
    to IFRS 17, keeps the old tag's rows frozen at the old year). Flows must span about a year; instants just need an annual form."""
    node_all = (facts.get("facts") or {}).get(taxonomy) or {}
    found = []
    for tag in tags:
        node = node_all.get(tag)
        if not node:
            continue
        best = {}
        for cur, rows in (node.get("units") or {}).items():
            for e in rows:
                if not str(e.get("form", "")).startswith(forms) or not e.get("end") or e.get("val") is None:
                    continue
                if flow:
                    d = _days(e.get("start"), e["end"])
                    if d is None or not 330 <= d <= 400:
                        continue
                k = e["end"]
                if k not in best or str(e.get("filed", "")) > str(best[k][3]):
                    best[k] = (e["end"], e["val"], cur, e.get("filed", ""))
        if best:
            series = [(v[0], v[1], v[2]) for v in sorted(best.values())]
            if not latest_tag:
                return series
            found.append(series)
    if not found:
        return []
    newest = max(f[-1][0] for f in found)
    return next(f for f in found if f[-1][0] == newest)


def alt_revenue(facts):
    """(latest, prior) annual revenue from the alternate tags, or (None, None). Several alternates can exist at once (a mortgage REIT
    has total interest income AND a small real-estate revenue line), so take the LARGEST among the series that reach the newest year."""
    cands = []
    for tag in ALT_REVENUE_TAGS:
        s = _annual_rows(facts, "us-gaap", [tag], ("10-K",))
        if s and s[-1][1]:
            cands.append(s)
    if not cands:
        return None, None
    newest = max(c[-1][0] for c in cands)
    best = max((c for c in cands if c[-1][0] == newest), key=lambda c: c[-1][1])
    return best[-1][1], (best[-2][1] if len(best) > 1 else None)


def filing_profile(facts):
    """(latest filing date under ANY taxonomy, has 20-F/40-F rows). Foreign filers' recent annual reports often show up only as cover-page
    (dei) facts while the financial statements lag, so recency must look at every taxonomy, not just the financial one."""
    latest, foreign = "", False
    for tags in (facts.get("facts") or {}).values():
        for node in tags.values():
            for rows in (node.get("units") or {}).values():
                for e in rows:
                    f = str(e.get("filed") or "")[:10]
                    if f > latest:
                        latest = f
                    if not foreign and str(e.get("form", "")).startswith(("20-F", "40-F")):
                        foreign = True
    return (latest or None), foreign


def other_taxonomy_latest(facts):
    """Latest filing date seen under any taxonomy other than us-gaap/dei (for example ifrs-full), else None."""
    best = None
    for tax, tags in (facts.get("facts") or {}).items():
        if tax in ("us-gaap", "dei"):
            continue
        for node in tags.values():
            for rows in (node.get("units") or {}).values():
                for e in rows:
                    f = str(e.get("filed") or "")[:10]
                    if f and (best is None or f > best):
                        best = f
    return best


def ifrs_compact(facts):
    """Same compact record for a 20-F/40-F filer reporting under IFRS. Percentages are currency-neutral; `revenue` is in
    `currency` (s1['currency']). None when there is no annual revenue to work with."""
    forms = ("20-F", "40-F")

    def last2(key, flow=True):
        s = _annual_rows(facts, "ifrs-full", IFRS[key], forms, flow, latest_tag=True)
        return (s[-1][1], (s[-2][1] if len(s) > 1 else None), s[-1][2], s[-1][0]) if s else (None, None, None, None)
    rev, rev_prev, cur, fy_end = last2("revenue")
    ni = last2("net_income")[0]
    ocf = last2("ocf")[0]
    capex = last2("capex")[0]
    assets = last2("assets", False)[0]
    liab = last2("liabilities", False)[0]
    eq = last2("equity", False)[0]
    fcf = (ocf - abs(capex or 0)) if ocf is not None else None
    if rev is None and all(v is None for v in (ni, ocf, assets)):
        return None                              # nothing annual to read under IFRS
    pct = lambda n, d: round(n / d * 100, 2) if (n is not None and d) else None
    dates = sorted(str(f)[:10] for f in [e.get("filed") for rows in
                   (((facts.get("facts") or {}).get("ifrs-full") or {}).get("Assets") or {}).get("units", {}).values() for e in rows
                   if str(e.get("form", "")).startswith(forms)] if f)
    cash = cash_position(facts, "ifrs-full")
    return {"basis": "ifrs", "currency": cur, "revenue": rev, "cash": cash, "runway_months": runway_from_fcf(cash, fcf), "foreign_filer": True,
            "rev_growth_pct": pct(rev - rev_prev, rev_prev) if (rev is not None and rev_prev) else None, "rev_cagr_pct": None, "rev_accel_pts": None,
            "gross_margin_pct": None, "net_margin_pct": pct(ni, rev), "fcf_margin_pct": pct(fcf, rev),
            "debt_to_equity": round(liab / eq, 2) if (liab is not None and eq) else None,
            "roe_pct": pct(ni, eq), "dividend_payout_pct": None, "fcf_dividend_coverage": None,
            "share_change_pct": None, "piotroski": None, "piotroski_of": None, "altman_zone": None, "current_ratio": None,
            "first_filed": dates[0] if dates else None, "last_filed": dates[-1] if dates else None, "fiscal_year_end": fy_end}


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
    rev = ttm.get("revenue") if ttm.get("revenue") is not None else m.get("revenue")
    used_alt = False
    if rev is None or rev < MIN_REVENUE:                       # banks / REITs / insurers: other revenue tags
        a_rev, a_prev = alt_revenue(facts)
        if a_rev and a_rev >= MIN_REVENUE:
            rev, used_alt = a_rev, True
            g = round((a_rev - a_prev) / a_prev * 100, 2) if a_prev else None
    nm = ttm.get("net_margin_pct", r.get("net_margin_pct"))
    if used_alt and m.get("net_income") is not None:
        nm = round(m["net_income"] / rev * 100, 2)
    fcf_m = ttm.get("fcf_margin_pct") if ttm.get("fcf_margin_pct") is not None else r.get("fcf_margin_pct")
    cash = cash_position(facts)
    return {
        "revenue": rev, "revenue_alt_tags": used_alt, "current_ratio": r.get("current_ratio"),
        "cash": cash, "runway_months": runway_from_fcf(cash, ttm.get("free_cash_flow") if ttm.get("free_cash_flow") is not None else m.get("free_cash_flow")),
        "rev_growth_pct": g, "rev_cagr_pct": cagr,
        "rev_accel_pts": round(g - cagr, 2) if (g is not None and cagr is not None) else None,
        "gross_margin_pct": r.get("gross_margin_pct"), "net_margin_pct": nm,
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


def _enrich_stale(s1, facts, today):
    """For a record whose financials look old: add the latest filing under any taxonomy and whether this is a foreign annual filer."""
    if not s1 or not _stale(s1.get("last_filed"), today):
        return
    latest, foreign = filing_profile(facts)
    s1["last_filed_any"], s1["financials_stale"] = latest, True
    s1["foreign_filer"] = bool(s1.get("foreign_filer") or foreign)


def _stale(last, today):
    try:
        return bool(last) and (today - datetime.date.fromisoformat(last)).days > STALE_FILING_DAYS
    except ValueError:
        return False


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
    last = s1.get("last_filed_any") or s1.get("last_filed")
    if _stale(last, today):
        if s1.get("foreign_filer") or s1.get("basis") == "ifrs":
            return out("reconsider", "stale_foreign", f"Foreign filer: nothing newer than {last} in SEC's data, so these numbers may be out of date.", 90)
        return out("blacklist", "dormant", f"No SEC filing since {last}: dormant, delisted or deregistered.", 180)
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
        cr = s1.get("current_ratio")
        if cr is not None and cr >= 1.5:         # plenty of liquid assets against near-term bills: not a bankruptcy candidate
            return out("reconsider", "burning_cash_liquid",
                       f"Shrinking revenue and burning cash, but liquidity is solid (current ratio {cr:.1f}); watch the burn.", 90)
        return out("blacklist", "distressed", "Financial distress score, shrinking revenue, burning cash and thin liquidity.", 180)
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
                    i1 = ifrs_compact(facts) if res.get("sec_filer") else None
                    if i1:
                        _enrich_stale(i1, facts, today)
                        results[cik] = (i1, flags(i1), "ok")
                        stats["ifrs_read"] = stats.get("ifrs_read", 0) + 1
                    else:
                        results[cik] = (None, None, "no_gaap" if res.get("sec_filer") else "no_facts")
                        stats["no_gaap"] += 1 if res.get("sec_filer") else 0
                else:
                    s1 = compact(res, facts)
                    results[cik] = (s1, flags(s1) if s1 else None, "ok")
                    _enrich_stale(s1, facts, today)
                    if s1 and _stale(s1.get("last_filed"), today) and (other_taxonomy_latest(facts) or "") > (s1.get("last_filed") or ""):
                        # Old us-gaap numbers, newer filings under another taxonomy: a foreign filer that switched to IFRS, not a dormant one.
                        i1 = ifrs_compact(facts)
                        _enrich_stale(i1, facts, today)
                        results[cik] = (i1, flags(i1) if i1 else None, "ok" if i1 else "no_gaap")
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
