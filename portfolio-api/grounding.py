# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
grounding.py — the dated-constant registry + staleness watchdog (Fable G13a).

Every tax/ACA/Social-Security figure the Fiduciary computes with is a REAL-WORLD
fact with an expiry date: brackets re-index every January (IRS Rev. Proc., ~Oct),
FPL guidelines re-publish (~Jan), SSA announces COLA/bend points (~Oct). If those
constants silently age, every downstream number (Monte Carlo gross-ups, Roth
headroom, ACA cliff, SS floor) drifts wrong while LOOKING precise.

This module makes that risk visible instead of silent:
  • registry() — one record per dated constant, read LIVE from the module that
    actually uses it (so the registry can never disagree with the code):
    {key, label, value, applies_to_year, source, source_url, verified, kind}
  • alerts()   — staleness assessment: any constant whose applies_to_year is
    behind the current calendar year is flagged REVERIFY; every December a
    "new-year figures publish soon" reminder fires; January is the re-verify
    window (the annual research-run the G13 design calls for is a human/agent
    task — this is the checklist + tripwire that forces it).

Pure arithmetic + attribute reads: zero network, zero GPU, read-only.
"""

import datetime
import json
import os

import taxtables
import tax
import aca
import ss
import estate
import roth
import benefits

# Month precision is deliberate: the modules were authored/verified against the
# cited primary sources in July 2026 (see each module's docstring). If you
# re-verify a figure, bump the date here.
VERIFIED = {"tax": "2026-07", "aca": "2026-07", "ss": "2026-07",
            "estate": "2026-07-10",   # verified vs Rev. Proc. 2025-32 coverage at build time
            "roth": "2026-07-10",     # IRMAA verified vs CMS 2026 Part B fact sheet (G8)
            "benefits": "2026-07-17"} # 401k/415c/HSA/IRA limits vs IRS Notice 2025-67 (GAP-5)

# kind: "indexed" re-publishes every year (MUST re-verify annually);
#       "statutory" is fixed in law until Congress acts (verify the law still stands);
#       "policy" is a regime flag that can flip with legislation (watch closely).
PARAM_SPECS = [
    {"key": "tax.ordinary_brackets", "label": "Federal ordinary income brackets",
     "value": lambda: tax.ORDINARY_BRACKETS, "year": lambda: tax.TAX_YEAR, "kind": "indexed",
     "source": lambda: tax.SOURCE,
     "source_url": "https://taxfoundation.org/data/all/federal/2026-tax-brackets/",
     "module": "tax"},
    {"key": "tax.ltcg_brackets", "label": "LTCG / qualified-dividend brackets",
     "value": lambda: tax.LTCG_BRACKETS, "year": lambda: tax.TAX_YEAR, "kind": "indexed",
     "source": lambda: tax.SOURCE,
     "source_url": "https://taxfoundation.org/data/all/federal/2026-tax-brackets/",
     "module": "tax"},
    {"key": "tax.standard_deduction", "label": "Standard deduction (+65 add-on)",
     "value": lambda: {"base": tax.STANDARD_DEDUCTION, "senior_add": tax.SENIOR_ADD},
     "year": lambda: tax.TAX_YEAR, "kind": "indexed", "source": lambda: tax.SOURCE,
     "source_url": "https://taxfoundation.org/data/all/federal/2026-tax-brackets/",
     "module": "tax"},
    {"key": "tax.niit", "label": "NIIT rate + MAGI thresholds",
     "value": lambda: {"rate": tax.NIIT_RATE, "threshold": tax.NIIT_THRESHOLD},
     "year": lambda: None, "kind": "statutory", "source": lambda: "IRC §1411 (thresholds NOT inflation-indexed)",
     "source_url": "https://www.irs.gov/individuals/net-investment-income-tax",
     "module": "tax"},
    {"key": "aca.fpl", "label": "Federal poverty guidelines (48 states, coverage-year lag)",
     "value": lambda: {"fpl": aca.FPL_2026, "increment": aca.FPL_INCREMENT},
     "year": lambda: aca.COVERAGE_YEAR, "kind": "indexed", "source": lambda: aca.SOURCE,
     "source_url": "https://aspe.hhs.gov/topics/poverty-economic-mobility/poverty-guidelines",
     "module": "aca"},
    {"key": "aca.applicable_pct", "label": "ACA §36B applicable-percentage scale",
     "value": lambda: aca.APPLICABLE_PCT_2026, "year": lambda: aca.COVERAGE_YEAR, "kind": "indexed",
     "source": lambda: aca.SOURCE,
     "source_url": "https://thefinancebuff.com/federal-poverty-levels-for-obamacare.html",
     "module": "aca"},
    {"key": "aca.cliff_regime", "label": "PTC 100%–400% FPL band + hard 400% cliff (enhanced subsidies EXPIRED 2026)",
     "value": lambda: {"floor_pct": aca.PTC_FLOOR_PCT, "cliff_pct": aca.PTC_CLIFF_PCT},
     "year": lambda: aca.COVERAGE_YEAR, "kind": "policy",
     "source": lambda: "ARPA/IRA enhanced PTC expired 2026-01-01; pre-ARPA §36B rules apply",
     "source_url": "https://www.kff.org/affordable-care-act/",
     "module": "aca"},
    {"key": "ss.bend_points", "label": "SS PIA bend points + rates",
     "value": lambda: {"bend_points": ss.BEND_POINTS_2026, "rates": ss.PIA_RATES},
     "year": lambda: ss.BENEFIT_YEAR, "kind": "indexed", "source": lambda: ss.SOURCE,
     "source_url": "https://www.ssa.gov/oact/cola/bendpoints.html",
     "module": "ss"},
    {"key": "ss.cola_wagebase_max", "label": "SS COLA / OASDI wage base / max FRA benefit",
     "value": lambda: {"cola": ss.COLA_2026, "wage_base": ss.OASDI_WAGE_BASE_2026,
                       "max_fra_monthly": ss.MAX_FRA_BENEFIT_2026},
     "year": lambda: ss.BENEFIT_YEAR, "kind": "indexed", "source": lambda: ss.SOURCE,
     "source_url": "https://www.ssa.gov/news/en/cola/factsheets/2026.html",
     "module": "ss"},
    {"key": "ss.earnings_test", "label": "SS retirement earnings-test exempt amounts",
     "value": lambda: ss.EARNINGS_TEST_2026, "year": lambda: ss.BENEFIT_YEAR, "kind": "indexed",
     "source": lambda: ss.SOURCE,
     "source_url": "https://www.ssa.gov/news/en/cola/factsheets/2026.html",
     "module": "ss"},
    {"key": "ss.taxation_thresholds", "label": "SS benefit-taxation provisional-income thresholds",
     "value": lambda: ss.TAX_THRESHOLDS, "year": lambda: None, "kind": "statutory",
     "source": lambda: "IRC §86 (thresholds NOT inflation-indexed since 1983/1993)",
     "source_url": "https://www.irs.gov/publications/p915",
     "module": "ss"},
    {"key": "estate.exemption", "label": "Estate/gift basic exclusion + GST exemption + top rate",
     "value": lambda: {"exemption": estate.ESTATE_EXEMPTION, "gst": estate.GST_EXEMPTION,
                       "top_rate": estate.TOP_RATE},
     "year": lambda: estate.TAX_YEAR, "kind": "indexed", "source": lambda: estate.SOURCE,
     "source_url": "https://www.irs.gov/businesses/small-businesses-self-employed/estate-tax",
     "module": "estate"},
    {"key": "estate.annual_exclusion", "label": "Annual gift-tax exclusion per donee (+529 superfund ×5)",
     "value": lambda: {"exclusion": estate.ANNUAL_EXCLUSION, "superfund_years": estate.SUPERFUND_YEARS},
     "year": lambda: estate.TAX_YEAR, "kind": "indexed", "source": lambda: estate.SOURCE,
     "source_url": "https://www.irs.gov/businesses/small-businesses-self-employed/frequently-asked-questions-on-gift-taxes",
     "module": "estate"},
    {"key": "roth.irmaa", "label": "Medicare IRMAA Part B brackets + standard premium (2-yr MAGI lookback)",
     "value": lambda: {"std_monthly": roth.IRMAA_PART_B_STD_MONTHLY,
                       "multipliers": roth.IRMAA_MULTIPLIERS, "thresholds": roth.IRMAA_THRESHOLDS},
     "year": lambda: roth.IRMAA_YEAR, "kind": "indexed", "source": lambda: roth.IRMAA_SOURCE,
     "source_url": "https://www.cms.gov/newsroom/fact-sheets/2026-medicare-parts-b-premiums-deductibles",
     "module": "roth"},
    {"key": "benefits.contribution_limits", "label": "401(k)/415(c)/HSA/IRA contribution limits",
     "value": lambda: benefits.LIMITS_2026, "year": lambda: benefits.LIMITS_YEAR, "kind": "indexed",
     "source": lambda: "IRS Notice 2025-67 + Rev. Proc. (HSA) — verified 2026-07-17",
     "source_url": benefits.SOURCE_URL,
     "module": "benefits"},
]


def registry():
    """One JSON-able record per dated constant, values read live from the owning module (which fills them from tax_tables/<year>.json).
    `verified`, `source_url` and the carry-forward facts come from the table entry the module is actually using."""
    out = []
    for s in PARAM_SPECS:
        try:
            res = taxtables.resolve(s["key"], datetime.date(taxtables._loaded_year, 1, 1))   # the year the engines are using
            out.append({"key": s["key"], "label": s["label"], "value": s["value"](),
                        "applies_to_year": (None if s["kind"] == "statutory" else res["year"]), "kind": s["kind"],
                        "source": s["source"](), "source_url": res.get("source_url") or s["source_url"],
                        "verified": res.get("verified") or VERIFIED.get(s["module"]),
                        "partial": bool(res.get("partial")), "carried_fields": res.get("carried_fields") or [],
                        "carried_from": res.get("carried_from")})
        except Exception as e:            # a broken spec should surface, not vanish
            out.append({"key": s["key"], "label": s["label"], "error": str(e)})
    return out


def alerts(today=None):
    """Staleness tripwires. STALE = an indexed/policy constant whose year is behind the
    calendar year (numbers are WRONG for the current year until re-verified). December
    fires a heads-up (new-year figures publish Oct–Nov); January is the re-verify window."""
    today = today or datetime.date.today()
    out, stale = [], []
    for r in registry():
        yr, kind = r.get("applies_to_year"), r.get("kind")
        if r.get("error"):
            out.append({"level": "error", "key": r["key"], "msg": f"registry error: {r['error']}"})
        elif kind in ("indexed", "policy") and yr and yr < today.year:
            stale.append(r["key"])
            out.append({"level": "stale", "key": r["key"],
                        "msg": f"{r['label']} is for {yr} but it is {today.year} — figures are carried forward until the new year's "
                               f"are confirmed; re-verify against the primary source ({r['source_url']}) and add them to tax_tables/{today.year}.json."})
        elif kind in ("indexed", "policy") and r.get("partial"):
            stale.append(r["key"])
            out.append({"level": "stale", "key": r["key"],
                        "msg": f"{r['label']}: only part of {yr}'s figures are published/confirmed; {', '.join(r['carried_fields'][:6])} "
                               f"are carried forward from {r['carried_from']} ({r['source_url']})."})
    if today.month == 12:
        out.append({"level": "info", "key": "annual_cycle",
                    "msg": f"Next-year figures are out (IRS Rev. Proc. ~Oct, SSA COLA ~Oct, HHS FPL ~Jan): "
                           f"schedule the January {today.year + 1} re-verification pass."})
    if today.month == 1 and stale:
        out.append({"level": "action", "key": "annual_cycle",
                    "msg": "January re-verification window: update every STALE constant above from its "
                           "primary source, then bump grounding.VERIFIED for that module."})
    return out


UPDATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tax_updates")
_NEXT_STATUS_ORDER = {"pending": 0, "secondary": 1, "primary": 2, "statutory": 3}


def next_year(today=None):
    """The researched-but-NOT-YET-ACTIVE figures for next calendar year (tax_updates/<year>.json), joined to the registry's labels.
    Shown in AUDITS > GROUNDING so you can see what is coming, how it was verified, and what is still pending. The tax engines do
    not read this file: switching a year on is a deliberate, tested change. Returns None when no file exists for next year."""
    today = today or datetime.date.today()
    path = os.path.join(UPDATES_DIR, f"{today.year + 1}.json")
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    labels = {s["key"]: s["label"] for s in PARAM_SPECS}
    try:
        groups = taxtables._file(today.year + 1).get("groups") or {}
    except OSError:
        groups = {}
    items = []
    for it in d.get("items") or []:
        g = groups.get(it.get("key"))
        state = ("not_in_tables" if g is None else ("awaiting_confirmation" if g.get("confirmed", True) is False else "active_from_jan_1"))
        items.append(dict(it, label=labels.get(it.get("key"), it.get("key")), table_state=state))
    items.sort(key=lambda i: (_NEXT_STATUS_ORDER.get(i.get("status"), 9), i.get("label") or ""))
    counts = {}
    for it in items:
        counts[it.get("status")] = counts.get(it.get("status"), 0) + 1
    return {"year": d.get("year"), "compiled": d.get("compiled"), "how": d.get("how"), "items": items, "counts": counts,
            "active": False}


def summary(today=None):
    today = today or datetime.date.today()
    al = alerts(today)
    n_stale = sum(1 for a in al if a["level"] == "stale")
    return {"params": registry(), "alerts": al, "stale_count": n_stale, "next_year": next_year(today),
            "status": "stale" if n_stale else "current",
            "note": ("Every dated constant the tax/ACA/SS engines compute with, read live from the code, "
                     "with its primary source and verification date. Indexed figures re-publish every year "
                     "— a STALE flag means downstream numbers (MC gross-up, Roth headroom, ACA cliff, SS "
                     "floor) are silently wrong for the current year until re-verified.")}
