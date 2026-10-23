# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
SEC EDGAR / GAAP fundamentals for the Fiduciary.

Pulls company facts (XBRL) from SEC EDGAR, normalizes the messy tag variants into a
standard set of GAAP concepts, and computes core ratios (liquidity, profitability,
solvency, quality of earnings). Read-only public data.

SEC requires a descriptive User-Agent containing a contact email or it returns 403 —
set SEC_USER_AGENT (e.g. "Local AI Fiduciary you@example.com").

Coverage: US SEC-registered operating companies. Foreign-listed micro-caps and
funds/ETFs have no company facts -> returns {"available": False, "reason": ...}.
"""

import os
import time
import datetime
import re
import requests
import xml.etree.ElementTree as ET
import store

# NOTE the `or`, not a get() default (2026-08-08). docker-compose passes
# `SEC_USER_AGENT=${SEC_USER_AGENT}`, so an unset or blank value in .env sets the variable to an
# EMPTY STRING inside the container rather than leaving it absent -- and os.environ.get(k, default)
# returns "" in that case, because the key exists. The app then sent `User-Agent: ""`, SEC replied
# 403, and every EDGAR feature died silently. Confirmed live via /api/edgar-status.
SEC_UA = (os.environ.get("SEC_USER_AGENT") or "").strip() or "Local AI Fiduciary admin@example.com"


def ua_is_placeholder(ua=None):
    """True when SEC_UA is blank or still an example/default string. SEC 403s generic agents, so
    this is the single most likely cause of a dead EDGAR subsystem."""
    s = (SEC_UA if ua is None else ua or "").strip()
    if not s:
        return True
    low = s.lower()
    return ("example.com" in low or "you@" in low or "admin@example" in low
            or "@" not in s)          # SEC requires a contact address in the string
EDGAR_TTL = int(os.environ.get("EDGAR_TTL", "86400"))   # cache fundamentals ~1 day

# How long the DATA stays true once we can no longer refresh it -- distinct from EDGAR_TTL, which
# only says how often we re-ask (see store.cached_fetch). GAAP fundamentals come from 10-Q/10-K
# filings, so a copy pulled last week is the same copy a successful fetch would return today;
# calling that "stale" the moment one request 403s is noise, and noise gets ignored.
EDGAR_STALE_AFTER = int(os.environ.get("EDGAR_STALE_AFTER", "2592000"))    # 30d (quarterly filings)
NPORT_STALE_AFTER = int(os.environ.get("NPORT_STALE_AFTER", "7776000"))    # 90d (monthly, ~60d lag)


def _apply_cache_meta(res, meta):
    """Stamp store.cached_fetch provenance onto a returned dict, for callers that surface it."""
    res["_cache_fetched_at"] = meta.get("fetched_at")
    res["_cache_stale"] = bool(meta.get("stale"))
    res["_cache_fallback"] = bool(meta.get("fallback"))
    res["_cache_error"] = bool(meta.get("error"))
    return res

_CIK = {"ts": 0.0, "map": {}}
# 2026-07-30: fundamentals()/fund_holdings() caching moved onto store.cached_fetch() (disk-
# persisted, survives restarts -- see store.py's "external_cache" table) -- no more bare
# module-level dicts here.

# Curated fund/ETF NET expense ratios (%). Authoritative source: each fund's prospectus /
# fact sheet. A ticker NOT listed here shows "unknown" in the fee audit — just add it.
# (SEC's Risk/Return expense-ratio data lives in bulk prospectus datasets, not a clean
#  per-ticker API, so a small maintained map is the robust, low-friction choice.)
FUND_EXPENSE_RATIOS = {
    # index funds (cheap)
    "FXAIX": 0.015,   # Fidelity 500 Index
    "SFLNX": 0.25,    # Schwab Fundamental US Large Company Index
    "VOO": 0.03, "VTI": 0.03, "VXUS": 0.05, "SCHD": 0.06, "VYM": 0.06,
    "SPY": 0.0945, "QQQ": 0.20, "IVV": 0.03, "SCHB": 0.03,
    # active funds (pricier)
    "FZAHX": 0.60,    # Fidelity Advisor Growth Opportunities Z
    "BTEKX": 0.84,    # BlackRock Technology Opportunities K
}


def fund_expense_ratio(ticker):
    """Net expense ratio (%) for a fund/ETF ticker, or None if not in the curated map.
    Extend FUND_EXPENSE_RATIOS above from the fund's prospectus for full coverage."""
    if not ticker:
        return None
    return FUND_EXPENSE_RATIOS.get(str(ticker).upper().strip())

# standard concept -> ordered candidate US-GAAP XBRL tags (first one present wins)
TAGS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"],
    "cost_of_revenue": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "inventory": ["InventoryNet"],
    "total_assets": ["Assets"],
    "total_liabilities": ["Liabilities"],
    "equity": ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "interest_expense": ["InterestExpense", "InterestExpenseNonoperating", "InterestAndDebtExpense"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities",
                            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
              "PaymentsForCapitalImprovements"],
    "accounts_receivable": ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent"],
    "dividends_paid": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
    "retained_earnings": ["RetainedEarningsAccumulatedDeficit"],
    "shares": ["WeightedAverageNumberOfDilutedSharesOutstanding",
               "WeightedAverageNumberOfSharesOutstandingBasic", "CommonStockSharesOutstanding"],
}


def _headers():
    return {"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"}


class CikMapUnavailable(Exception):
    """SEC's ticker->CIK map could not be loaded, and no previously-loaded copy exists.

    Raised rather than swallowed (2026-08-08). This map is the entry point to EVERY EDGAR call
    in this module, so when it fails silently the whole SEC subsystem degrades into a lie:
    get_cik() returned None for every ticker and fundamentals() reported
    "no SEC CIK (foreign-listed, fund/ETF, or not SEC-registered)" -- a benign, permanent-sounding
    classification -- for household S&P names. Nothing on screen distinguished that from the
    truth, so 28 of 28 briefs were quietly built with no SEC fundamentals at all. The most
    likely trigger is SEC_USER_AGENT still being the placeholder: SEC requires a real declared
    contact string and 403s generic ones."""


