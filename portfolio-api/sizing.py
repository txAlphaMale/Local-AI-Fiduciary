"""
sizing.py — position sizing & concentration action program for the Fiduciary (Fable G7).

Turns "revealed-aggressive" (a concentrated single-name book) into "engineered-aggressive":
instead of just FLAGGING concentration (that's G4/exposure), this prices the CHOICES and
gives an executable de-risking path. Pure arithmetic; the app feeds in per-name volatility &
CVaR (from the G4 reconstructed history) and embedded gains, and does the tax staging via
tax.py. Read-only / decision-support — never places an order.

Four lenses (per the Grand-Design G7 spec + the triage caveats):
  1. Vol-scaled target weights — inverse-volatility targets that EQUALIZE each name's standalone
     risk contribution (a defensible, edge-free "engineered" weight), shown against current
     weights so oversized-on-risk names are obvious.
  2. Max single-name loss budget at CVaR — the PRIMARY sizing gate. Given a tolerance (max % of
     net worth you'd lose from one name in a tail), each name's CVaR implies a maximum position
     value; positions over it are the action list. This is the honest, measurable constraint.
  3. Staged-sale schedule — a multi-year trim that spends the annual 0%-LTCG gain-harvest budget
     first (tax-free), quantifying the tax SAVED versus selling everything now.
  4. Optional half-Kelly reference ceiling — DE-EMPHASIZED per triage: single-name edge is
     unmeasurable, so this is a caveated reference line only, never a recommendation driver.

Exchange-fund (7-year basket) education is surfaced as a note when a name is both very large and
very appreciated. Collar economics is deferred (needs G10 option chains).
"""


def inverse_vol_targets(positions):
    """Inverse-volatility target weights (%) over the names that HAVE a volatility estimate —
    weight_i proportional to 1/vol_i, so each name contributes roughly equal standalone risk.
    positions: [{ticker, vol_annual (decimal, e.g. 0.35), ...}]. Cash/funds without vol are
    excluded from the target set (they're not the concentration problem). {} if none."""
    have = [p for p in positions if p.get("vol_annual")]
    if not have:
        return {}
    inv = {p["ticker"]: 1.0 / p["vol_annual"] for p in have}
    s = sum(inv.values()) or 1.0
    return {t: round(v / s * 100, 2) for t, v in inv.items()}


def loss_budget_analysis(positions, net_worth, loss_budget_pct, horizon_days):
    """PRIMARY sizing gate. Each name's tail loss over the horizon = value x CVaR_frac (CVaR
    already horizon-scaled and passed in). The loss budget = loss_budget_pct% of net worth is
    the max you'll accept losing from ONE name; that implies a max position value = budget/CVaR.
    positions: [{ticker, value, weight_pct, cvar_frac}]. Returns per-name at-risk $, the cap,
    an over-budget flag, and the suggested trim to reach the cap."""
    nw = net_worth or 0.0
    budget_dollars = (loss_budget_pct / 100.0) * nw
    rows = []
    for p in positions:
        cv = p.get("cvar_frac")
        val = p.get("value") or 0.0
        if not cv or cv <= 0 or val <= 0:
            continue
        at_risk = val * cv
        cap_value = budget_dollars / cv if cv > 0 else None
        rows.append({
            "ticker": p["ticker"], "value": round(val, 2), "weight_pct": p.get("weight_pct"),
            "cvar_pct_horizon": round(cv * 100, 1),
            "at_risk_dollars": round(at_risk, 0),
            "at_risk_pct_of_nw": round(at_risk / nw * 100, 2) if nw else None,
            "max_position_value": round(cap_value, 0) if cap_value else None,
            "over_budget": bool(cap_value is not None and val > cap_value),
            "suggested_trim": round(max(0.0, val - cap_value), 0) if cap_value else None})
    rows.sort(key=lambda r: -(r["at_risk_dollars"] or 0))
    return {"loss_budget_pct": loss_budget_pct, "budget_dollars": round(budget_dollars, 0),
            "horizon_days": horizon_days, "net_worth": round(nw, 2), "positions": rows,
            "total_over_budget": sum(1 for r in rows if r["over_budget"]),
            "total_suggested_trim": round(sum(r["suggested_trim"] or 0 for r in rows), 0),
            "note": "A name's tail loss = position value x its CVaR over the horizon. The loss "
                    "budget (a % of net worth) caps how much one name may put at risk, implying a "
                    "max position value. Over-budget names are the trim list. This is the primary, "
                    "measurable sizing constraint — vol targets and Kelly are secondary."}


