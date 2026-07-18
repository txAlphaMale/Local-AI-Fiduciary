"""
states.py — 50-state + DC residency-aware facts registry (Fable RESUME-TRACKER item 3).

The app was hardcoded to Texas assumptions in estate.py (community property, Transfer-on-
Death deeds, no state estate tax), aca.py (Medicaid non-expansion coverage gap), tax.py and
cashdebt.py (no state income tax). This module centralizes the facts so those modules read
from ONE `state_of_residence` setting (household register, default "TX") instead of
hardcoding Jason's state — needed now that a move to Colorado is a live possibility, and two
other users of this codebase are in North Carolina and Georgia, all three of which differ
from Texas on EVERY axis below.

Verification status (2026-07-18): structural facts (which states have no income tax, which
are community-property, which allow TOD deeds, which levy an estate/inheritance tax, which
have NOT expanded Medicaid) were verified via live web search against Tax Foundation, KFF,
and Nolo (updated 2025-06-23) — see SOURCES at the bottom. These are legislative facts that
DO change (a state can adopt a TOD-deed statute or expand Medicaid), so — exactly like every
other dated constant in this app (grounding.py) — treat this file as a snapshot to
periodically re-verify, not a permanent truth. Numeric income-tax RATES are only populated
for the 4 states this session had a specific reason to get right (TX, CO, NC, GA); every
other state is classified (none/flat/progressive) but left rate=None rather than guessed —
see NOTE_UNVERIFIED_RATE.
"""

NOTE_UNVERIFIED_RATE = ("Structural classification (flat/progressive) is stable; the specific "
                         "current rate was NOT verified this session — check the state's "
                         "Department of Revenue before relying on it for a real number.")

# ---------------------------------------------------------------------------------------
# No state income tax (9 states). NH fully repealed its interest/dividends tax as of tax
# year 2025, so it is a true no-income-tax state now (previously only wages were exempt).
# WA has NO wage income tax but DOES levy a state capital-gains excise tax on individuals
# above a large annual exemption (7% up to $1M of WA long-term gains, 9.9% above) — worth
# flagging for this app specifically since it targets concentrated-stock households.
# ---------------------------------------------------------------------------------------
NO_INCOME_TAX_STATES = {"AK", "FL", "NV", "NH", "SD", "TN", "TX", "WA", "WY"}
WA_CAPITAL_GAINS_EXCISE_NOTE = ("Washington has no wage income tax but DOES levy a state "
                                "capital-gains excise tax on individuals: 7% on WA long-term "
                                "gains above a large annual exemption, 9.9% above $1M of gains "
                                "in a year. Relevant for concentrated-stock sales/staged-sale "
                                "planning even though WA is otherwise a 'no income tax' state.")

# ---------------------------------------------------------------------------------------
# Flat-rate income tax states (14) — single rate regardless of income. Rates below are as
# verified 2026-07-18 (Tax Foundation-sourced for CO/GA/NC specifically; the rest are the
# most recently reported 2026 rate at time of writing — re-verify before relying on them).
# GA and NC are both mid-phase-down under multi-year rate-reduction schedules.
# ---------------------------------------------------------------------------------------
FLAT_INCOME_TAX_STATES = {
    "AZ": 2.5, "CO": 4.4, "GA": 5.09, "ID": 5.3, "IL": 4.95, "IN": 2.95, "KY": 3.5,
    "LA": 3.0, "MA": 5.0, "MI": 4.25, "MS": 4.0, "NC": 3.99, "PA": 3.07, "UT": 4.5,
}
GA_PHASE_NOTE = ("Georgia's flat rate dropped to 5.09% on 2026-01-01 and is on a schedule "
                  "to fall 0.10pt/year toward an eventual 4.99% floor — confirm the "
                  "then-current rate for any year beyond 2026.")
NC_PHASE_NOTE = ("North Carolina's flat rate dropped to 3.99% on 2026-01-01 — reported as "
                  "the LAST scheduled decrease in its multi-year phased-reduction plan, but "
                  "confirm current law hasn't extended the schedule further.")
MA_SURTAX_NOTE = ("Massachusetts is flat at 5% for income under $1M/yr, plus a 4% "
                   "'millionaire's surtax' on income above $1M (effective ~9% marginal above "
                   "that threshold) — not a pure flat tax at the very top.")

# All other states + DC use progressive brackets (derived by elimination, not individually
# rate-verified this session — see NOTE_UNVERIFIED_RATE).
PROGRESSIVE_INCOME_TAX_STATES = {
    "AL", "AR", "CA", "CT", "DE", "HI", "IA", "KS", "ME", "MD", "MN", "MO", "MT", "NE",
    "NJ", "NM", "NY", "ND", "OH", "OK", "OR", "RI", "SC", "VT", "VA", "WV", "WI", "DC",
}

