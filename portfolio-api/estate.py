"""
estate.py — estate & generational-wealth engine for the Fiduciary (Fable G2).

The Grand-Design review's literal mission gap: the app knew how to GROW wealth but
had zero awareness of how it TRANSFERS. This module adds the four decision-support
lenses that matter for a sub-exemption Texas household:

  1. Estate snapshot — net estate vs the federal exemption(s), headroom, and the
     trajectory year the estate would CROSS the exemption at an assumed real growth
     rate (the trust-planning tripwire).
  2. Step-up ledger — per-account embedded capital gain and the tax that basis
     step-up at death would WIPE (IRC §1014). Texas is a community-property state:
     §1014(b)(6) gives community property a FULL (double) step-up at the FIRST
     spouse's death — both halves reset, not just the decedent's.
  3. Gift-vs-hold analyzer — post-OBBBA the dominant question for estates UNDER the
     exemption is NOT estate tax (there is none) but BASIS: a lifetime gift carries
     the donor's basis over to the donee (heir pays LTCG on the full gain when sold);
     holding until death steps the basis up (that gain is never taxed). Gifting only
     wins on the estate-tax side once the estate is projected ABOVE the exemption,
     where moving future APPRECIATION out saves ~40 cents on the growth dollar.
  4. Gifting capacity — annual-exclusion math ($/donee/yr, doubled with gift-splitting)
     plus the 529 5-year superfund election.

Pure arithmetic, zero network/GPU. Decision-support only — NOT legal or tax advice;
estate documents (wills, trusts, beneficiary designations) need a licensed attorney.
All dated constants below are registered in grounding.py (G13) so they trip the
January re-verification alarm like every other tax figure.
"""

TAX_YEAR = 2026
SOURCE = ("OBBBA (P.L. 119-21, July 2025) §70106: $15M basic exclusion per person from 2026, "
          "indexed after; IRC §§2010/2503/2505/1014; annual exclusion per Rev. Proc. 2025-32")

# Federal basic exclusion (estate + lifetime gift, unified) per PERSON for 2026.
# OBBBA set it to a flat $15,000,000 for 2026, indexed for inflation in later years.
# A married couple shields 2x with portability (DSUE) — surviving spouse must file
# Form 706 at the first death to CLAIM the unused exclusion, even when no tax is due.
ESTATE_EXEMPTION = 15_000_000
GST_EXEMPTION = 15_000_000          # generation-skipping transfer exemption (same figure, NOT portable)
TOP_RATE = 0.40                     # top federal estate/gift rate (statutory)

# Annual gift-tax exclusion per donee (2026). Gift-splitting with a spouse doubles it.
ANNUAL_EXCLUSION = 19_000
SUPERFUND_YEARS = 5                 # §529(c)(2)(B): elect 5 years of exclusions up front

# Texas: community-property state, and levies NO state estate or inheritance tax.
COMMUNITY_PROPERTY_STATE = True
STATE_ESTATE_TAX = 0.0