def _cik_map():
    """SEC ticker -> zero-padded CIK. Refreshed weekly, and disk-persisted (2026-08-08) so a
    transient SEC 403/timeout can't take the entire EDGAR subsystem down until restart."""
    if _CIK["map"] and (time.time() - _CIK["ts"] < 604800):
        return _CIK["map"]

    def _compute():
        try:
            r = requests.get("https://www.sec.gov/files/company_tickers.json",
                             headers=_headers(), timeout=30)
            r.raise_for_status()
            m = {}
            for row in (r.json() or {}).values():
                t = (row.get("ticker") or "").upper()
                if t:
                    m[t] = str(row.get("cik_str")).zfill(10)
            return m
        except Exception as e:
            _CIK["last_error"] = f"{type(e).__name__}: {e}"
            return {}

    # A map with no entries is never a legitimate answer -- SEC always returns thousands.
    m, meta = store.cached_fetch("sec_cik_map", "all", 604800, _compute, is_ok=lambda v: bool(v),
                                 stale_after=2592000)   # the map changes glacially; 30d
    if not m:
        raise CikMapUnavailable(_CIK.get("last_error") or "SEC ticker map fetch returned nothing")
    _CIK.update(ts=time.time(), map=m, from_cache=bool(meta.get("fallback")))
    return m


def get_cik(ticker):
    """CIK for a ticker, or None if the ticker genuinely isn't in SEC's map.

    Propagates CikMapUnavailable when the MAP ITSELF could not be loaded -- callers must not
    conflate "SEC has no such registrant" with "we couldn't ask SEC"."""
    return _cik_map().get((ticker or "").upper())


def cik_map_status():
    """Health of the SEC ticker map, for diagnostics/UI. Never raises."""
    try:
        m = _cik_map()
        return {"ok": True, "entries": len(m), "served_from_cache": bool(_CIK.get("from_cache")),
                "loaded_at": _CIK.get("ts")}
    except CikMapUnavailable as e:
        return {"ok": False, "entries": 0, "error": str(e)}


_EXCH = {"ts": 0.0, "map": {}}    # ticker -> {name, exchange}


def _exchange_map():
    """SEC ticker -> {name, exchange} from company_tickers_exchange.json (stocks AND exchange-listed
    ETFs, with a name). Weekly, disk-persisted, same pattern as _cik_map."""
    if _EXCH["map"] and (time.time() - _EXCH["ts"] < 604800):
        return _EXCH["map"]

    def _compute():
        try:
            r = requests.get("https://www.sec.gov/files/company_tickers_exchange.json",
                             headers=_headers(), timeout=30)
            r.raise_for_status()
            j = r.json() or {}
            f = j.get("fields") or ["cik", "name", "ticker", "exchange"]
            ni, ti, xi = f.index("name"), f.index("ticker"), f.index("exchange")
            return {(row[ti] or "").upper(): {"name": row[ni], "exchange": row[xi]}
                    for row in (j.get("data") or []) if row[ti]}
        except Exception as e:
            _EXCH["last_error"] = f"{type(e).__name__}: {e}"
            return {}

    m, _meta = store.cached_fetch("sec_exchange_map", "all", 604800, _compute,
                                  is_ok=lambda v: bool(v), stale_after=2592000)
    if not m:
        raise CikMapUnavailable(_EXCH.get("last_error") or "SEC exchange ticker map fetch returned nothing")
    _EXCH.update(ts=time.time(), map=m)
    return m


def ticker_profile(ticker):
    """What SEC can tell us about a ticker, for auto-filling a holdings row:
    {found, ticker, name, type ('Stock'|'ETF'|'Mutual Fund'), exchange, source} or {found: False, ...}.
    Funds: the series name comes from the fund's latest N-PORT (cached, shared with look-through);
    stocks: the SEC registrant name (the same map that fixed VOYG). Type: a ticker in SEC's fund map is a
    Mutual Fund when it is not exchange-listed and looks like one (5 letters ending in X), else an ETF.
    Never raises; an SEC outage returns {found: False, error: ...} so the caller leaves the row alone."""
    tkr = (ticker or "").upper().strip()
    if not tkr or not re.fullmatch(r"[A-Z0-9.\-]{1,10}", tkr):
        return {"found": False, "ticker": tkr}
    try:
        fund = fund_series(tkr)
        ex = _exchange_map().get(tkr)
        if fund:
            name = None
            try:
                name = (fund_holdings(tkr, top=1) or {}).get("series_name")
            except Exception:
                pass
            name = name or (ex or {}).get("name")
            listed = bool(ex and ex.get("exchange"))
            is_mf = (not listed) and bool(re.fullmatch(r"[A-Z]{4}X", tkr)) and "ETF" not in (name or "").upper()
            return {"found": True, "ticker": tkr, "name": name, "exchange": (ex or {}).get("exchange"),
                    "type": "Mutual Fund" if is_mf else "ETF", "source": "SEC fund registry"}
        if ex:
            nm = ex.get("name") or ""
            etf = bool(re.search(r"\bETF\b|^(ISHARES|SPDR|PROSHARES|VANGUARD)\b", nm.upper()))
            return {"found": True, "ticker": tkr, "name": nm, "exchange": ex.get("exchange"),
                    "type": "ETF" if etf else "Stock", "source": "SEC company tickers"}
        cik = get_cik(tkr)
        if cik:
            return {"found": True, "ticker": tkr, "name": None, "type": "Stock", "source": "SEC company tickers"}
    except CikMapUnavailable as e:
        return {"found": False, "ticker": tkr, "error": f"SEC lookup unavailable ({e})"}
    except Exception as e:
        return {"found": False, "ticker": tkr, "error": f"{type(e).__name__}"}
    return {"found": False, "ticker": tkr}


# ---------------- Fund look-through via SEC N-PORT (free, authoritative) ----------------
# Mutual funds and ETFs file NPORT-P monthly, disclosing their portfolio holdings (public,
# with a ~60-day lag). We resolve a fund TICKER -> registrant CIK + seriesId, pull the latest
# NPORT-P for that series, and parse its holdings so the portfolio's TRUE single-name exposure
# (direct positions + positions held inside funds) can be aggregated.
_MF = {"ts": 0.0, "map": {}}      # ticker -> {cik, seriesId, classId}


