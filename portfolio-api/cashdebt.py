"""
cashdebt.py — GAP-2 (final review): cash, liquidity & debt hygiene — the household-CFO desk.

Four recurring real-dollar questions no panel answered until now:
  · EMERGENCY FUND — months of ESSENTIAL spend (the funded-ratio "needs" line) covered by
    actual cash; target months is a knob (default 6).
  · IDLE-CASH DRAG — what parked cash forfeits vs the 3-month T-bill, in $/yr.
  · MORTGAGE PAYOFF vs INVEST — after-tax mortgage rate (TX + post-2017 standard deduction:
    interest is usually NOT effectively deductible, so after-tax ≈ nominal) vs the
    guaranteed alternative (T-bill) and the risky alternative (expected nominal return).
  · REFI BREAK-EVEN + AVALANCHE — months to recoup closing costs; non-mortgage debts ranked
    by rate against the invest hurdle.

Pure logic, injected inputs (balance sheet from the feed, rates from FRED, household facts
from the insurance/household register) — unit-tests offline. Decision-support, not advice.
"""

DEFAULT_KNOBS = {
    "ef_target_months": 6.0,
    "inflation_pct": 2.5,          # to build a NOMINAL hurdle from the plan's real return
    "deposit_yield_pct": 0.40,     # assumed yield on parked checking/savings cash (knob)
    "refi_term_years": 30,
}


def _f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def monthly_payment(balance, rate_pct, term_years):
    """Standard amortized P&I payment."""
    b, n = _f(balance, 0.0), int(_f(term_years, 30) * 12)
    r = _f(rate_pct, 0.0) / 100.0 / 12.0
    if b <= 0 or n <= 0:
        return 0.0
    if r <= 0:
        return round(b / n, 2)
    return round(b * r / (1 - (1 + r) ** (-n)), 2)


def emergency_fund(cash_total, essential_monthly, target_months):
    c, m = _f(cash_total, 0.0), _f(essential_monthly, 0.0)
    months = round(c / m, 1) if m else None
    status = None
    if months is not None:
        status = "ok" if months >= target_months else ("low" if months >= target_months / 2 else "critical")
    return {"cash_total": round(c, 2), "essential_monthly": round(m, 2) if m else None,
            "months_covered": months, "target_months": target_months, "status": status,
            "gap_dollars": (round(max(0.0, target_months * m - c), 2) if m else None),
            "note": ("Essential spend = annual expenses × the funded-ratio needs share. Cash beyond "
                     "the target is strategic dry powder or investable — see idle-cash drag.")}


def idle_cash(cash_total, tbill_pct, deposit_yield_pct, ef_target_dollars):
    c = _f(cash_total, 0.0)
    tb, dep = _f(tbill_pct, 0.0), _f(deposit_yield_pct, 0.0)
    spread = max(0.0, tb - dep)
    drag = round(c * spread / 100.0, 2)
    excess = max(0.0, c - _f(ef_target_dollars, 0.0))
    return {"cash_total": round(c, 2), "tbill_pct": tb, "deposit_yield_pct": dep,
            "spread_pct": round(spread, 2), "drag_annual": drag,
            "excess_over_ef": round(excess, 2),
            "note": ("Drag assumes parked cash earns the deposit knob; T-bills/HYSA/money-market "
                     "capture the spread with T+1 liquidity (Treasuries are also TX-friendly: no "
                     "state tax anyway, but exempt where it matters for others). The excess-over-EF "
                     "slice is the first candidate.")}


def payoff_vs_invest(balance, rate_pct, tbill_pct, expected_real_pct, inflation_pct):
    b, r = _f(balance, 0.0), _f(rate_pct)
    if b <= 0 or r is None:
        return {"available": False,
                "note": "Set the mortgage rate in the household register to run payoff-vs-invest."}
    tb = _f(tbill_pct, 0.0)
    exp_nom = round(_f(expected_real_pct, 5.0) + _f(inflation_pct, 2.5), 2)
    after_tax = r   # TX + standard deduction: interest typically NOT effectively deductible
    if after_tax > exp_nom:
        verdict = "payoff-favored"
        msg = (f"After-tax mortgage cost {after_tax:.2f}% exceeds even the risky expected return "
               f"{exp_nom:.2f}% — extra principal is the better *expected* use of marginal dollars.")
    elif after_tax > tb:
        verdict = "risk-preference"
        msg = (f"Rate {after_tax:.2f}% sits between the guaranteed {tb:.2f}% (T-bill) and the risky "
               f"{exp_nom:.2f}% expected return: paying principal is a GUARANTEED {after_tax:.2f}% "
               "return; investing wins only if risk shows up on your side. A split is defensible.")
    else:
        verdict = "keep-mortgage"
        msg = (f"Rate {after_tax:.2f}% is at/below the T-bill {tb:.2f}% — the mortgage is cheap "
               "leverage; even riskless cash out-earns prepayment. Don't prepay.")
    return {"available": True, "balance": round(b, 2), "rate_pct": r, "after_tax_rate_pct": after_tax,
            "tbill_pct": tb, "expected_nominal_pct": exp_nom, "verdict": verdict, "msg": msg,
            "note": ("After-tax ≈ nominal because TX has no state tax and the post-2017 standard "
                     "deduction usually swallows itemized interest — if you itemize, the true "
                     "after-tax rate is lower and the case for prepaying weakens. Liquidity is "
                     "one-way: principal paid in is hard to get back out.")}


