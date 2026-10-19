# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
realestate.py — rental real-estate deal underwriter (Fable G9).

Real estate is a named wealth-building avenue the app only ever ASSUMED (the AVENUES cap-rate
row). This module underwrites an actual deal: the income statement (NOI/cap rate), the financing
(amortized debt service, DSCR), the levered cash return (cash-on-cash), the tax shield
(depreciation, optional cost-seg / bonus), and the whole-hold after-tax levered IRR including the
sale (depreciation recapture at 25% + LTCG on appreciation), with a 1031-exchange note. Pure
arithmetic — the app supplies the deal inputs; county comps stay manual. Decision-support only.
"""


def _pmt(principal, annual_rate_pct, years):
    """Level annual debt service on a fully-amortizing loan (interest compounded monthly, paid
    as 12 monthly payments summed to an annual figure)."""
    n = int(years * 12)
    r = (annual_rate_pct / 100.0) / 12.0
    if n <= 0:
        return 0.0
    if r == 0:
        return principal / years
    monthly = principal * r / (1 - (1 + r) ** (-n))
    return monthly * 12.0


def _remaining_balance(principal, annual_rate_pct, years, months_paid):
    """Loan balance after `months_paid` monthly payments (standard amortization)."""
    n = int(years * 12)
    r = (annual_rate_pct / 100.0) / 12.0
    if n <= 0:
        return 0.0
    if r == 0:
        return max(0.0, principal * (1 - months_paid / n))
    monthly = principal * r / (1 - (1 + r) ** (-n))
    bal = principal * (1 + r) ** months_paid - monthly * (((1 + r) ** months_paid - 1) / r)
    return max(0.0, bal)


def _interest_for_year(principal, annual_rate_pct, years, year_idx):
    """Total mortgage interest paid during calendar year `year_idx` (1-based)."""
    start = _remaining_balance(principal, annual_rate_pct, years, (year_idx - 1) * 12)
    end = _remaining_balance(principal, annual_rate_pct, years, year_idx * 12)
    ds = _pmt(principal, annual_rate_pct, years)
    principal_paid = start - end
    return max(0.0, ds - principal_paid)


def _irr(cashflows, lo=-0.95, hi=1.0):
    """Annual IRR of a cashflow series (index = year). Bisection on NPV=0; None if no sign change."""
    def npv(r):
        return sum(cf / ((1 + r) ** i) for i, cf in enumerate(cashflows))
    flo, fhi = npv(lo), npv(hi)
    if flo == 0:
        return lo
    if flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        fm = npv(mid)
        if abs(fm) < 1e-6:
            return round(mid, 5)
        if flo * fm < 0:
            hi = mid
        else:
            lo, flo = mid, fm
    return round((lo + hi) / 2.0, 5)


def _g(inp, key, d=0.0):
    try:
        return float(inp.get(key, d))
    except (TypeError, ValueError):
        return d


def underwrite(inp):
    """Underwrite one rental deal. inp keys (all optional, sensible defaults):
    price, down_pct, closing_pct, loan_rate_pct, term_years, gross_annual_rent, vacancy_pct,
    opex_annual (or opex_pct of EGI), annual_appreciation_pct, rent_growth_pct, hold_years,
    sale_cost_pct, land_frac, depr_years, bonus_depr_pct, marginal_tax_pct, ltcg_pct."""
    price = _g(inp, "price", 400000)
    down_pct = _g(inp, "down_pct", 25)
    closing_pct = _g(inp, "closing_pct", 3)
    rate = _g(inp, "loan_rate_pct", 6.57)
    term = _g(inp, "term_years", 30)
    gross_rent = _g(inp, "gross_annual_rent", price * 0.08)     # ~8% gross rent default
    vacancy = _g(inp, "vacancy_pct", 5)
    opex_annual = _g(inp, "opex_annual", 0)
    opex_pct = _g(inp, "opex_pct", 35)                          # % of EGI if opex_annual not given
    appr = _g(inp, "annual_appreciation_pct", 3)
    rent_growth = _g(inp, "rent_growth_pct", 2)
    hold = int(_g(inp, "hold_years", 10)) or 10
    sale_cost_pct = _g(inp, "sale_cost_pct", 7)
    land_frac = _g(inp, "land_frac", 0.2)
    depr_years = _g(inp, "depr_years", 27.5)
    bonus_pct = _g(inp, "bonus_depr_pct", 0)                    # cost-seg / bonus in year 1
    mtax = _g(inp, "marginal_tax_pct", 24) / 100.0
    ltcg = _g(inp, "ltcg_pct", 15) / 100.0

    loan = price * (1 - down_pct / 100.0)
    down = price - loan
    closing = price * closing_pct / 100.0
    cash_invested = down + closing
    egi0 = gross_rent * (1 - vacancy / 100.0)
    opex0 = opex_annual if opex_annual > 0 else egi0 * opex_pct / 100.0
    noi0 = egi0 - opex0
    cap_rate = (noi0 / price * 100.0) if price else None
    debt_service = _pmt(loan, rate, term)
    dscr = (noi0 / debt_service) if debt_service else None
    pretax_cf0 = noi0 - debt_service
    coc = (pretax_cf0 / cash_invested * 100.0) if cash_invested else None

    building_basis = price * (1 - land_frac)
    straight_line = building_basis / depr_years if depr_years else 0.0
    bonus_dep = building_basis * bonus_pct / 100.0

    # whole-hold after-tax cashflow series for IRR
    flows = [-cash_invested]
    egi, opex = egi0, opex0
    total_depr_taken = 0.0
    for yr in range(1, hold + 1):
        if yr > 1:
            egi *= (1 + rent_growth / 100.0)
            opex *= (1 + rent_growth / 100.0)
        noi = egi - opex
        interest = _interest_for_year(loan, rate, term, yr)
        depr = straight_line + (bonus_dep if yr == 1 else 0.0)
        total_depr_taken += depr
        taxable = noi - interest - depr
        tax = taxable * mtax                       # negative = passive shelter (assume usable)
        pretax_cf = noi - debt_service
        after_tax_cf = pretax_cf - tax
        if yr == hold:
            sale_price = price * (1 + appr / 100.0) ** hold
            sale_costs = sale_price * sale_cost_pct / 100.0
            payoff = _remaining_balance(loan, rate, term, hold * 12)
            net_before_tax = sale_price - sale_costs - payoff
            recapture = total_depr_taken * 0.25    # §1250 unrecaptured, capped at 25%
            gain_over_basis = (sale_price - sale_costs) - (price - total_depr_taken)
            ltcg_gain = max(0.0, gain_over_basis - total_depr_taken)
            sale_tax = recapture + ltcg_gain * ltcg
            after_tax_cf += net_before_tax - sale_tax
        flows.append(after_tax_cf)

    irr = _irr(flows)
    return {
        "price": round(price, 0), "loan": round(loan, 0), "down_payment": round(down, 0),
        "closing_costs": round(closing, 0), "cash_invested": round(cash_invested, 0),
        "egi_year1": round(egi0, 0), "opex_year1": round(opex0, 0), "noi_year1": round(noi0, 0),
        "cap_rate_pct": round(cap_rate, 2) if cap_rate is not None else None,
        "annual_debt_service": round(debt_service, 0),
        "dscr": round(dscr, 2) if dscr is not None else None,
        "pretax_cashflow_year1": round(pretax_cf0, 0),
        "cash_on_cash_pct": round(coc, 2) if coc is not None else None,
        "annual_depreciation": round(straight_line, 0), "bonus_depreciation_year1": round(bonus_dep, 0),
        "hold_years": hold, "after_tax_irr_pct": round(irr * 100, 2) if irr is not None else None,
        "sale_price_est": round(price * (1 + appr / 100.0) ** hold, 0),
        "cashflows": [round(f, 0) for f in flows],
        "flags": _flags(cap_rate, dscr, coc, dscr),
        "note": "Levered after-tax IRR over the hold, including sale with depreciation recapture "
                "(25%) + LTCG on appreciation. DSCR < 1.25 is a lender/again-risk flag; cap rate and "
                "cash-on-cash gauge the income. Passive losses assumed usable (real-estate-pro or "
                "income limits may defer them — confirm with a CPA). A 1031 exchange defers the "
                "entire sale tax into the next property. County rent/expense comps are your input.",
    }


def _flags(cap, dscr, coc, _):
    out = []
    if dscr is not None and dscr < 1.25:
        out.append(f"DSCR {dscr:.2f} < 1.25 — thin debt coverage (lender + vacancy risk)")
    if cap is not None and cap < 5:
        out.append(f"cap rate {cap:.1f}% < 5% — priced for appreciation, not income")
    if coc is not None and coc < 0:
        out.append(f"cash-on-cash {coc:.1f}% negative — the deal bleeds cash year 1")
    return out
