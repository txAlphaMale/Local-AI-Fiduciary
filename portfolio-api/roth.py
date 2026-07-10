"""
roth.py — unified marginal-rate curve + multi-year Roth conversion ladder (Fable G8).

The pro artifact for decumulation tax planning: instead of looking at the income-tax bracket
alone, compose EVERY marginal cost of the next dollar of ordinary income (a Roth conversion or
IRA withdrawal) into ONE effective-marginal-rate curve — federal ordinary tax, the long-term
capital-gains "bump zone" (ordinary income pushing stacked LTCG from 0%->15%->20%), NIIT, the
ACA premium-subsidy phase-out and its 400%-FPL CLIFF (pre-65), and IRMAA Medicare surcharge
steps (age 63+ because IRMAA looks back two years). The curve reveals where the cheap room is
and where the punitive spikes hide, and the ladder fills conversions up to a target ceiling
each year over the pre-RMD "valley," honoring the per-conversion 5-year clock.

Texas has no state income tax, so the ONLY marginal cost is federal + ACA/IRMAA — which makes
conversions in the low-income early-retirement years unusually cheap; the curve makes that
visible. Social Security is deprioritized per the household directive, so its "tax torpedo" is
omitted by default (can be layered later).

The heavy composition (tax.py / aca.py calls) lives in app.py; this module holds the pure math:
IRMAA tables + surcharge, the numerical marginal/average-rate transform, the fill-to-ceiling
recommendation, and the multi-year ladder. Decision-support only — not tax advice.
"""

# ---- IRMAA 2026 (VERIFIED 2026-07-10 vs the CMS 2026 Medicare Part B fact sheet). ----
# IRMAA looks back two years: 2026 surcharge is set by 2024 MAGI. Standard Part B = $202.90/mo.
# Surcharge = (multiplier - 1) x standard premium, per person on Medicare. Part D adds a smaller
# separate IRMAA (national-average based) that we do NOT model in dollars (avoids guessing) —
# a knob lets the user add a Part D estimate.
IRMAA_YEAR = 2026
IRMAA_PART_B_STD_MONTHLY = 202.90
IRMAA_MULTIPLIERS = [1.0, 1.4, 2.0, 2.6, 3.2, 3.4]
# Upper edge (exclusive) of each non-top tier's MAGI; index i is crossed when magi >= edge[i].
IRMAA_THRESHOLDS = {
    "single": [109_000, 137_000, 171_000, 205_000, 500_000],
    "mfj":    [218_000, 274_000, 342_000, 410_000, 750_000],
}
IRMAA_SOURCE = ("CMS 2026 Medicare Parts B premium fact sheet — standard $202.90/mo; MFJ IRMAA "
                "tiers $218k/$274k/$342k/$410k/$750k (single half), multipliers 1.4/2.0/2.6/3.2/3.4x")


def irmaa_annual_part_b(magi, status, people_on_medicare=2):
    """Annual Part B IRMAA SURCHARGE (above the standard premium) at a MAGI, x people on
    Medicare. mfs uses its special compressed schedule; single/mfj use the tiered tables."""
    st = status if status in ("single", "mfj") else ("mfj" if status == "mfj" else "single")
    if status == "mfs":
        mult = 1.0 if magi < 109_000 else (3.2 if magi < 397_000 else 3.4)
    else:
        th = IRMAA_THRESHOLDS[st]
        idx = len(th)
        for i, edge in enumerate(th):
            if magi < edge:
                idx = i
                break
        mult = IRMAA_MULTIPLIERS[idx]
    surcharge_monthly = (mult - 1.0) * IRMAA_PART_B_STD_MONTHLY
    return round(surcharge_monthly * 12 * max(1, people_on_medicare), 2)


def irmaa_tier_edges(status):
    """The MAGI thresholds where an IRMAA step occurs (for marking the curve)."""
    if status == "mfs":
        return [109_000, 397_000]
    return list(IRMAA_THRESHOLDS.get(status, IRMAA_THRESHOLDS["single"]))