def _ln(tag):
    """Local (namespace-stripped) tag name."""
    return tag.rsplit("}", 1)[-1]


def _sub_text(elem, name):
    """First descendant with local-name == name that has text."""
    for e in elem.iter():
        if _ln(e.tag) == name and (e.text or "").strip():
            return e.text.strip()
    return None


def _sub_attr(elem, name, attr="value"):
    """First descendant with local-name == name that carries the given attribute."""
    for e in elem.iter():
        if _ln(e.tag) == name and e.get(attr):
            return e.get(attr)
    return None


def _mf_map():
    """SEC ticker -> {cik, seriesId, classId} for mutual funds / ETFs. Refreshed weekly,
    disk-persisted (2026-08-08) for the same reason as _cik_map above."""
    if _MF["map"] and (time.time() - _MF["ts"] < 604800):
        return _MF["map"]

    def _compute():
        try:
            r = requests.get("https://www.sec.gov/files/company_tickers_mf.json",
                             headers=_headers(), timeout=30)
            r.raise_for_status()
            j = r.json() or {}
        except Exception as e:
            _MF["last_error"] = f"{type(e).__name__}: {e}"
            return {}
        fields = j.get("fields") or ["cik", "seriesId", "classId", "symbol"]
        idx = {name: i for i, name in enumerate(fields)}
        ci, si, li, yi = (idx.get("cik", 0), idx.get("seriesId", 1),
                          idx.get("classId", 2), idx.get("symbol", 3))
        m = {}
        for row in (j.get("data") or []):
            try:
                sym = (row[yi] or "").upper().strip()
                if not sym:
                    continue
                # first class per symbol wins (symbols are unique in this file anyway)
                m.setdefault(sym, {"cik": str(row[ci]).zfill(10),
                                   "seriesId": row[si], "classId": row[li]})
            except Exception:
                continue
        return m

    m, _meta = store.cached_fetch("sec_mf_map", "all", 604800, _compute,
                                  is_ok=lambda v: bool(v), stale_after=2592000)
    if not m:
        raise CikMapUnavailable(_MF.get("last_error") or "SEC fund ticker map fetch returned nothing")
    _MF.update(ts=time.time(), map=m)
    return m


def fund_series(ticker):
    """Resolve a fund/ETF ticker to {cik, seriesId, classId}, or None if not a registered fund.
    Propagates CikMapUnavailable when the MAP could not be loaded (see get_cik) -- 'not a
    registered fund' and 'we could not ask SEC' must not collapse into the same None."""
    return _mf_map().get((ticker or "").upper().strip())


def fund_holdings_cached(ticker):
    """The last stored look-through result for a fund, WITHOUT any network call (None if never fetched). For display paths
    that must not trigger SEC requests (HOLDINGS table)."""
    try:
        hit = store.ext_cache_get("edgar_fund_holdings", (ticker or "").upper().strip())
    except Exception:
        return None
    if not hit:
        return None
    fetched_at, val = hit
    return dict(val, _cache_fetched_at=fetched_at) if isinstance(val, dict) else None


def _nport_by_series(series_id):
    """Fetch the latest NPORT-P for a specific fund SERIES directly via EDGAR browse-edgar
    (which accepts an S000... series id in the CIK field). One request, resolves the exact series
    regardless of how many series the registrant files — the robust path for big fund families.
    Returns (xml_text, accession) or None."""
    if not series_id:
        return None
    try:
        r = requests.get("https://www.sec.gov/cgi-bin/browse-edgar",
                         params={"action": "getcompany", "CIK": series_id, "type": "NPORT-P",
                                 "dateb": "", "owner": "include", "count": "5", "output": "atom"},
                         headers=_headers(), timeout=30)
        if r.status_code >= 400:
            return None
        txt = r.text or ""
    except Exception:
        return None
    m = re.search(r"/Archives/edgar/data/(\d+)/(\d+)/", txt)   # first (newest) filing folder
    if not m:
        return None
    cik_i, accnd = m.group(1), m.group(2)
    try:
        rr = requests.get(f"https://www.sec.gov/Archives/edgar/data/{cik_i}/{accnd}/primary_doc.xml",
                          headers=_headers(), timeout=30)
        if rr.status_code >= 400:
            return None
        return (rr.text, accnd)
    except Exception:
        return None


def _latest_nport(cik, series_id):
    """Return (xml_text, accession) for the most recent NPORT-P matching series_id, else None."""
    # Preferred: resolve the exact series directly (works even for huge multi-series trusts).
    direct = _nport_by_series(series_id)
    if direct and (not series_id or series_id in direct[0]):
        return direct
    try:
        r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                         headers=_headers(), timeout=30)
        if r.status_code >= 400:
            return None
        recent = ((r.json() or {}).get("filings") or {}).get("recent") or {}
    except Exception:
        return None
    forms = recent.get("form") or []
    accs = recent.get("accessionNumber") or []
    dates = recent.get("filingDate") or []
    cands = sorted(((dates[i] if i < len(dates) else "", accs[i])
                    for i, f in enumerate(forms) if f in ("NPORT-P", "NPORT-P/A")),
                   reverse=True)
    first_doc = None
    # Scan a wide window: large fund families (Fidelity/Schwab) file many series' NPORT-P
    # filings each month, so the target series may be several filings deep.
    for dt, acc in cands[:25]:
        accnd = acc.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accnd}/primary_doc.xml"
        try:
            rr = requests.get(url, headers=_headers(), timeout=30)
            if rr.status_code >= 400:
                continue
            xml = rr.text
        except Exception:
            continue
        if first_doc is None:
            first_doc = (xml, acc)
        if not series_id:
            return (xml, acc)          # nothing to disambiguate -> newest filing
        if series_id in xml:
            return (xml, acc)          # correct series matched
    # Series was given but not matched in the scan window: return None (unresolved) rather than
    # a WRONG series' holdings — better to show the fund as not-looked-through than to mislabel
    # someone else's portfolio as this fund's.
    return None if series_id else first_doc


