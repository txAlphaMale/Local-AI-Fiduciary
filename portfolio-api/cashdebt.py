"""
cashdebt.py — GAP-2 (final review): cash, liquidity & debt hygiene — the household-CFO desk.

Four recurring real-dollar questions no panel answered until now:
  · EMERGENCY FUND — months of ESSENTIAL spend (the funded-ratio "needs" line) covered by the
    reserve; target months is a knob (default 6). The reserve is whatever the household has
    explicitly DESIGNATED as an EF source (2026-08-08) — including a brokerage account held as
    reserve precisely because savings yields are poor — each counted up to its own optional cap,
    reported both nominally and after a market-drawdown haircut. Absent any designation it falls
    back to the old auto-detected-cash guess and says so.
  · IDLE-CASH DRAG — what parked cash forfeits vs the 3-month T-bill, in $/yr.
  · MORTGAGE PAYOFF vs INVEST — after-tax mortgage rate (TX + post-2017 standard deduction:
    interest is usually NOT effectively deductible, so after-tax ≈ nominal) vs the
    guaranteed alternative (T-bill) and the risky alternative (expected nominal return).
  · REFI BREAK-EVEN + AVALANCHE — months to recoup closing costs; non-mortgage debts ranked
    by rate against the invest hurdle, each with payments-left both standalone (its own payment,
    nothing else changes) and in-plan (the avalanche schedule, with rollover + extra $/mo).

Pure logic, injected inputs (balance sheet from the feed, rates from FRED, household facts
from the insurance/household register) — unit-tests offline. Decision-support, not advice.
State-of-residence-aware (RESUME-TRACKER item 3) for the mortgage-interest and Treasury-
interest notes below; the core math is federal-only and doesn't otherwise change by state.
"""

import math

import states

DEFAULT_KNOBS = {
    "ef_target_months": 6.0,
    "inflation_pct": 2.5,          # to build a NOMINAL hurdle from the plan's real return
    "deposit_yield_pct": 0.40,     # assumed yield on parked checking/savings cash (knob)
    "refi_term_years": 30,
    # ef_stress_drawdown_pct (2026-08-08, Jason): the equity drawdown the stress-adjusted
    # emergency-fund figure assumes. An emergency fund is disproportionately drawn during
    # recessions, which is exactly when equities are down — job loss and market drawdowns are
    # positively correlated, so counting a brokerage EF source at its sunny-day balance
    # overstates what's actually available in the scenario the fund exists for. -20% is a mild
    # recession-grade equity drawdown (2022 peak-to-trough on the S&P was ~-25%, 2020 ~-34%,
    # 2008 ~-57%) — deliberately conservative-but-not-doom. Cash and money-market sources are
    # unaffected; only market-risk tiers take the haircut.
    "ef_stress_drawdown_pct": 20.0,
}

# Liquidity tiers for emergency-fund sources — how fast a dollar can actually be in the
# household's hands, and what it costs to get it there. This is the ordering that matters for an
# EF, not the account's investment merit:
#   0 cash        — checking/savings/money-market: same-day, at par, no tax event.
#   1 taxable     — brokerage: T+1 settlement, market risk (haircut applies), realized gains are
#                   a taxable event in the year you tap it.
#   2 roth_basis  — Roth IRA CONTRIBUTIONS only: withdrawable anytime, tax- and penalty-free (the
#                   basis is NOT the balance — the app cannot see it, so this tier is only ever
#                   counted up to an explicit user-entered cap; see ef_sources()).
#   3 deferred    — 401(k)/traditional IRA/HSA/529: ordinary income tax plus a 10% early-
#                   withdrawal penalty (HSA: only for non-medical spending; 529: penalty on
#                   earnings). Not emergency money — ineligible for designation, and counted at
#                   zero if one ever arrives here anyway.
EF_TIERS = {
    "cash":       {"rank": 0, "stress": False, "label": "cash"},
    "taxable":    {"rank": 1, "stress": True,  "label": "taxable brokerage"},
    "roth_basis": {"rank": 2, "stress": True,  "label": "Roth contribution basis"},
    "deferred":   {"rank": 3, "stress": True,  "label": "tax-deferred (not EF-eligible)"},
}
EF_INELIGIBLE_TIERS = {"deferred"}


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