def snapshot(net_estate, married=True, growth_real_pct=5.0, horizon_years=40):
    """Estate vs exemption: headroom, estimated tax if death today, and the year the
    estate would cross the exemption at `growth_real_pct` (real, vs the inflation-indexed
    exemption held constant in real terms — a fair apples-to-apples approximation)."""
    ne = max(0.0, float(net_estate or 0))
    shield = ESTATE_EXEMPTION * (2 if married else 1)
    taxable = max(0.0, ne - shield)
    est_tax = round(taxable * TOP_RATE, 2)
    headroom = shield - ne
    cross_year = None
    if ne > 0 and headroom > 0 and growth_real_pct and growth_real_pct > 0:
        g = growth_real_pct / 100.0
        bal, yrs = ne, 0
        while bal < shield and yrs < horizon_years:
            bal *= (1 + g)
            yrs += 1
        cross_year = yrs if bal >= shield else None
    return {"net_estate": round(ne, 2), "married": bool(married),
            "exemption_per_person": ESTATE_EXEMPTION, "household_shield": shield,
            "headroom": round(headroom, 2),
            "pct_of_shield_used": round(ne / shield * 100, 1) if shield else None,
            "taxable_estate_if_today": round(taxable, 2), "est_federal_tax_if_today": est_tax,
            "years_until_exemption_crossed": cross_year,
            "state_estate_tax": STATE_ESTATE_TAX,
            "portability_note": ("Married: the couple shields ~$%s with portability, but the DSUE "
                                 "is NOT automatic — the survivor must file Form 706 within 5 years "
                                 "of the first death to claim it, even with no tax due."
                                 % f"{shield:,.0f}") if married else None,
            "note": ("Federal only — Texas has no estate or inheritance tax. Net estate here is "
                     "portfolio net worth; life insurance you own, business interests, and personal "
                     "property are NOT in Monarch and belong in the real number. Exemption is the "
                     "2026 OBBBA $15M/person (indexed after 2026); trajectory compares REAL growth "
                     "against the exemption held constant in real terms.")}


def stepup_ledger(accounts):
    """Per-account step-up view. `accounts` = [{name, tax_status, value, basis_total, gain_total}].
    Only TAXABLE accounts get a basis step-up at death; Traditional IRA/401k is IRD (income in
    respect of a decedent — heirs pay ordinary income tax, SECURE Act 10-year drain for most
    non-spouse heirs); Roth passes income-tax-free (also 10-year, but growth stays tax-free)."""
    rows, wiped = [], 0.0
    for a in accounts or []:
        status = a.get("tax_status")
        val = a.get("value") or 0.0
        gain = a.get("gain_total")
        if status == "taxable":
            treatment = "step-up at death (IRC §1014) — embedded gain is never income-taxed"
            if gain and gain > 0:
                wiped += gain
        elif status == "real_estate":
            treatment = ("step-up at death (IRC §1014) wipes the home's embedded gain. Note: a "
                         "primary residence already gets the §121 exclusion ($250k single / $500k MFJ) "
                         "on a LIFETIME sale, so the step-up's EXTRA benefit is the gain ABOVE that "
                         "exclusion. TX community property gets the FULL double step-up if titled as "
                         "community property. Set the residence cost basis to quantify the wiped gain.")
            if gain and gain > 0:
                wiped += gain
        elif status == "deferred":
            treatment = ("NO step-up — IRD: heirs pay ORDINARY income tax on withdrawals; "
                         "most non-spouse heirs must drain within 10 years (SECURE Act)")
        elif status == "roth":
            treatment = "no step-up needed — passes income-tax-FREE (non-spouse heirs: 10-year drain, growth stays tax-free)"
        elif status == "hsa":
            treatment = ("worst asset to die with (non-spouse): loses HSA status, FULL value taxed "
                         "as heir's ordinary income in year of death — spend HSA first late in life")
        else:
            treatment = "review titling with counsel"
        rows.append({"account": a.get("name"), "tax_status": status, "value": round(val, 2),
                     "basis_total": (round(a["basis_total"], 2) if a.get("basis_total") is not None else None),
                     "embedded_gain": (round(gain, 2) if gain is not None else None),
                     "treatment": treatment})
    rows.sort(key=lambda x: -(x.get("value") or 0))
    return {"accounts": rows, "taxable_embedded_gain": round(wiped, 2),
            "community_property_note": ("TEXAS DOUBLE STEP-UP: community property gets a FULL basis "
                                        "reset at the FIRST spouse's death (§1014(b)(6)) — both halves, "
                                        "not just the decedent's. Confirm brokerage accounts are titled "
                                        "as community property (not JTWROS, which steps up only half) — "
                                        "a titling detail worth real money on appreciated positions."),
            "note": ("Embedded gain in taxable accounts is the capital-gains tax a step-up would wipe. "
                     "This reverses the usual drawdown order late in life: heavily-appreciated taxable "
                     "positions can be worth HOLDING for the step-up while spending from IRA/HSA first.")}


