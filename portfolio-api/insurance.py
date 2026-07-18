"""
insurance.py — P0-3: the protection desk (insurance & risk-transfer gap analysis).

The final review's biggest professional-parity hole: a mid-six-figure FI plan is ended
faster by an uninsured death, disability, or liability suit than by any market drawdown.
This module quantifies NEED vs IN-FORCE vs GAP per line of coverage:

  · LIFE — capital-needs analysis (primary; the industry-standard method), with DIME and
    human-life-value cross-checks so the number can be argued with.
  · DISABILITY — income-replacement gap to retirement age (default 60% of gross).
  · UMBRELLA — liability limit sized to net worth (rounded up to the next $1M).
  · HOME/AUTO (P&C) — adequacy checklist (dwelling vs replacement cost, liability limits,
    TX flood/wind notes). Data is declarations-page manual entry.
  · LTC — self-insure vs transfer stance by net-worth band and age (education tier).

Pure logic: app.py injects the household snapshot (net worth, liabilities, ages, incomes,
expenses) and the user's policy register; everything unit-tests offline. Real-dollar PVs
discounted at the supplied real rate (TIPS yield, same convention as the funded ratio).
Decision-support only — quotes and suitability come from licensed agents; this tool never
sells or recommends a specific product.
"""

import states

DEFAULT_KNOBS = {
    "disability_replace_pct": 60.0,   # % of gross income DI should replace
    "self_consumption_pct": 30.0,     # HLV: share of own income the earner consumes
    "final_expenses": 15000.0,        # funeral/settlement costs in the life need
    "education_per_child": 120000.0,  # per-child education funding in the life need
    "umbrella_min_nw": 500000.0,      # NW above this → umbrella recommended
    "umbrella_round_to": 1000000.0,   # umbrella limits sell in $1M increments
    "ltc_annual_cost": 75000.0,       # planning cost of a care year (TX-ish; verify locally)
    "ltc_plan_years": 3.0,            # planning-length LTC event
    "term_expiry_warn_years": 2,      # warn when a term policy expires within N years
}

LIFE_KINDS = ("life",)
DI_KINDS = ("disability", "di", "ltd")
UMB_KINDS = ("umbrella",)
HOME_KINDS = ("home", "homeowners", "dwelling")
AUTO_KINDS = ("auto",)
LTC_KINDS = ("ltc", "long-term care", "longtermcare")


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def pv_annuity(annual, years, real_rate_pct):
    """PV of a level REAL annual payment for `years` at a real discount rate."""
    a, n, r = _f(annual), max(0.0, _f(years)), _f(real_rate_pct) / 100.0
    if n <= 0 or a <= 0:
        return 0.0
    if abs(r) < 1e-9:
        return a * n
    return a * (1 - (1 + r) ** (-n)) / r


def _in_force(policies, kinds, who=None):
    """Sum of coverage for the given kinds (and owner, if given). Joint counts for both."""
    tot = 0.0
    for p in policies or []:
        if (p.get("kind") or "").strip().lower() not in kinds:
            continue
        owner = (p.get("owner") or "self").strip().lower()
        if who and owner not in (who, "joint"):
            continue
        tot += _f(p.get("coverage"))
    return round(tot, 2)


def life_needs(income, years_to_retire, debts, mortgage, children, liquid_assets,
               real_rate_pct, knobs, survivor_expense_share=None):
    """Capital-needs analysis (primary) + DIME + human-life-value cross-checks.
    capital_needs: PV of replacing the earner's income to retirement + debt payoff +
    final expenses + education, minus liquid assets already available to the survivor."""
    k = knobs
    inc, yrs = _f(income), max(0.0, _f(years_to_retire))
    edu = _f(children) * k["education_per_child"]
    replace = pv_annuity(inc, yrs, real_rate_pct)
    capital = replace + _f(debts) + k["final_expenses"] + edu - max(0.0, _f(liquid_assets))
    capital = max(0.0, round(capital, 2))
    dime = round(_f(debts) - _f(mortgage) + inc * min(yrs, 10) + _f(mortgage) + edu, 2)  # D+I(≤10y)+M+E
    hlv = round(pv_annuity(inc * (1 - k["self_consumption_pct"] / 100.0), yrs, real_rate_pct), 2)
    return {"capital_needs": capital, "dime": max(0.0, dime), "human_life_value": hlv,
            "inputs": {"income": inc, "years_to_retire": yrs, "debts": _f(debts),
                       "education": edu, "liquid_assets_offset": _f(liquid_assets)},
            "method_note": ("capital-needs = PV(income to retirement, real) + debts + final "
                            "expenses + education − liquid assets. DIME and human-life-value "
                            "shown as cross-checks; if the three disagree wildly, the inputs "
                            "(not the math) are the thing to revisit.")}