def remaining_payments(balance, rate_pct, payment, pay_in_full=False, max_months=600):
    """How many more monthly payments retire ONE debt on its own, at its own payment — the
    "if I change nothing" number (2026-08-08, Jason). Solves the standard amortization identity
    for n rather than iterating:

        n = -ln(1 - r·B/P) / ln(1+r)      (r = APR/12, P = monthly payment, B = balance)

    and degenerates to n = B/P when r == 0 (a 0%-APR promotional card is a real, common case
    here — two of the household's cards sit at 0% — and the log form is undefined there).
    Rounded UP: a final partial payment is still a payment you have to make.

    DELIBERATELY NOT ERROR-PRONE. Every branch that can't produce an honest number returns
    months=None with a machine-readable `status` and, where relevant, a `missing` list naming the
    exact fields to fill in, instead of raising or silently substituting a guess:
      · paid    — balance ≤ 0 (or pay_in_full set): nothing left to amortize, no number to show.
      · unknown — APR and/or payment missing. NOTE the asymmetry: a missing APR is only fatal
                  when there IS interest to model. We do NOT fall back to B/P when the APR is
                  merely unset, because that silently reports a 19.49% card as if it were 0% —
                  the one direction where a "helpful" guess is materially misleading (it
                  understates the payment count, sometimes by years).
      · never   — P ≤ B·r: the payment doesn't even cover one month's interest, so the balance
                  grows forever. The log argument would go ≤ 0 here; this branch is what keeps
                  the math from ever reaching it.
      · capped  — resolves, but past max_months (50 yr default). Reported as a floor, not a
                  false precision.

    `payment` is whatever the row has on file (the minimum payment today). Real credit-card
    minimums are a PERCENTAGE of the balance and therefore fall as the balance does, which
    stretches the true payoff well past this figure; a fixed-payment installment loan matches
    it closely. See `note` on the return."""
    b = _f(balance)          # NO default: an unparseable/absent balance is not a $0 balance, and
    r_pct = _f(rate_pct)     # reporting "0 payments left / paid" for one would be a lie the user
    p = _f(payment)          # has no way to spot.
    if pay_in_full:
        return {"months": None, "status": "paid", "missing": [],
                "note": "Marked paid-in-full each statement — not a balance being amortized."}
    if b is None:
        return {"months": None, "status": "unknown", "missing": ["balance"],
                "note": "No balance on file for this debt."}
    if b <= 0:
        return {"months": 0, "status": "paid", "missing": [],
                "note": "Zero balance — nothing left to pay."}
    missing = []
    if r_pct is None:
        missing.append("APR")
    if not p or p <= 0:
        missing.append("minimum payment")
    if missing:
        return {"months": None, "status": "unknown", "missing": missing,
                "note": "Enter " + " and ".join(missing) + " to get a payment count."}
    r = r_pct / 100.0 / 12.0
    if r <= 0:
        n = math.ceil(b / p)
        capped = n > max_months
        return {"months": (max_months if capped else int(n)),
                "status": ("capped" if capped else "ok"), "missing": [],
                "note": ("0% APR — the balance divided by the payment, no interest to carry. "
                         "Confirm when the promotional rate expires; this count assumes it doesn't.")}
    monthly_interest = b * r
    if p <= monthly_interest:
        return {"months": None, "status": "never", "missing": [],
                "monthly_interest": round(monthly_interest, 2),
                "note": (f"The ${p:,.0f} payment doesn't cover this month's ${monthly_interest:,.0f} "
                         "of interest — at this payment the balance never retires, it grows. "
                         "Raise the payment above the monthly interest first.")}
    try:
        n = -math.log(1.0 - (r * b / p)) / math.log(1.0 + r)
    except (ValueError, ZeroDivisionError):
        # Unreachable given the guards above; kept so a pathological input can never 500 a panel.
        return {"months": None, "status": "unknown", "missing": [],
                "note": "Balance, APR and payment don't resolve to a payoff schedule."}
    n = math.ceil(n)
    capped = n > max_months
    return {"months": (max_months if capped else int(n)),
            "status": ("capped" if capped else "ok"), "missing": [],
            "note": ("Payments left at this exact payment, this debt alone — no extra, no "
                     "rollover from other payoffs. A credit-card minimum is usually a PERCENTAGE "
                     "of the balance, so it shrinks as you pay and the real count runs longer; a "
                     "fixed installment payment matches this closely.")}


