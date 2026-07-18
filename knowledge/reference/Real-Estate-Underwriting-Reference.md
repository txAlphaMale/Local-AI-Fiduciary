# Rental Real-Estate Underwriting — Frameworks Reference (2026)

_Corpus doc for the Fiduciary knowledge RAG (G9 companion). Concepts behind `realestate.py` and PLAN → REAL ESTATE. Complements `Business-Acquisition-QSBS-Reference.md`, which covers cap-rate-adjacent metrics (DSCR, CoC) from the small-business side. Not investment or tax advice._

## Income metrics: NOI, cap rate, DSCR, cash-on-cash
- **EGI (Effective Gross Income)** = gross scheduled rent × (1 − vacancy%). **NOI (Net Operating Income)** = EGI − operating expenses (taxes, insurance, maintenance, management, reserves) — deliberately BEFORE debt service, so it measures the property's own profitability independent of how it's financed.
- **Cap rate** = NOI ÷ purchase price. An unlevered yield used to compare deals of different sizes and financing structures on a like-for-like basis; a cap rate below roughly 5% typically signals the deal is priced for appreciation rather than income.
- **DSCR (Debt Service Coverage Ratio)** = NOI ÷ annual debt service (principal + interest). Lenders gate financing on a minimum DSCR — commonly around 1.25× — because it's their margin of safety if rent or expenses move against the deal; a DSCR near 1.0 means the property barely covers its own mortgage payment with nothing left over.
- **Cash-on-cash (CoC) return** = year-1 pre-tax cash flow (NOI − debt service) ÷ actual cash invested (down payment + closing costs). This is the LEVERED return — the number that reflects your personal capital efficiency, as opposed to cap rate's unlevered, financing-agnostic view of the deal itself.

## The levered after-tax IRR, and what feeds it
- A full underwriting projects after-tax cash flow across the entire hold: NOI grows with rent growth, debt service is fixed (fully-amortizing loan), and each year's TAXABLE income is NOI − mortgage interest − depreciation (interest and depreciation are both deductible against rental income even though only interest is a real cash cost).
- **Depreciation**: the building's basis (purchase price minus the land allocation, since land doesn't depreciate) is written off over a statutory life — 27.5 years for residential rental property — producing an annual non-cash deduction that shelters cash flow from tax. **Bonus/cost-segregation depreciation** can accelerate a portion of that write-off into year one by reclassifying some building components to shorter depreciable lives.
- **At sale**: proceeds net of selling costs and the remaining loan payoff are taxed in two pieces — **§1250 unrecaptured depreciation recapture** (the accumulated depreciation taken, taxed at a rate capped at 25%) plus ordinary LTCG on any remaining gain above that. A **§1031 like-kind exchange** can defer the ENTIRE sale-year tax bill by rolling the proceeds into a new property, at the cost of carrying the low basis (and the eventual recapture) forward rather than resetting it.
- Chaining every year's after-tax cash flow plus the after-tax sale proceeds into one series and solving for the discount rate that zeroes its NPV gives the **levered after-tax IRR** — the single number that captures financing, taxes, depreciation, and the exit together, as opposed to any single-year snapshot metric above.

## Reading the flags
- **DSCR < 1.25** — thin debt coverage; a lender is unlikely to approve at this level, and even if self-financed, a small rent shortfall or expense surprise erases the margin.
- **Cap rate < 5%** — the deal is priced for appreciation, not income; the investment thesis has to rest on rent growth or value appreciation actually showing up.
- **Negative cash-on-cash** — the deal bleeds cash in year one; every month is funded out of pocket regardless of what the IRR eventually works out to.
- Passive-loss usability caveat: the tax shelter from depreciation assumes the loss is usable in the year taken. Real-estate-professional status or passive-activity-loss income limits can defer rather than eliminate that benefit — confirm actual usability with a CPA rather than assuming the full shelter applies.

## Sources
IRS Publication 527, "Residential Rental Property" (depreciable life, basis allocation, recapture); IRC §1250 (unrecaptured depreciation recapture) and §1031 (like-kind exchanges); standard commercial/residential real-estate underwriting practice for cap rate, DSCR, and cash-on-cash as defined by industry bodies (e.g. CCIM, appraisal-institute materials). Definitional/methodological content is stable; current mortgage rates, rent comps, and expense ratios are deal-specific inputs, not something this doc can supply.
