# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
valuehist.py -- pure helpers for the daily value history: reject half-loaded snapshots, spot the ones already stored, and build the
cost-basis series (2026-10-23).

WHY: the history stores one invested_value per day. On 2026-09-06 and 2026-09-18 it recorded a portfolio that had only partly loaded
(a provider hiccup), so the chart showed the line collapsing for one day and every risk statistic (volatility, drawdown, Sharpe,
the vol-regime alert) was computed on those fake moves. A snapshot is now written only when the load looks complete, and bad
rows already stored can be excluded (reversibly -- never deleted).
"""

DROP_FRACTION = 0.30        # a day's invested value more than 30% below the last good day ...
POSITION_DROP = 0.10        # ... together with 10% fewer position lines is a half-loaded portfolio
OUTLIER_DEV = 0.30          # stored one-day spike/dip: >30% away from BOTH neighbours (which agree with each other)
NEIGHBOUR_TOL = 0.15


def snapshot_incomplete(d, prev):
    """Return (True, reason) when `d` (a freshly built portfolio) should NOT be recorded as today's snapshot.
    prev: the newest stored, non-excluded row ({invested_value, positions}) or None."""
    for b in d.get("by_account") or []:
        if b.get("error"):
            return True, "an account's holdings failed to load"
    inv = d.get("invested_value") or 0
    if prev and (prev.get("invested_value") or 0) > 0:
        pv = prev["invested_value"]
        npos = d.get("position_count") or 0
        ppos = prev.get("positions") or 0
        if inv < pv * (1 - DROP_FRACTION) and (not ppos or npos < ppos * (1 - POSITION_DROP)):
            return True, "invested value fell sharply while positions went missing"
    return False, ""


MAX_RUN = 3                 # a bad stretch can span a few snapshots (2026-09-06 and -07 were both half-loaded)


def detect_outliers(rows):
    """Dates of short spikes/dips already in the history: a run of 1-3 consecutive days whose values are all >30% away, in the same direction,
    from the mean of the day before and the day after the run -- where those two neighbours are within 15% of each other (so a genuine step
    change, such as a new account that stays, is NOT flagged)."""
    pts = [(r.get("date"), r.get("invested_value") or r.get("net_worth")) for r in rows or []]
    pts = [(d, v) for d, v in pts if d and v]
    flagged = []
    i = 1
    while i < len(pts) - 1:
        hit = None
        for run in range(1, MAX_RUN + 1):
            j = i + run                                  # index of the neighbour after the run
            if j >= len(pts):
                break
            a, b = pts[i - 1][1], pts[j][1]
            if max(a, b) <= 0 or abs(a - b) / max(a, b) > NEIGHBOUR_TOL:
                continue
            base = (a + b) / 2.0
            seg = [v for _, v in pts[i:j]]
            if all(abs(v - base) / base >= OUTLIER_DEV for v in seg) and (all(v < base for v in seg) or all(v > base for v in seg)):
                hit = run
                break
        if hit:
            flagged += [d for d, _ in pts[i:i + hit]]
            i += hit
        else:
            i += 1
    return flagged


def cost_series(rows):
    """[(date, covered_value, total_cost)] for rows that recorded cost-basis data, ascending."""
    out = []
    for r in rows or []:
        c, v = r.get("total_cost"), r.get("cost_covered_value")
        if c is not None and v is not None and c > 0 and v > 0:
            out.append((r["date"], float(v), float(c)))
    return out
