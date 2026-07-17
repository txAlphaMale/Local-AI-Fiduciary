"""
backtest.py — P0-2: does the AI actually add value? (decision backtest)

Scores the model's HISTORICAL calls at FIXED horizons (1/3/6/12 months) instead of the
scorecard's "to now" read, and — the honest part — against an SPY counterfactual over the
SAME window: a Buy that rose 5% while SPY rose 12% is a miss, not a hit. Also scores the
Strategist's paper BUYs the same way (each entry vs putting the same dollars in SPY).

Pure logic: app.py injects `hist_fn` (its cached `_stooq_daily`, [[date, close], ...]
ascending) so this module has no network and unit-tests offline (mirrors risk.py's design).
Read-only, educational — small samples are noisy; this measures, it does not advise.
"""
import datetime

# Trading-day horizons: ~1m, 3m, 6m, 12m.
HORIZONS = {"1m": 21, "3m": 63, "6m": 126, "12m": 252}
# Same implied-P convention as the scorecard/G13 calibration (stated assumption).
CONF_P = {"high": 0.75, "med": 0.60, "low": 0.50}
BENCH = "SPY"


def _index(series):
    """[[date, close], ...] -> ({date: i}, [dates], [closes]). Dates are ISO strings."""
    dates = [r[0] for r in series]
    closes = [float(r[1]) for r in series]
    return {d: i for i, d in enumerate(dates)}, dates, closes


def _at_or_after(idx_map, dates, d, max_slip=7):
    """Index of the first trading day >= d (ISO), within max_slip calendar days; else None."""
    if d in idx_map:
        return idx_map[d]
    try:
        base = datetime.date.fromisoformat(d)
    except Exception:
        return None
    for k in range(1, max_slip + 1):
        cand = (base + datetime.timedelta(days=k)).isoformat()
        if cand in idx_map:
            return idx_map[cand]
    return None


def _fwd(closes, i, n):
    """Return pct from closes[i] to closes[i+n], or None if the future isn't in-sample yet."""
    j = i + n
    if i is None or j >= len(closes) or closes[i] <= 0:
        return None
    return (closes[j] / closes[i] - 1.0) * 100.0


def _agg(rows, key):
    vals = [r[key] for r in rows if r.get(key) is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 2)


def rating_backtest(log, hist_fn, horizons=None):
    """Score each dated Buy/Sell/Hold at fixed horizons vs the SPY counterfactual.
    log: rating_log entries [{date, ticker, rating, confidence, price}, ...]."""
    horizons = horizons or HORIZONS
    bench = hist_fn(BENCH) or []
    if not bench:
        return {"status": "no-benchmark", "note": f"No {BENCH} history available yet."}
    b_idx, b_dates, b_closes = _index(bench)
    hist_cache = {}
    scored = []                      # one row per (call, horizon) that has resolved
    for e in log or []:
        t, d, rating = e.get("ticker"), e.get("date"), e.get("rating")
        if not t or not d or rating not in ("Buy", "Hold", "Sell"):
            continue
        if t not in hist_cache:
            hist_cache[t] = hist_fn(t) or []
        series = hist_cache[t]
        if not series:
            continue
        idx_map, dates, closes = _index(series)
        i_t = _at_or_after(idx_map, dates, d)
        i_b = _at_or_after(b_idx, b_dates, d)
        if i_t is None or i_b is None:
            continue
        for hname, n in horizons.items():
            r_t = _fwd(closes, i_t, n)
            r_b = _fwd(b_closes, i_b, n)
            if r_t is None or r_b is None:
                continue                     # horizon not resolved yet — not a miss
            excess = r_t - r_b
            hit = None
            if rating == "Buy":
                hit = excess > 0
            elif rating == "Sell":
                hit = excess < 0             # a good Sell got out of a laggard
            scored.append({"ticker": t, "date": d, "rating": rating,
                           "confidence": e.get("confidence"), "horizon": hname,
                           "fwd_pct": round(r_t, 2), "spy_pct": round(r_b, 2),
                           "excess_pct": round(excess, 2), "hit": hit})
    by_h = {}
    for hname in horizons:
        rows = [r for r in scored if r["horizon"] == hname]
        directional = [r for r in rows if r["hit"] is not None]
        hits = sum(1 for r in directional if r["hit"])
        # Brier on the EXCESS-based hit (skill vs the market, not vs zero).
        cal = [r for r in directional if r.get("confidence") in CONF_P]
        brier = (round(sum((CONF_P[r["confidence"]] - (1.0 if r["hit"] else 0.0)) ** 2
                           for r in cal) / len(cal), 3) if cal else None)
        by_h[hname] = {
            "n": len(rows), "n_directional": len(directional),
            "avg_fwd_pct": _agg(rows, "fwd_pct"), "avg_spy_pct": _agg(rows, "spy_pct"),
            "avg_excess_pct": _agg(rows, "excess_pct"),
            "buy_avg_excess_pct": _agg([r for r in rows if r["rating"] == "Buy"], "excess_pct"),
            "sell_avg_excess_pct": _agg([r for r in rows if r["rating"] == "Sell"], "excess_pct"),
            "hold_avg_excess_pct": _agg([r for r in rows if r["rating"] == "Hold"], "excess_pct"),
            "hit_rate_pct": round(hits / len(directional) * 100, 1) if directional else None,
            "brier_vs_market": brier,
        }
    resolved = [r for r in scored if r["hit"] is not None]
    return {"status": "ready" if scored else "building",
            "calls_logged": len(log or []), "rows_scored": len(scored),
            "by_horizon": by_h,
            "recent": sorted(scored, key=lambda r: r["date"], reverse=True)[:24],
            "hit_rate_all_pct": (round(sum(1 for r in resolved if r["hit"]) / len(resolved) * 100, 1)
                                 if resolved else None),
            "implied_p": CONF_P,
            "note": ("Each dated call scored at fixed horizons AGAINST SPY over the same window "
                     "(hit = Buy beat SPY / Sell lagged SPY). Unresolved horizons are excluded, "
                     "not counted as misses. Brier <0.25 beats a coin flip. Small N is noisy — "
                     "judge trends, not single cells.")}


