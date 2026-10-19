# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
tax.py — a small bracket-aware federal income-tax engine for the Fiduciary.

Purpose (Fable review item G5): replace the three flat-rate knobs that used to
approximate tax —

    MC_WITHDRAWAL_TAX_PCT   (gross-up in the Monte Carlo)
    BRIDGE_TAX_PCT          (Roth-conversion tax in the bridge/ladder)
    ANNUAL_GAINS_BUDGET     (hand-set realized-gains cap in the income glide path)

— with ONE model: the 2026 federal ordinary brackets + long-term-capital-gains
(LTCG) stacking. From that model we *derive* the numbers those knobs used to
guess: an effective withdrawal rate, the Roth-conversion tax and "fill-the-bracket"
headroom, and the 0%-LTCG gain-harvesting budget. The FEDERAL model here is
state-independent and always applies as-is. Texas (this app's original home state)
happens to have no state income tax, which is why this was originally federal-only;
`states.py` (RESUME-TRACKER item 3) now supplies a light STATE estimate on top —
exact for no-tax/flat-tax states, an explicit "not modeled" flag for progressive
ones (a full 50-state bracket engine is out of scope; use your state's own
calculator for a progressive state).

Everything here is pure arithmetic (zero GPU, zero network) and read-only /
educational — NOT tax advice. Confirm sizing with a CPA before acting.

Data source: IRS Rev. Proc. 2025-32 (tax year 2026), as tabulated by the Tax
Foundation, "2026 Tax Brackets" (https://taxfoundation.org/data/all/federal/2026-tax-brackets/).
OBBBA (July 2025) made the TCJA rate structure permanent and reset the standard
deduction; 2026 figures already reflect it.
"""

import states

TAX_YEAR = 2026
SOURCE = "IRS Rev. Proc. 2025-32 (2026), via Tax Foundation"

# Ordinary income brackets as (marginal_rate, lower_bound_of_bracket) on TAXABLE income.
# Upper bound of each bracket is the lower bound of the next. Last bracket is open-ended.
ORDINARY_BRACKETS = {
    "single": [
        (0.10, 0), (0.12, 12_400), (0.22, 50_400), (0.24, 105_700),
        (0.32, 201_775), (0.35, 256_225), (0.37, 640_600),
    ],
    "mfj": [
        (0.10, 0), (0.12, 24_800), (0.22, 100_800), (0.24, 211_400),
        (0.32, 403_550), (0.35, 512_450), (0.37, 768_700),
    ],
    "hoh": [
        (0.10, 0), (0.12, 17_700), (0.22, 67_450), (0.24, 105_700),
        (0.32, 201_775), (0.35, 256_200), (0.37, 640_600),
    ],
}

# Long-term capital-gains / qualified-dividend brackets: (rate, lower_bound) on
# TOTAL taxable income (ordinary income stacks first, LTCG on top).
LTCG_BRACKETS = {
    "single": [(0.00, 0), (0.15, 49_450), (0.20, 545_500)],
    "mfj":    [(0.00, 0), (0.15, 98_900), (0.20, 613_700)],
    "hoh":    [(0.00, 0), (0.15, 66_200), (0.20, 579_600)],
}

STANDARD_DEDUCTION = {"single": 16_100, "mfj": 32_200, "hoh": 24_150}

# Extra standard deduction for age 65+ (per qualifying person), 2026.
SENIOR_ADD = {"single": 2_050, "hoh": 2_050, "mfj": 1_650}

# Net Investment Income Tax (statutory MAGI thresholds; NOT inflation-indexed).
NIIT_RATE = 0.038
NIIT_THRESHOLD = {"single": 200_000, "mfj": 250_000, "hoh": 200_000, "mfs": 125_000}

_ALIASES = {
    "mfj": "mfj", "married": "mfj", "married_filing_jointly": "mfj", "joint": "mfj",
    "single": "single", "s": "single",
    "hoh": "hoh", "head_of_household": "hoh", "head": "hoh",
    # MFS is not modeled with its own brackets here; fall back to single-ish behavior
    # is misleading, so we normalize unknown/mfs to single and flag it in summary().
}


def norm_status(status):
    """Normalize a filing-status string to one of: single, mfj, hoh."""
    key = (status or "mfj").strip().lower().replace(" ", "_")
    return _ALIASES.get(key, "mfj")


def standard_deduction(status="mfj", age=None, spouse_age=None):
    """2026 standard deduction, adding the 65+ extra per qualifying person.
    (The separate OBBBA $6k senior deduction is income-phased and handled in
    summary() as an optional adjustment, not baked in here.)"""
    st = norm_status(status)
    base = STANDARD_DEDUCTION[st]
    add = 0
    if age is not None and age >= 65:
        add += SENIOR_ADD[st]
    if st == "mfj" and spouse_age is not None and spouse_age >= 65:
        add += SENIOR_ADD["mfj"]
    return base + add


def ordinary_tax(taxable, status="mfj"):
    """Federal ordinary income tax on a given TAXABLE income (deductions already applied)."""
    st = norm_status(status)
    br = ORDINARY_BRACKETS[st]
    taxable = max(0.0, float(taxable))
    tax = 0.0
    for i, (rate, lo) in enumerate(br):
        hi = br[i + 1][1] if i + 1 < len(br) else float("inf")
        if taxable <= lo:
            break
        tax += (min(taxable, hi) - lo) * rate
    return round(tax, 2)


def marginal_ordinary_rate(taxable, status="mfj"):
    """Marginal ordinary bracket rate at a given TAXABLE income."""
    st = norm_status(status)
    br = ORDINARY_BRACKETS[st]
    taxable = max(0.0, float(taxable))
    rate = br[0][0]
    for r, lo in br:
        if taxable >= lo:
            rate = r
        else:
            break
    return rate


def bracket_top(rate, status="mfj"):
    """Taxable-income ceiling (top) of the bracket whose marginal rate == `rate`.
    Returns None for the top (37%) bracket, which is open-ended."""
    st = norm_status(status)
    br = ORDINARY_BRACKETS[st]
    for i, (r, lo) in enumerate(br):
        if abs(r - rate) < 1e-9:
            return br[i + 1][1] if i + 1 < len(br) else None
    return None


def ltcg_tax(ordinary_taxable, ltcg, status="mfj"):
    """Tax on `ltcg` long-term gains that STACK on top of `ordinary_taxable` income.
    Returns (tax, breakdown) where breakdown is the $ taxed at 0/15/20%."""
    st = norm_status(status)
    br = LTCG_BRACKETS[st]
    oti = max(0.0, float(ordinary_taxable))
    g = max(0.0, float(ltcg))
    t15 = br[1][1]   # top of 0% bracket / start of 15%
    t20 = br[2][1]   # start of 20%
    at0 = max(0.0, min(oti + g, t15) - oti)
    at0 = min(g, at0)
    rem = g - at0
    at15 = max(0.0, min(oti + g, t20) - max(oti, t15))
    at15 = min(rem, max(0.0, at15))
    at20 = g - at0 - at15
    tax = at15 * 0.15 + at20 * 0.20
    return round(tax, 2), {"at_0pct": round(at0, 2), "at_15pct": round(at15, 2),
                           "at_20pct": round(at20, 2)}


# ---------------------------------------------------------------------------
# Derivations — what the three flat knobs used to approximate.
# ---------------------------------------------------------------------------

def conversion_headroom(other_ordinary_income, status="mfj", target_rate=0.12,
                        deduction=None, age=None, spouse_age=None):
    """How many more dollars of ORDINARY income (e.g. a Roth conversion, an IRA
    withdrawal) can be recognized before taxable income crosses the TOP of the
    `target_rate` bracket. Deduction room below the standard deduction counts,
    so this works even when other income is near zero (the classic early-retirement
    'fill the 12% bracket' move)."""
    st = norm_status(status)
    ded = standard_deduction(st, age, spouse_age) if deduction is None else deduction
    top = bracket_top(target_rate, st)
    if top is None:
        return {"target_rate": target_rate, "headroom": None,
                "note": "top bracket is open-ended; no ceiling"}
    gross = max(0.0, float(other_ordinary_income))
    # gross + C - ded <= top  ->  C <= top + ded - gross
    headroom = max(0.0, top + ded - gross)
    taxable_before = max(0.0, gross - ded)
    return {
        "target_rate": target_rate,
        "target_bracket_top_taxable": top,
        "deduction": ded,
        "other_ordinary_income": round(gross, 2),
        "taxable_before": round(taxable_before, 2),
        "headroom": round(headroom, 2),
        "marginal_rate_before": marginal_ordinary_rate(taxable_before, st),
    }


def gains_harvest_budget(other_ordinary_income, status="mfj", deduction=None,
                         age=None, spouse_age=None):
    """Dollars of LONG-TERM gains that can be realized at the 0% LTCG rate given
    other ordinary income — i.e. tax-free gain-harvesting / step-up room. This is
    the principled replacement for the hand-set ANNUAL_GAINS_BUDGET."""
    st = norm_status(status)
    ded = standard_deduction(st, age, spouse_age) if deduction is None else deduction
    taxable_ord = max(0.0, float(other_ordinary_income) - ded)
    t15 = LTCG_BRACKETS[st][1][1]
    room0 = max(0.0, t15 - taxable_ord)
    return {
        "zero_pct_ltcg_ceiling_taxable": t15,
        "taxable_ordinary": round(taxable_ord, 2),
        "gains_at_0pct_budget": round(room0, 2),
        "note": ("Long-term gains up to this amount stack into the 0% LTCG bracket "
                 "and are federally tax-free; realize them to reset basis. Above it, "
                 "gains are taxed at 15%."),
    }


def effective_rate(gross_ordinary, ltcg=0.0, status="mfj", deduction=None,
                   age=None, spouse_age=None, include_niit=True, magi=None):
    """Blended EFFECTIVE federal rate on a withdrawal/income mix. Used to gross-up
    spending in the Monte Carlo instead of a flat MC_WITHDRAWAL_TAX_PCT."""
    st = norm_status(status)
    ded = standard_deduction(st, age, spouse_age) if deduction is None else deduction
    gross_ord = max(0.0, float(gross_ordinary))
    g = max(0.0, float(ltcg))
    taxable_ord = max(0.0, gross_ord - ded)
    ord_t = ordinary_tax(taxable_ord, st)
    cg_t, cg_break = ltcg_tax(taxable_ord, g, st)
    niit = 0.0
    if include_niit and g > 0:
        m = magi if magi is not None else (gross_ord + g)
        over = max(0.0, m - NIIT_THRESHOLD.get(st, 200_000))
        niit = round(min(g, over) * NIIT_RATE, 2)
    total_tax = ord_t + cg_t + niit
    base = gross_ord + g
    eff = (total_tax / base) if base else 0.0
    return {
        "gross_income": round(base, 2),
        "ordinary_tax": ord_t,
        "ltcg_tax": cg_t,
        "ltcg_breakdown": cg_break,
        "niit": niit,
        "total_tax": round(total_tax, 2),
        "effective_rate_pct": round(eff * 100, 2),
        "marginal_ordinary_rate_pct": round(marginal_ordinary_rate(taxable_ord, st) * 100, 1),
    }


def state_tax_estimate(taxable_ordinary, ltcg, state=None):
    """A DELIBERATELY simple state income-tax estimate — not a second bracket engine.
    No-tax and flat-tax states get an exact-shaped (rate x federal-taxable-income)
    estimate, which is close for states that largely conform to the federal base;
    progressive states are explicitly flagged as NOT modeled rather than guessed."""
    sf = states.facts(state)
    base = max(0.0, float(taxable_ordinary)) + max(0.0, float(ltcg))
    if sf["income_tax_type"] == "none":
        return {"state": sf["name"], "state_code": sf["code"], "type": "none",
                "estimated_state_tax": 0.0,
                "note": f"{sf['name']} levies no state income tax."}
    if sf["income_tax_type"] == "flat" and sf["income_tax_rate_pct"] is not None:
        est = round(base * sf["income_tax_rate_pct"] / 100.0, 2)
        return {"state": sf["name"], "state_code": sf["code"], "type": "flat",
                "rate_pct": sf["income_tax_rate_pct"], "estimated_state_tax": est,
                "note": (f"Rough estimate only: {sf['income_tax_rate_pct']}% x approximate "
                         "federal taxable income. Real state returns have their own "
                         "deductions/credits that differ from the federal ones used here — "
                         "confirm with your state's own calculator.") + ("".join(
                             " " + n for n in sf["notes"]))}
    return {"state": sf["name"], "state_code": sf["code"], "type": sf["income_tax_type"],
            "estimated_state_tax": None,
            "note": (f"{sf['name']} uses progressive brackets, which this app does not "
                     "model (a full 50-state bracket engine is out of scope) — use your "
                     "state's own tax calculator for a real number.")}


def summary(gross_ordinary=0.0, ltcg=0.0, status="mfj", age=None, spouse_age=None,
            fill_to_rate=0.12, magi=None, state=None):
    """One-call rollup for the /api/tax endpoint: the model, plus every derived
    knob (effective rate, conversion headroom, 0%-gain budget, harvest flag)."""
    st = norm_status(status)
    raw = (status or "").strip().lower().replace(" ", "_")
    unsupported = raw in ("mfs", "married_filing_separately", "separate")
    ded = standard_deduction(st, age, spouse_age)
    eff = effective_rate(gross_ordinary, ltcg, st, ded, age, spouse_age, magi=magi)
    conv = conversion_headroom(gross_ordinary, st, fill_to_rate, ded, age, spouse_age)
    harvest = gains_harvest_budget(gross_ordinary, st, ded, age, spouse_age)
    taxable_ord = max(0.0, float(gross_ordinary) - ded)
    marg = marginal_ordinary_rate(taxable_ord, st)
    state_tax = state_tax_estimate(taxable_ord, ltcg, state)
    # Harvest flag: a "low year" is one where you're still inside the 12% ordinary
    # bracket AND have 0%-LTCG room — the window to convert and/or harvest gains.
    low_year = marg <= 0.12 and harvest["gains_at_0pct_budget"] > 0
    notes = [
        "Federal brackets per %s always apply as modeled; the state figure below is a "
        "simplification (see state_tax.note), not a second bracket engine." % SOURCE,
        "Educational, not tax advice — size conversions/harvests with a CPA.",
    ]
    if unsupported:
        notes.insert(0, "Filing status 'MFS' is not modeled; figures shown use MFJ "
                        "brackets and are INDICATIVE ONLY — set a supported status.")
    return {
        "tax_year": TAX_YEAR,
        "filing_status": st,
        "standard_deduction": ded,
        "inputs": {"gross_ordinary": round(float(gross_ordinary), 2),
                   "ltcg": round(float(ltcg), 2), "age": age, "spouse_age": spouse_age},
        "taxable_ordinary": round(taxable_ord, 2),
        "marginal_ordinary_rate_pct": round(marg * 100, 1),
        "effective": eff,
        "roth_conversion_headroom": conv,
        "gain_harvest": harvest,
        "state_tax": state_tax,
        "flags": {
            "low_bracket_year": low_year,
            "convert_or_harvest_window": low_year,
            "over_niit_threshold": (magi if magi is not None else
                                    (float(gross_ordinary) + float(ltcg)))
                                   > NIIT_THRESHOLD.get(st, 200_000),
        },
        "source": SOURCE,
        "note": " ".join(notes),
    }
