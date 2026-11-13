# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""signals.py -- S3 of the idea scavenger: signals for the stocks that pass a sleeve's SEC rules.
  analyst  -- share of analysts rating it a buy (Finnhub recommendation trends, 1 call per name)
  insider  -- net open-market buying by officers and directors over 120 days (SEC Form 4; existing insider module)
  short    -- FINRA short interest (days to cover, change), only when FINRA credentials are saved
Record builders and the due-selection live here as pure functions; app.py supplies the fetchers (behind the budget gates)."""
import time

ANALYST_REFRESH_DAYS = 7
INSIDER_REFRESH_DAYS = 3
SHORT_REFRESH_DAYS = 14
DEFAULT_ANALYST_TRANCHE = 600      # one Finnhub call each, background pace
DEFAULT_INSIDER_TRANCHE = 400      # about 10 SEC requests each, background pace
DEFAULT_SHORT_TRANCHE = 200


def build_analyst(recs, now=None):
    """From Finnhub /stock/recommendation (newest first). `available` False when no analyst covers the name."""
    latest = (recs or [None])[0] or {}
    counts = {k: (latest.get(k) or 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")}
    total = sum(counts.values())
    return {"available": total > 0, "total": total, "counts": counts, "period": latest.get("period"),
            "buy_pct": round((counts["strongBuy"] + counts["buy"]) / total * 100, 1) if total else None,
            "fetched_at": int(now or time.time())}


def build_insider(res, now=None):
    """From the app's insider signal (insider.aggregate output, or an unavailable stub)."""
    res = res or {}
    if not res.get("available"):
        return {"available": False, "reason": res.get("reason"), "fetched_at": int(now or time.time())}
    return {"available": True, "net_value": res.get("net_value"), "net_buy_pct": res.get("net_buy_pct"), "signal": res.get("signal"),
            "buyers": (res.get("buy") or {}).get("insiders", 0), "sellers": (res.get("sell") or {}).get("insiders", 0),
            "buy_value": (res.get("buy") or {}).get("value", 0.0), "sell_value": (res.get("sell") or {}).get("value", 0.0),
            "filings": res.get("filings"), "as_of": res.get("as_of"), "lookback_days": res.get("lookback_days"),
            "fetched_at": int(now or time.time())}


def build_short(si, now=None):
    if not si:
        return {"available": False, "fetched_at": int(now or time.time())}
    return {"available": True, "days_to_cover": si.get("days_to_cover"), "change_pct": si.get("change_pct"),
            "settlement_date": si.get("settlement_date"), "flag": si.get("flag"), "fetched_at": int(now or time.time())}


def display(s3):
    """Short human strings for the sleeve table: (insider text, analyst text)."""
    s3 = s3 or {}
    ins, an = s3.get("insider") or {}, s3.get("analyst") or {}
    itxt = None
    if ins.get("available"):
        nv = ins.get("net_value") or 0
        money = (f"${abs(nv) / 1e6:.1f}M" if abs(nv) >= 1e6 else f"${abs(nv) / 1e3:.0f}K" if abs(nv) >= 1e3 else f"${abs(nv):.0f}")
        itxt = f"{ins.get('signal')}" + (f" ({'+' if nv >= 0 else '-'}{money}, {ins.get('buyers', 0)} buying)" if nv else "")
    atxt = f"{an['buy_pct']:.0f}% buy ({an['total']})" if an.get("available") else None
    return itxt, atxt