def ef_sources(sources, stress_drawdown_pct=20.0):
    """Price out the accounts the household has explicitly DESIGNATED as emergency-fund
    resources (2026-08-08, Jason).

    Why this exists: the emergency fund used to be whatever a name-substring heuristic
    recognized as "cash" (checking / savings / money market / a Cash-typed position). That
    heuristic is structurally unable to see a reserve deliberately parked somewhere with a real
    return — Jason's Wells Fargo investment account, opened precisely because savings and HYSA
    yields were unsatisfying — so the gauge reported a household with a funded reserve as
    1.8 months and "critical". Guessing from account names cannot fix that; only the household
    knows which balances are earmarked. This function consumes that designation.

    `sources` is a list of {name, balance, tier, cap, institution}:
      · tier — a key of EF_TIERS. Sets whether the market-drawdown haircut applies and how the
        source is described. Anything unrecognized is treated as `cash` (at par); anything in
        EF_INELIGIBLE_TIERS counts ZERO and is reported with a reason, so a 401(k) that somehow
        got designated can never quietly inflate the reserve.
      · cap — optional "count at most $X of this account". Blank/None counts the whole balance.
        This is what makes a partial earmark honest (only part of a brokerage is really the
        reserve) and it is MANDATORY in spirit for a Roth: the withdrawable amount is the
        contribution BASIS, which no feed exposes and which is nothing like the balance.

    Stress figure: the cap is applied to the ALREADY-drawn-down balance — min(cap, balance ×
    (1 − haircut)) — not the other way round. A $25k earmark inside a $60k brokerage is still
    fully available after a 20% drop ($48k remains); haircutting the cap itself would double-count
    the loss. For a Roth the same expression is exactly right for a different reason: a drawdown
    doesn't shrink your contribution basis, but it can drag the account value below it."""
    hair_pct = max(0.0, min(90.0, _f(stress_drawdown_pct, 20.0) or 0.0))
    rows, nominal, stressed = [], 0.0, 0.0
    for s in sources or []:
        if not isinstance(s, dict):
            continue   # a malformed entry skips itself rather than taking the whole panel down
        tier = s.get("tier") if s.get("tier") in EF_TIERS else "cash"
        meta = EF_TIERS[tier]
        bal = max(0.0, _f(s.get("balance"), 0.0) or 0.0)
        cap = _f(s.get("cap"))
        if cap is not None and cap < 0:
            cap = None
        eligible = tier not in EF_INELIGIBLE_TIERS
        hair = (hair_pct / 100.0) if meta["stress"] else 0.0
        if eligible:
            counted = bal if cap is None else min(bal, cap)
            counted_stress = bal * (1.0 - hair) if cap is None else min(cap, bal * (1.0 - hair))
        else:
            counted = counted_stress = 0.0
        rows.append({
            "name": s.get("name"), "institution": s.get("institution"),
            "tier": tier, "tier_label": meta["label"], "tier_rank": meta["rank"],
            "balance": round(bal, 2), "cap": cap, "eligible": eligible,
            "counted": round(counted, 2), "counted_stress": round(counted_stress, 2),
            "haircut_pct": round(hair * 100.0, 1),
            # A cap set long ago against a much smaller balance quietly under-reports the reserve
            # forever; surface it rather than letting it rot silently.
            "cap_binding": bool(cap is not None and bal > cap),
            "reason": (None if eligible else
                       "Tax-deferred — an early withdrawal costs ordinary income tax plus a 10% "
                       "penalty, so it isn't emergency money. Counted at $0."),
        })
        nominal += counted
        stressed += counted_stress
    rows.sort(key=lambda x: (x["tier_rank"], -x["counted"]))
    return {"rows": rows, "total": round(nominal, 2), "total_stress": round(stressed, 2),
            "stress_drawdown_pct": hair_pct,
            "cash_tier_total": round(sum(r["counted"] for r in rows if r["tier"] == "cash"), 2),
            "noncash_total": round(sum(r["counted"] for r in rows if r["tier"] != "cash"), 2),
            "ineligible": [r["name"] for r in rows if not r["eligible"]]}