# ---------------------------------------------------------------------------------------
# Community-property states (9) — the DEFAULT marital-property regime, meaning community
# property gets a FULL (double) basis step-up at the FIRST spouse's death (IRC §1014(b)(6)),
# vs. common-law/JTWROS states where only the decedent's HALF steps up. This was hardcoded
# as always-True in estate.py — wrong for any of the other 41 states + DC.
# A handful of common-law states (AK, FL, KY, SD, TN) additionally let couples OPT IN to
# community-property treatment via a community-property trust — that's an affirmative
# election, not the default, so they're tracked separately and NOT in the default set.
# ---------------------------------------------------------------------------------------
COMMUNITY_PROPERTY_STATES = {"AZ", "CA", "ID", "LA", "NV", "NM", "TX", "WA", "WI"}
OPT_IN_COMMUNITY_PROPERTY_STATES = {"AK", "FL", "KY", "SD", "TN"}

# ---------------------------------------------------------------------------------------
# Transfer-on-Death (TOD) / beneficiary deed states — lets real property pass outside
# probate via a simple recorded deed, exactly like a TOD brokerage registration. Per Nolo
# (updated 2025-06-23) plus Delaware's 2025 statute (not yet reflected on that page).
# Maryland's TOD-deed law is enacted but not yet effective (2026-10-01) — deliberately
# EXCLUDED from the "available now" set; see MD_PENDING_NOTE.
# ---------------------------------------------------------------------------------------
TOD_DEED_STATES = {
    "AK", "AZ", "AR", "CA", "CO", "DC", "DE", "GA", "HI", "IL", "IN", "KS", "ME", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NM", "NY", "ND", "OH", "OK", "OR", "SD", "TX",
    "UT", "VA", "WA", "WV", "WI", "WY",
}
MD_PENDING_NOTE = ("Maryland has enacted a TOD-deed statute but it is not effective until "
                    "2026-10-01 — treated as unavailable until that date.")
# "Lady Bird" / enhanced life estate deeds: a functionally similar probate-avoidance tool
# (transfers while the owner is alive but the owner keeps full control, including the right
# to sell or revoke) available in a few states that lack a standard TOD deed, or as an
# alternative in states that have both.
LADY_BIRD_DEED_STATES = {"FL", "MI", "TX", "VT", "WV"}

# ---------------------------------------------------------------------------------------
# State estate tax (12 + DC) and state inheritance tax (6) — Texas has NEITHER (hardcoded
# as STATE_ESTATE_TAX = 0.0, correct for TX but not for these 18 jurisdictions). Maryland is
# the only state with BOTH. Iowa's inheritance tax is being phased to zero (effectively $0
# for deaths in 2025-2026 per its repeal schedule) but is left in the set with a note rather
# than silently dropped, since the phase-out's exact completion date is worth re-confirming.
# None of TX/CO/NC/GA (this session's states of interest) have either tax.
# ---------------------------------------------------------------------------------------
STATE_ESTATE_TAX_STATES = {"CT", "HI", "IL", "ME", "MD", "MA", "MN", "NY", "OR", "RI", "VT", "WA", "DC"}
STATE_INHERITANCE_TAX_STATES = {"IA", "KY", "MD", "NE", "NJ", "PA"}
IA_INHERITANCE_PHASEOUT_NOTE = ("Iowa's inheritance tax is being phased out and should be "
                                 "effectively $0 for most beneficiaries by 2025-2026 — confirm "
                                 "current status rather than assuming the older, higher rates.")

# ---------------------------------------------------------------------------------------
# ACA Medicaid expansion status — the SINGLE most consequential fact for the ACA/subsidy-
# cliff module, since it flips which income band is a "coverage gap" (no help at all,
# hardcoded as TX's reality in aca.py) vs. an income band where the state's OWN Medicaid
# program is more generous than any marketplace plan (expansion states, up to 138% FPL).
# 10 holdout states as of 2026-03 per KFF-sourced reporting. Georgia and Wisconsin run
# PARTIAL/alternative coverage programs (GA's "Pathways to Coverage" work-requirement
# program; WI covers close to 100% FPL through a non-ACA mechanism) but are still classified
# as non-expansion for ACA subsidy-cliff purposes — worth flagging explicitly since Georgia
# is one of this session's named states of interest and its partial program meaningfully
# changes the picture from a pure holdout like Texas. North Carolina expanded in December
# 2023 and is EXPANSION, not a holdout — a material difference from Texas worth calling out
# since NC is also a named state of interest here.
# ---------------------------------------------------------------------------------------
MEDICAID_NONEXPANSION_STATES = {"AL", "FL", "GA", "KS", "MS", "SC", "TN", "TX", "WI", "WY"}
GA_PARTIAL_NOTE = ("Georgia runs 'Pathways to Coverage,' a partial Medicaid program with work "
                    "requirements and low enrollment — NOT full ACA expansion. Still classified "
                    "non-expansion for subsidy-cliff purposes: below 100% FPL is still largely a "
                    "coverage gap in practice, though Pathways may cover a slice of that population "
                    "who meet its work requirement. Confirm current Pathways status/enrollment.")