def gift_vs_hold(value, basis, heir_ltcg_pct=15.0, growth_pct=5.0, years=20,
                 estate_over_exemption=False):
    """The post-OBBBA core question for one appreciated position: gift it now (carryover
    basis — donee eventually pays LTCG on the FULL gain incl. future growth) vs hold to
    death (step-up wipes the gain). Under the exemption, step-up wins on tax for any
    appreciated asset — gift CASH or high-basis assets instead. Over it, gifting removes
    future appreciation from the 40% estate base, which usually dominates."""
    value = float(value or 0)
    basis = float(basis if basis is not None else value)
    r = (growth_pct or 0) / 100.0
    cg = (heir_ltcg_pct or 0) / 100.0
    fv = value * ((1 + r) ** max(0, int(years)))
    growth = fv - value
    gain_today = max(0.0, value - basis)
    # GIFT now: carryover basis — donee's eventual LTCG on today's gain + all future growth.
    gift_cg_tax = (max(0.0, fv - basis)) * cg
    # HOLD to death: §1014 step-up — capital-gains tax = 0; if estate is over the
    # exemption the ASSET + GROWTH sit in the 40% base (gift would have removed them).
    hold_estate_tax = (fv * TOP_RATE) if estate_over_exemption else 0.0
    gift_estate_tax = 0.0     # gifted asset + growth are out of the estate (basic exclusion absorbs it)
    gift_total = gift_cg_tax + gift_estate_tax
    hold_total = hold_estate_tax
    verdict = "hold for step-up" if hold_total <= gift_total else "gift now"
    return {"value": round(value, 2), "basis": round(basis, 2),
            "embedded_gain": round(gain_today, 2),
            "assumed": {"heir_ltcg_pct": heir_ltcg_pct, "growth_pct": growth_pct, "years": years,
                        "estate_over_exemption": bool(estate_over_exemption)},
            "future_value": round(fv, 2),
            "gift_now": {"heir_cg_tax_when_sold": round(gift_cg_tax, 2),
                         "estate_tax_on_asset": 0.0},
            "hold_to_death": {"cg_tax_wiped_by_stepup": round(max(0.0, fv - basis) * cg, 2),
                              "estate_tax_on_asset": round(hold_estate_tax, 2)},
            "advantage_of_holding": round(gift_total - hold_total, 2),
            "verdict": verdict,
            "note": ("Sub-exemption estates: lifetime gifts of APPRECIATED assets forgo the step-up "
                     "— the single most expensive unforced error in modern estate planning. Gift cash "
                     "or high-basis assets; hold the winners. Over-exemption estates: the 40% saved on "
                     "future growth usually beats the forgone step-up. Not advice — model, then counsel.")}


def gifting_capacity(donees=0, married=True, years=10, growth_pct=5.0):
    """Annual-exclusion runway: $/yr removable with ZERO gift-tax filing (splitting doubles it),
    what N years of it compounds to outside the estate, and the 529 superfund option."""
    donees = max(0, int(donees or 0))
    per_donee = ANNUAL_EXCLUSION * (2 if married else 1)
    annual = per_donee * donees
    r = (growth_pct or 0) / 100.0
    removed = fv = 0.0
    for _ in range(max(0, int(years))):
        removed += annual
        fv = (fv + annual) * (1 + r)
    superfund = ANNUAL_EXCLUSION * SUPERFUND_YEARS * (2 if married else 1)
    return {"annual_exclusion_per_donee": ANNUAL_EXCLUSION, "gift_splitting": bool(married),
            "per_donee_per_year": per_donee, "donees": donees,
            "annual_capacity": annual, "years": years,
            "total_removed": round(removed, 2),
            "future_value_outside_estate": round(fv, 2),
            "superfund_529_per_beneficiary": superfund,
            "note": (f"${per_donee:,.0f}/donee/yr moves free of gift tax and filing "
                     f"(${ANNUAL_EXCLUSION:,.0f} exclusion" + (", doubled by spousal gift-splitting" if married else "") +
                     f"). A 529 superfund front-loads {SUPERFUND_YEARS} years — ${superfund:,.0f} per "
                     "beneficiary at once (Form 709 election, then no exclusion gifts to that "
                     "beneficiary for 5 years). Growth on gifted dollars compounds OUTSIDE the estate. "
                     "Appreciated-asset gifts carry basis over — prefer cash (see gift-vs-hold)."),
            }