def emergency_fund(cash_total, essential_monthly, target_months, sources=None,
                   stress_drawdown_pct=20.0):
    """Months of ESSENTIAL spend covered by the emergency reserve.

    `sources` (optional) is ef_sources()' output — the household's explicitly designated EF
    accounts. When present and non-empty it is authoritative and `cash_total` is ignored for the
    headline figure. When absent the function falls back to the old auto-detected-cash behavior
    unchanged, so a household that never designates anything sees exactly what it saw before —
    plus a prompt telling it the number is a guess and how to stop guessing.

    Two figures are always reported: NOMINAL (designated balances at face value) and STRESSED
    (market-risk tiers haircut by the drawdown knob). The stressed one is not pessimism theatre:
    the single most common reason to drain an emergency fund is job loss, job loss clusters in
    recessions, and recessions are when equities are down — the nominal figure quietly assumes
    those two events are independent when they are anything but."""
    m = _f(essential_monthly, 0.0)
    designated = bool(sources and sources.get("rows"))
    if designated:
        c = _f(sources.get("total"), 0.0)
        c_stress = _f(sources.get("total_stress"), c)
        basis = "designated"
    else:
        c = _f(cash_total, 0.0)
        c_stress = c   # auto-detected cash is cash — nothing to haircut
        basis = "auto-cash"
    months = round(c / m, 1) if m else None
    months_stress = round(c_stress / m, 1) if m else None

    def _status(mo):
        if mo is None:
            return None
        return "ok" if mo >= target_months else ("low" if mo >= target_months / 2 else "critical")

    # Designation is deliberately all-or-nothing: the moment ONE account is designated, the
    # name-guess fallback switches off completely, because a figure that mixes "what you told me"
    # with "what I guessed" is one nobody can interpret. Correct, but it has a sharp edge — the
    # household that designates only its brokerage watches its checking accounts silently drop OUT
    # of a number they were previously inside, which reads as the reserve shrinking. This surfaces
    # the spendable cash that is NOT designated, so leaving it out is always a visible choice
    # rather than an accident. (2026-08-08: hit immediately on the first real use.)
    undesignated_cash = round(max(0.0, _f(cash_total, 0.0)), 2) if designated else 0.0

    note = ("Essential spend = annual expenses × the funded-ratio needs share. Cash beyond "
            "the target is strategic dry powder or investable — see idle-cash drag.")
    if designated:
        note += (" Reserve = the accounts you designated as EF sources, each counted up to its own "
                 "cap. The stressed figure re-prices market-risk sources after a "
                 f"{_f(stress_drawdown_pct, 20.0):.0f}% drawdown, because the emergencies that "
                 "drain a reserve tend to arrive with bad markets attached.")
        if undesignated_cash > 0:
            note += (f" NOTE: ${undesignated_cash:,.0f} of spendable cash is NOT designated and so "
                     "is not counted above — designate those accounts too if you'd draw on them.")
    else:
        note += (" No accounts are designated as EF sources yet, so this figure is the app GUESSING "
                 "from account names — it counts checking/savings/money-market only and is blind to "
                 "a reserve held anywhere that earns a real return. Designate your actual EF "
                 "accounts to replace the guess.")
    return {"cash_total": round(c, 2), "essential_monthly": round(m, 2) if m else None,
            "months_covered": months, "target_months": target_months, "status": _status(months),
            "gap_dollars": (round(max(0.0, target_months * m - c), 2) if m else None),
            "ef_total": round(c, 2), "ef_total_stress": round(c_stress, 2),
            "months_covered_stress": months_stress, "status_stress": _status(months_stress),
            "gap_dollars_stress": (round(max(0.0, target_months * m - c_stress), 2) if m else None),
            "basis": basis, "designated": designated,
            "undesignated_cash": undesignated_cash,
            "stress_drawdown_pct": _f(stress_drawdown_pct, 20.0),
            "sources": (sources or {}).get("rows") or [],
            "noncash_total": (sources or {}).get("noncash_total", 0.0) if designated else 0.0,
            "note": note}


def idle_cash(cash_total, tbill_pct, deposit_yield_pct, ef_target_dollars, state=None,
              ef_covered_by_noncash=0.0):
    """Drag is always computed on ACTUAL parked cash — never on the designated EF total. Those two
    figures diverged the moment EF designation allowed a brokerage to be an EF source (2026-08-08):
    a Wells Fargo investment account earmarked as reserve is not sitting at a 0.4% deposit yield,
    so folding it into `cash_total` here would invent a five-figure phantom "drag" against money
    that is already earning a market return. Cash in, cash out.

    `ef_covered_by_noncash` is the slice of the EF target already met by NON-cash designated
    sources. Cash only has to cover the remainder, so anything past that remainder is genuinely
    excess and free to be moved into T-bills — without this the panel would keep calling cash
    "needed for the EF" that a brokerage reserve already covers."""
    sf = states.facts(state)
    c = _f(cash_total, 0.0)
    tb, dep = _f(tbill_pct, 0.0), _f(deposit_yield_pct, 0.0)
    spread = max(0.0, tb - dep)
    drag = round(c * spread / 100.0, 2)
    ef_target_from_cash = max(0.0, _f(ef_target_dollars, 0.0) - (_f(ef_covered_by_noncash, 0.0) or 0.0))
    excess = max(0.0, c - ef_target_from_cash)
    treasury_note = ("Treasury interest is exempt from state income tax in every state (moot in "
                     f"{sf['name']}, which has none anyway)." if sf["no_income_tax"] else
                     f"Treasury interest is exempt from {sf['name']} state income tax too — a real "
                     "edge over an in-state HYSA/money-market at the same headline yield.")
    return {"cash_total": round(c, 2), "tbill_pct": tb, "deposit_yield_pct": dep,
            "spread_pct": round(spread, 2), "drag_annual": drag,
            "excess_over_ef": round(excess, 2),
            "ef_target_from_cash": round(ef_target_from_cash, 2),
            "ef_covered_by_noncash": round(_f(ef_covered_by_noncash, 0.0) or 0.0, 2),
            "note": ("Drag assumes parked cash earns the deposit knob; T-bills/HYSA/money-market "
                     f"capture the spread with T+1 liquidity. {treasury_note} The excess-over-EF "
                     "slice is the first candidate.")}