def staged_diversification_plan(total_trim, total_gain, annual_0pct_budget,
                                ltcg_rate_pct, sell_now_tax, max_years=5):
    """Multi-year trim that spends the annual 0%-LTCG gain-harvest budget first (tax-free),
    then prices any residual at the LTCG rate — versus selling everything now. Aggregate/blended
    across the trim list (blended gain fraction = total_gain/total_trim). Returns the year-by-year
    schedule + total staged tax + tax saved vs. an all-at-once sale (sell_now_tax computed by the
    app via tax.ltcg_tax so it stacks correctly on ordinary income). None if nothing to trim."""
    if total_trim is None or total_trim <= 0:
        return None
    gf = (total_gain / total_trim) if total_trim else 0.0
    schedule = []
    remaining_gain = max(0.0, total_gain)
    yr = 0
    while remaining_gain > 1 and yr < max_years and annual_0pct_budget > 0:
        yr += 1
        g0 = min(remaining_gain, annual_0pct_budget)
        val_sold = (g0 / gf) if gf > 0 else 0.0
        schedule.append({"year": yr, "gain_realized": round(g0, 0),
                         "value_sold": round(val_sold, 0), "tax": 0.0})
        remaining_gain -= g0
    residual_tax = round(remaining_gain * ltcg_rate_pct / 100.0, 0) if remaining_gain > 1 else 0.0
    staged_total_tax = residual_tax
    return {"blended_gain_fraction_pct": round(gf * 100, 1),
            "annual_0pct_budget": round(annual_0pct_budget, 0),
            "years_tax_free": len(schedule), "schedule": schedule,
            "residual_gain": round(max(0.0, remaining_gain), 0),
            "residual_tax_at_ltcg": residual_tax, "ltcg_rate_pct": ltcg_rate_pct,
            "staged_total_tax": staged_total_tax,
            "sell_now_tax": round(sell_now_tax or 0, 0),
            "tax_saved_vs_now": round((sell_now_tax or 0) - staged_total_tax, 0),
            "note": "Spends the annual 0%-LTCG budget first (tax-free), spreading the trim over "
                    "years; any gain beyond the budget x years is taxed at the LTCG rate. "
                    "Compared with realizing the whole gain in one tax year."}


def half_kelly_ceiling(mu_annual, vol_annual, rf=0.04):
    """DE-EMPHASIZED reference only. Continuous-approx Kelly fraction f* = (mu - rf)/vol^2, halved.
    Returns a % of wealth or None. mu_annual is a NOISY trailing estimate — single-name edge is
    not reliably measurable, so this is a ceiling sanity-check, never a sizing recommendation."""
    if not vol_annual or vol_annual <= 0 or mu_annual is None:
        return None
    f = (mu_annual - rf) / (vol_annual ** 2)
    return round(max(0.0, f * 0.5) * 100, 1)


def exchange_fund_note(weight_pct, gain_fraction_pct, weight_trigger=15.0, gain_trigger=40.0):
    """Surface exchange-fund (7-year basket) education only when a name is BOTH very large and
    very appreciated — the case where diversifying by sale is most tax-punitive."""
    if (weight_pct or 0) >= weight_trigger and (gain_fraction_pct or 0) >= gain_trigger:
        return ("Large + highly appreciated: an exchange fund (contribute the stock to a "
                "diversified partnership, receive a basket after ~7 years) defers the gain "
                "entirely, trading liquidity/fees/lockup for diversification without a taxable "
                "sale. Accredited-investor product — educational only, weigh fees vs. the tax.")
    return None
