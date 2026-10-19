# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
benefits.py — GAP-5 (final review): employer benefits & contribution-room optimizer.

Contribution room is the cheapest guaranteed return the household has (match = instant
100%/50%; tax-advantaged space compounds untaxed). This module turns plan facts into a
ranked "next marginal dollar" sheet: match capture → HSA → employee 401(k) headroom →
Roth IRA (direct if MAGI allows) → after-tax 401(k) / mega-backdoor capacity (415(c)).

2026 constants VERIFIED 2026-07-17 against IRS Notice 2025-67 / irs.gov newsroom (see
LIMITS_2026 sources; registered in the G13 grounding watchdog). Educational, not advice.
"""
import datetime

LIMITS_YEAR = 2026
LIMITS_2026 = {
    "k401_employee": 24500.0,        # §402(g) elective deferral
    "k401_catchup_50": 8000.0,       # age 50+ (must be ROTH if prior-yr wages > $150k — SECURE 2.0)
    "k401_catchup_60_63": 11250.0,   # super catch-up, ages 60-63
    "catchup_roth_wage_floor": 150000.0,
    "sec415c_total": 72000.0,        # employee + employer + after-tax (catch-ups excluded)
    "hsa_self": 4400.0, "hsa_family": 8750.0, "hsa_catchup_55": 1000.0,
    "ira_limit": 7500.0, "ira_catchup": 1100.0,
    "roth_ira_mfj_phaseout": (242000.0, 252000.0),
}
SOURCE_URL = "https://www.irs.gov/newsroom/401k-limit-increases-to-24500-for-2026-ira-limit-increases-to-7500"

# Pay-frequency → periods/year, for the salary cross-check below. Found 2026-07-18 (Jason):
# this used to be hardcoded to biweekly (x26) with no way to say otherwise — silently wrong
# for weekly (52), semi-monthly/1st-15th (24), or monthly (12) payrolls. "custom" (e.g. gig
# work with irregular pay) supplies its own periods/year rather than picking from this table.
PAY_FREQUENCIES_PER_YEAR = {"weekly": 52, "biweekly": 26, "semimonthly": 24, "monthly": 12}


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def match_analysis(salary, pretax_pct, tier1_pct, tier1_rate, tier2_pct, tier2_rate):
    """Employer match earned at the current deferral vs the max available.
    Formula: tier1_rate% of the first tier1_pct% + tier2_rate% of the NEXT tier2_pct%."""
    s, ee = _f(salary), _f(pretax_pct)
    t1p, t1r, t2p, t2r = _f(tier1_pct), _f(tier1_rate), _f(tier2_pct), _f(tier2_rate)
    full_ee_pct = t1p + t2p                       # employee % needed for the full match
    max_match_pct = t1p * t1r / 100.0 + t2p * t2r / 100.0
    earned_pct = (min(ee, t1p) * t1r / 100.0
                  + max(0.0, min(ee - t1p, t2p)) * t2r / 100.0)
    return {"needed_for_full_pct": full_ee_pct,
            "max_match_pct": round(max_match_pct, 2),
            "earned_match_pct": round(earned_pct, 2),
            "earned_match_dollars": round(s * earned_pct / 100.0, 2),
            "full_capture": earned_pct >= max_match_pct - 1e-9,
            "left_on_table": round(s * (max_match_pct - earned_pct) / 100.0, 2)}


def pay_periods_per_year(pay_frequency, pay_periods_custom=None):
    """Resolve periods/year from a pay_frequency enum ('weekly'/'biweekly'/'semimonthly'/
    'monthly'/'custom') + an optional custom override. Unknown/blank frequency falls back to
    biweekly — the assumption this cross-check always silently made before, so anyone who
    already had a value saved keeps the same behavior until they pick a real frequency."""
    pf = (pay_frequency or "biweekly").lower()
    if pf == "custom":
        c = _f(pay_periods_custom)
        return c if c > 0 else 26.0
    return PAY_FREQUENCIES_PER_YEAR.get(pf, 26)


def _roth_pacing(limit, contributed, monthly, today=None):
    """2026-07-30 stretch goal (RESUME-TRACKER 2026-07-20 Roth item, part 3 -- "on pace to max by
    December" vs "behind pace"). Projects a single spouse's Roth IRA contributions against
    `limit` for the rest of the calendar year, given what's already been contributed YTD
    (`contributed`) and an optional recurring monthly amount (`monthly`).

    `months_left` counts the CURRENT month as still contributable (checking on the 3rd of the
    month doesn't mean that month's contribution already happened), so a check on Dec 1 still
    reports 1 month left, not 0 -- only Jan 1 of the FOLLOWING year would report 12 again.

    Returns a dict with `status` one of:
      "maxed"         — already at or over the limit; nothing left to project.
      "no_monthly_set" — remaining room exists but no recurring monthly figure was entered,
                         so there's nothing to project against (not itself a problem — mirrors
                         the HSA section's "not entered" info-only treatment, not an alert).
      "on_pace"       — at the entered monthly rate, projected to reach the limit by Dec 31.
      "behind_pace"   — at the entered monthly rate, projected to fall short.
    `required_monthly` is always populated (except when already maxed) so a "behind_pace" status
    can be paired with the concrete catch-up number, not just a yes/no verdict."""
    today = today or datetime.date.today()
    remaining = round(max(0.0, limit - contributed), 2)
    if remaining <= 0:
        return {"status": "maxed", "months_left": 0, "projected_total": round(contributed, 2),
               "required_monthly": 0.0}
    months_left = 13 - today.month   # inclusive of the current month (Jan=12 left, Dec=1 left)
    monthly = _f(monthly)
    projected_total = round(contributed + monthly * months_left, 2)
    required_monthly = round(remaining / months_left, 2) if months_left > 0 else remaining
    if not monthly:
        status = "no_monthly_set"
    elif projected_total >= limit - 0.01:
        status = "on_pace"
    else:
        status = "behind_pace"
    return {"status": status, "months_left": months_left, "projected_total": projected_total,
           "required_monthly": required_monthly}


def run(facts, limits=None, today=None):
    """facts: {salary, age, spouse_age, pretax_pct, aftertax_pct, match_t1_pct, match_t1_rate,
    match_t2_pct, match_t2_rate, plan_aftertax(bool), inservice_rollover('yes'/'no'/'unknown'),
    hsa_coverage('self'/'family'), hsa_contribution, hsa_invested(bool), magi, biweekly_total,
    pay_frequency('weekly'/'biweekly'/'semimonthly'/'monthly'/'custom'), pay_periods_custom,
    roth_contributed_self/_spouse (YTD $), roth_monthly_self/_spouse (optional recurring $/mo,
    2026-07-30 pacing stretch goal)}.
    `biweekly_total` is really "per-paycheck total (employee+match)" now — the key name is kept
    for backward compatibility with already-saved config, but it's multiplied by whatever
    pay_frequency resolves to, not hardcoded to 26 anymore. `today` is an optional override
    (defaults to the real date) purely so pacing math is exactly testable without monkeypatching
    the datetime module."""
    L = {**LIMITS_2026, **(limits or {})}
    f = facts or {}
    salary, age = _f(f.get("salary")), _f(f.get("age"))
    out = {"year": LIMITS_YEAR, "source": SOURCE_URL, "sections": {}, "actions": [], "rank": []}
    if salary <= 0:
        out["setup_needed"] = True
        out["note"] = "Enter salary (household register income) + plan facts to compute headroom."
        return out

    # ---- salary cross-check from the per-paycheck total (employee + match), if provided ----
    bw = _f(f.get("biweekly_total"))
    m = match_analysis(salary, f.get("pretax_pct"), f.get("match_t1_pct"), f.get("match_t1_rate"),
                       f.get("match_t2_pct"), f.get("match_t2_rate"))
    if bw:
        periods = pay_periods_per_year(f.get("pay_frequency"), f.get("pay_periods_custom"))
        combined_pct = _f(f.get("pretax_pct")) + m["earned_match_pct"]
        implied = round(bw * periods / combined_pct * 100.0, 0) if combined_pct else None
        pf_label = (f.get("pay_frequency") or "biweekly").lower()
        pf_label = pf_label if pf_label in PAY_FREQUENCIES_PER_YEAR or pf_label == "custom" else "biweekly"
        out["salary_crosscheck"] = {"per_paycheck_total": bw, "biweekly_total": bw,  # dup key: back-compat
                                    "pay_frequency": pf_label, "periods_per_year": periods,
                                    "combined_pct": combined_pct, "implied_salary": implied,
                                    "note": f"implied = per-paycheck total (employee+match) × "
                                            f"{periods:g} {pf_label} paychecks/yr ÷ combined %"}

    # ---- 401(k) employee deferral ----
    ee_dollars = salary * _f(f.get("pretax_pct")) / 100.0
    catchup = 0.0
    if 60 <= age <= 63:
        catchup = L["k401_catchup_60_63"]
    elif age >= 50:
        catchup = L["k401_catchup_50"]
    ee_limit = L["k401_employee"]
    k = {"deferral_dollars": round(ee_dollars, 2), "employee_limit": ee_limit,
         "headroom": round(max(0.0, ee_limit - ee_dollars), 2),
         "catchup_available": catchup,
         "catchup_roth_mandatory": bool(catchup and salary > L["catchup_roth_wage_floor"]),
         "match": m}
    out["sections"]["k401"] = k
    if not m["full_capture"]:
        out["actions"].append({"level": "action",
                               "msg": f"Match NOT fully captured — raising deferral to "
                                      f"{m['needed_for_full_pct']:.0f}% earns "
                                      f"${m['left_on_table']:,.0f}/yr of free money."})
    if k["headroom"] > 0:
        out["rank"].append({"avenue": "401(k) pre-tax/Roth headroom",
                            "dollars": k["headroom"],
                            "why": "tax-advantaged compounding; Roth-vs-pretax by current bracket"})

    # ---- HSA ----
    cov = (f.get("hsa_coverage") or "family").lower()
    hsa_limit = L["hsa_family"] if cov.startswith("f") else L["hsa_self"]
    if age >= 55:
        hsa_limit += L["hsa_catchup_55"]
    hsa_c = _f(f.get("hsa_contribution"))
    hsa = {"coverage": cov, "limit": hsa_limit, "contribution": hsa_c,
           "headroom": round(max(0.0, hsa_limit - hsa_c), 2) if hsa_c else None,
           "invested": bool(f.get("hsa_invested")),
           "note": ("Triple-advantaged (deductible in, growth untaxed, medical out tax-free) — "
                    "the single best account per dollar; invest-don't-spend and keep receipts "
                    "(the 'shoebox' lets it reimburse decades later).")}
    out["sections"]["hsa"] = hsa
    if hsa_c and hsa["headroom"] > 0:
        out["rank"].insert(0, {"avenue": "HSA headroom", "dollars": hsa["headroom"],
                               "why": "triple tax advantage beats every other wrapper"})
    elif not hsa_c:
        out["actions"].append({"level": "info",
                               "msg": f"Enter the annual HSA contribution to size headroom "
                                      f"(2026 {cov} limit ${hsa_limit:,.0f})."})

    # ---- Roth IRA (direct vs backdoor) ----
    # RESUME-TRACKER 2026-07-20: previously always ranked the full per_spouse_limit x2 as
    # available headroom with no input for what's already been contributed this year — it
    # couldn't tell "$0 contributed" from "already maxed both IRAs in January". contributed_self/
    # _spouse are YTD dollar figures from the household register (mirrors how hsa_contribution
    # already works); remaining_self/_spouse subtract them before ranking so the "avenue" dollar
    # figure reflects what's actually still open, not the gross annual limit.
    magi = _f(f.get("magi")) or salary
    lo, hi = L["roth_ira_mfj_phaseout"]
    per_spouse = L["ira_limit"] + (L["ira_catchup"] if age >= 50 else 0.0)
    contributed_self = _f(f.get("roth_contributed_self"))
    contributed_spouse = _f(f.get("roth_contributed_spouse"))
    remaining_self = round(max(0.0, per_spouse - contributed_self), 2)
    remaining_spouse = round(max(0.0, per_spouse - contributed_spouse), 2)
    remaining_total = round(remaining_self + remaining_spouse, 2)
    contributed_note = ""
    if contributed_self or contributed_spouse:
        contributed_note = (f" (${contributed_self + contributed_spouse:,.0f} already contributed "
                            f"YTD across both — ${remaining_total:,.0f} of this year's room remains.)")
    if magi < lo:
        ira_status, ira_msg = "direct", (f"MAGI ~${magi:,.0f} is below the ${lo:,.0f} MFJ "
                                         f"phase-out — BOTH spouses can contribute "
                                         f"${per_spouse:,.0f} directly to Roth IRAs; no backdoor "
                                         "gymnastics needed." + contributed_note)
        if remaining_total > 0:
            out["rank"].append({"avenue": "Roth IRA ×2 (direct)", "dollars": remaining_total,
                                "why": "tax-free growth, no RMD, heirs inherit income-tax-free"})
    elif magi < hi:
        ira_status, ira_msg = "partial", (f"MAGI in the ${lo:,.0f}–${hi:,.0f} phase-out — partial "
                                          "direct, or backdoor." + contributed_note)
    else:
        ira_status, ira_msg = "backdoor", ("Over the phase-out — the backdoor route (watch "
                                           "pro-rata if any pre-tax IRA exists)." + contributed_note)
    monthly_self = _f(f.get("roth_monthly_self"))
    monthly_spouse = _f(f.get("roth_monthly_spouse"))
    pacing_self = _roth_pacing(per_spouse, contributed_self, monthly_self, today)
    pacing_spouse = _roth_pacing(per_spouse, contributed_spouse, monthly_spouse, today)
    out["sections"]["roth_ira"] = {"status": ira_status, "per_spouse_limit": per_spouse,
                                   "phaseout_mfj": [lo, hi], "msg": ira_msg,
                                   "contributed_self": contributed_self, "contributed_spouse": contributed_spouse,
                                   "remaining_self": remaining_self, "remaining_spouse": remaining_spouse,
                                   "remaining_total": remaining_total,
                                   "monthly_self": monthly_self, "monthly_spouse": monthly_spouse,
                                   "pacing_self": pacing_self, "pacing_spouse": pacing_spouse}
    for who, pacing in (("self", pacing_self), ("spouse", pacing_spouse)):
        if pacing["status"] == "behind_pace":
            out["actions"].append({"level": "action",
                                   "msg": f"Roth IRA ({who}) is BEHIND PACE to max out this year — "
                                          f"at ${(monthly_self if who=='self' else monthly_spouse):,.0f}/mo "
                                          f"you're projected to reach ${pacing['projected_total']:,.0f} of "
                                          f"the ${per_spouse:,.0f} limit. Raise it to "
                                          f"${pacing['required_monthly']:,.0f}/mo for the rest of the year "
                                          f"({pacing['months_left']} mo left) to max out."})

    # ---- Mega-backdoor capacity (§415(c)) ----
    total_415 = ee_dollars + m["earned_match_dollars"] + salary * _f(f.get("aftertax_pct")) / 100.0
    capacity = max(0.0, L["sec415c_total"] - total_415)   # catch-ups excluded from 415(c)
    isr = (f.get("inservice_rollover") or "unknown").lower()
    mb = {"sec415c_limit": L["sec415c_total"], "used": round(total_415, 2),
          "aftertax_capacity": round(capacity, 2),
          "plan_offers_aftertax": bool(f.get("plan_aftertax")),
          "inservice_rollover": isr,
          "complete": bool(f.get("plan_aftertax")) and isr == "yes"}
    out["sections"]["mega_backdoor"] = mb
    if mb["plan_offers_aftertax"]:
        if isr == "unknown":
            out["actions"].append({"level": "action",
                                   "msg": "Plan OFFERS after-tax contributions — one question to "
                                          "Principal completes the picture: 'does the plan allow "
                                          "in-service distributions/conversions of the after-tax "
                                          "source?' Yes → a mega-backdoor Roth lane of "
                                          f"~${capacity:,.0f}/yr exists."})
        elif isr == "yes":
            out["rank"].append({"avenue": "Mega-backdoor Roth (after-tax 401k → Roth)",
                                "dollars": capacity,
                                "why": "largest Roth lane available; convert promptly to keep "
                                       "earnings from accruing pre-tax"})
    out["note"] = ("Ranked room = the guaranteed-return ladder for the next saved dollar: match "
                   "first (instant 50-100%), HSA (triple), then wrapper headroom. Roth-vs-pretax "
                   "on the deferral itself is a bracket call — see PLAN → ROTH's marginal curve. "
                   "Educational, not advice; plan documents govern.")
    return out