def umbrella_need(net_worth, knobs):
    nw, step = max(0.0, _f(net_worth)), knobs["umbrella_round_to"]
    if nw < knobs["umbrella_min_nw"]:
        return 0.0
    import math
    return round(math.ceil(nw / step) * step, 2)


def ltc_stance(net_worth, oldest_age, knobs):
    nw, age = _f(net_worth), _f(oldest_age)
    cost = knobs["ltc_annual_cost"] * knobs["ltc_plan_years"]
    if nw < 300000:
        band, verdict = "low", ("Asset base is in the range where an extended care event exhausts it "
                                "regardless — Medicaid planning (elder-law attorney) matters more than "
                                "LTC premiums.")
    elif nw < 2500000:
        band, verdict = "exposed", (f"The danger band: a {knobs['ltc_plan_years']:.0f}-year care event "
                                    f"(~${cost:,.0f} at ~${knobs['ltc_annual_cost']:,.0f}/yr) would consume "
                                    "a material share of the estate and impair the surviving spouse. "
                                    "Price hybrid life/LTC and TX Partnership policies for comparison — "
                                    "premiums are cheapest and underwriting easiest in the early/mid 50s.")
    else:
        band, verdict = "self-insure", ("Asset base can typically absorb a multi-year care event; "
                                        "self-insuring with an earmarked reserve is the common choice — "
                                        "make it explicit in the plan rather than implicit.")
    timing = None
    if age and age < 50:
        timing = "No urgency at the current age — put a reminder at 50–55 when pricing is best."
    elif age and age <= 60:
        timing = "This is the prime shopping window (best pricing/underwriting) if transfer is chosen."
    return {"band": band, "planning_event_cost": round(cost, 2), "verdict": verdict, "timing": timing}


