# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
ss.py — a small Social Security benefit + taxation engine for the Fiduciary.

Purpose (Fable review item G2): the retirement Monte Carlo used to decumulate the
portfolio for the WHOLE horizon with no Social Security, which understates success
because it ignores a large inflation-indexed income floor that switches on at the
claiming age. This module estimates the benefit and feeds a real-terms SS floor
into the Monte Carlo so success rates aren't understated — and models the three
levers that matter: the PIA (from earnings/AIME), the claiming-age adjustment
(62 / FRA / 70), and how the benefit is taxed federally (up to 85% of it) which
ties back into tax.py and the aca.py MAGI ledger (SS counts 100% toward ACA MAGI).

Everything here is pure arithmetic (zero GPU, zero network) and read-only /
educational — NOT financial advice. A real benefit depends on your full 35-year
indexed earnings history; the most reliable input is the "at full retirement age"
estimate on your SSA statement (ssa.gov/myaccount) — pass it as
`estimated_fra_benefit` and this module skips the AIME approximation entirely.

Data sources (verified July 2026):
- 2026 COLA 2.8%, OASDI taxable maximum $184,500, retirement earnings-test exempt
  amounts ($24,480 under FRA / $65,160 in the FRA year), max benefit at FRA
  $4,152/mo: SSA "2026 Social Security Changes" fact sheet
  (https://www.ssa.gov/news/en/cola/factsheets/2026.html).
- 2026 PIA bend points $1,286 and $7,749 (90%/32%/15%): SSA Office of the Chief
  Actuary / Federal Register 2025-19763 "Cost-of-Living Increase and Other
  Determinations for 2026".
- FRA-by-birth-year, early-claiming reduction (5/9 of 1%/mo for the first 36
  months, 5/12 of 1%/mo beyond) and delayed-retirement credit (8%/yr for those
  born 1943+) are set by statute (42 U.S.C. §402/§415; 20 CFR 404.313).
- Benefit-taxation thresholds ($25k/$34k single, $32k/$44k MFJ) are set by statute
  and are NOT inflation-indexed (IRC §86 / IRS Pub 915).
"""

BENEFIT_YEAR = 2026
SOURCE = "SSA 2026 fact sheet + Federal Register 2025-19763 (bend points); IRC §86 (taxation)"

# 2026 PIA formula: marginal replacement rates applied to bands of AIME split at
# the two bend points (monthly dollars). PIA is the benefit at Full Retirement Age.
BEND_POINTS_2026 = (1_286, 7_749)
PIA_RATES = (0.90, 0.32, 0.15)

COLA_2026 = 0.028                 # applied Jan 2026
OASDI_WAGE_BASE_2026 = 184_500   # max earnings subject to (and credited for) SS
MAX_FRA_BENEFIT_2026 = 4_152.0   # max monthly benefit for a worker retiring AT FRA in 2026

# Retirement earnings test (only bites if you claim early AND keep working).
EARNINGS_TEST_2026 = {
    "under_fra_annual": 24_480,   # $1 withheld per $2 over
    "under_fra_withhold_ratio": 2.0,
    "fra_year_annual": 65_160,    # $1 withheld per $3 over, months before FRA only
    "fra_year_withhold_ratio": 3.0,
}

# Benefit-taxation "provisional income" thresholds (statutory, NOT indexed).
# provisional income = other income (AGI-ish) + tax-exempt interest + 50% of SS.
TAX_THRESHOLDS = {
    "single": (25_000, 34_000),
    "hoh":    (25_000, 34_000),
    "mfj":    (32_000, 44_000),
    "mfs":    (0, 0),             # MFS: generally up to 85% taxable from dollar one
}

# Statutory claiming knobs.
EARLY_RED_FIRST36_PER_MO = 5.0 / 9.0 / 100.0    # 0.5556%/mo for the first 36 months early
EARLY_RED_BEYOND_PER_MO = 5.0 / 12.0 / 100.0    # 0.4167%/mo beyond 36 months early
DRC_PER_YEAR_1943PLUS = 0.08                    # 8%/yr delayed-retirement credit, to age 70
MIN_CLAIM_AGE = 62
MAX_CLAIM_AGE = 70

_STATUS_ALIASES = {
    "mfj": "mfj", "married": "mfj", "married_filing_jointly": "mfj", "joint": "mfj",
    "single": "single", "s": "single",
    "hoh": "hoh", "head_of_household": "hoh", "head": "hoh",
    "mfs": "mfs", "married_filing_separately": "mfs", "separate": "mfs",
}


def norm_status(status):
    key = (status or "mfj").strip().lower().replace(" ", "_")
    return _STATUS_ALIASES.get(key, "mfj")


# ---------------------------------------------------------------------------
# Full Retirement Age (birth-year table, in months).
# ---------------------------------------------------------------------------

def fra_months(birth_year):
    """Full Retirement Age as a total number of months, per SSA's birth-year table.
    <=1954 -> 66y; 1955..1959 step +2mo/yr; >=1960 -> 67y. (Pre-1943 treated as 66.)"""
    y = int(birth_year)
    if y <= 1954:
        return 66 * 12
    if y >= 1960:
        return 67 * 12
    return 66 * 12 + (y - 1954) * 2   # 1955->66y2m ... 1959->66y10m


def fra_dict(birth_year):
    m = fra_months(birth_year)
    return {"years": m // 12, "months": m % 12, "total_months": m}


# ---------------------------------------------------------------------------
# PIA from AIME (or a rough AIME from salary).
# ---------------------------------------------------------------------------

def pia_from_aime(aime):
    """Primary Insurance Amount (monthly, today's-dollar) from Average Indexed
    Monthly Earnings using the 2026 bend points. PIA is the FRA benefit and is the
    base for every claiming adjustment. Capped at the 2026 max-FRA benefit."""
    a = max(0.0, float(aime))
    b1, b2 = BEND_POINTS_2026
    r1, r2, r3 = PIA_RATES
    pia = r1 * min(a, b1)
    if a > b1:
        pia += r2 * (min(a, b2) - b1)
    if a > b2:
        pia += r3 * (a - b2)
    # SSA truncates PIA to the next lower dime; cap at the 2026 max-FRA benefit.
    pia = int(pia * 10) / 10.0
    return round(min(pia, MAX_FRA_BENEFIT_2026), 2)


def aime_from_salary(annual_salary, years=35):
    """VERY rough AIME proxy when the user has no SSA statement: assume a flat career
    at `annual_salary` (indexed), capped at the wage base. This ignores the real
    35-year indexing and low-earning years, so it OVER-estimates for most people —
    flagged as low confidence. Prefer `estimated_fra_benefit` from ssa.gov."""
    s = min(max(0.0, float(annual_salary)), OASDI_WAGE_BASE_2026)
    return round(s / 12.0, 2)


# ---------------------------------------------------------------------------
# Claiming-age adjustment (early reduction / delayed credit).
# ---------------------------------------------------------------------------

def claim_factor(claim_age, birth_year):
    """Benefit as a fraction of PIA for claiming at `claim_age` (years, may be
    fractional like 62.5). Clamped to [62, 70]. FRA -> 1.0; earlier reduces,
    later adds delayed-retirement credits (to 70)."""
    fam = fra_months(birth_year)
    cm = int(round(max(MIN_CLAIM_AGE, min(MAX_CLAIM_AGE, float(claim_age))) * 12))
    if cm == fam:
        return 1.0
    if cm < fam:                              # early
        n = fam - cm
        first = min(n, 36)
        rest = max(0, n - 36)
        reduction = first * EARLY_RED_FIRST36_PER_MO + rest * EARLY_RED_BEYOND_PER_MO
        return round(1.0 - reduction, 5)
    # delayed
    n = min(cm, MAX_CLAIM_AGE * 12) - fam
    return round(1.0 + n * (DRC_PER_YEAR_1943PLUS / 12.0), 5)


def monthly_benefit(pia, claim_age, birth_year):
    """Monthly benefit (today's-dollar) = PIA * claim_factor."""
    return round(float(pia) * claim_factor(claim_age, birth_year), 2)


# ---------------------------------------------------------------------------
# Federal taxation of benefits (IRC §86 provisional-income worksheet).
# ---------------------------------------------------------------------------

def taxable_portion(annual_ss, other_income, status="mfj", tax_exempt_interest=0.0):
    """Portion of annual SS benefits that is federally TAXABLE, via the provisional-
    income worksheet (up to 85%). `other_income` is your other AGI (IRA/401k
    withdrawals, dividends, realized gains, wages...). Thresholds are statutory and
    not inflation-indexed, so more of the benefit becomes taxable over time."""
    st = norm_status(status)
    ss = max(0.0, float(annual_ss))
    if ss == 0:
        return 0.0
    base1, base2 = TAX_THRESHOLDS[st]
    provisional = max(0.0, float(other_income)) + max(0.0, float(tax_exempt_interest)) + 0.5 * ss
    if st == "mfs":
        return round(min(0.85 * ss, 0.85 * provisional), 2)
    if provisional <= base1:
        return 0.0
    if provisional <= base2:
        return round(min(0.5 * ss, 0.5 * (provisional - base1)), 2)
    lesser = min(0.5 * ss, 0.5 * (base2 - base1))
    return round(min(0.85 * ss, 0.85 * (provisional - base2) + lesser), 2)


# ---------------------------------------------------------------------------
# Earnings test (only relevant if claiming early while still working).
# ---------------------------------------------------------------------------

def earnings_test_withholding(annual_earnings, under_fra=True, fra_this_year=False):
    """Benefits withheld under the retirement earnings test (2026). Only applies
    before FRA. Returns the annual $ withheld (recouped later as a higher benefit
    at FRA — this is a timing effect, not a permanent loss)."""
    e = max(0.0, float(annual_earnings))
    if fra_this_year:
        thr = EARNINGS_TEST_2026["fra_year_annual"]
        return round(max(0.0, e - thr) / EARNINGS_TEST_2026["fra_year_withhold_ratio"], 2)
    if under_fra:
        thr = EARNINGS_TEST_2026["under_fra_annual"]
        return round(max(0.0, e - thr) / EARNINGS_TEST_2026["under_fra_withhold_ratio"], 2)
    return 0.0


# ---------------------------------------------------------------------------
# One-person estimate + household rollup.
# ---------------------------------------------------------------------------

def _resolve_pia(pia_monthly=None, estimated_fra_benefit=None, aime=None,
                 annual_salary=None):
    """Resolve a monthly PIA (FRA benefit) from whatever the user supplied, in
    priority order, and report the confidence + source of the number."""
    if pia_monthly:
        return round(float(pia_monthly), 2), "high", "explicit PIA (monthly)"
    if estimated_fra_benefit:
        # SSA's "at full retirement age" estimate IS the PIA in today's dollars.
        return round(min(float(estimated_fra_benefit), MAX_FRA_BENEFIT_2026), 2), "high", \
            "SSA statement estimate at FRA"
    if aime:
        return pia_from_aime(aime), "medium", "PIA from supplied AIME (2026 bend points)"
    if annual_salary:
        return pia_from_aime(aime_from_salary(annual_salary)), "low", \
            "PIA from a flat-salary AIME proxy — rough; use your SSA statement estimate"
    return None, "none", "no earnings input supplied"


def estimate(birth_year=None, claim_age=None, pia_monthly=None,
             estimated_fra_benefit=None, aime=None, annual_salary=None,
             status="mfj", other_income=0.0, label="you"):
    """Estimate one person's Social Security benefit and its taxation.
    Returns monthly/annual benefit at the chosen claim age, the claim factor vs
    PIA, the FRA, and the taxable portion given `other_income`."""
    if birth_year is None:
        return {"label": label, "available": False,
                "note": "No birth_year — can't set FRA or the claiming adjustment."}
    pia, conf, src = _resolve_pia(pia_monthly, estimated_fra_benefit, aime, annual_salary)
    if pia is None:
        return {"label": label, "available": False, "birth_year": int(birth_year),
                "fra": fra_dict(birth_year),
                "note": "No PIA input (need estimated_fra_benefit, pia_monthly, aime, or salary)."}
    fam = fra_months(birth_year)
    ca = float(claim_age) if claim_age is not None else fam / 12.0
    ca = max(MIN_CLAIM_AGE, min(MAX_CLAIM_AGE, ca))
    factor = claim_factor(ca, birth_year)
    m = round(pia * factor, 2)
    annual = round(m * 12.0, 2)
    taxable = taxable_portion(annual, other_income, status)
    return {
        "label": label,
        "available": True,
        "birth_year": int(birth_year),
        "fra": fra_dict(birth_year),
        "pia_monthly": pia,
        "pia_confidence": conf,
        "pia_source": src,
        "claim_age": round(ca, 2),
        "claim_factor": factor,
        "claim_vs_pia_pct": round(factor * 100, 1),
        "monthly_benefit": m,
        "annual_benefit": annual,
        "taxable_portion": taxable,
        "taxable_pct_of_benefit": round((taxable / annual * 100) if annual else 0.0, 1),
        "note": ("Today's-dollar estimate (COLA keeps it ~flat in real terms). Claiming at "
                 f"{ca:.1f} pays {round(factor*100,1)}% of the ${pia:,.0f}/mo PIA. "
                 "Educational, not advice — verify at ssa.gov/myaccount."),
    }


def summary(birth_year=None, claim_age=None, status="mfj", other_income=0.0,
            retire_age=None, self_kwargs=None, spouse=None):
    """Household rollup for /api/ss. Estimates the primary earner and (optionally)
    a spouse, sums the annual benefit, and produces a Monte-Carlo-ready schedule of
    (years_into_retirement, annual_gross_benefit) steps so the decumulation floor
    switches on at each person's claim age. `retire_age` anchors the schedule."""
    self_kwargs = dict(self_kwargs or {})
    self_kwargs.setdefault("birth_year", birth_year)
    self_kwargs.setdefault("claim_age", claim_age)
    self_kwargs.setdefault("status", status)
    self_kwargs.setdefault("other_income", other_income)
    self_est = estimate(label="you", **self_kwargs)

    people = [self_est]
    if spouse:
        sp = dict(spouse)
        sp.setdefault("status", status)
        sp.setdefault("other_income", other_income)
        people.append(estimate(label="spouse", **sp))

    total_annual = round(sum(p.get("annual_benefit", 0) or 0 for p in people if p.get("available")), 2)
    total_taxable = round(sum(p.get("taxable_portion", 0) or 0 for p in people if p.get("available")), 2)

    # Monte-Carlo floor schedule: each claimant's gross annual benefit starts at
    # (their claim_age - retire_age) years into retirement (clamped at 0).
    schedule = []
    if retire_age is not None:
        for p in people:
            if not p.get("available"):
                continue
            start = max(0, int(round(p["claim_age"] - float(retire_age))))
            schedule.append({"label": p["label"], "start_year": start,
                             "annual_benefit": p["annual_benefit"], "claim_age": p["claim_age"]})

    return {
        "benefit_year": BENEFIT_YEAR,
        "cola_pct": round(COLA_2026 * 100, 1),
        "filing_status": norm_status(status),
        "people": people,
        "household_annual_benefit": total_annual,
        "household_monthly_benefit": round(total_annual / 12.0, 2),
        "household_taxable_portion": total_taxable,
        "mc_floor_schedule": schedule,
        "source": SOURCE,
        "note": ("Real-terms (today's-dollar) Social Security estimate. It is an inflation-indexed "
                 "income floor that reduces how much the portfolio must fund from each person's "
                 "claim age onward — which is why omitting it understates Monte-Carlo success. Up "
                 "to 85% of benefits is federally taxable (TX has no state tax) and 100% counts "
                 "toward ACA MAGI. Educational, not advice — verify at ssa.gov/myaccount."),
    }