def payoff_vs_invest(balance, rate_pct, tbill_pct, expected_real_pct, inflation_pct, state=None):
    b, r = _f(balance, 0.0), _f(rate_pct)
    if b <= 0 or r is None:
        return {"available": False,
                "note": "Set the mortgage rate in the household register to run payoff-vs-invest."}
    sf = states.facts(state)
    tb = _f(tbill_pct, 0.0)
    exp_nom = round(_f(expected_real_pct, 5.0) + _f(inflation_pct, 2.5), 2)
    after_tax = r   # post-2017 standard deduction: interest typically NOT effectively deductible
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
    if sf["no_income_tax"]:
        tax_note = (f"After-tax ≈ nominal because {sf['name']} has no state income tax and the "
                    "post-2017 federal standard deduction usually swallows itemized interest — if "
                    "you itemize FEDERALLY, the true after-tax rate is lower and the case for "
                    "prepaying weakens.")
    else:
        tax_note = (f"After-tax ≈ nominal shown here is FEDERAL-only. {sf['name']} does levy state "
                    f"income tax ({sf['income_tax_type']}"
                    + (f" {sf['income_tax_rate_pct']}%" if sf['income_tax_rate_pct'] else "")
                    + "), so if the state also allows an itemized mortgage-interest deduction and you "
                      "itemize (federally or at the state level, depending on the state's rules), the "
                      "true after-tax rate is lower than shown — not modeled here, since state "
                      "itemization conformity varies; check your state return.")
    return {"available": True, "balance": round(b, 2), "rate_pct": r, "after_tax_rate_pct": after_tax,
            "tbill_pct": tb, "expected_nominal_pct": exp_nom, "verdict": verdict, "msg": msg,
            "note": tax_note + " Liquidity is one-way: principal paid in is hard to get back out."}


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
    """Non-mortgage debts ranked by rate; anything above the hurdle is 'pay first'.
    Also ranks by real DOLLAR cost (RESUME-TRACKER 2026-07-20, Jason's "lazy" assessment):
    rate-only ranking can bury a large low-rate balance's real $/yr cost behind a tiny
    high-rate one (e.g. a $21 card at 19.49% vs a $16,000 loan at 7.7%) — `dollar_rank` and
    `top_dollar_cost`/`rate_vs_dollar_disagree` surface that without changing the existing
    rate-based `priority`/row order, which is still the correct avalanche METHOD (rate is the
    right thing to target first for the guaranteed-return argument; dollar cost is a separate,
    complementary fact worth seeing alongside it, not a replacement).

    Two carve-outs on top of the rate-based `priority` (2026-07-23, Jason):
    - balance <= 0 never reads 'pay-first', regardless of APR — there is nothing to pay off, and
      the old behavior (e.g. a $0-balance 16% card still flagged pay-first) fired a $0/yr "avalanche
      target" alert that was pure noise.
    - `pay_in_full=True` (a per-account override the household sets when they know a card is a
      transactor paid off every statement, not real revolving debt) forces 'paid-in-full' and zeroes
      `annual_interest`, since no interest is actually being charged on it, regardless of whatever
      balance happens to be sitting on it mid-cycle. This is a household-supplied fact the balance
      snapshot alone can never distinguish from real revolving debt.

    ROW ORDER (2026-07-23, Jason: "I expect it to ... list the debts in order of which need to be
    paid off first"): grouped by `priority` first -- pay-first, then carry, then set-rate, then
    paid-in-full -- and rate-descending WITHIN each group. This is not a new ranking method, it's
    making the existing one legible: strict rate-descending order (the old sort) interleaved
    zero-balance/paid-in-full/low-rate rows among the real targets, so the table didn't read
    top-to-bottom as an attack sequence even though `priority` already encoded one. Rate-first
    IS still the actual interest-minimizing sequence -- `payoff_simulator()` below runs the real
    month-by-month avalanche-vs-snowball comparison and avalanche (rate order) always wins or ties
    on total interest by construction, since every dollar of extra payment is guaranteed to be
    earning its highest available rate of return at each step. Paid-in-full rows sink to the bottom
    because they're not part of the payoff project at all, not because they're low-priority debt."""
    rows = []
    for d in debts or []:
        bal, rate = _f(d.get("balance"), 0.0), _f(d.get("rate_pct"))
        minp = _f(d.get("min_payment"))
        pay_in_full = bool(d.get("pay_in_full"))
        if pay_in_full:
            priority = "paid-in-full"
        elif rate is None:
            priority = "set-rate"
        elif bal <= 0:
            priority = "carry"
        elif rate > invest_hurdle_pct:
            priority = "pay-first"
        else:
            priority = "carry"
        annual_interest = 0.0 if pay_in_full else (round(bal * rate / 100.0, 2) if rate is not None else None)
        # 2026-08-08, Jason: payments-left per row. Standalone (this debt, its own payment, no
        # rollover) — the "if I change nothing" count. The complementary "if I follow the plan"
        # month is stamped on by run() from payoff_simulator()'s per-debt retirement schedule,
        # which is where rollover and the extra $/mo actually live.
        rp = remaining_payments(bal, rate, minp, pay_in_full)
        rows.append({"name": d.get("name"), "balance": round(bal, 2), "rate_pct": rate,
                     "min_payment": minp, "pay_in_full": pay_in_full,
                     "priority": priority, "annual_interest": annual_interest,
                     "remaining_payments": rp["months"],
                     "remaining_payments_status": rp["status"],
                     "remaining_payments_missing": rp["missing"],
                     "remaining_payments_note": rp["note"],
                     "plan_months": None})   # filled by run() from the simulator, when available
    _PRIORITY_ORDER = {"pay-first": 0, "carry": 1, "set-rate": 2, "paid-in-full": 3}
    rows.sort(key=lambda x: (_PRIORITY_ORDER.get(x["priority"], 9), -(x["rate_pct"] or -1)))
    by_dollar = sorted([r for r in rows if r["annual_interest"] is not None],
                       key=lambda x: -x["annual_interest"])
    for rank, r in enumerate(by_dollar, start=1):
        r["dollar_rank"] = rank
    top_rate_name = rows[0]["name"] if rows and rows[0]["rate_pct"] is not None else None
    top_dollar_name = by_dollar[0]["name"] if by_dollar else None
    return {"rows": rows, "invest_hurdle_pct": invest_hurdle_pct,
            "top_dollar_cost": by_dollar[0] if by_dollar else None,
            "rate_vs_dollar_disagree": bool(top_rate_name and top_dollar_name and top_rate_name != top_dollar_name),
            "note": ("Avalanche = highest rate first; any debt costing more than the expected "
                     "nominal return is a guaranteed-return prepayment target. 'set-rate' rows "
                     "need their APR entered in the register. Rate ranks the guaranteed RETURN of "
                     "paying a debt off first; dollar_rank ranks actual $/yr interest cost — a "
                     "large low-rate balance can cost more in real dollars than a tiny high-rate "
                     "one, even though the smaller one is still the mathematically better target.")}


