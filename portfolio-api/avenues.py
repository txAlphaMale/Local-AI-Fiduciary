"""
avenues.py — cross-avenue after-tax comparison sheet (Fable G12).

The clearest expression of the "any wealth-building opportunity, not just investing" directive:
put the MARGINAL dollar's options side by side, each reduced to a comparable AFTER-TAX return,
tagged with its risk and liquidity, so "where should the next dollar go?" has a defensible
answer instead of a vibe. Texas has no state income tax, so every after-tax figure here is
federal-only.

Two kinds of avenue, kept separate because they aren't the same unit:
  • RECURRING — an ongoing expected/guaranteed after-tax annual return (pay down debt, Treasuries,
    a broad index, a concentrated add, real estate, a borrow-to-invest spread). Ranked head-to-head.
  • ONE-TIME TAX ALPHA — a capped, do-it-this-year win (0%-LTCG harvest, a Roth conversion at a
    lower rate than later, an employer match). Shown with the $ captured, not annualized into the
    recurring column (that would overstate them).

v1-with-knobs, exactly as the triage asked: expected returns and rates are ASSUMPTIONS (knobs);
this module does the after-tax arithmetic and the risk framing, and the app supplies the numbers
(pulling the marginal tax rate, 0%-harvest budget, and loan rate from the live engines). It never
tells you the future — it makes the trade-offs explicit. Decision-support, not advice.
"""


