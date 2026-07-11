"""
bizacq.py — small-business acquisition underwriter (Fable G11).

The all-avenue directive's business-ownership leg: underwrite buying a cash-flowing small business
(the "boring-business"/search-fund path) the way G9 underwrites a rental. Given the asking price and
SDE (Seller's Discretionary Earnings — the single-owner-operator cash flow), it computes the entry
multiple, the SBA/seller debt service and DSCR (the lender's gate and the buyer's safety margin),
cash-on-cash on the equity injection, the payback period, and the whole-hold levered after-tax IRR
with an exit at an assumed multiple — plus the QSBS (§1202) exclusion note where it actually applies.

Reuses the realestate amortization + IRR helpers (same math). Pure arithmetic — the app supplies the
deal inputs (SDE, add-backs and QoE are the buyer's diligence). Decision-support, not advice.
"""

import realestate as _re


def _g(inp, key, d=0.0):
    try:
        return float(inp.get(key, d))
    except (TypeError, ValueError):
        return d


def underwrite(inp):
    """Underwrite one acquisition. inp keys (defaults in parens):
    price, sde (SDE / owner cash flow), revenue, down_pct(15), seller_note_pct(0), loan_rate_pct(10),
    loan_term_years(10), working_capital(0), owner_salary(0 = SDE already nets owner comp; set if you
    need a W-2 draw ON TOP), sde_growth_pct(3), hold_years(5), exit_multiple(=entry), marginal_tax_pct(24),
    ltcg_pct(15), qsbs(0/1 — C-corp §1202)."""
    price = _g(inp, "price", 1000000)
    sde = _g(inp, "sde", 300000)
    revenue = _g(inp, "revenue", 0)
    down_pct = _g(inp, "down_pct", 15)
    seller_pct = _g(inp, "seller_note_pct", 0)
    rate = _g(inp, "loan_rate_pct", 10)
    term = _g(inp, "loan_term_years", 10)
    wc = _g(inp, "working_capital", 0)
    owner_salary = _g(inp, "owner_salary", 0)
    growth = _g(inp, "sde_growth_pct", 3)
    hold = int(_g(inp, "hold_years", 5)) or 5
    exit_mult = _g(inp, "exit_multiple", 0)
    mtax = _g(inp, "marginal_tax_pct", 24) / 100.0
    ltcg = _g(inp, "ltcg_pct", 15) / 100.0
    qsbs = _g(inp, "qsbs", 0) >= 1

    entry_mult = (price / sde) if sde else None
    bank_loan = price * (1 - (down_pct + seller_pct) / 100.0)
    seller_note = price * seller_pct / 100.0
    equity_in = price * down_pct / 100.0 + wc          # buyer's cash: down payment + working capital
    debt = bank_loan + seller_note
    debt_service = _re._pmt(bank_loan, rate, term) + _re._pmt(seller_note, rate, term)
    dscr = (sde / debt_service) if debt_service else None
    # SDE already includes the owner's comp; an ADDITIONAL W-2 draw reduces free cash to equity.
    free_cash0 = sde - debt_service - owner_salary
    coc = (free_cash0 / equity_in * 100.0) if equity_in else None
    payback = (equity_in / free_cash0) if free_cash0 > 0 else None

    # whole-hold after-tax levered IRR (SDE taxed as ordinary; exit gain at LTCG unless QSBS)
    flows = [-equity_in]
    sde_y = sde
    for yr in range(1, hold + 1):
        if yr > 1:
            sde_y *= (1 + growth / 100.0)
        interest = _re._interest_for_year(bank_loan, rate, term, yr) + _re._interest_for_year(seller_note, rate, term, yr)
        taxable = max(0.0, sde_y - interest - owner_salary)     # principal isn't deductible; SDE is ordinary
        tax = taxable * mtax
        after_tax_cf = (sde_y - debt_service - owner_salary) - tax
        if yr == hold:
            em = exit_mult if exit_mult > 0 else (entry_mult or 0)
            exit_value = sde_y * em
            sale_costs = exit_value * 0.10                       # broker/legal ~10%
            payoff = _re._remaining_balance(bank_loan, rate, term, hold * 12) + \
                _re._remaining_balance(seller_note, rate, term, hold * 12)
            net_before_tax = exit_value - sale_costs - payoff
            gain = max(0.0, (exit_value - sale_costs) - price)    # basis ~ purchase price
            if qsbs:
                excluded = min(gain, max(10_000_000.0, 10 * price))  # §1202 greater of $10M or 10x basis
                sale_tax = (gain - excluded) * ltcg
            else:
                sale_tax = gain * ltcg
            after_tax_cf += net_before_tax - sale_tax
        flows.append(after_tax_cf)

    irr = _re._irr(flows)
    return {
        "price": round(price, 0), "sde": round(sde, 0),
        "entry_multiple": round(entry_mult, 2) if entry_mult is not None else None,
        "bank_loan": round(bank_loan, 0), "seller_note": round(seller_note, 0),
        "equity_injection": round(equity_in, 0), "working_capital": round(wc, 0),
        "annual_debt_service": round(debt_service, 0),
        "dscr": round(dscr, 2) if dscr is not None else None,
        "free_cash_to_owner_year1": round(free_cash0, 0),
        "cash_on_cash_pct": round(coc, 2) if coc is not None else None,
        "payback_years": round(payback, 1) if payback else None,
        "hold_years": hold, "exit_multiple": (exit_mult if exit_mult > 0 else (round(entry_mult, 2) if entry_mult else None)),
        "after_tax_irr_pct": round(irr * 100, 2) if irr is not None else None,
        "qsbs": qsbs, "cashflows": [round(f, 0) for f in flows],
        "flags": _flags(entry_mult, dscr, coc, qsbs),
        "note": "SDE = single-owner-operator cash flow (verify with a Quality-of-Earnings review; "
                "watch add-backs). DSCR < 1.5 is tight for an operating business (a bad quarter can "
                "break debt coverage). Entry vs exit multiple drives the IRR — don't assume multiple "
                "expansion. QSBS (§1202) only applies to C-corp stock held 5+ yrs (most small deals are "
                "asset sales of S-corps/LLCs and DON'T qualify) — confirm structure with counsel/CPA.",
    }


def _flags(entry_mult, dscr, coc, qsbs):
    out = []
    if dscr is not None and dscr < 1.5:
        out.append(f"DSCR {dscr:.2f} < 1.5 — thin coverage for an operating business (SBA wants ~1.5+)")
    if entry_mult is not None and entry_mult > 4:
        out.append(f"entry multiple {entry_mult:.1f}x SDE > 4x — rich for a small owner-operated business")
    if coc is not None and coc < 0:
        out.append(f"cash-on-cash {coc:.1f}% negative — debt service exceeds the business's cash flow")
    if qsbs:
        out.append("QSBS assumed — only valid for C-corp stock (5-yr hold); verify the deal is a "
                   "qualifying stock purchase, not an asset sale")
    return out
