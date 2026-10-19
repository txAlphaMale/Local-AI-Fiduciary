# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
options.py — P1/G10: options analytics for the concentrated book (data-gate now open).

Free data path (reworked 2026-07-18): CBOE's free delayed-quotes endpoint
(cdn.cboe.com/api/global/delayed_quotes/options/<SYM>.json) is tried FIRST — Yahoo's options
endpoint started refusing requests outright even with the crumb workaround (rate-limited,
"Edge: Too Many Requests", including on liquid names like PLTR/RKLB). CBOE bundles EVERY
expiration's contracts into one call (no per-expiration re-fetch needed) and includes native
delta/gamma/vega/theta/rho — no local Black-Scholes needed on this path. Caveat verified by
hand: CBOE's response has NO underlying/spot-price field anywhere, so the caller should pass
`spot_hint` (e.g. from the app's own cached daily-close history); without one, spot is
approximated from the near-the-money contract's strike (~1 strike-increment accurate). The
original KEYLESS Yahoo options endpoint (cookie+crumb workaround, query1→query2 failover) is
kept as the fallback for symbols CBOE doesn't have (or if CBOE itself goes down) — greeks
aren't in that feed, so delta is computed locally with Black-Scholes from implied vol there.
[] / None on total failure everywhere (the app degrades to "building/unavailable", never
crashes).

Two household use cases (per the grand design / final review):
  · COVERED CALL income on a concentrated holding — pick ~30-45 DTE, ~0.20-0.30 delta:
    premium yield, annualized, assignment probability (≈delta), upside kept to strike.
  · COLLAR cost — buy ~10% OTM put, fund it by selling a call: floor %, cap %, net cost.
    The classic "can't sell (basis), can't sleep (concentration)" instrument.

Delayed data is fine at this cadence. Educational decision-support, not advice; assignment
has TAX consequences on appreciated shares (gains realized; qualified-covered-call rules).
"""
import json
import math
import re
import time
import datetime

import requests

UA = {"User-Agent": "Mozilla/5.0"}
_CRUMB = {"cookie": None, "crumb": None, "ts": 0}
CRUMB_TTL = 6 * 3600
# OCC-style contract symbol, e.g. "AAPL260702C00110000" = root + YYMMDD + C/P + strike*1000.
_OCC_RE = re.compile(r"^[A-Z]+(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


# ---------- Black-Scholes (stdlib only) ----------
def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_delta(spot, strike, t_years, iv, r_pct=4.0, call=True):
    """BS delta from implied vol. Returns None on degenerate inputs."""
    try:
        s, k, t, v, r = float(spot), float(strike), float(t_years), float(iv), float(r_pct) / 100.0
        if s <= 0 or k <= 0 or t <= 0 or v <= 0:
            return None
        d1 = (math.log(s / k) + (r + v * v / 2.0) * t) / (v * math.sqrt(t))
        return round(_norm_cdf(d1) if call else _norm_cdf(d1) - 1.0, 4)
    except (TypeError, ValueError, OverflowError):
        return None


def _mid(c):
    b, a = c.get("bid"), c.get("ask")
    if b and a and a >= b > 0:
        return round((b + a) / 2.0, 4)
    lp = c.get("lastPrice")
    return round(float(lp), 4) if lp else None


# ---------- Yahoo fetch (defensive; deploy-iterates on the host) ----------
def _crumb_pair():
    """Cookie+crumb workaround for Yahoo's occasional 401s; cached; None-tolerant."""
    now = time.time()
    if _CRUMB["crumb"] and now - _CRUMB["ts"] < CRUMB_TTL:
        return _CRUMB["cookie"], _CRUMB["crumb"]
    try:
        r1 = requests.get("https://fc.yahoo.com", headers=UA, timeout=15)
        cookie = r1.headers.get("Set-Cookie", "").split(";")[0] or None
        r2 = requests.get("https://query1.finance.yahoo.com/v1/test/getcrumb",
                          headers={**UA, **({"Cookie": cookie} if cookie else {})}, timeout=15)
        crumb = r2.text.strip() if r2.status_code < 400 and 0 < len(r2.text.strip()) < 64 else None
        _CRUMB.update(cookie=cookie, crumb=crumb, ts=now)
    except Exception:
        _CRUMB.update(ts=now)   # don't retry every call
    return _CRUMB["cookie"], _CRUMB["crumb"]


def _get_json(url, params, cookie=None):
    try:
        r = requests.get(url, params=params,
                         headers={**UA, **({"Cookie": cookie} if cookie else {})}, timeout=25)
        if r.status_code >= 400:
            return None
        return r.json()
    except Exception:
        return None


def fetch_chain(sym, date_epoch=None):
    """One expiration's chain (or the default nearest) from Yahoo v7 options.
    Returns {"expirations": [...], "spot": x, "calls": [...], "puts": [...]} or None."""
    sym = (sym or "").upper().strip()
    if not sym:
        return None
    params = {"date": int(date_epoch)} if date_epoch else {}
    attempts = []
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        attempts.append((f"https://{host}/v7/finance/options/{sym}", dict(params), None))
    cookie, crumb = _crumb_pair()
    if crumb:
        for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
            attempts.append((f"https://{host}/v7/finance/options/{sym}",
                             {**params, "crumb": crumb}, cookie))
    for url, p, ck in attempts:
        j = _get_json(url, p, ck)
        res = (((j or {}).get("optionChain") or {}).get("result") or [None])[0]
        if not res:
            continue
        quote = res.get("quote") or {}
        opt = (res.get("options") or [{}])[0] or {}
        return {"expirations": res.get("expirationDates") or [],
                "spot": quote.get("regularMarketPrice"),
                "expiration": opt.get("expirationDate"),
                "calls": opt.get("calls") or [], "puts": opt.get("puts") or []}
    return None


def pick_expiration(expirations, min_dte=25, max_dte=55, now_ts=None):
    """The expiration closest to ~40 DTE within [min,max]; else nearest above min; else None."""
    now_ts = now_ts or time.time()
    scored = []
    for e in expirations or []:
        dte = (e - now_ts) / 86400.0
        if dte >= min_dte:
            scored.append((abs(dte - 40), dte <= max_dte, e, dte))
    if not scored:
        return None, None
    in_band = [s for s in scored if s[1]]
    best = min(in_band or scored, key=lambda s: s[0])
    return best[2], round(best[3], 1)


# ---------- CBOE fetch (primary; keyless, one call = every expiration) ----------
def _fetch_chain_cboe(sym):
    """ALL expirations' contracts in ONE call — grouped by expiration epoch, normalized to the
    same per-contract field names covered_call()/collar()/_mid() already expect (strike,
    impliedVolatility, openInterest, bid, ask, lastPrice), PLUS native delta/gamma/vega/theta
    straight from CBOE. Returns {"expirations": [epoch,...], "by_exp": {epoch: {"calls":[...],
    "puts":[...]}}} or None on total failure / unparseable response."""
    sym = (sym or "").upper().strip()
    if not sym:
        return None
    try:
        r = requests.get(f"https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json",
                         headers=UA, timeout=25)
        if r.status_code >= 400:
            return None
        opts = (((r.json() or {}).get("data") or {}).get("options")) or []
    except Exception:
        return None
    if not opts:
        return None
    by_exp = {}
    for o in opts:
        m = _OCC_RE.match((o.get("option") or "").upper())
        if not m:
            continue
        yy, mm, dd, cp, strike8 = m.groups()
        try:
            exp_epoch = int(datetime.datetime(2000 + int(yy), int(mm), int(dd)).timestamp())
            strike = int(strike8) / 1000.0
        except (ValueError, OverflowError):
            continue
        c = {"strike": strike, "impliedVolatility": o.get("iv"), "openInterest": o.get("open_interest"),
             "bid": o.get("bid"), "ask": o.get("ask"), "lastPrice": o.get("last_trade_price"),
             "delta": o.get("delta"), "gamma": o.get("gamma"), "vega": o.get("vega"),
             "theta": o.get("theta"), "volume": o.get("volume")}
        slot = by_exp.setdefault(exp_epoch, {"calls": [], "puts": []})
        slot["calls" if cp == "C" else "puts"].append(c)
    return {"expirations": sorted(by_exp), "by_exp": by_exp} if by_exp else None


def _infer_spot(calls, puts):
    """CBOE's response has NO spot/underlying-price field anywhere (hand-verified against a live
    fetch) — approximate it as the strike of the near-the-money contract (native |delta| closest
    to 0.5), accurate to about one strike increment. Only used when the caller doesn't pass
    `spot_hint`. None if no contract in this expiration has a usable native delta."""
    best = None
    for c in (calls or []):
        d = c.get("delta")
        if d is None:
            continue
        score = abs(d - 0.5)
        if best is None or score < best[0]:
            best = (score, c.get("strike"))
    for p in (puts or []):
        d = p.get("delta")
        if d is None:
            continue
        score = abs(d + 0.5)
        if best is None or score < best[0]:
            best = (score, p.get("strike"))
    return best[1] if best else None


# ---------- analytics (pure; injected chain data → offline-testable) ----------
def covered_call(spot, calls, dte, r_pct=4.0, delta_lo=0.15, delta_hi=0.35):
    """Best income call in the target delta band (closest to 0.25). Premium = mid. Prefers a
    contract's own native delta (CBOE path) over the local Black-Scholes estimate (Yahoo path,
    where no greeks are in the feed) — the `c.get("delta")` key is only ever present on CBOE-
    sourced contracts, so this is a no-op for Yahoo data."""
    t = max(1.0, dte) / 365.0
    cands = []
    for c in calls or []:
        if (c.get("strike") or 0) <= spot:
            continue
        prem = _mid(c)
        d = c.get("delta")
        if d is None:
            d = bs_delta(spot, c["strike"], t, c.get("impliedVolatility"), r_pct, call=True)
        if not prem or d is None or not (delta_lo <= d <= delta_hi):
            continue
        cands.append((abs(d - 0.25), c, prem, d))
    if not cands:
        return None
    _, c, prem, d = min(cands, key=lambda x: x[0])
    yld = prem / spot * 100.0
    return {"strike": c["strike"], "premium": prem, "delta": d,
            "iv_pct": round((c.get("impliedVolatility") or 0) * 100, 1),
            "open_interest": c.get("openInterest"),
            "yield_pct": round(yld, 2),
            "annualized_yield_pct": round(yld * 365.0 / max(1.0, dte), 1),
            "assign_prob_pct": round(d * 100, 1),
            "upside_kept_pct": round((c["strike"] / spot - 1) * 100, 1),
            "note": ("Sells the upside above the strike for cash now; assignment on appreciated "
                     "shares REALIZES the gain (check lot basis first) and deep-ITM rolls can "
                     "break qualified-covered-call treatment.")}


def collar(spot, calls, puts, dte, r_pct=4.0, floor_pct=10.0):
    """Buy ~floor_pct OTM put, sell the cheapest call that fully funds it (net ≤ 0 preferred)."""
    t = max(1.0, dte) / 365.0
    target_put = spot * (1 - floor_pct / 100.0)
    put_c = [p for p in puts or []
             if p.get("strike") and p["strike"] <= spot and _mid(p)]
    if not put_c:
        return None
    put = min(put_c, key=lambda p: abs(p["strike"] - target_put))
    put_cost = _mid(put)
    call_c = []
    for c in calls or []:
        if (c.get("strike") or 0) <= spot:
            continue
        prem = _mid(c)
        if not prem:
            continue
        call_c.append((c, prem))
    funded = [(c, prem) for c, prem in call_c if prem >= put_cost]
    if funded:
        c, prem = max(funded, key=lambda x: x[0]["strike"])   # highest cap that still funds the put
    elif call_c:
        c, prem = max(call_c, key=lambda x: x[1])             # best partial funding
    else:
        return None
    net = round(put_cost - prem, 4)
    d_put = put.get("delta")
    if d_put is None:
        d_put = bs_delta(spot, put["strike"], t, put.get("impliedVolatility"), r_pct, call=False)
    return {"put_strike": put["strike"], "put_cost": put_cost,
            "put_delta": d_put,
            "call_strike": c["strike"], "call_premium": prem,
            "net_cost": net, "net_cost_pct": round(net / spot * 100, 2),
            "floor_pct": round((1 - put["strike"] / spot) * 100, 1),
            "cap_pct": round((c["strike"] / spot - 1) * 100, 1),
            "zero_cost": net <= 0,
            "note": ("Floor = max further downside to the put strike; cap = upside surrendered at "
                     "the call strike. A zero-cost collar trades all upside beyond the cap for the "
                     "floor. Collars on appreciated stock can suspend the holding period and "
                     "constructive-sale rules apply to very tight collars — confirm with a CPA.")}


def analyze(sym, hist_iv_note=None, r_pct=4.0, floor_pct=10.0, spot_hint=None):
    """Full per-symbol hedging card. Tries CBOE first (one call, native greeks); falls back to
    the Yahoo crumb-workaround path if CBOE has nothing for this symbol (or is unreachable).
    `spot_hint` (recommended): the caller's own known current price — e.g. the app passes its
    cached daily-close (`_stooq_daily`) so the CBOE path doesn't need to approximate spot from
    a strike. None if neither source has a usable chain."""
    cboe = _fetch_chain_cboe(sym)
    if cboe and cboe.get("expirations"):
        exp, dte = pick_expiration(cboe["expirations"])
        if exp:
            slot = cboe["by_exp"].get(exp) or {}
            calls, puts = slot.get("calls") or [], slot.get("puts") or []
            spot = spot_hint or _infer_spot(calls, puts)
            if spot:
                cc = covered_call(spot, calls, dte, r_pct)
                col = collar(spot, calls, puts, dte, r_pct, floor_pct)
                if cc or col:
                    return {"ticker": sym, "spot": spot,
                            "expiration": datetime.date.fromtimestamp(exp).isoformat(), "dte": dte,
                            "covered_call": cc, "collar": col,
                            "generated_at": int(time.time()), "source": "cboe-delayed"}
    # CBOE unreachable / no chain for this symbol / no usable spot — Yahoo fallback.
    first = fetch_chain(sym)
    if not first or not first.get("spot"):
        return None
    exp, dte = pick_expiration(first.get("expirations"))
    if not exp:
        return None
    chain = first if first.get("expiration") == exp else fetch_chain(sym, exp)
    if not chain:
        return None
    spot = spot_hint or chain.get("spot") or first["spot"]
    exp_date = datetime.date.fromtimestamp(exp).isoformat()
    cc = covered_call(spot, chain.get("calls"), dte, r_pct)
    col = collar(spot, chain.get("calls"), chain.get("puts"), dte, r_pct, floor_pct)
    if not cc and not col:
        return None
    return {"ticker": sym, "spot": spot, "expiration": exp_date, "dte": dte,
            "covered_call": cc, "collar": col,
            "generated_at": int(time.time()),
            "source": "yahoo-delayed"}