def run(household, policies, knobs=None):
    """household: {net_worth, liquid_assets, debts_total, mortgage_balance, income_self,
    income_spouse, age_self, age_spouse, retire_age, children, home_value, real_rate_pct}."""
    k = {**DEFAULT_KNOBS, **(knobs or {})}
    h = household or {}
    sf = states.facts(h.get("state"))
    rr = _f(h.get("real_rate_pct"), 2.0)
    retire = _f(h.get("retire_age"), 60)
    gap_rows, alerts = [], []

    def row(line, need, in_force, note=""):
        need_r, have = round(_f(need), 2), round(_f(in_force), 2)
        gap = round(max(0.0, need_r - have), 2)
        status = "ok" if (need_r <= 0 or have >= need_r) else "gap"
        gap_rows.append({"line": line, "need": need_r, "in_force": have, "gap": gap,
                         "status": status, "note": note})
        return gap

    # ---- life, per earner ----
    life = {}
    for who, inc_key, age_key in (("self", "income_self", "age_self"),
                                  ("spouse", "income_spouse", "age_spouse")):
        inc = _f(h.get(inc_key))
        if inc <= 0:
            continue
        yrs = max(0.0, retire - _f(h.get(age_key), retire))
        need = life_needs(inc, yrs, h.get("debts_total"), h.get("mortgage_balance"),
                          h.get("children"), h.get("liquid_assets"), rr, k)
        have = _in_force(policies, LIFE_KINDS, who)
        need["in_force"] = have
        need["gap"] = round(max(0.0, need["capital_needs"] - have), 2)
        life[who] = need
        g = row(f"Life ({who})", need["capital_needs"], have,
                "capital-needs method; DIME/HLV cross-checks in detail")
        if g > 0:
            alerts.append({"level": "gap", "line": f"life-{who}",
                           "msg": f"Life coverage gap of ${g:,.0f} on {who} — level term to "
                                  f"~age {retire:.0f} is the standard instrument to quote."})

    # ---- disability, per earner ----
    disability = {}
    for who, inc_key, age_key in (("self", "income_self", "age_self"),
                                  ("spouse", "income_spouse", "age_spouse")):
        inc = _f(h.get(inc_key))
        if inc <= 0:
            continue
        need_mo = round(inc / 12.0 * k["disability_replace_pct"] / 100.0, 2)
        have_mo = _in_force(policies, DI_KINDS, who)     # DI coverage entered as $/month benefit
        gap_mo = round(max(0.0, need_mo - have_mo), 2)
        disability[who] = {"needed_monthly": need_mo, "in_force_monthly": have_mo,
                           "gap_monthly": gap_mo,
                           "note": ("Group LTD paid with pre-tax employer dollars is TAXABLE when "
                                    "claimed — a '60%' group benefit nets well under 60%.")}
        row(f"Disability ({who}) $/mo", need_mo, have_mo, f"{k['disability_replace_pct']:.0f}% of gross")
        if gap_mo > 0:
            alerts.append({"level": "gap", "line": f"di-{who}",
                           "msg": f"Disability gap of ${gap_mo:,.0f}/mo on {who} — statistically the "
                                  "most likely working-years claim, and the most-skipped coverage."})

    # ---- umbrella ----
    umb_need = umbrella_need(h.get("net_worth"), k)
    umb_have = _in_force(policies, UMB_KINDS)
    row("Umbrella liability", umb_need, umb_have, "next $1M above net worth")
    if umb_need > 0 and umb_have < umb_need:
        alerts.append({"level": "gap", "line": "umbrella",
                       "msg": f"Umbrella below the ~${umb_need/1e6:.0f}M sized to net worth — typically "
                              "$200–400/yr per $1M; requires underlying auto/home liability minimums."})

    # ---- P&C checklist (manual declarations data) ----
    pnc = []
    home_val = _f(h.get("home_value"))
    dwell = _in_force(policies, HOME_KINDS)
    if home_val > 0:
        if dwell <= 0:
            pnc.append({"item": "Dwelling coverage", "status": "unknown",
                        "msg": "Enter the declarations-page dwelling limit — it should track "
                               "REPLACEMENT COST (rebuild), not market value; confirm replacement-cost "
                               "endorsement and current rebuild $/sqft."})
        elif dwell < 0.8 * home_val:
            pnc.append({"item": "Dwelling coverage", "status": "review",
                        "msg": f"Dwelling limit ${dwell:,.0f} is under 80% of the home's ${home_val:,.0f} "
                               "value — below insurers' coinsurance threshold; claims can be prorated."})
        else:
            pnc.append({"item": "Dwelling coverage", "status": "ok", "msg": "Tracks home value."})
    auto = _in_force(policies, AUTO_KINDS)
    pnc.append({"item": "Auto liability limits", "status": "ok" if auto >= 500000 else "review",
                "msg": ("Per-occurrence liability entered: $%s. " % f"{auto:,.0f}" if auto else
                        "Enter per-occurrence liability. ") +
                       "With a positive net worth, 250/500 (or a 500k CSL) is the working floor — "
                       "and the umbrella requires it."})
    pnc.append({"item": f"Flood ({sf['code']})", "status": "review",
                "msg": (f"Homeowners policies EXCLUDE flood everywhere, not just {sf['name']} — "
                        "check your FEMA flood zone regardless of state; NFIP or private flood is "
                        "a separate policy. Regional perils (flood, wind/hail, wildfire, earthquake) "
                        "and their typical deductible structure (often %-of-dwelling for wind/hail in "
                        "coastal/high-wind states) vary by state — confirm what's relevant for "
                        f"{sf['name']} specifically with your agent, since this isn't modeled by state here.")})

    # ---- LTC ----
    oldest = max(_f(h.get("age_self")), _f(h.get("age_spouse")))
    ltc = ltc_stance(h.get("net_worth"), oldest, k)
    ltc["in_force"] = _in_force(policies, LTC_KINDS)

    # ---- register hygiene ----
    import datetime
    yr_now = datetime.date.today().year
    premiums = 0.0
    for p in policies or []:
        premiums += _f(p.get("premium_annual"))
        exp = p.get("term_expires")
        if exp and _f(exp) and yr_now <= _f(exp) <= yr_now + k["term_expiry_warn_years"]:
            alerts.append({"level": "review", "line": p.get("kind"),
                           "msg": f"{p.get('kind','policy').title()} policy "
                                  f"'{p.get('name') or p.get('owner','')}' term ends {int(_f(exp))} — "
                                  "re-quote early; pricing rises with age and any new conditions."})
    if not policies:
        alerts.append({"level": "info", "line": "register",
                       "msg": "No policies entered yet — add each in-force policy from its "
                              "declarations page to turn the NEED column into a real GAP analysis."})

    return {"life": life, "disability": disability,
            "umbrella": {"recommended": umb_need, "in_force": umb_have},
            "pnc": pnc, "ltc": ltc,
            "gap_table": gap_rows, "alerts": alerts,
            "premiums_annual_total": round(premiums, 2),
            "knobs": k,
            "note": ("Decision-support, not advice or a solicitation: needs are computed from your "
                     "own balance sheet at a real discount rate; quotes, suitability, and policy "
                     "selection belong with a licensed agent. The cheapest risk transfer is bought "
                     "before it's needed.")}