_NAME_STOP = {"THE", "INC", "CORP", "CO", "FUND", "FUNDS", "TRUST", "CLASS", "SERIES", "PORTFOLIO",
              "INDEX", "INSTITUTIONAL", "INVESTOR", "SHARES", "ADMIRAL", "ETF"}


def _sig_tokens(name):
    """Significant (>2-char, non-boilerplate) tokens of a fund name, for match verification."""
    return [t for t in re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper()).split()
            if len(t) > 2 and t not in _NAME_STOP]


def _nport_by_name(name):
    """Fallback resolver for funds NOT in the ticker map (e.g. some share classes): find the fund's
    latest NPORT-P via EDGAR full-text search on its NAME, then verify the filing actually matches
    (most of the name's significant tokens must appear) so we never attach the wrong fund's holdings.
    Returns (xml_text, accession) or None."""
    toks = _sig_tokens(name)
    if len(toks) < 2:                      # too generic to disambiguate safely
        return None
    try:
        r = requests.get("https://efts.sec.gov/LATEST/search-index",
                         params={"q": " ".join(toks), "forms": "NPORT-P"},
                         headers=_headers(), timeout=30)
        if r.status_code >= 400:
            return None
        hits = ((r.json() or {}).get("hits") or {}).get("hits") or []
    except Exception:
        return None
    # full-text search ranks by relevance; we want the MOST RECENT verified filing
    hits.sort(key=lambda h: (h.get("_source") or {}).get("file_date", ""), reverse=True)
    for h in hits[:8]:
        _id = h.get("_id") or ""
        src = h.get("_source") or {}
        ciks = src.get("ciks") or []
        acc = _id.split(":")[0] if _id else ""
        if not (ciks and acc):
            continue
        accnd = acc.replace("-", "")
        cik_i = int(str(ciks[0]).lstrip("0") or "0")
        try:
            rr = requests.get(f"https://www.sec.gov/Archives/edgar/data/{cik_i}/{accnd}/primary_doc.xml",
                              headers=_headers(), timeout=30)
            if rr.status_code >= 400:
                continue
            xml = rr.text
        except Exception:
            continue
        up = xml.upper()
        present = sum(1 for t in toks if t in up)     # verify the filing is actually this fund
        if present >= max(2, len(toks) - 1):
            return (xml, acc)
    return None


def _parse_nport(xml, acc, tkr, top, series=None):
    """Parse an NPORT-P primary_doc.xml into the fund_holdings result dict."""
    try:
        root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    except Exception:
        return {"ticker": tkr, "resolved": False, "reason": "NPORT XML parse error"}
    as_of = series_name = None
    gi = next((e for e in root.iter() if _ln(e.tag) == "genInfo"), None)
    if gi is not None:
        as_of = _sub_text(gi, "repPdDate") or _sub_text(gi, "repPdEnd")
        series_name = _sub_text(gi, "seriesName")
    holdings = []
    for sec in (e for e in root.iter() if _ln(e.tag) == "invstOrSec"):
        name = _sub_text(sec, "name")
        cusip = _sub_text(sec, "cusip")
        pct = _sub_text(sec, "pctVal")
        val = _sub_text(sec, "valUSD")
        utkr = _sub_attr(sec, "ticker") or _sub_attr(sec, "tickerSymbol")
        asset_cat = _sub_text(sec, "assetCat")
        try:
            pct = round(float(pct), 4) if pct is not None else None
        except Exception:
            pct = None
        try:
            val = round(float(val), 2) if val is not None else None
        except Exception:
            val = None
        if name is None and cusip is None and utkr is None:
            continue
        holdings.append({"name": name, "cusip": cusip,
                         "ticker": (utkr.upper().strip() if utkr else None),
                         "pct": pct, "value": val, "asset_cat": asset_cat})
    holdings.sort(key=lambda h: -(h["pct"] or 0))
    total_pct = round(sum(h["pct"] or 0 for h in holdings), 2)
    with_ticker = sum(1 for h in holdings if h["ticker"])
    return {"ticker": tkr, "resolved": True, "as_of": as_of, "series": series,
            "series_name": series_name, "accession": acc, "count": len(holdings),
            "ticker_coverage_pct": round(with_ticker / len(holdings) * 100, 1) if holdings else 0,
            "total_pct": total_pct, "holdings": holdings[:top]}


def fund_holdings(ticker, ttl=604800, top=600, name=None):
    """Underlying holdings of a fund/ETF from its latest SEC NPORT-P. Resolves the fund by ticker
    (SEC ticker map), or — if the ticker isn't mapped (some share classes aren't) — by NAME via
    EDGAR full-text search with match verification. Returns {resolved, as_of, count, total_pct,
    holdings:[...], _cache_fetched_at, _cache_stale}. Cached ~7 days (disk-persisted via
    store.cached_fetch, 2026-07-30 -- survives a restart); resolved=False with a reason if it
    can't be built."""
    tkr = (ticker or "").upper().strip()
    if not tkr:
        return None

    def _compute():
        try:
            info = fund_series(tkr)
        except CikMapUnavailable as e:
            return {"ticker": tkr, "resolved": False, "error": True,
                    "reason": f"SEC fund ticker map unavailable ({e})"}
        if info:
            cik, series = info["cik"], info.get("seriesId")
            doc = _latest_nport(cik, series)
            if not doc:
                return {"ticker": tkr, "resolved": False, "cik": cik, "series": series,
                        "reason": "no NPORT-P filing found"}
            return _parse_nport(doc[0], doc[1], tkr, top, series)
        # ticker not in SEC map -> try resolving by fund name (verified)
        if name:
            doc = _nport_by_name(name)
            if doc:
                return _parse_nport(doc[0], doc[1], tkr, top)
            return {"ticker": tkr, "resolved": False,
                    "reason": "not in SEC ticker map; name search found no verified NPORT-P"}
        return {"ticker": tkr, "resolved": False,
                "reason": "not in SEC mutual-fund/ETF ticker map"}

    res, meta = store.cached_fetch(
        "edgar_fund_holdings", tkr, ttl, _compute,
        is_ok=lambda r: bool(r.get("resolved")),
        # "this ticker isn't a registered fund / has filed no NPORT-P" is an ANSWER, not an
        # outage -- it must never light a staleness flag, and it never resolves if it does.
        is_empty=lambda r: bool(isinstance(r, dict) and not r.get("resolved") and not r.get("error")),
        stale_after=NPORT_STALE_AFTER)
    res = dict(res)
    _apply_cache_meta(res, meta)
    return res


