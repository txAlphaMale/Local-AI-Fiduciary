# Small-Business Acquisition & QSBS — Frameworks Reference (2026)

_Corpus doc for the Fiduciary knowledge RAG (G11 companion). Decision-support frameworks the underwriter (`bizacq.py`, PLAN → BUSINESS) implements; this doc gives the chat/personas the concepts behind the numbers. Not investment or tax advice._

## Valuation: SDE vs EBITDA
- **SDE (Seller's Discretionary Earnings)** = pre-tax profit + owner's salary/benefits + one-time/discretionary add-backs. The metric for owner-operator businesses (roughly < $1–2M earnings). **EBITDA** (with a market-rate manager salary SUBTRACTED) is the metric once management is hired — the same business is worth a lower multiple of SDE than its EBITDA multiple suggests, because SDE includes the owner's unpaid labor.
- Typical main-street multiples: **~1.5×–3.5× SDE** for service businesses under ~$1M SDE; quality drivers push the range: revenue durability/recurrence, customer concentration (any client >15–20% is a red flag), owner-dependence (can it run 4 weeks without the seller?), clean books, transferable licenses/leases.
- **Quality of earnings (QoE) diligence** exists to attack add-backs: every add-back claimed by a seller is a negotiation, not a fact.

## Deal structure & the lender's gate
- Standard structure: SBA 7(a) loan (10-year term, ~10% floating range in 2025–26), **10–20% buyer down payment**, often a **seller note (5–15%) on standby** — the seller note both bridges valuation gaps and keeps the seller honest post-close.
- **DSCR (Debt Service Coverage Ratio) = SDE (less any manager salary) ÷ annual debt service.** Lenders gate at **≥ 1.25×**; a buyer should want ≥ 1.5× headroom, because year-1 SDE routinely dips 10–20% in transition.
- Buyer safety metrics beyond DSCR: **cash-on-cash return** on the down payment (compare against public-market expected return + illiquidity premium), **payback period** on equity, and the levered after-tax IRR over the hold (the app computes all of these).
- Principal payments are NOT deductible; SDE is ordinary income to the owner-operator. Model taxes on (SDE − interest − W-2 draw).

## QSBS — §1202 exclusion (TWO regimes after OBBBA)
- **Stock acquired on or before 2025-07-04 (pre-OBBBA):** 100% federal LTCG exclusion after a **5-year** holding period, capped at the **greater of $10M or 10× basis** per issuer. C-corp original-issue stock only; ≤ $50M aggregate gross assets at issuance; active qualified trade (no pure services/finance/real-estate).
- **Stock acquired after 2025-07-04 (OBBBA regime):** cap raised to **$15M (indexed) or 10× basis**; gross-asset ceiling raised to **$75M**; and a **tiered holding schedule** — 50% exclusion at 3 years, 75% at 4 years, **100% at 5 years**. ⚠ The app's `bizacq.py` currently applies the greater-of-$10M/10× rule; for post-2025 acquisitions the $15M/tiered figures apply — verify vintage when interpreting its QSBS line.
- Planning consequence: buying/founding through a C-corp trades pass-through taxation during the hold for a potentially enormous tax-free exit; the breakeven depends on hold length, distribution needs, and exit size. S-corp/LLC → C-corp conversions start the QSBS clock at conversion.

## How this ranks against other avenues
A levered small-business purchase is the highest-dispersion avenue the household can access: modeled after-tax IRRs (15–30%+) come with key-person risk, illiquidity, and personal guarantees on SBA debt. Compare its risk-adjusted, after-tax IRR against the cross-avenue sheet (RISK · EXPOSURE → AVENUES) — leverage-on-securities, real estate, Roth-conversion tax alpha — before treating the headline IRR as real.

## Sources (verified July 2026)
The Tax Adviser, "QSBS gets a makeover: §1202's new look" (Nov 2025) · RSM, "OBBBA tax: QSBS changes" · Regalis Capital, SDE/EBITDA multiples guide · CT Acquisitions, SBA 7(a) business-acquisition guide.