def payoff_simulator(debts, extra_monthly=0.0, max_months=600):
    """Month-by-month amortization comparing AVALANCHE (highest rate first) against SNOWBALL
    (smallest balance first), with each retired debt's own minimum payment rolling PERMANENTLY
    onto the remaining queue (the real "snowball" behavior Jason described — a payoff frees
    capacity for every month after, not just the one it happens in), plus any user-chosen extra
    monthly budget. Within a single month, if the current target's balance is smaller than the
    available extra pool, the leftover cascades to the next debt in priority order the SAME
    month rather than being wasted.

    Interest accrues monthly on the remaining balance (simple monthly compounding) — a
    reasonable decision-support approximation, not a precise daily-accrual schedule; a debt
    with an intro/variable rate will drift from this estimate once its real rate changes.

    Only debts with BOTH an APR and a minimum payment set are simulated — a guessed minimum
    payment could materially mislead a real payoff schedule, so a debt missing either is
    reported in `excluded` (with which field is missing) instead of silently assumed. Debts
    flagged `pay_in_full` are skipped outright (2026-07-23) — they're not real revolving debt
    being paid down over time, so they don't belong in an amortization schedule regardless of
    whatever balance/APR happen to be on file for them."""
    ready, excluded = [], []
    for d in (debts or []):
        name = d.get("name")
        bal = _f(d.get("balance"), 0.0)
        rate = _f(d.get("rate_pct"))
        minp = _f(d.get("min_payment"))
        if bal <= 0 or d.get("pay_in_full"):
            continue
        if rate is None or not minp:
            missing = []
            if rate is None:
                missing.append("APR")
            if not minp:
                missing.append("minimum payment")
            excluded.append({"name": name, "balance": round(bal, 2), "missing": missing})
            continue
        ready.append({"name": name, "balance": bal, "rate_pct": rate, "min_payment": minp})
    if not ready:
        return {"available": False, "excluded": excluded,
                "note": "Enter both an APR and a minimum payment for at least one debt to run the payoff simulator."}

    def _simulate(order_key):
        sim = [dict(d) for d in ready]
        months, total_interest = 0, 0.0
        # retired_at{name: month} (2026-08-08, Jason): the month each individual debt hits zero
        # under this strategy. Falls straight out of the loop that was already running — it's the
        # "if I follow the plan" companion to remaining_payments()' standalone count, and unlike
        # that one it DOES include rolled-over minimums and the extra $/mo, so it's normally the
        # shorter of the two for high-priority debts and the longer one for whatever sits last in
        # the queue.
        retired_at = {}
        extra_pool_monthly = _f(extra_monthly, 0.0) or 0.0
        while any(d["balance"] > 0.01 for d in sim) and months < max_months:
            months += 1
            for d in sim:
                if d["balance"] > 0:
                    interest = d["balance"] * (d["rate_pct"] / 100.0 / 12.0)
                    d["balance"] += interest
                    total_interest += interest
            for d in sim:
                if d["balance"] <= 0:
                    continue
                pay = min(d["min_payment"], d["balance"])
                d["balance"] -= pay
                if d["balance"] <= 0.01:
                    d["balance"] = 0.0
                    retired_at.setdefault(d["name"], months)
                    extra_pool_monthly += d["min_payment"]   # freed permanently, from this month on
            remaining = sorted((d for d in sim if d["balance"] > 0.01), key=order_key)
            pool = extra_pool_monthly
            for d in remaining:
                if pool <= 0:
                    break
                pay = min(pool, d["balance"])
                d["balance"] -= pay
                pool -= pay
                if d["balance"] <= 0.01:
                    d["balance"] = 0.0
                    retired_at.setdefault(d["name"], months)
                    extra_pool_monthly += d["min_payment"]   # this one also frees up, for NEXT month
        resolved = months < max_months
        return {"months": months if resolved else None, "resolved": resolved,
                "total_interest": round(total_interest, 2),
                "retired_at": (retired_at if resolved else {})}

    av = _simulate(lambda d: -d["rate_pct"])
    sn = _simulate(lambda d: d["balance"])
    return {"available": True, "extra_monthly": _f(extra_monthly, 0.0) or 0.0, "excluded": excluded,
            "debts_simulated": [d["name"] for d in ready], "avalanche": av, "snowball": sn,
            "interest_saved_by_avalanche": (round(sn["total_interest"] - av["total_interest"], 2)
                                             if av["resolved"] and sn["resolved"] else None),
            "note": ("Avalanche targets the highest-rate debt first; snowball targets the "
                     "smallest balance first. Each payoff frees that debt's own minimum payment, "
                     "which rolls onto the remaining queue for every month after (true rollover), "
                     "plus your extra monthly budget above. Avalanche minimizes total interest by "
                     "construction; snowball can still be the better real-world choice if the "
                     "faster small-balance wins keep you paying at all — a behavioral tradeoff, "
                     "not a math error.")}