# 8-K item codes -> human labels (a filed 8-K IS a material-event disclosure)
EIGHTK_ITEMS = {
    "1.01": "Material agreement entered", "1.02": "Material agreement terminated",
    "1.05": "Cybersecurity incident", "2.01": "Acquisition/disposition of assets",
    "2.02": "Results of operations (earnings)", "2.03": "Material financial obligation",
    "2.05": "Costs from exit/disposal", "2.06": "Material impairment", "3.01": "Delisting/listing notice",
    "3.02": "Unregistered equity sale", "4.01": "Auditor change", "4.02": "Non-reliance on prior financials",
    "5.01": "Change in control", "5.02": "Officer/director change", "5.03": "Bylaw/charter amendment",
    "5.07": "Shareholder vote results", "7.01": "Reg FD disclosure", "8.01": "Other material event",
    "9.01": "Financial statements & exhibits"}


def recent_8k(ticker, days=30, limit=4):
    """Recent 8-K (material-event) filings for a ticker from SEC EDGAR submissions — free, authoritative."""
    try:
        cik = get_cik(ticker)
    except CikMapUnavailable:
        return []      # caller's contract is a plain list; the map outage surfaces via fundamentals()
    if not cik:
        return []
    try:
        r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=_headers(), timeout=30)
        if r.status_code >= 400:
            return []
        recent = ((r.json() or {}).get("filings") or {}).get("recent") or {}
    except Exception:
        return []
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accs = recent.get("accessionNumber") or []
    items = recent.get("items") or []
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    out = []
    for i, f in enumerate(forms):
        if f != "8-K":
            continue
        dt = dates[i] if i < len(dates) else ""
        if dt and dt < cutoff:
            continue
        acc = accs[i] if i < len(accs) else ""
        itm = items[i] if i < len(items) else ""
        labels = [EIGHTK_ITEMS.get(x.strip(), x.strip()) for x in (itm.split(",") if itm else []) if x.strip()]
        accnd = acc.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accnd}/{acc}-index.htm" if acc else None
        out.append({"date": dt, "items": itm, "labels": labels, "url": url})
        if len(out) >= limit:
            break
    return out


def _fetch_form4_xml(cik_i, accnd):
    """Fetch the raw ownership XML for one Form 4 accession folder (via its index.json).
    Returns the XML text of the ownershipDocument, or None. Skips the xslF345 rendered variant."""
    base = f"https://www.sec.gov/Archives/edgar/data/{cik_i}/{accnd}"
    try:
        idx = requests.get(f"{base}/index.json", headers=_headers(), timeout=30)
        if idx.status_code >= 400:
            return None
        items = ((idx.json() or {}).get("directory") or {}).get("item") or []
    except Exception:
        return None
    names = [it.get("name") for it in items
             if str(it.get("name", "")).lower().endswith(".xml")
             and not str(it.get("name", "")).lower().startswith("xsl")]
    for nm in names:
        try:
            rr = requests.get(f"{base}/{nm}", headers=_headers(), timeout=30)
            if rr.status_code >= 400:
                continue
            if "ownershipDocument" in (rr.text or ""):
                return rr.text
        except Exception:
            continue
    return None


def recent_form4(ticker, days=120, limit=16):
    """Recent Form 4 (insider-transaction) filings for a ticker's issuer, as raw ownership XML.
    Uses EDGAR browse-edgar (atom, owner='only' → ownership forms) to list the issuer's Form 4
    filings, then fetches each filing's ownershipDocument XML. Returns a list of
    {date, accession, xml} newest-first, bounded by `limit` and `days`. Free, authoritative public
    data. Empty list if the ticker has no CIK or on any fetch failure (defensive throughout)."""
    try:
        cik = get_cik(ticker)
    except CikMapUnavailable:
        return []
    if not cik:
        return []
    try:
        r = requests.get("https://www.sec.gov/cgi-bin/browse-edgar",
                         params={"action": "getcompany", "CIK": cik, "type": "4",
                                 "dateb": "", "owner": "only", "count": str(max(limit * 2, 20)),
                                 "output": "atom"},
                         headers=_headers(), timeout=30)
        if r.status_code >= 400:
            return []
        feed = r.text or ""
    except Exception:
        return []
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    out = []
    for chunk in feed.split("<entry")[1:]:
        mtype = re.search(r"<filing-type>([^<]+)</filing-type>", chunk)
        if not mtype or mtype.group(1).strip() != "4":
            continue
        mdate = re.search(r"<filing-date>(\d{4}-\d{2}-\d{2})</filing-date>", chunk)
        dt = mdate.group(1) if mdate else ""
        if dt and dt < cutoff:
            continue
        mfold = re.search(r"/Archives/edgar/data/(\d+)/(\d+)/", chunk)
        if not mfold:
            continue
        xml = _fetch_form4_xml(mfold.group(1), mfold.group(2))
        if xml:
            out.append({"date": dt, "accession": mfold.group(2), "xml": xml})
        if len(out) >= limit:
            break
    return out


def _fetch_13f_infotable(cik_i, accnd):
    """Fetch the 13F-HR INFORMATION TABLE xml for one accession folder (via index.json). The
    information table is the .xml doc containing <infoTable> rows (not primary_doc.xml / xsl).
    Returns the XML text or None."""
    base = f"https://www.sec.gov/Archives/edgar/data/{cik_i}/{accnd}"
    try:
        idx = requests.get(f"{base}/index.json", headers=_headers(), timeout=30)
        if idx.status_code >= 400:
            return None
        items = ((idx.json() or {}).get("directory") or {}).get("item") or []
    except Exception:
        return None
    names = [it.get("name") for it in items
             if str(it.get("name", "")).lower().endswith(".xml")
             and not str(it.get("name", "")).lower().startswith("xsl")
             and str(it.get("name", "")).lower() != "primary_doc.xml"]
    for nm in names:
        try:
            rr = requests.get(f"{base}/{nm}", headers=_headers(), timeout=30)
            if rr.status_code >= 400:
                continue
            if "infoTable" in (rr.text or ""):
                return rr.text
        except Exception:
            continue
    return None


