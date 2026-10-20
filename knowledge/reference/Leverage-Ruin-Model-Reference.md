# Asset-Backed Leverage & Ruin Modeling — Frameworks Reference (2026)

_Corpus doc for the Fiduciary knowledge RAG (G6 companion). Concepts behind `leverage.py` and RISK · EXPOSURE → LEVERAGE. This module never borrows or places an order — it models the decision. Not investment or tax advice._

## The core idea: borrowing against securities
- A **securities-backed line of credit (SBLOC)**, **pledged-asset line (PAL)**, or plain **margin loan** lets you borrow against existing holdings without selling them — no capital-gains tax is triggered because no sale occurs. The mechanism behind **"buy, borrow, die"**: hold appreciated stock, borrow against it to fund spending instead of selling, and at death the heir's cost basis resets to fair market value (step-up, §1014), permanently erasing the embedded gain the loan let you avoid realizing.
- This looks like a free lunch until the collateral drops. Against a volatile, concentrated single-name book it's one of the fastest paths to a forced liquidation at the worst possible time — so any credible treatment of the topic has to lead with the failure mode, not the tax trick.

## LTV, maintenance margin, and the margin call
- **LTV (loan-to-value)** = loan balance ÷ collateral value. A broker's **maintenance margin requirement** (m) is the minimum equity/value ratio it will tolerate before demanding you post more collateral or pay down the loan — a **margin call**.
- With loan D = LTV × V₀, a margin call triggers once the collateral value V falls below D/(1−m). The maximum tolerable drawdown before that happens is **1 − LTV/(1−m)** — this shrinks fast as LTV rises: a 50% LTV against a 30% maintenance requirement tolerates roughly a 29% drawdown, but a 70% LTV tolerates only about 0%, meaning almost any decline triggers the call.
- A margin call that isn't met is resolved by the BROKER selling collateral on ITS timeline, not yours — frequently at the exact bottom of the drawdown that caused the call in the first place. This is the risk the "max prudent LTV" concept exists to bound.

## Ruin modeling: simulating the collateral's own path
- Rather than assuming a generic volatility figure, a proper ruin analysis bootstraps thousands of simulated paths by resampling the collateral's OWN historical daily returns (with replacement) over the intended horizon, and tracks the running minimum value along each path.
- For a given LTV, a path "breaches" if its running minimum ever falls below the margin-call threshold. The share of simulated paths that breach is an estimated **probability of a margin call** at that LTV over that horizon. The **maximum prudent LTV** is the highest LTV whose breach probability stays under an acceptable tolerance (e.g. 5%).
- Caveat inherent to i.i.d. resampling: it captures the fat-tailed MARGINAL distribution of daily moves but not **volatility clustering** (the tendency for large moves to bunch together, as in a real crash) — so a bootstrap of this kind tends to UNDERSTATE true breach risk, and any "max prudent LTV" it produces should be treated as an upper bound to stay well under, not a target to run right up to.

## Borrow vs. sell: the after-tax tradeoff
- The honest alternative to borrowing is selling the appreciated stock and paying the tax now. Selling costs the LTCG tax on the realized gain plus the growth forgone on the capital that left the market; borrowing costs the loan's interest carry but keeps the full position compounding and defers (potentially eliminates, via step-up at death) the gain.
- On pure carry — ignoring every risk above — borrowing beats selling only when the asset's expected return exceeds the loan's interest rate. That comparison says nothing about margin-call risk; it's a necessary condition for the trade to make sense, not a sufficient one.

## The three risks "buy, borrow, die" is usually pitched without
1. **Margin call** — the ruin-curve risk above; the real binding constraint in practice, not the tax math.
2. **Rate reset** — SBLOC/PAL/margin rates are typically VARIABLE, floating on a reference rate like SOFR and resetting periodically. A carry advantage calculated at today's rate can flip negative if rates rise or the asset's return stalls.
3. **Legislative risk** — the strategy's entire payoff depends on the basis step-up at death surviving in the tax code. Step-up has been a recurring repeal target in tax-reform proposals; the strategy is a bet on current law persisting, not a guarantee.

## Sources
IRC §1014 (basis step-up at death); Regulation T and FINRA Rule 4210 (margin/maintenance requirements, general framework — specific broker maintenance requirements vary by firm and by security and are typically higher than the regulatory minimum for concentrated/volatile names); standard treatments of bootstrap resampling for tail-risk estimation (see also the sourcing in `Risk-Metrics-Reference.md`, whose VaR/CVaR framing this module's ruin curve extends to a leverage-specific breach probability). Broker-specific maintenance requirements, current SBLOC/margin rates, and step-up's legislative status all change independently of this doc — verify current terms with your broker/lender before acting on any LTV figure.