def marginal_and_avg(deltas, costs):
    """Given ascending conversion amounts `deltas` (starting at 0, cost 0) and the matching
    incremental total COSTS, return per-point marginal rate (slope to the previous point) and
    average rate (cost/delta). The marginal series is where bracket steps, the LTCG bump zone,
    NIIT, the ACA slope+cliff, and IRMAA steps all show up."""
    out = []
    for i in range(len(deltas)):
        d, c = deltas[i], costs[i]
        if i == 0:
            mr = None
        else:
            dd = deltas[i] - deltas[i - 1]
            mr = ((costs[i] - costs[i - 1]) / dd * 100.0) if dd else None
        out.append({"delta": d, "cost": round(c, 0),
                    "marginal_rate_pct": round(mr, 1) if mr is not None else None,
                    "avg_rate_pct": round(c / d * 100.0, 1) if d else None})
    return out


def recommend_fill(points, ceiling_pct, hard_cap_delta=None):
    """Largest conversion whose MARGINAL rate stays at/below the ceiling, stopping at the first
    breach (fill to the top of the comfortable bracket) and never exceeding a hard cap (e.g. the
    ACA cliff headroom, pre-65). Returns the amount + the point where it stops."""
    best, best_pt = 0.0, None
    for p in points:
        if p["delta"] == 0:
            continue
        if hard_cap_delta is not None and p["delta"] > hard_cap_delta:
            break
        if p["marginal_rate_pct"] is not None and p["marginal_rate_pct"] <= ceiling_pct:
            best, best_pt = p["delta"], p
        else:
            break
    return {"fill_to": round(best, 0), "at_point": best_pt,
            "hard_capped": bool(hard_cap_delta is not None and best_pt is not None
                                and hard_cap_delta <= (best_pt["delta"] if best_pt else 0) + 1)}


def ladder(annual_fill, n_years, start_age, start_year, trad_balance,
           growth_pct=5.0, accessible_after=5, aca_cliff_fill=None, aca_until_age=65):
    """Multi-year conversion schedule over the pre-RMD valley. Each year converts up to the
    annual fill (or the tighter ACA-cliff fill while under Medicare age), draws down the
    traditional balance, grows the remainder, and stamps the 5-year-clock accessibility age.
    Pure — caller supplies the fill amounts from the curve."""
    rows = []
    bal = float(trad_balance or 0)
    for k in range(max(0, int(n_years))):
        age = start_age + k if start_age is not None else None
        under_medicare = (age is not None and aca_cliff_fill is not None and age < aca_until_age)
        cap = aca_cliff_fill if under_medicare else annual_fill
        conv = max(0.0, min(cap or 0.0, bal))
        rows.append({"year_index": k, "year": (start_year + k) if start_year else None, "age": age,
                     "conversion": round(conv, 0),
                     "aca_capped": bool(under_medicare and (aca_cliff_fill or 0) < (annual_fill or 0)),
                     "accessible_at_age": (age + accessible_after) if age is not None else None,
                     "trad_before": round(bal, 0), "trad_after": round(bal - conv, 0)})
        bal = (bal - conv) * (1 + growth_pct / 100.0)
        if bal <= 0:
            break
    return {"years": rows,
            "total_converted": round(sum(r["conversion"] for r in rows), 0),
            "trad_remaining": round(max(0.0, bal), 0),
            "accessible_after_years": accessible_after,
            "note": ("Each year's conversion fills to the target ceiling (or the ACA-cliff cap "
                     "while pre-Medicare), draining the traditional balance into Roth. Each "
                     "conversion's principal is penalty-free after its own 5-year clock (shown "
                     "as the accessible age). Converting in these low-income years moves money "
                     "to tax-free growth before RMDs force it out at higher rates.")}