def recent_13f(cik, n=2):
    """The latest n 13F-HR information-table XMLs for a manager's CIK, newest first, as
    [{date, accession, xml}]. Two filings lets the caller diff quarter-over-quarter. Free EDGAR
    data; [] if the CIK has no 13F-HR filings or on any fetch failure (defensive)."""
    cik = str(cik).zfill(10) if cik else None
    if not cik:
        return []
    try:
        r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                         headers=_headers(), timeout=30)
        if r.status_code >= 400:
            return []
        recent = ((r.json() or {}).get("filings") or {}).get("recent") or {}
    except Exception:
        return []
    forms = recent.get("form") or []
    accs = recent.get("accessionNumber") or []
    dates = recent.get("filingDate") or []
    cands = sorted(((dates[i] if i < len(dates) else "", accs[i])
                    for i, f in enumerate(forms) if f in ("13F-HR", "13F-HR/A")),
                   reverse=True)
    out = []
    for dt, acc in cands:
        xml = _fetch_13f_infotable(int(cik), acc.replace("-", ""))
        if xml:
            out.append({"date": dt, "accession": acc, "xml": xml})
        if len(out) >= n:
            break
    return out


def _annual(facts, tags):
    """For the first matching tag, return its annual (10-K/FY) series as
    [(end_date, value), ...] oldest->newest, deduped by end (latest filing wins)."""
    us = (facts.get("facts") or {}).get("us-gaap") or {}
    for tag in tags:
        node = us.get(tag)
        if not node:
            continue
        units = node.get("units") or {}
        rows = units.get("USD") or (next(iter(units.values()), []) if units else [])
        ann = [e for e in rows if str(e.get("form", "")).startswith("10-K") and e.get("fp") == "FY"]
        if not ann:
            continue
        by_end = {}
        for e in ann:
            k = e.get("end")
            if not k:
                continue
            if k not in by_end or str(e.get("filed", "")) > str(by_end[k].get("filed", "")):
                by_end[k] = e
        series = [(e["end"], e.get("val")) for e in sorted(by_end.values(), key=lambda x: x.get("end", ""))]
        if series:
            return series
    return []


def _latest(facts, key):
    """(latest_value, prior_year_value, fiscal_year_end) for a GAAP concept."""
    s = _annual(facts, TAGS[key])
    if not s:
        return None, None, None
    end, val = s[-1]
    prev = s[-2][1] if len(s) >= 2 else None
    return val, prev, end


def _series(facts, key, n=5):
    """Last n annual values for a concept: [{fy, val}, ...] oldest->newest."""
    s = _annual(facts, TAGS[key])
    return [{"fy": e[0][:4], "val": e[1]} for e in s[-n:]]


def _cagr(series):
    """Compound annual growth rate (%) across a {fy,val} series (positive endpoints only)."""
    if len(series) < 2:
        return None
    a, b, yrs = series[0]["val"], series[-1]["val"], len(series) - 1
    if a and b and a > 0 and b > 0 and yrs > 0:
        return round(((b / a) ** (1 / yrs) - 1) * 100, 2)
    return None


def _companyfacts(cik):
    r = requests.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
                     headers=_headers(), timeout=45)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def fundamentals(ticker, ttl=None):
    """GAAP fundamentals + ratios for a ticker (cached, disk-persisted via store.cached_fetch,
    2026-07-30 -- survives a restart), or {'available': False, 'reason': ...}. Also carries
    `_cache_fetched_at`/`_cache_stale` so a caller that renders this to the user can show when the
    data was actually retrieved (RESUME-TRACKER "last-known-good cache" item, Part 2)."""
    key = (ticker or "").upper()
    ttl = EDGAR_TTL if ttl is None else ttl
    res, meta = store.cached_fetch(
        "edgar_fundamentals", key, ttl, lambda: _compute(key),
        is_ok=lambda r: r.get("available") is not False,
        # available=False WITHOUT error=True means SEC genuinely has no us-gaap company facts for
        # this security (ETF, fund, foreign private issuer). That is a correct, permanent answer.
        # It used to be scored as a fetch failure, which is why every fund and ADR in the book
        # carried a red STALE chip on every run with no way to ever clear it.
        is_empty=lambda r: bool(isinstance(r, dict) and r.get("available") is False
                                and not r.get("error")),
        stale_after=EDGAR_STALE_AFTER)
    res = dict(res)
    _apply_cache_meta(res, meta)
    return res