def refi_break_even(balance, current_payment, refi_rate_pct, closing_costs, term_years):
    b, cp = _f(balance, 0.0), _f(current_payment)
    rr, cc = _f(refi_rate_pct), _f(closing_costs, 0.0)
    if b <= 0 or not cp or rr is None:
        return {"available": False,
                "note": "Set current P&I payment + a candidate refi rate to run the break-even."}
    newp = monthly_payment(b, rr, term_years)
    save = round(cp - newp, 2)
    be = round(cc / save, 1) if save > 0 else None
    return {"available": True, "balance": round(b, 2), "current_payment": cp,
            "refi_rate_pct": rr, "new_payment": newp, "monthly_savings": save,
            "closing_costs": cc, "break_even_months": be, "term_years": term_years,
            "worth_it": (be is not None and be <= 36),
            "note": ("Break-even = closing costs ÷ monthly savings; rule-of-thumb: refinance when "
                     "you'll hold past break-even (≤36mo is comfortable). CAVEAT: comparing against "
                     "a fresh full term restarts amortization — total interest can rise even as the "
                     "payment falls; match the remaining term for the honest comparison.")}


def avalanche(debts, invest_hurdle_pct):
    """Non-mortgage debts ranked by rate; anything above the hurdle is 'pay first'."""
    rows = []
    for d in debts or []:
        bal, rate = _f(d.get("balance"), 0.0), _f(d.get("rate_pct"))
        rows.append({"name": d.get("name"), "balance": round(bal, 2), "rate_pct": rate,
                     "priority": ("set-rate" if rate is None else
                                  ("pay-first" if rate > invest_hurdle_pct else "carry")),
                     "annual_interest": (round(bal * rate / 100.0, 2) if rate is not None else None)})
    rows.sort(key=lambda x: -(x["rate_pct"] or -1))
    return {"rows": rows, "invest_hurdle_pct": invest_hurdle_pct,
            "note": ("Avalanche = highest rate first; any debt costing more than the expected "
                     "nominal return is a guaranteed-return prepayment target. 'set-rate' rows "
                     "need their APR entered in the register.")}


def run(inputs, knobs=None):
    k = {**DEFAULT_KNOBS, **(knobs or {})}
    i = inputs or {}
    essential_mo = None
    if _f(i.get("annual_expenses")) and _f(i.get("needs_pct"), 65.0):
        essential_mo = _f(i["annual_expenses"]) * _f(i.get("needs_pct"), 65.0) / 100.0 / 12.0
    ef = emergency_fund(i.get("cash_total"), essential_mo, k["ef_target_months"])
    ef_target = (ef["essential_monthly"] or 0) * k["ef_target_months"]
    dep = _f(i.get("deposit_yield_pct"), k["deposit_yield_pct"])
    ic = idle_cash(i.get("cash_total"), i.get("tbill_pct"), dep, ef_target)
    exp_nom = round(_f(i.get("expected_real_pct"), 5.0) + k["inflation_pct"], 2)
    mort = payoff_vs_invest(i.get("mortgage_balance"), i.get("mortgage_rate_pct"),
                            i.get("tbill_pct"), i.get("expected_real_pct"), k["inflation_pct"])
    refi = refi_break_even(i.get("mortgage_balance"), i.get("mortgage_payment_monthly"),
                           i.get("refi_rate_pct"), i.get("refi_closing_costs"),
                           _f(i.get("refi_term_years"), k["refi_term_years"]))
    av = avalanche(i.get("debts"), exp_nom)
    alerts = []
    if ef["status"] == "critical":
        alerts.append({"level": "action", "msg": f"Emergency fund covers only "
                       f"{ef['months_covered']} months of essentials (target "
                       f"{k['ef_target_months']:.0f}) — gap ${ef['gap_dollars']:,.0f}."})
    elif ef["status"] == "low":
        alerts.append({"level": "warn", "msg": f"Emergency fund at {ef['months_covered']} months "
                       f"vs the {k['ef_target_months']:.0f}-month target."})
    if ic["drag_annual"] >= 500:
        alerts.append({"level": "warn", "msg": f"Idle cash is forfeiting ~${ic['drag_annual']:,.0f}/yr "
                       f"vs the {ic['tbill_pct']}% T-bill."})
    for r_ in av["rows"]:
        if r_["priority"] == "pay-first":
            alerts.append({"level": "warn", "msg": f"{r_['name']}: {r_['rate_pct']}% APR beats the "
                           f"{exp_nom}% invest hurdle — avalanche target "
                           f"(~${r_['annual_interest']:,.0f}/yr interest)."})
    return {"emergency_fund": ef, "idle_cash": ic, "mortgage": mort, "refi": refi,
            "avalanche": av, "alerts": alerts, "knobs": k,
            "note": ("Cash, liquidity and debt hygiene from the live balance sheet. Educational "
                     "decision-support — liquidity and job-security context always trumps the "
                     "spreadsheet on emergency-fund sizing.")}