def titling_audit(accounts):
    """How each account actually TRANSFERS — beneficiary designation vs probate — plus the
    action checklist. Beneficiary designations OVERRIDE the will; stale ones are the most
    common (and most litigated) estate failure."""
    rows = []
    for a in accounts or []:
        status = a.get("tax_status")
        if status in ("deferred", "roth", "hsa"):
            passes = "beneficiary designation (overrides the will)"
            action = "confirm primary + contingent beneficiaries are current"
        elif status == "taxable":
            passes = "TOD registration if set; otherwise PROBATE via the will"
            action = ("add Transfer-on-Death registration (free at most brokerages) to skip probate; "
                      "confirm community-property titling for the double step-up")
        elif status == "real_estate":
            passes = "Transfer-on-Death deed if recorded; otherwise PROBATE via the will"
            action = ("Texas allows a Transfer-on-Death Deed — record one to pass the home outside "
                      "probate; confirm community-property titling (not JTWROS) for the double step-up")
        else:
            passes = "check registration"
            action = "review with counsel"
        rows.append({"account": a.get("name"), "tax_status": status,
                     "value": round(a.get("value") or 0, 2),
                     "passes_by": passes, "action": action})
    rows.sort(key=lambda x: -(x.get("value") or 0))
    return {"accounts": rows,
            "note": ("Beneficiary designations and TOD registrations move assets OUTSIDE the will — "
                     "they win even when the will says otherwise, so they must be audited like code. "
                     "Texas probate is relatively cheap (independent administration) but still public "
                     "and slow; TOD/beneficiary routing avoids it entirely.")}


def trust_triggers(net_estate, married=True, minor_heirs=None, cross_year=None):
    """Decision-SUPPORT (not drafting): which trust conversations the trajectory justifies."""
    out = []
    shield = ESTATE_EXEMPTION * (2 if married else 1)
    out.append({"trigger": "always", "vehicle": "Will + durable POA + medical directives",
                "why": "the floor for any estate; without a will Texas intestacy rules decide"})
    out.append({"trigger": "beneficiaries who are minors or spend-vulnerable",
                "vehicle": "testamentary or revocable-living-trust provisions",
                "why": "direct inheritances to minors force a court-supervised guardianship; "
                       "trust terms stage distributions by age/milestone"})
    out.append({"trigger": "probate avoidance / privacy / incapacity planning",
                "vehicle": "revocable living trust",
                "why": "no tax benefit (grantor trust, assets still in the estate, still stepped up) "
                       "— its value is avoiding probate, staying private, and seamless incapacity handoff"})
    if net_estate and net_estate > 0.5 * shield or (cross_year is not None and cross_year <= 20):
        out.append({"trigger": "trajectory approaching the exemption"
                               + (f" (crosses in ~{cross_year}y at assumed growth)" if cross_year else ""),
                    "vehicle": "irrevocable strategies (SLAT, ILIT, GRAT, gifting program)",
                    "why": "these need YEARS of lead time and are cheapest while the estate is still "
                           "under the exemption — the planning window is before the crossing, not after"})
    return {"triggers": out,
            "note": "Education only. Trust drafting, funding and Texas community-property nuances "
                    "require a licensed estate attorney — bring this trajectory to that meeting."}
