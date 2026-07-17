"""
washsale.py — GAP-4 (final review): make TLH recommendations shippable, not booby-trapped.

§1091 disallows a realized loss if the SAME or a SUBSTANTIALLY IDENTICAL security is bought
within 30 days BEFORE or AFTER the sale — across ALL accounts (IRAs included; an IRA
repurchase disallows the loss PERMANENTLY, Rev. Rul. 2008-5). The old TLH note said this in
prose; this module enforces it with data:

  · SHARE-EVENT LEDGER — a daily per-ticker total-share snapshot (all accounts). Day-over-day
    INCREASES are inferred buy events (catches DRIP reinvestments — the classic silent
    wash-sale trigger); the ledger accrues automatically, no broker import needed.
  · VERDICT per TLH candidate — blocked (buy inside the 31-day lookback), caution-drip (a
    dividend reinvestment could land inside the ±30-day window), or clear; with the date the
    window reopens.
  · REPLACEMENT PAIRS — curated not-substantially-identical alternatives for common funds
    (different INDEX, similar exposure), same-index clones explicitly flagged as risky, and
    sector-proxy guidance for single stocks.

Pure logic; app.py owns the snapshot file. Educational, not tax advice — lot-level truth
lives at the brokerage; confirm before filing.
"""
import datetime

WINDOW_DAYS = 31          # 30 days + the sale day, conservatively
MIN_EVENT_SHARES = 1e-4   # ignore float dust

# Curated replacements: ticker -> (suggestions, rationale). Suggestions track a DIFFERENT
# index with similar exposure — the accepted practitioner standard for avoiding
# "substantially identical" (undefined by the IRS; same-index clones are the risky case).
REPLACEMENT_PAIRS = {
    "VTI":  (["ITOT", "SCHB"], "total-market: CRSP → S&P 1500 / Dow Jones US — different index, ~99% overlap in behavior"),
    "ITOT": (["VTI", "SCHB"], "total-market swap across index families"),
    "VOO":  (["VTI", "SCHX", "SPLG†"], "S&P 500 → total-market or large-cap different-index; †SPLG tracks the SAME S&P 500 — riskier"),
    "SPY":  (["VTI", "SCHX"], "S&P 500 → different index; IVV/VOO/SPLG are same-index clones — avoid"),
    "IVV":  (["VTI", "SCHX"], "S&P 500 → different index; SPY/VOO are same-index clones — avoid"),
    "QQQ":  (["VGT", "VONG", "XLK"], "Nasdaq-100 → tech/growth different-index; QQQM is the SAME index — avoid"),
    "VXUS": (["IXUS", "VEU"], "total intl: FTSE → MSCI — different index family"),
    "IXUS": (["VXUS", "VEU"], "total intl swap across index families"),
    "VEA":  (["IEFA", "SCHF"], "developed intl: FTSE → MSCI"),
    "VWO":  (["IEMG", "SPEM"], "emerging: FTSE → MSCI/S&P"),
    "BND":  (["IUSB", "SCHZ"], "agg bonds: float-adjusted Agg → broader/different index; AGG is near-identical composition — debated, prefer IUSB"),
    "AGG":  (["IUSB", "SCHZ"], "agg bonds → different index; BND is the debated near-clone"),
    "VYM":  (["SCHD", "DVY"], "dividend: FTSE High Div → Dow Jones 100 — different methodology"),
    "SCHD": (["VYM", "DVY"], "dividend methodology swap"),
    "VUG":  (["IWF", "SCHG"], "growth: CRSP → Russell/DJ"),
    "VTV":  (["IWD", "SCHV"], "value: CRSP → Russell/DJ"),
    "VNQ":  (["SCHH", "IYR"], "REIT index families differ"),
}


def _iso(d):
    return d.isoformat() if isinstance(d, datetime.date) else str(d)


