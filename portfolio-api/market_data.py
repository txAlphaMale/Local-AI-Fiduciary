# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""market_data.py -- S2 of the idea scavenger: market data (price, size, yield, beta, sector) for the stocks that pass a sleeve's
SEC-data rules. Selection and record-building are pure functions here; app.py supplies the Finnhub fetchers, which already run
behind the API budget gate, share the app's caches, and defer (rather than report "no data") when the gate is paused.

Heavy first, cheap later: a never-checked candidate always goes first, then the stalest. Steady state is a small refresh."""
import datetime
import time

import sleeves as sleeves_mod

DEFAULT_TRANCHE = 800            # tickers per nightly run (3 Finnhub calls each, paced at the batch rate)
REFRESH_DAYS = 3                 # re-fetch a candidate's market data after this long
UNAVAILABLE_DAYS = 30            # a symbol Finnhub answered "nothing" for (OTC, foreign): look again after this long
BALANCE_CANDIDATES = 400         # Balance would otherwise mean every healthy stock; take the best-scoring names


def candidates(sleeve_list, rows, today=None):
    """Tickers worth fetching market data for: pass any enabled sleeve's SEC-data rules (market-data rules ignored here).
    -> ordered list of ticker strings (best-ranked first, de-duplicated)."""
    seen, out = set(), []
    for sl in sleeve_list:
        if sl.get("enabled") is False:
            continue
        s1_only = {k: v for k, v in sl.items() if k != "fill_gap"}      # a sector test needs market data we do not have yet
        s1_only["rules"] = [r for r in sl.get("rules", []) if sleeves_mod.METRICS[r["metric"]][2] == "s1"]
        matched = sleeves_mod.evaluate(s1_only, rows, today)["matches"]
        if sl.get("fill_gap"):
            matched = matched[:BALANCE_CANDIDATES]
        for r in matched:
            if r["ticker"] not in seen:
                seen.add(r["ticker"])
                out.append(r["ticker"])
    return out


def select_tranche(tickers, s2_by_ticker, now=None, limit=DEFAULT_TRANCHE):
    """Never-checked first, then oldest check; skip anything checked recently enough. s2_by_ticker: {ticker: {'fetched_at', 'available'}}."""
    now = now or time.time()
    due = []
    for t in tickers:
        s2 = s2_by_ticker.get(t)
        if not s2 or not s2.get("fetched_at"):
            due.append((0, 0, t))
            continue
        age_days = (now - s2["fetched_at"]) / 86400
        need = REFRESH_DAYS if s2.get("available", True) else UNAVAILABLE_DAYS
        if age_days >= need:
            due.append((1, s2["fetched_at"], t))
    due.sort()
    return [t for _, _, t in due[:max(1, int(limit))]], len(due)


def build_record(price, valuation, profile, now=None):
    """Compact market-data record from Finnhub's quote / metric / profile2 answers. `available` False when Finnhub has nothing at all."""
    v, p = valuation or {}, profile or {}
    mcap_musd = v.get("market_cap_musd") or p.get("marketCapitalization")
    hi, lo = v.get("high_52w"), v.get("low_52w")
    vol_m = v.get("avg_volume_3m_m") or v.get("avg_volume_10d_m")
    rec = {"price": price or None,
           "market_cap": round(mcap_musd * 1e6) if mcap_musd else None,
           "pe": v.get("pe_ttm"), "dividend_yield_pct": v.get("dividend_yield_pct"), "beta": v.get("beta"),
           "sector": (p.get("finnhubIndustry") or "").strip() or None,
           "avg_dollar_volume": round(vol_m * 1e6 * price) if (vol_m and price) else None,
           "pct_off_52w_high": round((price - hi) / hi * 100, 1) if (price and hi) else None,
           "high_52w": hi, "low_52w": lo, "fetched_at": int(now or time.time())}
    rec["available"] = any(rec[k] is not None for k in ("price", "market_cap", "pe", "beta", "sector"))
    return rec


def in_window(now_dt, start_hour=1, end_hour=6):
    """True inside the nightly scavenging window (01:00-06:00 local)."""
    return start_hour <= now_dt.hour < end_hour


def deadline_for(now_dt, end_hour=6):
    """Epoch seconds when a window run must stop, or None for a run started outside the window (a manual run)."""
    if not in_window(now_dt, end_hour=end_hour):
        return None
    end = now_dt.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    return end.timestamp()