def run(inputs, knobs=None):
    k = {**DEFAULT_KNOBS, **(knobs or {})}
    i = inputs or {}
    essential_mo = None
    if _f(i.get("annual_expenses")) and _f(i.get("needs_pct"), 65.0):
        essential_mo = _f(i["annual_expenses"]) * _f(i.get("needs_pct"), 65.0) / 100.0 / 12.0
    efs = ef_sources(i.get("ef_sources"), k["ef_stress_drawdown_pct"])
    ef = emergency_fund(i.get("cash_total"), essential_mo, k["ef_target_months"],
                        sources=(efs if efs["rows"] else None),
                        stress_drawdown_pct=k["ef_stress_drawdown_pct"])
    ef["source_detail"] = efs
    ef_target = (ef["essential_monthly"] or 0) * k["ef_target_months"]
    dep = _f(i.get("deposit_yield_pct"), k["deposit_yield_pct"])
    ic = idle_cash(i.get("cash_total"), i.get("tbill_pct"), dep, ef_target, i.get("state"),
                   ef_covered_by_noncash=(efs["noncash_total"] if ef["designated"] else 0.0))
    exp_nom = round(_f(i.get("expected_real_pct"), 5.0) + k["inflation_pct"], 2)
    mort = payoff_vs_invest(i.get("mortgage_balance"), i.get("mortgage_rate_pct"),
                            i.get("tbill_pct"), i.get("expected_real_pct"), k["inflation_pct"],
                            i.get("state"))
    refi = refi_break_even(i.get("mortgage_balance"), i.get("mortgage_payment_monthly"),
                           i.get("refi_rate_pct"), i.get("refi_closing_costs"),
                           _f(i.get("refi_term_years"), k["refi_term_years"]))
    av = avalanche(i.get("debts"), exp_nom)
    payoff = payoff_simulator(i.get("debts"), i.get("debt_extra_monthly"))
    # Stamp each avalanche row with the month it retires under the AVALANCHE plan (rollover +
    # extra $/mo), alongside the standalone count avalanche() already computed. Only the
    # avalanche schedule is merged in: it's the row order the table is sorted by and the strategy
    # the panel recommends, so mixing in snowball months would be comparing two different plans
    # inside one column.
    _retired = ((payoff.get("avalanche") or {}).get("retired_at") or {}) if payoff.get("available") else {}
    for _r in av["rows"]:
        _r["plan_months"] = _retired.get(_r["name"])
    alerts = []
    if ef["status"] == "critical":
        alerts.append({"level": "action", "msg": f"Emergency fund covers only "
                       f"{ef['months_covered']} months of essentials (target "
                       f"{k['ef_target_months']:.0f}) — gap ${ef['gap_dollars']:,.0f}."
                       + ("" if ef["designated"] else
                          " No EF source accounts are designated yet, so this counts auto-detected "
                          "cash only — designate the accounts you'd actually draw on before "
                          "acting on this number.")})
    elif ef["status"] == "low":
        alerts.append({"level": "warn", "msg": f"Emergency fund at {ef['months_covered']} months "
                       f"vs the {k['ef_target_months']:.0f}-month target."})
    # Funded on paper, short in the scenario the fund exists for. Only worth saying when the
    # nominal figure is NOT already raising an alarm of its own.
    if ef["status"] == "ok" and ef["status_stress"] in ("low", "critical"):
        alerts.append({"level": "warn",
                       "msg": f"Emergency fund is funded at {ef['months_covered']} months today but "
                              f"only {ef['months_covered_stress']} after a "
                              f"{k['ef_stress_drawdown_pct']:.0f}% market drawdown — and a drawdown "
                              "is the environment that typically triggers the need."})
    if ef.get("undesignated_cash", 0) > 0:
        alerts.append({"level": "info",
                       "msg": f"${ef['undesignated_cash']:,.0f} of spendable cash isn't designated "
                              "as an emergency-fund source, so it isn't in the figure above. "
                              "Designating one account switches off the name-based guess entirely "
                              "— check your checking/savings accounts too if you'd draw on them."})
    for _s in efs["rows"]:
        if not _s["eligible"]:
            alerts.append({"level": "warn",
                           "msg": f"{_s['name']} is designated as an emergency-fund source but is "
                                  "tax-deferred — counted at $0. Tapping it early costs income tax "
                                  "plus a 10% penalty."})
        elif _s["tier"] == "roth_basis" and _s["cap"] is None:
            alerts.append({"level": "warn",
                           "msg": f"{_s['name']} (Roth) is counted at its FULL balance — only your "
                                  "contribution basis comes out tax- and penalty-free. Set a cap "
                                  "equal to your basis, or the reserve is overstated."})
    if ic["drag_annual"] >= 500:
        alerts.append({"level": "warn", "msg": f"Idle cash is forfeiting ~${ic['drag_annual']:,.0f}/yr "
                       f"vs the {ic['tbill_pct']}% T-bill."})
    for r_ in av["rows"]:
        if r_["priority"] == "pay-first":
            alerts.append({"level": "warn", "msg": f"{r_['name']}: {r_['rate_pct']}% APR beats the "
                           f"{exp_nom}% invest hurdle — avalanche target "
                           f"(~${r_['annual_interest']:,.0f}/yr interest)."})
    if payoff.get("available") and payoff["avalanche"]["resolved"] and payoff["snowball"]["resolved"]:
        alerts.append({"level": "info",
                       "msg": f"At ${payoff['extra_monthly']:,.0f}/mo extra: avalanche clears "
                              f"non-mortgage debt in {payoff['avalanche']['months']} months "
                              f"(${payoff['avalanche']['total_interest']:,.0f} interest) vs "
                              f"snowball's {payoff['snowball']['months']} months "
                              f"(${payoff['snowball']['total_interest']:,.0f} interest)."})
    return {"emergency_fund": ef, "idle_cash": ic, "mortgage": mort, "refi": refi,
            "avalanche": av, "payoff_simulator": payoff, "alerts": alerts, "knobs": k,
            "note": ("Cash, liquidity and debt hygiene from the live balance sheet. Educational "
                     "decision-support — liquidity and job-security context always trumps the "
                     "spreadsheet on emergency-fund sizing.")}
