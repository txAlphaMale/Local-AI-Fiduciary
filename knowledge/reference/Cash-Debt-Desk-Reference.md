# Cash, Liquidity & Debt Desk — Frameworks Reference (2026)

_Corpus doc for the Fiduciary knowledge RAG (G2 companion). Concepts behind `cashdebt.py` and PLAN → CASH·DEBT — the household-CFO module for emergency funds, idle-cash drag, mortgage payoff-vs-invest, refinance timing, and debt payoff order. Not tax or lending advice._

## Emergency fund sizing
- Measured in **months of essential spending covered by cash**, not a flat dollar target — essential monthly spend is the "needs" share of total spending, not total spending itself (discretionary spend isn't something an emergency fund needs to cover). Status bands: at/above the target months is "ok," below half the target is "critical," and the range between is "low."
- Cash held BEYOND the target isn't automatically "good" — it's either intentional dry powder for a known near-term use or it's idle cash quietly losing to inflation/opportunity cost (see below).

## Idle-cash drag
- **Idle-cash drag** = the spread between what safe, liquid alternatives (T-bills, a high-yield savings account, a money-market fund) currently pay and what the actual deposit account pays, applied to the cash balance. A checking account paying near-zero against a 5% T-bill on a large balance is a real, quantifiable annual cost — not a rounding error.
- T-bills carry a practical advantage for residents of no-income-tax states beyond the "no state tax" framing sometimes cited for them: they're EXEMPT from state income tax everywhere, which specifically matters for readers in states that DO tax investment income; the T+1 liquidity (settles next business day) also makes them a reasonable middle ground between a checking account and locking money in a CD.
- The most actionable slice is cash held ABOVE the emergency-fund target — that portion has no liquidity justification and is a pure comparison-shopping opportunity.

## Mortgage payoff vs. invest
- The comparison is between the mortgage's (after-tax) interest rate, a GUARANTEED risk-free rate (T-bills), and a RISKY expected nominal investment return. Three verdicts fall out of where the mortgage rate sits relative to those two benchmarks:
  - **Rate above even the risky expected return** → paying down principal is the better EXPECTED use of marginal dollars — it's a guaranteed return that beats the risky alternative on expectation, not just on safety.
  - **Rate between the guaranteed and risky benchmarks** → a genuine risk-preference call: paying principal is a guaranteed return at that rate, while investing only wins if the market outperforms on your side of the bet. Splitting extra cash between the two is a defensible answer, not a dodge.
  - **Rate at/below the guaranteed benchmark** → the mortgage is cheap leverage; even riskless cash outperforms prepayment, so don't prepay.
- **The after-tax-rate caveat matters more than it looks**: mortgage interest is only effectively tax-advantaged if you ITEMIZE deductions, and the post-2017 standard deduction is large enough that many households no longer benefit from itemizing at all — meaning the mortgage's true after-tax cost equals its nominal rate for them, not a lower "tax-adjusted" figure. Confirm actual itemization status before assuming any tax benefit from the mortgage interest deduction.
- **Liquidity is one-way**: extra principal paid into a mortgage is hard to extract later without refinancing or a HELOC — treat prepayment as a comparatively illiquid choice relative to keeping the cash invested or in reserve.

## Refinance break-even
- **Break-even (months)** = closing costs ÷ monthly payment savings. A common rule of thumb treats a break-even of 36 months or less as comfortable, but that threshold should flex with how long you're actually confident you'll hold the loan.
- **The term-restart trap**: comparing a refinance's new payment against the old payment can make refinancing look good even when it's a bad deal overall, because resetting to a fresh full term (e.g., a new 30-year loan replacing a loan that was 5 years into its original 30) restarts amortization — you pay MORE total interest over time even though the monthly payment drops. The honest comparison matches the REMAINING term of the current loan against the new loan's term, not the new loan's advertised term against the old payment in isolation.

## Debt avalanche
- **Avalanche method**: rank non-mortgage debts by interest rate, highest first, and direct extra payments there. Any debt costing more than the expected nominal investment return is a GUARANTEED-return prepayment target — paying off a 22% credit card is a better use of a dollar than almost any investment, since the "return" (avoided interest) is certain.
- This is distinct from the "snowball" method (smallest balance first, for psychological momentum) — avalanche is the mathematically optimal order; snowball trades some optimality for behavioral adherence. The app implements avalanche because it's modeling the number, not the behavior.

## Sources
Standard emergency-fund and debt-avalanche personal-finance methodology (widely used in CFP curriculum); U.S. Treasury (T-bill state-tax-exemption mechanics); IRS mortgage-interest-deduction and standard-deduction rules post-Tax Cuts and Jobs Act of 2017 (itemization threshold effects); standard refinance break-even and amortization-restart analysis as taught in mortgage/lending industry materials. Specific current mortgage rates, T-bill yields, and deposit-account rates are inputs you supply or the app fetches live — this doc explains the FRAMEWORK, not today's numbers.
