# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
hsa.py -- how the Fiduciary understands a Health Savings Account (2026-11-10, Jason).

An HSA is a MEDICAL RESERVE first. It becomes a retirement asset only for the part ABOVE the healthcare the household can foresee, and only
from age 65 (before 65 a non-medical withdrawal costs ordinary income tax plus 20%; at 65 the 20% ends and it behaves like a traditional IRA
for non-medical use -- IRS Pub. 969). So:

    reserve   = min(balance, remaining healthcare need)           stays a medical fund: net worth, not retirement spending money
    surplus   = balance - reserve                                  above the need
    counted   = surplus if age >= 65 else 0                        the only part retirement projections may spend
    locked    = surplus if age <  65 else 0                        surplus that is not yet retirement money

The need is Fidelity's published retiree health-care cost estimate (a dated constant in tax_tables/<year>.json, per person, after tax,
excluding long-term care and most dental), scaled down by the years of retirement already lived, or the household's own override.
Only the single account balance from the provider is used: nothing here guesses how much of it is cash versus invested.
Pure functions; no I/O except the table lookup.
"""
import datetime

import taxtables

RETIRE_AGE = 65          # Fidelity's estimate assumes retirement at 65 ...
HORIZON_AGE = 90         # ... and runs to about 90, so the remaining need shrinks as retirement years are lived
KEY = "hsa.healthcare_need"


def table(today=None):
    """-> {per_person, year, verified, source, source_url, stale}. Never raises: a missing table yields per_person=None."""
    try:
        r = taxtables.resolve(KEY, today or datetime.date.today())
    except Exception:
        return {"per_person": None, "year": None, "verified": None, "source": None, "source_url": None, "stale": True}
    return {"per_person": (r.get("values") or {}).get("per_person_total"), "year": r.get("year"), "verified": r.get("verified"),
            "source": r.get("source"), "source_url": r.get("source_url"), "stale": bool(r.get("stale"))}


def remaining_fraction(age):
    """Share of the lifetime estimate still ahead: all of it until 65, then straight-line down to nothing at 90. Unknown age = all of it."""
    if age is None or age <= RETIRE_AGE:
        return 1.0
    return max(0.0, (HORIZON_AGE - age) / float(HORIZON_AGE - RETIRE_AGE))


def need(ages, override=None, today=None):
    """Foreseeable healthcare need for the household. `ages`: the living people's current ages (None for unknown), one entry per person.
    An override is the household's own TOTAL and replaces the estimate. -> {total, source, per_person, people, table}."""
    t = table(today)
    people = max(1, len(ages or [None]))
    if override not in (None, "", 0):
        return {"total": round(float(override), 2), "source": "your override", "per_person": None, "people": people, "table": t}
    if t["per_person"] is None:
        return {"total": None, "source": "no estimate available", "per_person": None, "people": people, "table": t}
    ages = list(ages or [None])
    total = sum(t["per_person"] * remaining_fraction(a) for a in ages)
    return {"total": round(total, 2), "source": "Fidelity retiree health care cost estimate", "per_person": t["per_person"],
            "people": people, "table": t}


def split(balance, age, healthcare_need):
    """Medical reserve versus retirement surplus for one household HSA balance. `age`: the age that governs access to the money (the
    account holder's; the 65 rule is per person, and the older holder's age is not assumed). Unknown age or need counts NOTHING as
    retirement money: the safe direction."""
    bal = max(0.0, float(balance or 0.0))
    if healthcare_need is None:
        return {"balance": round(bal, 2), "reserve": round(bal, 2), "surplus": 0.0, "counted_for_retirement": 0.0, "locked_until_65": 0.0,
                "basis": "no healthcare estimate: the whole balance is treated as a medical reserve"}
    reserve = min(bal, float(healthcare_need))
    surplus = round(bal - reserve, 2)
    if age is None:
        counted, locked, basis = 0.0, surplus, "age unknown: surplus is not counted as retirement money"
    elif age >= RETIRE_AGE:
        counted, locked, basis = surplus, 0.0, "age 65 or older: the surplus above the healthcare need can be spent on anything (ordinary income tax, no 20% penalty)"
    else:
        counted, locked, basis = 0.0, surplus, "under 65: surplus is not yet retirement money (non-medical withdrawals cost income tax plus 20%)"
    return {"balance": round(bal, 2), "reserve": round(reserve, 2), "surplus": surplus, "counted_for_retirement": round(counted, 2),
            "locked_until_65": round(locked, 2), "basis": basis}
