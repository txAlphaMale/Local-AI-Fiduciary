# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
leverage.py — asset-backed borrowing module, RUIN-MODEL-FIRST (Fable G6).

A named wealth-building avenue that was wholly absent: borrowing against existing assets
(a securities-backed line of credit / pledged-asset line / margin) is the cheapest capital a
concentrated owner has, and the mechanism behind "buy, borrow, die." But against a volatile,
concentrated single-name book it is also the fastest way to a forced liquidation at the bottom.

So this module leads with the RISK, exactly as the triage demanded — never presents borrowing
against PLTR/RKLB as a free lunch:

  1. Ruin model FIRST. Using the portfolio's OWN reconstructed daily returns (G4 — real, fat-
     tailed, concentrated), bootstrap-simulate the collateral value over a horizon and, for each
     loan-to-value, compute P(a maintenance-margin breach / forced margin call). That yields a
     conservative "max prudent LTV" where the margin-call probability stays under a tolerance.
  2. After-tax borrow-vs-sell. The alternative to borrowing is selling and paying LTCG. Compares
     the interest carry against the tax paid now + growth forgone on liquidated shares, and marks
     the breakeven (borrowing beats selling on carry only when the asset out-returns the loan
     rate — before risk).
  3. Buy-borrow-die framed WITH its risks: margin-call (from the ruin model), variable-rate reset
     (SBLOC rates float on SOFR), and legislative risk (the step-up that makes the strategy work
     could be curtailed). It MODELS borrowing — it never borrows, never places an order.

Pure arithmetic + a bootstrap (no network, no GPU). Read-only decision-support, not advice.
"""

import random


def breach_drawdown(ltv, maintenance):
    """The fraction the collateral can fall BEFORE a maintenance-margin breach. With loan D=ltv*V0,
    a broker maintenance requirement m (equity/value >= m) is breached when V < D/(1-m); the drop
    to that point is 1 - ltv/(1-m). 0 (or negative clamped to 0) means already at/over the line."""
    denom = 1.0 - maintenance
    if denom <= 0:
        return 0.0
    return max(0.0, 1.0 - ltv / denom)


def ruin_curve(daily_returns, ltv_grid, maintenance, horizon_days,
               n_paths=3000, tolerance_pct=5.0, seed=7):
    """Bootstrap P(margin call) vs LTV from the portfolio's OWN daily returns (fat-tailed). Each
    path samples `horizon_days` daily returns with replacement and tracks the running-minimum
    value factor; for an LTV, a breach is a running min below ltv/(1-maintenance). Reusing the
    same simulated paths across all LTVs keeps it one bootstrap. Returns per-LTV probabilities +
    the max prudent LTV (highest with P <= tolerance). None if too little history.

    NOTE: samples daily returns i.i.d. (captures the fat marginal tail but not volatility
    clustering), so treat the probabilities as a floor and keep margin."""
    rets = [r for r in (daily_returns or []) if r is not None]
    if len(rets) < 30 or not ltv_grid or horizon_days < 5:
        return None
    rng = random.Random(seed)
    n = len(rets)
    min_factors = []
    for _ in range(int(n_paths)):
        cum, mn = 1.0, 1.0
        for _ in range(int(horizon_days)):
            cum *= (1.0 + rets[rng.randrange(n)])
            if cum < mn:
                mn = cum
        min_factors.append(mn)
    denom = 1.0 - maintenance
    rows = []
    for L in ltv_grid:
        thr = (L / denom) if denom > 0 else 1e9
        breaches = sum(1 for mf in min_factors if mf < thr)
        rows.append({"ltv_pct": round(L * 100, 1),
                     "breach_drawdown_pct": round(breach_drawdown(L, maintenance) * 100, 1),
                     "p_margin_call_pct": round(breaches / len(min_factors) * 100, 1)})
    prudent = [r["ltv_pct"] for r in rows if r["p_margin_call_pct"] <= tolerance_pct]
    return {"rows": rows, "max_prudent_ltv_pct": (max(prudent) if prudent else 0.0),
            "tolerance_pct": tolerance_pct, "horizon_days": horizon_days,
            "n_paths": int(n_paths), "maintenance_pct": round(maintenance * 100, 1),
            "note": "P(margin call) = share of bootstrapped paths whose collateral drops past the "
                    "maintenance line at that LTV, drawn from your OWN fat-tailed daily returns. "
                    "Max prudent LTV keeps that probability under the tolerance. i.i.d. daily "
                    "draws understate clustering — keep a margin below the prudent line."}


def borrow_vs_sell(amount, gain_fraction, ltcg_rate_pct, loan_rate_pct,
                   expected_return_pct, horizon_years):
    """After-tax comparison of raising `amount` by BORROWING vs SELLING appreciated stock.
    Sell: realize gain (amount*gain_fraction) and pay LTCG now, and forgo growth on the sold
    capital. Borrow: pay interest carry but keep the shares compounding and DEFER the gain
    (eliminated entirely at death via step-up). Borrowing wins on carry only when the asset's
    expected return exceeds the loan rate — stated before risk."""
    amt = max(0.0, float(amount or 0))
    gf = max(0.0, float(gain_fraction or 0))
    sell_tax = amt * gf * ltcg_rate_pct / 100.0
    grow = amt * ((1 + expected_return_pct / 100.0) ** max(0.0, horizon_years) - 1)
    interest = amt * loan_rate_pct / 100.0 * max(0.0, horizon_years)
    return {"amount": round(amt, 0), "gain_fraction_pct": round(gf * 100, 1),
            "sell_tax_now": round(sell_tax, 0),
            "growth_forgone_if_sold": round(grow, 0),
            "interest_over_horizon": round(interest, 0),
            "horizon_years": horizon_years,
            "annual_carry_pct": round(loan_rate_pct - expected_return_pct, 2),
            "tax_deferred_by_borrowing": round(sell_tax, 0),
            "breakeven_asset_return_pct": round(loan_rate_pct, 2),
            "borrow_favored_on_carry": bool(expected_return_pct > loan_rate_pct),
            "note": "Selling costs the LTCG tax now plus growth forgone on the liquidated shares; "
                    "borrowing costs interest but keeps the shares compounding and defers the gain "
                    "(erased at death via step-up — see the estate module). Borrowing beats selling "
                    "on carry only if the asset out-returns the loan rate, and that ignores the "
                    "margin-call, rate-reset, and legislative risks below."}


BUY_BORROW_DIE_RISKS = [
    "Margin call: a concentrated single-name drop can force liquidation at the bottom — the ruin "
    "curve above is the real constraint. Borrow well under the max prudent LTV.",
    "Rate reset: SBLOC/PAL/margin rates are VARIABLE (float on SOFR, reset monthly). Today's "
    "carry can flip negative if rates rise or the asset stalls.",
    "Legislative risk: 'buy-borrow-die' works because death steps up basis and erases the gain. "
    "That step-up is a recurring target for repeal — the strategy's payoff is not guaranteed.",
    "Pledged shares are encumbered: you can't freely sell/hedge collateral, and a lender can "
    "demand paydown or sell your shares in a breach, on their timeline, not yours.",
]