def append_snapshot(log, quantities, today=None, keep_days=120):
    """One {date: {ticker: shares}} row per day (idempotent; today's row overwritten).
    log: {"rows": [[date, {tkr: sh}], ...]} ascending. Returns the updated log."""
    today = _iso(today or datetime.date.today())
    rows = [r for r in (log or {}).get("rows", []) if r and r[0] != today]
    rows.append([today, {t: round(float(q), 4) for t, q in (quantities or {}).items() if q}])
    rows.sort(key=lambda r: r[0])
    cutoff = _iso(datetime.date.fromisoformat(today) - datetime.timedelta(days=keep_days))
    return {"rows": [r for r in rows if r[0] >= cutoff]}


def buy_events(log, ticker, since_iso):
    """Inferred buys: day-over-day share increases for a ticker since a date."""
    rows = (log or {}).get("rows", [])
    out = []
    for prev, cur in zip(rows, rows[1:]):
        if cur[0] < since_iso:
            continue
        d0 = (prev[1] or {}).get(ticker) or 0.0
        d1 = (cur[1] or {}).get(ticker) or 0.0
        if d1 - d0 > MIN_EVENT_SHARES:
            out.append({"date": cur[0], "shares_added": round(d1 - d0, 4)})
    return out


def replacements(ticker, sector=None):
    t = (ticker or "").upper()
    if t in REPLACEMENT_PAIRS:
        sug, why = REPLACEMENT_PAIRS[t]
        return {"suggestions": sug, "rationale": why, "kind": "curated"}
    return {"suggestions": ([f"{sector} sector ETF (e.g. its SPDR/Vanguard sector fund)"]
                            if sector else ["a correlated sector/industry ETF"]),
            "rationale": ("single names are easy: ANY other stock or a sector ETF keeps the "
                          "exposure without being substantially identical — just don't rebuy "
                          "this ticker (any account, incl. IRAs) for 31 days"),
            "kind": "generic"}


def assess(candidates, log, drip_by_ticker=None, sectors=None, today=None):
    """Wash-sale verdict per TLH candidate. candidates: [{ticker, ...}]. Returns list of
    {ticker, verdict, recent_buys, window_opens, drip_on, replacement}."""
    today = today or datetime.date.today()
    since = _iso(today - datetime.timedelta(days=WINDOW_DAYS))
    drip_by_ticker = drip_by_ticker or {}
    sectors = sectors or {}
    have_history = len((log or {}).get("rows", [])) >= 2
    out = []
    for c in candidates or []:
        t = c.get("ticker")
        buys = buy_events(log, t, since) if have_history else []
        drip = bool(drip_by_ticker.get(t))
        if buys:
            last = max(b["date"] for b in buys)
            opens = _iso(datetime.date.fromisoformat(last) + datetime.timedelta(days=WINDOW_DAYS))
            verdict = "blocked"
            msg = (f"bought {sum(b['shares_added'] for b in buys):.4g} sh within the last "
                   f"{WINDOW_DAYS}d (latest {last}) — selling now DISALLOWS the loss on those "
                   f"shares; window reopens {opens}")
        elif drip:
            verdict, opens = "caution-drip", None
            msg = ("DRIP is ON — a dividend reinvestment within ±30 days of the sale washes "
                   "the loss on the reinvested shares. Turn off DRIP (or confirm no dividend "
                   "lands in the window) before harvesting")
        elif not have_history:
            verdict, opens = "unknown", None
            msg = ("share-event ledger still accruing (needs ≥2 daily snapshots) — check your "
                   "broker's recent buys manually this once")
        else:
            verdict, opens = "clear", None
            msg = f"no inferred buys in the last {WINDOW_DAYS}d across tracked accounts"
        out.append({"ticker": t, "verdict": verdict, "msg": msg,
                    "recent_buys": buys, "window_opens": opens, "drip_on": drip,
                    "replacement": replacements(t, sectors.get(t))})
    return out
