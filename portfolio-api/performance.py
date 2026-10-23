# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
performance.py — GAP-3 (final review): the fiduciary bottom line — "am I beating my policy?"

Adds what the terminal lacked beside money-weighted views: TIME-WEIGHTED returns over
STANDARD periods (QTD / YTD / 1Y / 3Y) against the household's OWN policy benchmark (the
IPS sleeve targets mapped to proxy ETFs) and SPY for reference.

Two return engines, honestly labeled:
  · RECONSTRUCTED (periods): current holdings × each ticker's daily history, renormalized
    over covered names — a pure price-return TWR that ASSUMES CURRENT WEIGHTS held all
    period (same stated convention as the G4/G5 risk/attribution suites). Coverage % shown.
  · TRACKED (snapshot window): chain-linked daily returns of the real invested value with a
    FLOW GUARD — snapshot days whose move vs SPY is implausibly large are treated as
    deposits/withdrawals and replaced with the benchmark move (count reported). Without a
    true external-flow ledger this is an approximation, and it says so.

Policy benchmark: sleeve targets × proxy ETFs (US Equity→VTI, Intl→VXUS, Bond→BND,
Cash→BIL), daily-rebalanced. Excess = portfolio − policy: positive means the *implementation*
(picks/tilts) beat the *allocation*; negative means the allocation would have done better alone.
Pure logic, injected hist_fn — unit-tests offline. Educational, not advice.
"""
import datetime

SLEEVE_PROXIES = {"US Equity": "VTI", "Intl Equity": "VXUS", "Bond": "BND", "Cash": "BIL"}
FLOW_ABS_PCT = 2.0        # daily |move − spy move| above this (and 4× typical) → flow day


def _f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _series_map(series):
    """[[date, close], ...] -> ({date: close}, sorted_dates)."""
    m = {r[0]: float(r[1]) for r in series or [] if r and r[1]}
    return m, sorted(m)


def _ret_from(m, dates, start_iso):
    """Price return from the first trading day >= start_iso to the last, in %."""
    if not dates:
        return None
    lo = None
    for d in dates:
        if d >= start_iso:
            lo = d
            break
    if lo is None or m.get(lo, 0) <= 0:
        return None
    return (m[dates[-1]] / m[lo] - 1.0) * 100.0


def period_starts(today=None):
    t = today or datetime.date.today()
    q_month = 3 * ((t.month - 1) // 3) + 1
    return {"QTD": t.replace(month=q_month, day=1).isoformat(),
            "YTD": t.replace(month=1, day=1).isoformat(),
            "1Y": (t - datetime.timedelta(days=365)).isoformat(),
            "3Y": (t - datetime.timedelta(days=1095)).isoformat()}


def composite_returns(weights, hist_fn, starts):
    """Weighted price return of a basket over each period; renormalizes over the tickers that
    have history. Returns (per-period dict, coverage_pct, missing)."""
    got, missing = {}, []
    for tkr, w in (weights or {}).items():
        if not w or w <= 0:
            continue
        m, dates = _series_map(hist_fn(tkr))
        (got.__setitem__(tkr, (w, m, dates)) if dates else missing.append(tkr))
    tot_w = sum(w for w, _, _ in got.values())
    all_w = tot_w + sum((weights or {}).get(t) or 0 for t in missing)
    out = {}
    for pname, start in starts.items():
        num = den = 0.0
        for w, m, dates in got.values():
            r = _ret_from(m, dates, start)
            if r is not None:
                num += w * r
                den += w
        out[pname] = round(num / den, 2) if den else None
    coverage = round(tot_w / all_w * 100, 1) if all_w else 0.0
    return out, coverage, missing


def tracked_twr(snapshots, flow_abs_pct=FLOW_ABS_PCT):
    """Chain-linked TWR of the real snapshot series with the flow guard. Snapshots:
    [{date, invested_value, spy}, ...] ascending, one per day."""
    rows = [(s.get("date"), _f(s.get("invested_value")), _f(s.get("spy")))
            for s in snapshots or [] if s.get("date") and _f(s.get("invested_value"))]
    rows.sort()
    if len(rows) < 2:
        return {"available": False, "note": "Needs ≥2 daily snapshots — accrues automatically."}
    resids = []
    pairs = []
    for (d0, v0, s0), (d1, v1, s1) in zip(rows, rows[1:]):
        rp = v1 / v0 - 1.0
        rs = (s1 / s0 - 1.0) if (s0 and s1) else 0.0
        pairs.append((d1, rp, rs))
        resids.append(abs(rp - rs))
    resids_sorted = sorted(resids)
    med = resids_sorted[len(resids_sorted) // 2] if resids_sorted else 0.0
    thresh = max(flow_abs_pct / 100.0, 4.0 * med)
    chain = 1.0
    flow_days = []
    for d1, rp, rs in pairs:
        if abs(rp - rs) > thresh:
            flow_days.append(d1)
            rp = rs          # replace the implausible move with the benchmark move
        chain *= (1.0 + rp)
    days = (datetime.date.fromisoformat(rows[-1][0]) - datetime.date.fromisoformat(rows[0][0])).days
    twr = (chain - 1.0) * 100.0
    ann = ((chain ** (365.0 / days)) - 1.0) * 100.0 if days >= 30 else None
    return {"available": True, "since": rows[0][0], "days": days,
            "twr_pct": round(twr, 2),
            "annualized_pct": round(ann, 2) if ann is not None else None,
            "flow_days_guarded": len(flow_days),
            "flow_days": flow_days[-10:],
            "threshold_pct": round(thresh * 100, 2),
            "note": ("Flow guard: days whose move vs SPY exceeded the threshold were treated as "
                     "deposits/withdrawals and replaced with SPY's move — an approximation until a "
                     "true flow ledger exists. If guarded-day count is high, treat with caution.")}


def run(weights, sleeve_targets, hist_fn, snapshots, today=None, proxies=None):
    starts = period_starts(today)
    px = {**SLEEVE_PROXIES, **(proxies or {})}
    port, coverage, missing = composite_returns(weights, hist_fn, starts)
    pol_weights = {}
    for sleeve, tgt in (sleeve_targets or {}).items():
        proxy = px.get(sleeve)
        if proxy and _f(tgt, 0) > 0:
            pol_weights[proxy] = pol_weights.get(proxy, 0.0) + float(tgt)
    policy, pol_cov, _pm = composite_returns(pol_weights, hist_fn, starts)
    spy_m, spy_d = _series_map(hist_fn("SPY"))
    periods = {}
    for p in ("QTD", "YTD", "1Y", "3Y"):
        spy_r = _ret_from(spy_m, spy_d, starts[p])
        row = {"portfolio_pct": port.get(p), "policy_pct": policy.get(p),
               "spy_pct": round(spy_r, 2) if spy_r is not None else None}
        row["excess_vs_policy_pct"] = (round(row["portfolio_pct"] - row["policy_pct"], 2)
                                       if row["portfolio_pct"] is not None and row["policy_pct"] is not None
                                       else None)
        periods[p] = row
    return {"periods": periods, "period_starts": starts,
            "coverage_pct": coverage, "missing_history": missing[:12],
            "policy_proxies": {s: px.get(s) for s in (sleeve_targets or {})},
            "policy_coverage_pct": pol_cov,
            "tracked": tracked_twr(snapshots),
            "note": ("Period returns assume CURRENT weights held all period (stated approximation; "
                     "same convention as the risk suite) — they answer 'did my picks beat my policy "
                     "mix?', not 'what did my account literally earn' (see the tracked TWR and the "
                     "money-weighted views for that). Excess vs policy > 0 = implementation added "
                     "value over pure allocation.")}


# ---------------------------------------------------------------------------------------------------------------------------
# HOLDINGS-BASED RETURN (2026-10-23): "what did the holdings I actually owned each day do?" -- the return a contribution cannot inflate.
# For each day pair (d0 -> d1) the return is  sum(shares_d0 x (close_d1 - close_d0)) / sum(shares_d0 x close_d0)  over the tickers that
# have price history. Because the shares are those held at the START of the day, buying more shares (a 401(k) deposit) changes the
# next day's weights but never counts as performance. Price return only (no dividends), and only for names with price history --
# the caller reports that coverage. This is the cash-flow-aware answer Quicken gets from transactions, built from the daily share log.
# ---------------------------------------------------------------------------------------------------------------------------
import bisect

MAX_DAILY_MOVE = 0.25        # a one-day move beyond this is treated as a data glitch (split, bad print), not a return


def _closer(series):
    """series [[date, close], ...] ascending -> price_on(date) = last close on/before date (None before the first)."""
    dates = [r[0] for r in series or []]
    closes = [_f(r[1]) for r in series or []]

    def price_on(date):
        i = bisect.bisect_right(dates, date) - 1
        return closes[i] if i >= 0 else None
    return price_on


def holdings_return_series(share_rows, hist_fn, spy_series, drop_dates=()):
    """share_rows: [[date, {ticker: shares}], ...] ascending (the wash-sale share log). Returns
    {"series": [{date, port, spy}], "covered_tickers": [...], "uncovered_tickers": [...], "glitch_days": n}. Both indexes start at 100 on
    the first usable day. Rows whose date is in drop_dates (proven-bad snapshots) are skipped."""
    rows = [r for r in (share_rows or []) if r and r[0] not in set(drop_dates) and (r[1] or {})]
    rows.sort(key=lambda r: r[0])
    if len(rows) < 2:
        return {"series": [], "covered_tickers": [], "uncovered_tickers": [], "glitch_days": 0}
    tickers = sorted({t for _, q in rows for t in q})
    price = {}
    for t in tickers:
        s = hist_fn(t) or []
        if s:
            price[t] = _closer(s)
    spy_p = _closer(spy_series)
    port = spy = 100.0
    out = [{"date": rows[0][0], "port": 100.0, "spy": 100.0}]
    glitches = 0
    for (d0, q0), (d1, _q1) in zip(rows, rows[1:]):
        gain = base = 0.0
        for t, sh in (q0 or {}).items():
            pf = price.get(t)
            if not pf or not sh:
                continue
            p0, p1 = pf(d0), pf(d1)
            if p0 and p1 and p0 > 0:
                gain += sh * (p1 - p0)
                base += sh * p0
        r = (gain / base) if base > 0 else 0.0
        if abs(r) > MAX_DAILY_MOVE:
            r = 0.0
            glitches += 1
        s0, s1 = spy_p(d0), spy_p(d1)
        rs = (s1 / s0 - 1.0) if (s0 and s1 and s0 > 0) else 0.0
        port *= (1.0 + r)
        spy *= (1.0 + rs)
        out.append({"date": d1, "port": round(port, 4), "spy": round(spy, 4)})
    return {"series": out, "covered_tickers": sorted(price), "uncovered_tickers": [t for t in tickers if t not in price],
            "glitch_days": glitches}
