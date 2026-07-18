"""
aca.py — ACA / MAGI ledger + premium-subsidy (PTC) estimator for the Fiduciary.

Purpose (Fable review item G1): give the early-retirement bridge a healthcare
lens. Before Medicare at 65, a DIY early retiree buys ACA marketplace coverage,
and the premium tax credit (PTC) is a step function of MAGI. Roth conversions and
realized gains RAISE MAGI, so there is a direct tension between "fill the low tax
bracket" (tax.py / G5) and "stay under the ACA subsidy cliff" (here). This module
builds the MAGI ledger, estimates the subsidy, and — critically for 2026 — flags
the 400%-FPL cliff.

*** 2026 POLICY STATE (verified July 2026): the ARPA/IRA enhanced premium tax
credits EXPIRED January 1, 2026. For plan year 2026 the pre-ARPA rules are back:
- PTC eligibility is 100%–400% FPL, with a HARD CLIFF at 400% (above it, $0 PTC).
- Applicable ("expected contribution") percentages reverted upward and are indexed.
This 100-400% FPL federal band is the same in every state. What DIFFERS by state is
what happens BELOW 100% FPL: Texas (this app's original home state) did NOT expand
Medicaid, so below 100% FPL is a true COVERAGE GAP (no Medicaid, no PTC — the
planning band is a narrow 100%-400% FPL). States that DID expand Medicaid (most of
them, including North Carolina as of Dec 2023) instead cover that population up to
~138% FPL via Medicaid — not a gap, but also not PTC/marketplace territory; see
`states.py` (RESUME-TRACKER item 3) for the current per-state expansion status. ***

Data sources:
- 2026-coverage FPL (based on 2025 HHS poverty guidelines, applied with a one-year
  lag): HHS/ASPE 2025 guidelines, 48 contiguous states + DC.
- 2026 applicable-percentage schedule: pre-ARPA §36B sliding scale (2.1%–9.96%),
  as tabulated by The Finance Buff, "2025/2026/2027 FPL for ACA."

Pure arithmetic, one static table, zero GPU/network. Read-only / educational —
NOT tax, legal, or insurance advice. The exact subsidy also depends on your local
Second-Lowest-Cost Silver Plan (SLCSP) benchmark and ages, which you supply.
"""

import states

COVERAGE_YEAR = 2026
SOURCE = "HHS/ASPE 2025 poverty guidelines (2026 coverage) + pre-ARPA §36B applicable %"
MEDICAID_EXPANSION_CEILING_PCT = 138.0   # expansion states cover up to ~this % FPL via Medicaid

# FPL for 2026 COVERAGE (48 contiguous states + DC). Household 1..8; +increment above 8.
FPL_2026 = {1: 15_650, 2: 21_150, 3: 26_650, 4: 32_150,
            5: 37_650, 6: 43_150, 7: 48_650, 8: 54_150}
FPL_INCREMENT = 5_500

PTC_FLOOR_PCT = 100.0    # below this: no PTC (in TX, a coverage gap)
PTC_CLIFF_PCT = 400.0    # 2026: hard cliff — at/above this, $0 subsidy

# 2026 applicable-percentage sliding scale (percent of MAGI you're expected to pay
# toward the benchmark Silver plan). Bands are (fpl_lo, fpl_hi, pct_lo, pct_hi),
# linearly interpolated within each band. First band (<133%) is flat 2.1%.
APPLICABLE_PCT_2026 = [
    (100.0, 133.0, 2.10, 2.10),
    (133.0, 150.0, 3.14, 4.19),
    (150.0, 200.0, 4.19, 6.60),
    (200.0, 250.0, 6.60, 8.44),
    (250.0, 300.0, 8.44, 9.96),
    (300.0, 400.0, 9.96, 9.96),
]


def fpl(household_size):
    """Federal poverty line for 2026 coverage, 48 states + DC."""
    n = max(1, int(household_size))
    if n <= 8:
        return FPL_2026[n]
    return FPL_2026[8] + FPL_INCREMENT * (n - 8)


def fpl_pct(magi, household_size):
    """MAGI as a percentage of the FPL for the household."""
    base = fpl(household_size)
    return round((float(magi) / base) * 100.0, 1) if base else None


def applicable_pct(fplp):
    """Expected-contribution percentage for a given %-of-FPL (2026 schedule).
    Returns None below 100% FPL (no PTC / gap) or at/above the 400% cliff."""
    if fplp is None or fplp < PTC_FLOOR_PCT or fplp >= PTC_CLIFF_PCT:
        return None
    for lo, hi, plo, phi in APPLICABLE_PCT_2026:
        if lo <= fplp < hi:
            if phi == plo:
                return round(plo, 2)
            frac = (fplp - lo) / (hi - lo)
            return round(plo + frac * (phi - plo), 2)
    # exactly 400 handled by cliff check above; fallthrough safety
    return round(APPLICABLE_PCT_2026[-1][3], 2)


