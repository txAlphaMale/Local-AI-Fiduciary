# Grounded Tax/ACA/SS/Estate Parameters — 2026 Snapshot

_Corpus doc for the Fiduciary knowledge RAG (G13 companion). SNAPSHOT generated 2026-07-16 from the app's live grounding registry. **The live source of truth is AUDITS → GROUNDING (`/api/grounding`)** — it reads these values from the engines themselves and fires STALE alerts when a year rolls. If this doc and the registry disagree, the registry wins. Regenerate this snapshot at the January re-verification pass._

## Federal income tax (tax year 2026, post-OBBBA) — indexed annually
- **Ordinary brackets (MFJ thresholds):** 10% from $0 · 12% from $24,800 · 22% from $100,800 · 24% from $211,400 · 32% from $403,550 · 35% from $512,450 · 37% from $768,700. (Single: 12% at $12,400 · 22% at $50,400 · 24% at $105,700 · 32% at $201,775.) Source: Tax Foundation 2026 brackets / IRS Rev. Proc.
- **LTCG / qualified-dividend brackets:** 0% up to **$98,900 MFJ** ($49,450 single) of taxable income; 15% above; 20% above **$1,091,000 MFJ** ($545,500 single). The 0% band is the "tax-free gain harvesting" window.
- **Standard deduction:** **$32,200 MFJ** / $16,100 single / $24,150 HoH; age-65+ add-on $2,050 single / $1,650 each MFJ.
- **NIIT (statutory, not indexed):** 3.8% on net investment income above MAGI **$250,000 MFJ** / $200,000 single.

## ACA (coverage year 2026) — the pre-65 bridge regime
- **Federal poverty guidelines (48 states):** 1p $15,650 · 2p $21,150 · 3p $26,650 · 4p $32,150 (+$5,500/person beyond).
- **Enhanced subsidies EXPIRED for 2026:** the §36B applicable-percentage scale runs ~2.1% of MAGI at 100% FPL up to ~9.6% near 400% — and the **hard 400%-FPL cliff is back**: $1 of MAGI over 400% FPL forfeits the entire premium tax credit. Texas did not expand Medicaid: household MAGI must be ≥ 100% FPL to qualify for any PTC at all (the "coverage gap" below it).
- Planning: Roth conversions and gain harvesting price against the cliff — the app's unified marginal-rate curve (PLAN → ROTH) includes it.

## Social Security (2026) — indexed annually
- **PIA bend points:** $1,286 / $7,749 monthly AIME; replacement rates 90% / 32% / 15%.
- **COLA 2.8% · OASDI wage base $184,500 · max FRA benefit $4,152/mo.**
- **Earnings test (pre-FRA):** $24,480/yr exempt, then $1 withheld per $2 earned ($65,160 and 1-per-3 in the FRA year; no test after FRA).
- **Benefit taxation thresholds (statutory, never indexed):** provisional income $32k/$44k MFJ ($25k/$34k single) → up to 50%/85% of benefits taxable.

## Estate & gift (2026) — indexed annually
- **Basic exclusion $15,000,000/person** (household shield $30M with portability via Form 706) · **GST exemption $15M, NOT portable** · top rate 40%.
- **Annual gift exclusion $19,000/donee** · 529 superfund election ×5 years.

## Medicare IRMAA (2026, 2-year MAGI lookback) — indexed annually
- **Part B standard premium $202.90/mo**; IRMAA multipliers 1.4× / 2.0× / 2.6× / 3.2× / 3.4× at MAGI tiers **MFJ: $218,000 / $274,000 / $342,000 / $410,000 / $750,000** (single: $109k / $137k / $171k / $205k / $500k). A $1 crossing of a tier reprices BOTH spouses' premiums two years later — the Roth ladder (G8) verifies each rung against these.

## Standing rules encoded in the app
- Every constant above is **read live from the owning module** by `grounding.py` with a primary-source URL and a verification date; indexed constants whose year lags the calendar fire a STALE alert; December fires the "new figures publish" notice; January is the re-verification window.
- No state income tax in Texas — state overlays are deliberately absent from every engine.