def paper_backtest(paper, hist_fn):
    """Score every paper BUY vs an SPY counterfactual: same dollars, same dates, marked to the
    latest close in history. Answers 'has the Strategist's stock-picking beaten just buying SPY?'"""
    bench = hist_fn(BENCH) or []
    if not bench:
        return {"status": "no-benchmark"}
    b_idx, b_dates, b_closes = _index(bench)
    rows = []
    for tx in (paper or {}).get("transactions") or []:
        if (tx.get("action") or "").lower() != "buy":
            continue
        t, px, sh = tx.get("ticker"), tx.get("price"), tx.get("shares")
        ts = tx.get("ts")
        if not t or not px or not sh or not ts:
            continue
        d = datetime.date.fromtimestamp(ts).isoformat()
        series = hist_fn(t) or []
        if not series:
            continue
        idx_map, dates, closes = _index(series)
        i_t = _at_or_after(idx_map, dates, d)
        i_b = _at_or_after(b_idx, b_dates, d)
        if i_t is None or i_b is None:
            continue
        r_t = (closes[-1] / closes[i_t] - 1.0) * 100.0 if closes[i_t] > 0 else None
        r_b = (b_closes[-1] / b_closes[i_b] - 1.0) * 100.0 if b_closes[i_b] > 0 else None
        if r_t is None or r_b is None:
            continue
        dollars = px * sh
        rows.append({"ticker": t, "date": d, "dollars": round(dollars, 2),
                     "ret_pct": round(r_t, 2), "spy_pct": round(r_b, 2),
                     "excess_pct": round(r_t - r_b, 2)})
    if not rows:
        return {"status": "empty", "note": "No scoreable paper buys yet."}
    tot = sum(r["dollars"] for r in rows) or 1.0
    w_ret = sum(r["ret_pct"] * r["dollars"] for r in rows) / tot
    w_spy = sum(r["spy_pct"] * r["dollars"] for r in rows) / tot
    return {"status": "ready", "n_buys": len(rows),
            "dollars_deployed": round(tot, 2),
            "weighted_ret_pct": round(w_ret, 2), "weighted_spy_pct": round(w_spy, 2),
            "weighted_excess_pct": round(w_ret - w_spy, 2),
            "winners_vs_spy": sum(1 for r in rows if r["excess_pct"] > 0),
            "rows": sorted(rows, key=lambda r: r["date"], reverse=True)[:24],
            "note": ("Every paper BUY vs the same dollars into SPY on the same day, both marked "
                     "to the latest close (entry-timing skill; ignores later exits). "
                     "Positive weighted excess = the picks beat the index counterfactual.")}


def run(log, paper, hist_fn):
    """Full backtest payload (app.py caches this to /data/backtest.json)."""
    import time as _time
    return {"generated_at": int(_time.time()),
            "ratings": rating_backtest(log, hist_fn),
            "paper": paper_backtest(paper, hist_fn),
            "benchmark": BENCH}