# MAGI components that count for ACA (AGI + tax-exempt interest + untaxed SS +
# excluded foreign income). Each is the full includable amount for the plan year.
MAGI_COMPONENTS = ("wages_other_ordinary", "roth_conversions", "ira_401k_withdrawals",
                   "taxable_interest", "tax_exempt_interest", "dividends",
                   "capital_gains", "social_security_total")


def magi(components):
    """Sum the MAGI ledger. `components` is a dict of any MAGI_COMPONENTS keys.
    NOTE: ACA MAGI adds back tax-exempt interest and counts 100% of Social
    Security (both the taxable and non-taxable parts), and includes the FULL
    realized capital gain — there is no deduction and no LTCG preference here."""
    total = 0.0
    ledger = {}
    for k in MAGI_COMPONENTS:
        v = float(components.get(k, 0) or 0)
        ledger[k] = round(v, 2)
        total += v
    return round(total, 2), ledger


def cliff(magi_value, household_size):
    """Distance to the 400%-FPL subsidy cliff — the key planning number: how much
    MORE MAGI (Roth conversion, gain harvest, IRA withdrawal) can be added before
    ALL premium subsidy is lost."""
    base = fpl(household_size)
    cliff_magi = round(PTC_CLIFF_PCT / 100.0 * base, 2)
    floor_magi = round(PTC_FLOOR_PCT / 100.0 * base, 2)
    headroom = round(cliff_magi - float(magi_value), 2)
    return {
        "fpl_base": base,
        "ptc_floor_magi": floor_magi,          # below this: coverage gap (non-expansion states) or Medicaid (expansion states)
        "cliff_magi": cliff_magi,              # at/above this: $0 subsidy
        "headroom_to_cliff": headroom,         # + = room left; - = already over
        "over_cliff": float(magi_value) >= cliff_magi,
        "below_floor": float(magi_value) < floor_magi,
    }


def ptc_estimate(magi_value, household_size, slcsp_annual=None, state=None):
    """Estimate the annual premium tax credit. Contribution = MAGI * applicable%;
    PTC = benchmark(SLCSP) - contribution, floored at 0. SLCSP (the local
    Second-Lowest-Cost Silver Plan annual premium) is location/age specific and
    must be supplied for a dollar estimate; without it we still return the
    expected-contribution figure and the eligibility band.

    The 100-400% FPL PTC band itself is federal and identical in every state; what
    differs is what MAGI below 100% FPL means (see `states.py`, item 3): a true
    coverage gap in a non-expansion state (TX, GA, and 8 others) vs. Medicaid
    eligibility (not a gap) in an expansion state (most states, incl. NC, CO)."""
    sf = states.facts(state)
    fplp = fpl_pct(magi_value, household_size)
    ap = applicable_pct(fplp)
    out = {"magi": round(float(magi_value), 2), "household_size": int(household_size),
           "fpl_base": fpl(household_size), "fpl_pct": fplp, "applicable_pct": ap,
           "state": sf["name"], "state_medicaid_expanded": sf["medicaid_expanded"]}
    if fplp is None:
        out["eligible"] = False
        out["band"] = "unknown"
        return out
    if fplp < PTC_FLOOR_PCT:
        if sf["medicaid_expanded"]:
            out.update(eligible=False, band="medicaid_likely",
                       note=(f"MAGI below 100% FPL. {sf['name']} expanded Medicaid, so this is "
                             "NOT a coverage gap — you're likely Medicaid-eligible (usually free "
                             "or near-free, better than any marketplace plan), just not "
                             "marketplace-PTC-eligible. Not a problem to solve with a Roth "
                             "conversion the way it would be in a non-expansion state."))
        else:
            out.update(eligible=False, band="coverage_gap",
                       note=(f"MAGI below 100% FPL. {sf['name']} has NOT expanded Medicaid, so "
                             "this is a real COVERAGE GAP: too low for a marketplace subsidy, "
                             "ineligible for Medicaid. Raising MAGI into 100-150% FPL (e.g. a "
                             "small Roth conversion) can restore subsidy eligibility."))
        return out
    if fplp >= PTC_CLIFF_PCT:
        out.update(eligible=False, band="over_cliff",
                   note=("MAGI at/above 400% FPL. For 2026 the enhanced subsidies have "
                         "expired, so this is the CLIFF: $0 premium tax credit — you pay the "
                         "full unsubsidized premium. Keeping MAGI just under the cliff is the "
                         "high-value early-retirement move."))
        return out
    contribution = round(float(magi_value) * ap / 100.0, 2)
    out.update(eligible=True, band="subsidized",
               expected_annual_contribution=contribution,
               expected_monthly_contribution=round(contribution / 12.0, 2))
    if sf["medicaid_expanded"] and fplp < MEDICAID_EXPANSION_CEILING_PCT:
        out["medicaid_likely_note"] = (f"{sf['name']} expanded Medicaid up to ~138% FPL — at "
                                       f"{fplp}% FPL you're technically PTC-eligible on paper, "
                                       "but Medicaid is usually free/cheaper and the more likely "
                                       "actual path here; confirm on your state's Medicaid site.")
    if slcsp_annual:
        ptc = round(max(0.0, float(slcsp_annual) - contribution), 2)
        out.update(slcsp_annual=round(float(slcsp_annual), 2),
                   estimated_annual_ptc=ptc,
                   estimated_monthly_ptc=round(ptc / 12.0, 2))
    return out


