"""
options.py — P1/G10: options analytics for the concentrated book (data-gate now open).

Free data path: the KEYLESS Yahoo options endpoint (same infra as the price-history
fallback). Yahoo sometimes requires a cookie+crumb pair — fetched once and cached, with a
plain no-crumb attempt first and query1→query2 failover; [] on total failure (the app
degrades to "building/unavailable", never crashes). Greeks are NOT in the feed, so delta
is computed locally with Black-Scholes from each contract's implied volatility — pure
stdlib math, exact enough for retail hedging decisions on liquid chains.

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
import time
import datetime

import requests

UA = {"User-Agent": "Mozilla/5.0"}
_CRUMB = {"cookie": None, "crumb": None, "ts": 0}
CRUMB_TTL = 6 * 3600


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


# ---------- analytics (pure; injected chain data → offline-testable) ----------
def covered_call(spot, calls, dte, r_pct=4.0, delta_lo=0.15, delta_hi=0.35):
    """Best income call in the target delta band (closest to 0.25). Premium = mid."""
    t = max(1.0, dte) / 365.0
    cands = []
    for c in calls or []:
        if (c.get("strike") or 0) <= spot:
            continue
        prem = _mid(c)
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


def analyze(sym, hist_iv_note=None, r_pct=4.0, floor_pct=10.0):
    """Full per-symbol hedging card. Network: 1-2 Yahoo calls. None if chain unavailable."""
    first = fetch_chain(sym)
    if not first or not first.get("spot"):
        return None
    exp, dte = pick_expiration(first.get("expirations"))
    if not exp:
        return None
    chain = first if first.get("expiration") == exp else fetch_chain(sym, exp)
    if not chain:
        return None
    spot = chain.get("spot") or first["spot"]
    exp_date = datetime.date.fromtimestamp(exp).isoformat()
    cc = covered_call(spot, chain.get("calls"), dte, r_pct)
    col = collar(spot, chain.get("calls"), chain.get("puts"), dte, r_pct, floor_pct)
    if not cc and not col:
        return None
    return {"ticker": sym, "spot": spot, "expiration": exp_date, "dte": dte,
            "covered_call": cc, "collar": col,
            "generated_at": int(time.time()),
            "source": "yahoo-delayed"}
