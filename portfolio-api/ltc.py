# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
ltc.py -- long-term care (LTC) planning (2026-11-10, Jason). Pure functions plus a dated constant in tax_tables/<year>.json.

Why: Medicare does not pay for custodial care (help with bathing, dressing, eating, supervision), and Medicaid pays only after assets are spent
down (about $2,000 countable for a single person in most states, with a 5-year look-back on gifts). So most households either insure, set money aside,
or plan to pay out of pocket. Fidelity's retiree health-care estimate EXCLUDES it, which is why it is modeled separately here.

What it does (planning, not prediction):
  * costs: Genworth/CareScout national median annual cost by type of care (home health aide, assisted living, nursing home), in today's dollars,
    with an optional real cost growth to the year care starts;
  * scenarios: a typical 3-year path (home care, then assisted living, then nursing home), single-type alternatives, and a 5-year nursing-home stress case;
  * insurance: an optional policy (daily benefit x benefit years) reduces the uncovered cost year by year;
  * probability: about 48% of people who reach 65 receive some PAID long-term care (HHS ASPE), 70% develop severe needs; the expected cost is that
    probability times the uncovered scenario cost, spread over the retirement years like the healthcare line;
  * funding view: the uncovered cost against investable assets and the HSA surplus above the healthcare need.
Averages hide wide variation (dementia, state, family history, care at home by relatives). Informational, not advice.
"""
import datetime

import taxtables

KEY = "ltc.cost_of_care"
SPREAD_YEARS = 25            # 65 -> 90, the same horizon as the healthcare estimate
DEFAULT_START_AGE = 82
DEFAULT_REAL_GROWTH_PCT = 1.0     # an ASSUMPTION: care prices have risen faster than general inflation; the engines work in today's dollars

SCENARIOS = {
    "typical_path": {"label": "Typical path: 1 year home care, 1 year assisted living, 1 year nursing home",
                     "path": [("home_health_aide", 1), ("assisted_living", 1), ("nursing_home_semi_private", 1)]},
    "home_care": {"label": "3 years of home care (home health aide, 44 hours a week)", "path": [("home_health_aide", 3)]},
    "assisted_living": {"label": "3 years in assisted living", "path": [("assisted_living", 3)]},
    "nursing_home": {"label": "3 years in a nursing home (semi-private room)", "path": [("nursing_home_semi_private", 3)]},
    "stress": {"label": "Stress case: 5 years in a private nursing-home room (long dementia)", "path": [("nursing_home_private", 5)]},
}
SERVICE_LABEL = {"home_health_aide": "home health aide", "assisted_living": "assisted living",
                 "nursing_home_semi_private": "nursing home (semi-private)", "nursing_home_private": "nursing home (private)"}


def table(today=None):
    """-> {values, year, verified, source, source_url, stale} (values empty when the table is missing)."""
    try:
        r = taxtables.resolve(KEY, today or datetime.date.today())
    except Exception:
        return {"values": {}, "year": None, "verified": None, "source": None, "source_url": None, "stale": True}
    return {"values": dict(r.get("values") or {}), "year": r.get("year"), "verified": r.get("verified"), "source": r.get("source"),
            "source_url": r.get("source_url"), "stale": bool(r.get("stale"))}


def _growth(years_ahead, growth_pct):
    return (1.0 + (growth_pct or 0.0) / 100.0) ** max(0.0, years_ahead or 0.0)


def compute(age, people=1, scenario="typical_path", start_age=None, insurance_daily=None, insurance_years=3.0, growth_pct=None,
            investable=None, hsa_surplus=0.0, today=None):
    """Plan for ONE person's care (a couple has two people: each has their own risk). `age` None = unknown (no cost growth applied).
    All dollars are today's dollars unless stated. Returns a dict; `ok` False with a reason when no cost table exists."""
    t = table(today)
    v = t["values"]
    if not v:
        return {"ok": False, "reason": "no long-term care cost table available", "table": t}
    sc = SCENARIOS.get(scenario) or SCENARIOS["typical_path"]
    start = int(start_age or DEFAULT_START_AGE)
    gp = DEFAULT_REAL_GROWTH_PCT if growth_pct is None else float(growth_pct)
    lead = max(0, start - age) if age is not None else 0
    years, yr_no = [], 0
    for service, n in sc["path"]:
        for _ in range(int(n)):
            base = float(v.get(service) or 0.0)
            years.append({"year": yr_no + 1, "age": start + yr_no, "service": SERVICE_LABEL.get(service, service), "base": round(base, 2),
                          "cost": round(base * _growth(lead + yr_no, gp), 2)})
            yr_no += 1
    total = round(sum(y["cost"] for y in years), 2)
    daily = float(insurance_daily or 0.0)
    ins_years = float(insurance_years if insurance_years is not None else 3.0)
    paid_by_policy = 0.0
    for i, y in enumerate(years):
        covered_fraction = max(0.0, min(1.0, ins_years - i))      # a 2.5-year policy covers half of year three
        pay = min(y["cost"], daily * 365.0) * covered_fraction if daily > 0 else 0.0
        y["insurance"] = round(pay, 2)
        y["uncovered"] = round(y["cost"] - pay, 2)
        paid_by_policy += pay
    paid_by_policy = round(paid_by_policy, 2)
    uncovered = round(total - paid_by_policy, 2)
    p_paid = float(v.get("p_paid_care") or 0.0)
    people = max(1, int(people or 1))
    expected_total = round(p_paid * uncovered * people, 2)
    out = {"ok": True, "scenario": scenario if scenario in SCENARIOS else "typical_path", "label": sc["label"], "start_age": start,
           "years": years, "total_cost": total, "insurance_pays": paid_by_policy, "uncovered": uncovered,
           "real_growth_pct": gp, "lead_years": lead, "people": people,
           "expected": {"p_paid_care": p_paid, "p_severe_needs": v.get("p_severe_needs"), "avg_years_any_care": v.get("avg_years_any_care"),
                        "household_expected_uncovered": expected_total, "annual": round(expected_total / SPREAD_YEARS, 2), "spread_years": SPREAD_YEARS},
           "funding": {"uncovered_pct_of_investable": (round(uncovered / investable * 100, 1) if investable else None),
                       "hsa_surplus": round(hsa_surplus or 0.0, 2),
                       "uncovered_after_hsa_surplus": round(max(0.0, uncovered - (hsa_surplus or 0.0)), 2)},
           "both_need_care_note": ("If both spouses needed this care, the cost would be about twice as large." if people > 1 else None),
           "table": {k: t[k] for k in ("year", "verified", "source", "source_url", "stale")},
           "medicare_note": "Medicare does not pay for custodial care; it covers only short skilled-nursing stays after a hospital stay. Medicaid pays only after "
                            "assets are spent down (about $2,000 countable for a single person in most states) and reviews gifts made in the previous 5 years.",
           "hsa_note": "An HSA can pay premiums for a qualified long-term care insurance policy up to an age-based yearly limit (IRS Pub. 969); qualified "
                       "care services may also count as medical expenses when a licensed practitioner certifies chronic illness. Confirm with a tax professional.",
           "note": "National median prices (Genworth/CareScout 2024 survey). Actual costs vary widely by state and by person, and the average hides long stays. "
                   "Informational, not advice."}
    return out