def summary(components=None, household_size=1, slcsp_annual=None,
            planned_conversion=0.0, planned_gains=0.0, state=None):
    """One-call rollup for /api/aca: MAGI ledger, %-FPL, subsidy band + estimate,
    and the cliff picture — including what a PLANNED conversion/harvest would do."""
    components = dict(components or {})
    sf = states.facts(state)
    magi_now, ledger = magi(components)
    est = ptc_estimate(magi_now, household_size, slcsp_annual, state)
    cl = cliff(magi_now, household_size)

    planned = None
    add = float(planned_conversion or 0) + float(planned_gains or 0)
    if add:
        comp2 = dict(components)
        comp2["roth_conversions"] = float(comp2.get("roth_conversions", 0) or 0) + float(planned_conversion or 0)
        comp2["capital_gains"] = float(comp2.get("capital_gains", 0) or 0) + float(planned_gains or 0)
        magi2, _ = magi(comp2)
        est2 = ptc_estimate(magi2, household_size, slcsp_annual, state)
        cl2 = cliff(magi2, household_size)
        crosses = (not cl["over_cliff"]) and cl2["over_cliff"]
        planned = {
            "added_magi": round(add, 2), "magi_after": magi2,
            "fpl_pct_after": est2.get("fpl_pct"), "band_after": est2.get("band"),
            "crosses_cliff": crosses,
            "headroom_after": cl2["headroom_to_cliff"],
            "note": ("This planned conversion/harvest would push MAGI ACROSS the 400% FPL "
                     "cliff — you'd lose ALL premium subsidy for the year. Consider capping "
                     "it at the cliff headroom below." if crosses else
                     "Planned amount stays within the subsidy band."),
        }

    flags = {
        "on_cliff_edge": est.get("band") == "subsidized" and cl["headroom_to_cliff"] < (0.15 * cl["cliff_magi"]),
        "over_cliff": cl["over_cliff"],
        # A real coverage GAP only exists below the floor in a non-expansion state; in an
        # expansion state that same MAGI band is (likely) Medicaid, not a gap — see est["band"].
        "below_floor_gap": cl["below_floor"] and not sf["medicaid_expanded"],
        "below_floor_medicaid_likely": cl["below_floor"] and sf["medicaid_expanded"],
        "planned_crosses_cliff": bool(planned and planned.get("crosses_cliff")),
    }
    return {
        "coverage_year": COVERAGE_YEAR,
        "household_size": int(household_size),
        "state": sf["name"], "state_code": sf["code"], "state_medicaid_expanded": sf["medicaid_expanded"],
        "magi": magi_now,
        "magi_ledger": ledger,
        "subsidy": est,
        "cliff": cl,
        "planned": planned,
        "flags": flags,
        "source": SOURCE,
        "note": ("ACA MAGI = AGI + tax-exempt interest + 100% of Social Security. For 2026 the "
                 "enhanced subsidies expired: PTC runs 100-400% FPL with a HARD CLIFF at 400% in "
                 f"every state. Below 100% FPL: {sf['name']} " +
                 ("has expanded Medicaid, so that band is Medicaid coverage, not a gap."
                  if sf["medicaid_expanded"] else
                  "has NOT expanded Medicaid, so that band is a real coverage gap.") +
                 " Educational, not tax/insurance advice — confirm your SLCSP benchmark and "
                 "enrollment on the marketplace. Only relevant pre-Medicare (before 65)."),
    }
