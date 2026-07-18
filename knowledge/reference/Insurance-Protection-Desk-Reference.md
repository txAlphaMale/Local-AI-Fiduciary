# Insurance & Protection-Desk — Frameworks Reference (2026)

_Corpus doc for the Fiduciary knowledge RAG. Concepts behind `insurance.py` and PLAN → INSURANCE — a gap analysis of the coverages that protect the plan from non-market ruin (death, disability, liability, long-term care), distinct from the investment-risk modules. This tool never sells or recommends products. Not investment, tax, or insurance advice._

## Life insurance need-sizing: three methods, used as cross-checks
- **Capital-needs analysis (the primary method here)** = the present value of replacing the earner's income until retirement, PLUS outstanding debts, PLUS final expenses, PLUS any education goal, MINUS liquid assets already available to the survivor. This is the most defensible method because it's built directly from the household's actual numbers rather than a rule of thumb.
- **DIME** = Debt + Income (replacement, typically capped around a 10-year horizon as a simpler proxy) + Mortgage + Education. A quick rule-of-thumb cross-check, not a substitute for the capital-needs figure.
- **Human-life-value (HLV)** = the present value of the earner's future income net of their own self-consumption (i.e., the portion of earnings that would actually have benefited the household, not what the earner would have spent on themselves). An economic upper-bound framing.
- **Reading the three together**: they're deliberately redundant. If capital-needs, DIME, and HLV land in the same neighborhood, that's a good sign the inputs are sound. If they disagree WILDLY, the right response is to revisit the inputs (income, horizon, debt figures) — not to average the three methods together or pick whichever number is most convenient.

## Umbrella liability
- **Umbrella liability** sits above your auto and homeowners policy limits and pays out once the underlying policy's limit is exhausted. It exists because a liability judgment (a serious auto accident, an injury on your property) isn't capped at whatever your base auto/home policy happens to carry — a judgment can exceed a $300k or $500k underlying limit easily, and without umbrella coverage the excess is paid from personal assets.
- Sizing convention: umbrella coverage is typically sized to at least match net worth (rounded up to a policy-available tier), on the logic that a liability judgment can reach for anything you own, not just the asset involved in the incident.

## Long-term care (LTC)
- The planning question is really about which of three bands the household falls into: **too little to insure** (an extended care event exhausts the estate regardless of LTC coverage, so Medicaid planning with an elder-law attorney matters more than premiums), an **exposed middle band** (a multi-year care event would consume a material share of the estate and impair a surviving spouse — this is where LTC insurance, hybrid life/LTC riders, or state Partnership policies earn their premium), or **large enough to self-insure** (the estate can absorb a care event, and the better move is an explicit self-insurance reserve rather than an implicit "we'll figure it out").
- **Timing matters independent of the band**: LTC underwriting and pricing are most favorable in the early-to-mid 50s. Waiting past the low-60s materially raises cost and underwriting risk (health changes are the main driver of decline/rate-up).

## Property & casualty checklist basics
- **Dwelling replacement cost** (rebuild cost, not market value or purchase price — these diverge significantly and market value is the wrong number to insure to) drives homeowners coverage adequacy.
- Auto liability limits should be evaluated alongside the umbrella policy — a low underlying auto limit can leave a gap even with umbrella coverage in place, since most umbrella policies require a minimum underlying limit to attach.
- Regional perils (e.g., flood, which is typically EXCLUDED from standard homeowners policies and requires separate NFIP or private flood coverage; wind/hurricane deductibles in coastal or high-wind zones) are easy to miss because they aren't automatically included and don't show up as a "gap" unless specifically checked.

## Sources
Standard capital-needs/DIME/human-life-value life-insurance need-analysis methodology (widely used in CFP curriculum and industry practice); general umbrella-liability underwriting convention (coverage sized to net worth); LTC industry pricing/underwriting-by-age patterns (e.g. American Association for Long-Term Care Insurance annual pricing studies); NFIP flood-exclusion-from-standard-homeowners-policy convention. This is decision-support to structure the gap analysis — actual coverage, pricing, and product selection require a licensed agent and current quotes.