def _d(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def _usd_rows(facts, tags):
    """Merged USD unit rows across ALL candidate tags for a concept, deduped by (start,end).
    Companies often report annual figures under one revenue/income tag and interim (10-Q) YTD
    figures under a different tag variant; merging lets the TTM bridge find both. Earlier-listed
    tag wins on conflict; within the same tag the latest filing wins."""
    us = (facts.get("facts") or {}).get("us-gaap") or {}
    best = {}
    for prio, tag in enumerate(tags):
        node = us.get(tag)
        if not node:
            continue
        units = node.get("units") or {}
        rows = units.get("USD") or (next(iter(units.values()), []) if units else [])
        for e in rows:
            k = (e.get("start"), e.get("end"))
            cur = best.get(k)
            if cur is None or prio < cur[0] or (prio == cur[0] and
                                                str(e.get("filed", "")) > str(cur[1].get("filed", ""))):
                best[k] = (prio, e)
    return [e for _, e in best.values()]


def _ttm_val(facts, tags):
    """Trailing-twelve-month sum for a FLOW concept (revenue, net income, cash flows).
    TTM = latest full FY (10-K) + current-year YTD interim - prior-year same-length YTD,
    which bridges the un-filed standalone Q4. Falls back to the latest annual when no
    newer interim exists; returns None when it can't bridge reliably (never guesses).
    This keeps a single trough/peak fiscal year from misrepresenting current profitability."""
    rows = _usd_rows(facts, tags)
    if not rows:
        return None
    ann = {}
    for e in rows:
        if str(e.get("form", "")).startswith("10-K") and e.get("fp") == "FY" and e.get("end"):
            k = e["end"]
            if k not in ann or str(e.get("filed", "")) > str(ann[k].get("filed", "")):
                ann[k] = e
    if not ann:
        return None
    a = max(ann.values(), key=lambda e: e["end"])
    a_end, a_val = _d(a["end"]), a.get("val")
    if a_end is None or a_val is None:
        return None
    cur = []
    for e in rows:
        s, en = _d(e.get("start")), _d(e.get("end"))
        if not s or not en or en <= a_end:
            continue
        if (en - s).days >= 350:                  # skip another full-year span
            continue
        if abs((s - a_end).days) <= 12:           # YTD interim starting at the new fiscal year
            cur.append((en, (en - s).days, e.get("val")))
    if not cur:
        return round(a_val, 2)                    # nothing newer filed -> TTM == latest annual
    c_end, c_dur, c_val = max(cur, key=lambda x: x[0])
    if c_val is None:
        return None
    prior = []
    for e in rows:
        s, en = _d(e.get("start")), _d(e.get("end"))
        if not s or not en:
            continue
        if abs((en - s).days - c_dur) <= 25 and en < c_end and abs((c_end - en).days - 365) <= 35:
            prior.append((en, e.get("val")))
    if not prior:
        return None
    p_val = max(prior, key=lambda x: x[0])[1]
    if p_val is None:
        return None
    return round(a_val + c_val - p_val, 2)


def _compute(ticker):
    # `error: True` marks a FETCH FAILURE, as opposed to SEC legitimately having nothing for this
    # security. Both shapes say available=False, but only the former should ever raise a flag to
    # the user -- see is_error()/is_absent() below and store.cached_fetch's classification.
    try:
        cik = get_cik(ticker)
    except CikMapUnavailable as e:
        return {"ticker": ticker, "available": False, "error": True,
                "reason": f"SEC ticker map unavailable ({e}) — EDGAR could not be queried at all. "
                          f"Check SEC_USER_AGENT: SEC requires a real declared contact string."}
    if not cik:
        return {"ticker": ticker, "available": False,
                "reason": "no SEC CIK (foreign-listed, fund/ETF, or not SEC-registered)"}
    try:
        facts = _companyfacts(cik)
    except Exception as e:
        return {"ticker": ticker, "available": False, "error": True,
                "reason": f"SEC fetch error: {e}"}
    if not facts:
        return {"ticker": ticker, "available": False, "reason": "no SEC company facts"}
    # 2026-08-06 diagnostic split. Every extractor in this module reads ONLY the `us-gaap`
    # taxonomy (see _latest/_merged_rows). A company that files with the SEC under IFRS -- a
    # foreign private issuer on a 20-F/40-F, common for the Canadian and Australian listings in
    # this portfolio -- has a perfectly valid CIK and real companyfacts, just under `ifrs-full`.
    # Before this, that case fell through to a generic empty result and the UI rendered
    # "GAAP: not SEC-filed", which is FLATLY WRONG: the company does file with the SEC, we just
    # can't read its taxonomy. Distinguishing the two matters because they have different fixes
    # (nothing to do vs. add IFRS tag mappings) and because telling a user a real filer doesn't
    # file is the kind of quiet inaccuracy that erodes trust in every other number on the page.
    _tax = (facts.get("facts") or {})
    if not _tax.get("us-gaap"):
        _other = sorted(k for k in _tax.keys() if k != "dei")
        return {"ticker": ticker, "available": False, "cik": cik,
                "sec_filer": True, "taxonomies": _other,
                "reason": ("SEC filer, but reports under " + (", ".join(_other) or "an unrecognized taxonomy")
                           + " — this module only parses us-gaap, so no ratios can be computed"
                           if _other else "SEC filer, but companyfacts contains no financial taxonomy")}

    rev, rev_prev, fy_end = _latest(facts, "revenue")
    cor = _latest(facts, "cost_of_revenue")[0]
    gross = _latest(facts, "gross_profit")[0]
    op_inc = _latest(facts, "operating_income")[0]
    ni = _latest(facts, "net_income")[0]
    ca = _latest(facts, "current_assets")[0]
    cl = _latest(facts, "current_liabilities")[0]
    inv = _latest(facts, "inventory")[0]
    liab = _latest(facts, "total_liabilities")[0]
    equity = _latest(facts, "equity")[0]
    int_exp = _latest(facts, "interest_expense")[0]
    ocf = _latest(facts, "operating_cash_flow")[0]
    assets = _latest(facts, "total_assets")[0]
    capex = _latest(facts, "capex")[0]
    ar = _latest(facts, "accounts_receivable")[0]
    div_paid = _latest(facts, "dividends_paid")[0]
    fcf = round(ocf - capex, 2) if (ocf is not None and capex is not None) else None

    # TTM (trailing-twelve-month) so a single trough/peak fiscal year doesn't mislead
    ttm_rev = _ttm_val(facts, TAGS["revenue"])
    ttm_ni = _ttm_val(facts, TAGS["net_income"])
    ttm_ocf = _ttm_val(facts, TAGS["operating_cash_flow"])
    ttm_capex = _ttm_val(facts, TAGS["capex"])
    ttm_fcf = round(ttm_ocf - ttm_capex, 2) if (ttm_ocf is not None and ttm_capex is not None) else None

    if gross is None and rev is not None and cor is not None:
        gross = rev - cor

    def _pct(n, d):
        return round(n / d * 100, 2) if (n is not None and d) else None

    def _ratio(n, d):
        return round(n / d, 2) if (n is not None and d) else None

    metrics = {
        "fiscal_year_end": fy_end,
        "revenue": rev,
        "revenue_growth_pct": _pct(rev - rev_prev, rev_prev) if (rev is not None and rev_prev) else None,
        "net_income": ni,
        "operating_cash_flow": ocf,
        "free_cash_flow": fcf,                                   # OCF - capex
        "capex": capex,
        "total_assets": assets,
        "stockholders_equity": equity,
        "ttm": {"revenue": ttm_rev, "net_income": ttm_ni, "free_cash_flow": ttm_fcf,
                "net_margin_pct": _pct(ttm_ni, ttm_rev), "fcf_margin_pct": _pct(ttm_fcf, ttm_rev)},
        "trends": {"revenue": _series(facts, "revenue"),
                   "revenue_cagr_pct": _cagr(_series(facts, "revenue")),
                   "net_income": _series(facts, "net_income")},
    }
    ratios = {
        "current_ratio": _ratio(ca, cl),
        "quick_ratio": _ratio((ca - (inv or 0)) if ca is not None else None, cl),
        "gross_margin_pct": _pct(gross, rev),
        "operating_margin_pct": _pct(op_inc, rev),
        "net_margin_pct": _pct(ni, rev),
        "fcf_margin_pct": _pct(fcf, rev),
        "return_on_equity_pct": _pct(ni, equity),               # ROE = net income / equity
        "return_on_assets_pct": _pct(ni, assets),               # ROA = net income / assets
        "debt_to_equity": _ratio(liab, equity),                 # total liabilities / equity
        "debt_to_assets": _ratio(liab, assets),                 # total liabilities / assets
        "interest_coverage": _ratio(op_inc, int_exp),           # operating income / interest expense
        "asset_turnover": _ratio(rev, assets),                  # revenue / assets (efficiency)
        "inventory_turnover": _ratio(cor, inv),                 # COGS / inventory
        "receivables_turnover": _ratio(rev, ar),                # revenue / receivables
        "quality_of_earnings": _ratio(ocf, ni) if (ni and ni > 0) else None,  # OCF / net income (>1 good)
        "dividend_payout_pct": _pct(div_paid, ni) if (div_paid and ni and ni > 0) else None,
        "fcf_dividend_coverage": _ratio(fcf, div_paid) if div_paid else None,  # FCF / dividends (>1 good)
    }
    # ---- disciplined quant scores (Piotroski F, Altman Z') from current vs prior-year GAAP ----
    ni_p = _latest(facts, "net_income")[1]
    assets_p = _latest(facts, "total_assets")[1]
    ca_p = _latest(facts, "current_assets")[1]
    cl_p = _latest(facts, "current_liabilities")[1]
    liab_p = _latest(facts, "total_liabilities")[1]
    gross_p = _latest(facts, "gross_profit")[1]
    cor_p = _latest(facts, "cost_of_revenue")[1]
    re_val = _latest(facts, "retained_earnings")[0]
    sh, sh_p, _shfy = _latest(facts, "shares")

    def _rr(n, d):
        return (n / d) if (n is not None and d) else None

    roa, roa_p = _rr(ni, assets), _rr(ni_p, assets_p)
    lev, lev_p = _rr(liab, assets), _rr(liab_p, assets_p)     # total-liab/assets as an LT-debt proxy
    cr, cr_p = _rr(ca, cl), _rr(ca_p, cl_p)
    gm = _rr(gross, rev)
    gross_pv = gross_p if gross_p is not None else ((rev_prev - cor_p) if (rev_prev is not None and cor_p is not None) else None)
    gm_p = _rr(gross_pv, rev_prev)
    at, at_p = _rr(rev, assets), _rr(rev_prev, assets_p)

    def _b(cond, ok):
        return None if cond is None else (1 if ok else 0)
    sig = {
        "roa_positive": _b(ni, ni is not None and ni > 0),
        "ocf_positive": _b(ocf, ocf is not None and ocf > 0),
        "roa_improving": _b(roa if roa_p is not None else None, roa is not None and roa_p is not None and roa > roa_p),
        "accruals_ocf_gt_ni": _b(ocf if ni is not None else None, ocf is not None and ni is not None and ocf > ni),
        "leverage_down": _b(lev if lev_p is not None else None, lev is not None and lev_p is not None and lev < lev_p),
        "current_ratio_up": _b(cr if cr_p is not None else None, cr is not None and cr_p is not None and cr > cr_p),
        "no_dilution": _b(sh if sh_p is not None else None, sh is not None and sh_p is not None and sh <= sh_p * 1.005),
        "gross_margin_up": _b(gm if gm_p is not None else None, gm is not None and gm_p is not None and gm > gm_p),
        "asset_turnover_up": _b(at if at_p is not None else None, at is not None and at_p is not None and at > at_p),
    }
    have = [v for v in sig.values() if v is not None]
    piotroski = None
    if have:
        sc = sum(have)
        mx = len(have)
        piotroski = {"score": sc, "of": mx, "max": 9, "signals": sig,
                     "rating": ("strong" if mx >= 6 and sc / mx >= 0.7 else
                                ("weak" if mx >= 6 and sc / mx <= 0.35 else "middling")),
                     "note": "Piotroski F-Score (9 fundamental-strength tests: profitability, "
                             "leverage/liquidity, efficiency). 'of' = tests with data available; missing "
                             "inputs are excluded, not failed. Higher is healthier."}

    wc = (ca - cl) if (ca is not None and cl is not None) else None
    altman = None
    if None not in (wc, re_val, op_inc, assets, liab, rev) and assets and liab:
        x1, x2, x3, x5 = wc / assets, re_val / assets, op_inc / assets, rev / assets
        x4 = (equity / liab) if (equity is not None and liab) else 0.0
        z = round(0.717 * x1 + 0.847 * x2 + 3.107 * x3 + 0.420 * x4 + 0.998 * x5, 2)
        altman = {"z_score": z, "zone": ("safe" if z > 2.9 else ("distress" if z < 1.23 else "grey")),
                  "variant": "Z' (book-value / private-firm coefficients)",
                  "note": "Altman Z'-Score bankruptcy-distress model. >2.9 safe, 1.23-2.9 grey, <1.23 distress. "
                          "Book-value variant (uses equity/liabilities, no market cap needed)."}

    return {"ticker": ticker, "available": True, "cik": cik,
            "entity": facts.get("entityName"), "metrics": metrics, "ratios": ratios,
            "scores": {"piotroski_f": piotroski, "altman_z": altman}}