def _guard(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def build_sheet(inp):
    """inp: dict of assumption knobs + live-engine inputs (all percents in %, dollars in $).
    Returns {recurring:[...], one_time:[...], best_recurring, assumptions, note}."""
    mrate = _guard(inp.get("marginal_ordinary_rate_pct")) / 100.0     # ordinary marginal rate
    ltcg = _guard(inp.get("ltcg_rate_pct"), 15.0) / 100.0             # LTCG / qualified-div rate
    recurring = []

    # 1) Pay down debt — a GUARANTEED, risk-free after-tax return = the rate (less deductibility).
    apr = _guard(inp.get("debt_apr_pct"))
    if apr > 0:
        at = apr * (1 - (mrate if inp.get("debt_deductible") else 0.0))
        recurring.append({"avenue": "Pay down debt", "kind": "guaranteed", "risk": "none",
                          "liquidity": "n/a", "gross_pct": round(apr, 2), "after_tax_pct": round(at, 2),
                          "note": "Retiring debt earns its rate risk-free" +
                                  (" (interest deductible → less)" if inp.get("debt_deductible") else
                                   " (non-deductible, so the full rate)") + ". Beats most risky bets on a risk-adjusted basis."})

    # 2) Treasuries / bonds — near risk-free, taxed as ordinary (TX has no state tax to save).
    ty = _guard(inp.get("treasury_yield_pct"))
    if ty > 0:
        recurring.append({"avenue": "Treasuries / bonds", "kind": "guaranteed", "risk": "low",
                          "liquidity": "high", "gross_pct": round(ty, 2),
                          "after_tax_pct": round(ty * (1 - mrate), 2),
                          "note": "Nominally risk-free; interest taxed at your ordinary rate (no state tax in TX)."})

    # 3) Broad index — expected market return; qualified dividends taxed yearly, gains deferred.
    mret = _guard(inp.get("market_return_pct"))
    dyld = _guard(inp.get("div_yield_pct"))
    if mret > 0:
        drag = dyld * ltcg
        recurring.append({"avenue": "Broad index fund", "kind": "expected", "risk": "market",
                          "liquidity": "high", "gross_pct": round(mret, 2),
                          "after_tax_pct": round(mret - drag, 2),
                          "note": "Diversified market return; only the dividend is taxed yearly, price gains defer to sale (LTCG)."})

    # 4) Concentrated add — same/expected return but idiosyncratic risk you AREN'T paid for.
    cret = _guard(inp.get("concentrated_return_pct"), mret)
    if cret > 0:
        recurring.append({"avenue": "Add to concentrated name", "kind": "expected", "risk": "high (idiosyncratic)",
                          "liquidity": "high", "gross_pct": round(cret, 2),
                          "after_tax_pct": round(cret - dyld * ltcg, 2),
                          "note": "Same expected after-tax return as the index but far MORE risk — "
                                  "the market doesn't pay a premium for diversifiable single-name risk, "
                                  "so risk-adjusted this ranks BELOW the index despite a matching headline."})

    # 5) Real estate — cap-rate income (depreciation-shielded) + appreciation (deferred, 1031-able).
    cap = _guard(inp.get("re_cap_rate_pct"))
    appr = _guard(inp.get("re_appreciation_pct"))
    if cap > 0 or appr > 0:
        shield = _guard(inp.get("re_depr_shield_frac"), 0.6)
        income_at = cap * (1 - mrate * (1 - shield))
        recurring.append({"avenue": "Real estate (rental)", "kind": "expected", "risk": "illiquid / leveraged",
                          "liquidity": "low", "gross_pct": round(cap + appr, 2),
                          "after_tax_pct": round(income_at + appr, 2),
                          "note": f"Cap-rate income partly shielded by depreciation (~{round(shield*100)}%), "
                                  "appreciation defers (1031-eligible). Illiquid; usually levered (amplifies both ways). "
                                  "Cap/appreciation are estimates — underwrite the actual deal (see G9)."})

    # 6) Borrow-to-invest spread (SBLOC) — the spread, flagged for ruin (see the leverage tab).
    loan = _guard(inp.get("loan_rate_pct"))
    if loan > 0 and mret > 0:
        recurring.append({"avenue": "Borrow-to-invest (SBLOC)", "kind": "spread", "risk": "HIGH (margin call)",
                          "liquidity": "high", "gross_pct": round(mret - loan, 2),
                          "after_tax_pct": round((mret - dyld * ltcg) - loan, 2),
                          "note": "Only the SPREAD (asset return − variable loan rate); a margin call can force a "
                                  "sale at the bottom. See the LEVERAGE tab's ruin curve before ever doing this."})

    for r in recurring:
        r["risk_adjusted_flag"] = ("↓ risk-adjusted below index" if r["risk"].startswith("high")
                                   or r["risk"].startswith("HIGH") else None)
    recurring.sort(key=lambda r: -(r["after_tax_pct"] or 0))
    best = recurring[0] if recurring else None

    one_time = []

    # A) 0%-LTCG harvest — save the future LTCG rate on gains realized at 0% now. Capped at the budget.
    hb = _guard(inp.get("harvest_budget"))
    if hb > 0:
        one_time.append({"avenue": "0%-LTCG gain harvest", "risk": "none", "value_pct": round(ltcg * 100, 1),
                         "captured_dollars": round(hb * ltcg, 0), "cap_dollars": round(hb, 0),
                         "note": f"Realize up to ${round(hb):,.0f} of long-term gains at 0% now, resetting basis — "
                                 f"saves ~{round(ltcg*100)}% ($" + f"{round(hb*ltcg):,.0f}) of future LTCG. Pure tax alpha, no market risk."})

    # B) Roth conversion arbitrage — convert now at a lower rate than the future (RMD) rate. Capped at fill.
    fill = _guard(inp.get("roth_fill"))
    now_r = _guard(inp.get("roth_now_rate_pct"))
    fut_r = _guard(inp.get("roth_future_rate_pct"))
    if fill > 0 and fut_r > now_r:
        spread = (fut_r - now_r) / 100.0
        one_time.append({"avenue": "Roth conversion (rate arb)", "risk": "none (legislative)",
                         "value_pct": round(fut_r - now_r, 1), "captured_dollars": round(fill * spread, 0),
                         "cap_dollars": round(fill, 0),
                         "note": f"Convert ~${round(fill):,.0f} at ~{round(now_r)}% now vs ~{round(fut_r)}% later "
                                 "(RMD-forced) — the rate gap is the win. See the ROTH tab."})

    # C) Employer / tax-advantaged match — an instant guaranteed return, when available.
    match = _guard(inp.get("match_rate_pct"))
    mcap = _guard(inp.get("match_cap_dollars"))
    if match > 0:
        one_time.append({"avenue": "Employer / HSA match", "risk": "none", "value_pct": round(match, 1),
                         "captured_dollars": round(mcap * match / 100.0, 0) if mcap else None,
                         "cap_dollars": round(mcap, 0) if mcap else None,
                         "note": f"An instant ~{round(match)}% guaranteed return — the highest-return dollar there is. "
                                 "Fund this before any market avenue (only if still contributing)."})

    one_time.sort(key=lambda r: -(r.get("value_pct") or 0))
    return {"recurring": recurring, "one_time": one_time, "best_recurring": best,
            "note": "After-tax, federal-only (TX has no state tax). RECURRING = ongoing annual return "
                    "(ranked); ONE-TIME = capped tax-alpha to capture this year. Do the guaranteed and "
                    "tax-alpha rows first (match → 0% harvest / Roth → debt paydown), then the risky "
                    "recurring avenues. Expected returns are ASSUMPTIONS — tune them in SETTINGS. "
                    "Decision-support, not advice."}