WI_PARTIAL_NOTE = ("Wisconsin covers adults up to ~100% FPL through a non-ACA state mechanism "
                    "(not federal expansion) — so unlike Texas/Georgia, WI residents below 100% "
                    "FPL are NOT in a true coverage gap, but WI is still classified non-expansion "
                    "since it never adopted the ACA's 138%-FPL expansion.")
NC_EXPANDED_NOTE = ("North Carolina expanded Medicaid in December 2023 — there is NO coverage "
                     "gap below 100% FPL; Medicaid covers up to ~138% FPL, which is typically "
                     "MORE generous than a marketplace plan in that income range.")

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota",
    "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
    "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}


def norm_state(code):
    """Normalize a state input to its 2-letter code; defaults to TX (this app's original
    home state) if unset/unrecognized, so every caller has a safe fallback."""
    c = (code or "").strip().upper()
    return c if c in STATE_NAMES else "TX"


def facts(code):
    """The full state-facts bundle for one state, used by estate.py/aca.py/tax.py/cashdebt.py
    instead of each hardcoding Texas. Every boolean below defaults to the TX-shaped answer
    only because TX happens to match those defaults for several axes (no income tax, no
    estate/inheritance tax, community property, has TOD deeds, non-expansion) — every field
    is looked up per-state, not actually hardcoded."""
    st = norm_state(code)
    name = STATE_NAMES[st]
    no_tax = st in NO_INCOME_TAX_STATES
    flat_rate = FLAT_INCOME_TAX_STATES.get(st)
    income_tax_type = "none" if no_tax else ("flat" if flat_rate is not None else "progressive")
    income_tax_rate_pct = 0.0 if no_tax else flat_rate  # None for progressive/unverified-flat
    community_property = st in COMMUNITY_PROPERTY_STATES
    opt_in_cp = st in OPT_IN_COMMUNITY_PROPERTY_STATES
    tod_deed = st in TOD_DEED_STATES
    lady_bird = st in LADY_BIRD_DEED_STATES
    estate_tax = st in STATE_ESTATE_TAX_STATES
    inheritance_tax = st in STATE_INHERITANCE_TAX_STATES
    medicaid_expanded = st not in MEDICAID_NONEXPANSION_STATES

    notes = []
    if st == "GA":
        notes.append(GA_PHASE_NOTE)
        notes.append(GA_PARTIAL_NOTE)
    if st == "NC":
        notes.append(NC_PHASE_NOTE)
        notes.append(NC_EXPANDED_NOTE)
    if st == "WA":
        notes.append(WA_CAPITAL_GAINS_EXCISE_NOTE)
    if st == "WI":
        notes.append(WI_PARTIAL_NOTE)
    if st == "MA":
        notes.append(MA_SURTAX_NOTE)
    if st == "IA" and inheritance_tax:
        notes.append(IA_INHERITANCE_PHASEOUT_NOTE)
    if st == "MD":
        notes.append(MD_PENDING_NOTE)
    if income_tax_type == "progressive" or (income_tax_type == "flat" and st not in ("TX", "CO", "NC", "GA")):
        notes.append(NOTE_UNVERIFIED_RATE)

    return {
        "code": st, "name": name,
        "no_income_tax": no_tax,
        "income_tax_type": income_tax_type,
        "income_tax_rate_pct": income_tax_rate_pct,
        "community_property": community_property,
        "opt_in_community_property": opt_in_cp,
        "tod_deed_available": tod_deed,
        "lady_bird_deed_available": lady_bird,
        "state_estate_tax": estate_tax,
        "state_inheritance_tax": inheritance_tax,
        "medicaid_expanded": medicaid_expanded,
        "notes": notes,
    }


# Sources (verified 2026-07-18 via live web search, not training-data recall):
#  - No-income-tax / flat-tax states + 2026 rates: Tax Foundation, "2026 State Income Tax
#    Rates and Brackets"; CBS News, "9 states are cutting individual income taxes in 2026"
#    (CO/GA/NC-specific figures and phase-down schedules).
#  - Community-property states: longstanding, stable list (unchanged for decades) —
#    structural family/marital-property law, not annually legislated.
#  - TOD/beneficiary deeds: Nolo, "States That Allow Transfer-On-Death Deeds for Real
#    Estate" (updated 2025-06-23); cross-checked against Trust & Will's beneficiary-deed
#    state list for Delaware/Maryland's 2025-2026 additions.
#  - State estate/inheritance tax: Kiplinger / Tax Foundation-sourced 2026 aggregation
#    ("16 States With Estate Taxes or Inheritance Taxes").
#  - Medicaid expansion status: KFF, "Status of State Medicaid Expansion Decisions"
#    (current as of 2026-03); Georgia Pathways and Wisconsin's alternative program status
#    noted as of the same reporting.
# Re-verify before relying on any of this for a real decision — legislatures change all of
# it, and this snapshot is only as current as 2026-07-18.
