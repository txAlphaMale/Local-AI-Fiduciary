# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""plan_timeline.py -- one year-by-year table that ASSEMBLES the engines the app already has (RV-032, phase 1).

Pure: inputs in, rows out, no network or storage. It adds no tax logic of its own; each year calls tax.py (brackets, deductions, NIIT),
ss.py (taxable share of Social Security), aca.py (premium-subsidy band and the 400% cliff), roth.py (Medicare surcharge, RMD age) and the
Uniform Lifetime Table for required distributions (a dated entry in tax_tables/).

Rules (from the task): real (today's-dollar) terms throughout; deterministic (the Monte Carlo is where uncertainty lives); never invent an
input (a year that cannot be computed says why in `flags` and leaves the cell blank); every bucket reconciles each year:
    end = start + contributions + conversions in - conversions out - withdrawals + RMD surplus reinvested + growth
and in total: end = start + contributions + growth + Social Security - spending - tax - penalty + unfunded spending (shortfall).

Timing convention: withdrawals and conversions happen at the START of the year, growth applies to what is left. Working years assume wages cover
living costs (no portfolio withdrawals, tax and health coverage not modeled: there is no wage input to compute them from).
"""
import ss as ss_engine
import tax
import aca
import roth

BUCKETS = ("taxable", "pretax", "roth", "hsa")
DEFAULT_ORDER = ("taxable", "pretax", "roth")        # HSA is only used for healthcare costs
EARLY_PENALTY_AGE = 59.5
EARLY_PENALTY_RATE = 0.10

# Where each column's figure comes from (shown on hover by the page).
SOURCES = {
    "balances": "starting balances: the app's account classification (taxable / pre-tax / Roth / HSA); 529 plans excluded",
    "spending": "living: retirement config annual expenses (real dollars); healthcare: hsa.py cost line; long-term care: ltc.py scenario",
    "ss": "ss.py estimate of the benefit, in real dollars, from the claim age; taxable share: ss.taxable_portion (statutory thresholds)",
    "rmd": "Uniform Lifetime Table (IRS Pub. 590-B, dated entry in tax_tables) and the RMD start age from the birth year (SECURE 2.0)",
    "conversion": "the conversion amount set for this year (capped at the pre-tax balance left after any required distribution)",
    "tax": "tax.effective_rate (federal brackets, standard deduction incl. senior deduction, LTCG stacking, NIIT) for the table year in force",
    "state_tax": "tax.state_tax_estimate: a deliberately rough flat/none estimate; progressive states are flagged not modeled",
    "aca": "aca.summary: modified AGI vs the federal poverty line, premium-subsidy band and the 400% cliff (before 65)",
    "medicare": "roth.irmaa_annual_part_b on the modified AGI of two years earlier (from 65)",
    "growth": "the plan's real return applied to the balance left after the year's withdrawals and conversions",
}


def rmd_divisor(age, divisors):
    """Uniform Lifetime Table divisor for an age (ages past the table use its last entry). None when the age is before the table starts or the
    table is empty. Keys may be ints or strings (JSON)."""
    table = {}
    for k, v in (divisors or {}).items():
        try:
            table[int(k)] = float(v)
        except (TypeError, ValueError):
            continue
    if not table or age < min(table):
        return None
    return table[max(a for a in table if a <= min(int(age), max(table)))]


def _tax_for(ordinary_ex_ss, ss_cash, ltcg, st, age, spouse_age, state, magi_extra=0.0):
    ss_taxable = ss_engine.taxable_portion(ss_cash, ordinary_ex_ss + ltcg, st) if ss_cash else 0.0
    gross_ord = ordinary_ex_ss + ss_taxable
    er = tax.effective_rate(gross_ord, ltcg, st, None, age, spouse_age, include_niit=True)
    ded = tax.standard_deduction(st, age, spouse_age, magi=gross_ord + ltcg)
    taxable_ord = max(0.0, gross_ord - ded)
    stt = tax.state_tax_estimate(taxable_ord, ltcg, state)
    return {"federal": er["total_tax"], "marginal_pct": er["marginal_ordinary_rate_pct"], "ordinary_taxable": round(taxable_ord, 2),
            "ss_taxable": ss_taxable, "gross_ordinary": round(gross_ord, 2), "magi": round(gross_ord + ltcg, 2), "state": stt,
            "deduction": round(ded, 2)}


def build(inp):
    """Build the table. `inp` keys (all real dollars):
      start_year, start_age, spouse_age (age now, optional), end_age, retire_age, birth_year (for the RMD start age), status, household_size,
      slcsp_annual (optional), state, balances {taxable, pretax, roth, hsa}, real_return_pct, savings {taxable, pretax, roth, hsa, employer_match}
      per working year, living_expenses, healthcare_fn(age)->dollars|None, ltc_fn(age)->dollars|None, withdrawal_order, conversions {year: amount},
      ss {include, claim_age, annual_benefit}, taxable_gain_fraction, prior_magi {year: magi}, rmd_divisors {age: divisor}.
    -> {"rows", "summary", "footer", "sources", "problems"}"""
    st = tax.norm_status(inp.get("status"))
    y0, a0 = int(inp["start_year"]), int(inp["start_age"])
    end_age, retire_age = int(inp["end_age"]), int(inp["retire_age"])
    sp0 = inp.get("spouse_age")
    r = float(inp.get("real_return_pct") or 0.0) / 100.0
    hh = int(inp.get("household_size") or (2 if st == "mfj" else 1))
    bal = {b: float((inp.get("balances") or {}).get(b) or 0.0) for b in BUCKETS}
    sav = {k: float((inp.get("savings") or {}).get(k) or 0.0) for k in (*BUCKETS, "employer_match")}
    order = [b for b in (inp.get("withdrawal_order") or DEFAULT_ORDER) if b in ("taxable", "pretax", "roth")]
    for b in DEFAULT_ORDER:
        if b not in order:
            order.append(b)
    convs = {int(k): float(v or 0) for k, v in (inp.get("conversions") or {}).items()}
    ssi = inp.get("ss") or {}
    gain_frac = max(0.0, min(1.0, float(inp.get("taxable_gain_fraction") if inp.get("taxable_gain_fraction") is not None else 0.0)))
    rmd_start = roth.rmd_age(inp.get("birth_year"))
    divisors = inp.get("rmd_divisors") or {}
    magi_hist = {int(k): float(v) for k, v in (inp.get("prior_magi") or {}).items()}
    living = float(inp.get("living_expenses") or 0.0)
    problems = []
    if not divisors:
        problems.append("The required-distribution table is not loaded: required distributions are not computed.")
    if rmd_start is None:
        problems.append("No birth year is set, so the required-distribution start age is unknown: required distributions are not computed.")
    rows, conv_years, first_rmd_done = [], [], False
    for k in range(max(0, end_age - a0 + 1)):
        year, age = y0 + k, a0 + k
        spouse_age = (int(sp0) + k) if sp0 not in (None, "") else None
        start = dict(bal)
        flags = []
        retired = age >= retire_age
        row = {"year": year, "age": age, "spouse_age": spouse_age, "phase": "retired" if retired else "working", "start": {b: round(start[b], 2) for b in BUCKETS},
               "start_total": round(sum(start.values()), 2)}
        contrib = {b: 0.0 for b in BUCKETS}
        wd = {b: 0.0 for b in BUCKETS}
        conv = rmd = surplus = shortfall = penalty = 0.0
        ss_cash = hc = ltc = 0.0
        taxr = None
        if not retired:
            for b in BUCKETS:
                contrib[b] = sav.get(b, 0.0)
            contrib["pretax"] += sav.get("employer_match", 0.0)
            flags.append("Working year: wages cover living costs; tax and health coverage are not modeled (no wage input).")
            row.update({"spending": {"living": None, "healthcare": None, "ltc": None, "total": None}, "money_in": {"savings": round(sum(contrib.values()), 2), "social_security": 0.0}})
        else:
            hc = float(inp["healthcare_fn"](age) or 0.0) if inp.get("healthcare_fn") else 0.0
            ltc = float(inp["ltc_fn"](age) or 0.0) if inp.get("ltc_fn") else 0.0
            spend = living + hc + ltc
            if ssi.get("include") and ssi.get("annual_benefit") and ssi.get("claim_age") is not None and age >= int(ssi["claim_age"]):
                ss_cash = float(ssi["annual_benefit"])
            div = rmd_divisor(age, divisors) if (rmd_start is not None and age >= rmd_start) else None
            if div:
                rmd = min(start["pretax"] / div, start["pretax"])
                if not first_rmd_done and rmd > 0:
                    flags.append(f"First required distribution (age {age}, divisor {div:g}).")
                    first_rmd_done = True
            conv = max(0.0, min(convs.get(year, 0.0), start["pretax"] - rmd))
            if convs.get(year, 0.0) > conv + 0.5:
                flags.append(f"Requested conversion of ${convs[year]:,.0f} limited to ${conv:,.0f} (the pre-tax balance left after the required distribution).")
            if conv > 0:
                conv_years.append(year)
            order_y = list(order)
            if age < EARLY_PENALTY_AGE and "pretax" in order_y:
                order_y = [b for b in order_y if b != "pretax"] + ["pretax"]       # before 59 1/2 the penalized pre-tax money is the last resort
            tax_guess = 0.0
            for _ in range(30):
                need = max(0.0, spend + tax_guess - ss_cash)
                wd = {b: 0.0 for b in BUCKETS}
                wd["pretax"] = rmd
                cash = rmd
                if hc and start["hsa"] > 0:
                    t = min(hc, start["hsa"], max(0.0, need - cash))
                    wd["hsa"] += t
                    cash += t
                for b in order_y:
                    avail = start[b] - wd[b] - (conv if b == "pretax" else 0.0)
                    t = max(0.0, min(avail, need - cash))
                    wd[b] += t
                    cash += t
                shortfall = max(0.0, need - cash)
                surplus = max(0.0, cash - need)
                ordinary = wd["pretax"] + conv
                ltcg = wd["taxable"] * gain_frac
                taxr = _tax_for(ordinary, ss_cash, ltcg, st, age, spouse_age, inp.get("state"))
                penalty = EARLY_PENALTY_RATE * max(0.0, wd["pretax"] - rmd) if age < EARLY_PENALTY_AGE else 0.0
                new_guess = taxr["federal"] + penalty + float((taxr["state"] or {}).get("estimated_state_tax") or 0.0)
                if abs(new_guess - tax_guess) < 0.5:
                    tax_guess = new_guess
                    break
                tax_guess = new_guess
            if shortfall > 0.5:
                flags.append(f"Plan runs short by ${shortfall:,.0f} this year: the buckets cannot fund spending and tax.")
            if penalty > 0:
                flags.append(f"Pre-tax withdrawals before 59 1/2 carry a 10% penalty (${penalty:,.0f}).")
            if age < EARLY_PENALTY_AGE and wd["roth"] > 0:
                flags.append("Roth withdrawals before 59 1/2 assume they come from contributions or conversions older than five years; verify.")
            if taxr["state"] and taxr["state"].get("estimated_state_tax") is None:
                flags.append(f"State tax not modeled for {taxr['state'].get('state')}.")
            row.update({"spending": {"living": round(living, 2), "healthcare": round(hc, 2), "ltc": round(ltc, 2), "total": round(spend, 2)},
                        "money_in": {"savings": 0.0, "social_security": round(ss_cash, 2)}})
        for cy in conv_years:
            if cy + 5 == year:
                flags.append(f"Roth conversions made in {cy} become penalty-free (five-year clock).")
        # health coverage
        cov = {"kind": None}
        if retired and age < 65 and taxr is not None:
            comps = {"roth_conversions": conv, "ira_401k_withdrawals": wd["pretax"], "capital_gains": wd["taxable"] * gain_frac, "social_security_total": ss_cash}
            a = aca.summary(components=comps, household_size=hh, slcsp_annual=inp.get("slcsp_annual"), state=inp.get("state"))
            sub, cl = a["subsidy"], a["cliff"]
            lost = bool(cl["over_cliff"])
            cov = {"kind": "aca", "magi": a["magi"], "fpl_pct": sub.get("fpl_pct"), "band": sub.get("band"), "subsidy_lost": lost,
                   "subsidy_dollars": (0.0 if lost else sub.get("estimated_annual_ptc")), "headroom_to_cliff": cl["headroom_to_cliff"]}
            if lost:
                flags.append(f"Over the ACA cliff by ${-cl['headroom_to_cliff']:,.0f}: no premium subsidy this year.")
            elif sub.get("band") == "subsidized" and cl["headroom_to_cliff"] < 0.1 * cl["cliff_magi"]:
                flags.append(f"Within ${cl['headroom_to_cliff']:,.0f} of the ACA cliff.")
        elif retired and age >= 65 and taxr is not None:
            look = magi_hist.get(year - 2)
            people = (1 if age >= 65 else 0) + (1 if (st == "mfj" and spouse_age is not None and spouse_age >= 65) else 0)
            if look is None:
                cov = {"kind": "medicare", "surcharge": None, "reason": "income two years earlier is before the plan starts and was not supplied"}
            else:
                sur = roth.irmaa_annual_part_b(look, st, max(1, people))
                cov = {"kind": "medicare", "magi_lookback": round(look, 2), "surcharge": sur, "people": max(1, people)}
                if sur > 0:
                    flags.append(f"Medicare Part B surcharge ${sur:,.0f} (income ${look:,.0f} two years earlier).")
        if taxr is not None:
            magi_hist[year] = taxr["magi"]
        # growth and end balances
        end = {}
        growth = {}
        for b in BUCKETS:
            moved_in = conv if b == "roth" else 0.0
            moved_out = conv if b == "pretax" else 0.0
            sur_in = surplus if b == "taxable" else 0.0
            base_amt = start[b] + contrib[b] + moved_in - moved_out - wd[b] + sur_in
            growth[b] = base_amt * r
            end[b] = base_amt + growth[b]
        bal = dict(end)
        row.update({
            "funding": {b: round(wd[b], 2) for b in BUCKETS}, "rmd": round(rmd, 2), "conversion": round(conv, 2), "rmd_surplus_reinvested": round(surplus, 2),
            "contributions": {b: round(contrib[b], 2) for b in BUCKETS},
            "tax": ({"ordinary_taxable": taxr["ordinary_taxable"], "ltcg": round(wd["taxable"] * gain_frac, 2), "ss_taxable": taxr["ss_taxable"],
                     "federal": round(taxr["federal"], 2), "marginal_pct": taxr["marginal_pct"], "deduction": taxr["deduction"], "magi": taxr["magi"],
                     "state": (taxr["state"] or {}).get("estimated_state_tax"), "state_note": (taxr["state"] or {}).get("note"),
                     "penalty": round(penalty, 2)} if taxr is not None else None),
            "coverage": cov, "growth": {b: round(growth[b], 2) for b in BUCKETS}, "growth_total": round(sum(growth.values()), 2),
            "shortfall": round(shortfall, 2),
            "end": {b: round(end[b], 2) for b in BUCKETS}, "end_total": round(sum(end.values()), 2), "flags": flags})
        rows.append(row)
    total_tax = sum((x["tax"]["federal"] + (x["tax"]["state"] or 0.0) + x["tax"]["penalty"]) for x in rows if x["tax"])
    lost_years = [x["year"] for x in rows if x["coverage"].get("subsidy_lost")]
    short_years = [x["year"] for x in rows if x["shortfall"] > 0.5]
    med_years = [x["year"] for x in rows if (x["coverage"].get("surcharge") or 0) > 0]
    summary = {"lifetime_tax": round(total_tax, 2), "final_total": rows[-1]["end_total"] if rows else None,
               "years_with_lost_subsidy": lost_years, "years_short": short_years, "first_year_short": short_years[0] if short_years else None,
               "short_year_count": len(short_years), "lost_subsidy_year_count": len(lost_years), "first_lost_subsidy_year": lost_years[0] if lost_years else None,
               "medicare_surcharge_years": len(med_years)}
    footer = (f"Real (today's-dollar) terms. Deterministic: one path at the plan's real return of {r * 100:.1f}% a year; see the Monte Carlo for uncertainty. "
              f"Tax tables: {tax.TAX_YEAR} figures in real terms for every year. Pre-tax money is used last before 59 1/2 (10% penalty otherwise). "
              "Required distributions start at the age set by your birth year (SECURE 2.0).")
    return {"rows": rows, "summary": summary, "footer": footer, "sources": SOURCES, "problems": problems}
