# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
taxtables.py -- the year-keyed tax/ACA/Social-Security/estate/benefit figures as DATA (tax_tables/<year>.json), not code.

Why: every figure here is re-published each year. Keeping them in per-year JSON files means moving to a new year is a data change that
can be reviewed and tested on its own, prior years stay available (history, projections), and the engines pick the right year by
themselves on January 1. The engine modules (tax.py, aca.py, ss.py, estate.py, roth.py, benefits.py) keep their familiar constant
names; they fill them from here at import and again when the calendar year changes (see each module's reload_tables()).

Resolution for calendar year Y, per figure group (e.g. "tax.ordinary_brackets"):
  * take the newest file with year <= Y that has the group AND marks it confirmed (confirmed=false entries are stored for review but
    ignored until a person flips the flag, e.g. figures that were only reported by a secondary source);
  * if that entry says "merge": true it only overrides the fields it lists and the rest come from the previous year (a partial update
    such as the HSA limits published before the 401(k) limits); the result is marked partial and names the carried-forward fields;
  * if nothing newer exists the older figures are carried forward unchanged and the result says so, which the grounding watchdog turns
    into a STALE alert (statutory groups are exempt: the law does not index them).
Nothing here reads the network. Adding a year is: add tax_tables/<year>.json, run the tests, review the diff.
"""
import copy
import datetime
import json
import os

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tax_tables")
_cache = {}


def _file(year):
    if year not in _cache:
        with open(os.path.join(DIR, f"{year}.json"), encoding="utf-8") as f:
            _cache[year] = json.load(f)
    return _cache[year]


def clear_cache():
    _cache.clear()


def years():
    out = []
    for n in os.listdir(DIR):
        if n.endswith(".json") and n[:-5].isdigit():
            out.append(int(n[:-5]))
    return sorted(out)


def _entry_year_candidates(upto):
    return [y for y in sorted(years(), reverse=True) if y <= upto]


def _resolve(key, upto):
    for y in _entry_year_candidates(upto):
        e = (_file(y).get("groups") or {}).get(key)
        if not e or e.get("confirmed", True) is False or "values" not in e:
            continue
        status = e.get("status")
        if e.get("merge"):
            base = _resolve(key, y - 1)
            vals = copy.deepcopy(base["values"])
            vals.update(copy.deepcopy(e["values"]))
            carried = sorted(set(base["values"]) - set(e["values"]))
            return {"key": key, "year": y, "values": vals, "status": status, "verified": e.get("verified"),
                    "source": e.get("source") or base.get("source"), "source_url": e.get("source_url") or base.get("source_url"),
                    "partial": True, "carried_fields": carried, "carried_from": base["year"], "extra": e}
        return {"key": key, "year": y, "values": copy.deepcopy(e["values"]), "status": status, "verified": e.get("verified"),
                "source": e.get("source"), "source_url": e.get("source_url"), "partial": False, "carried_fields": [],
                "carried_from": None, "extra": e}
    raise KeyError(f"no confirmed tax-table entry for {key!r} at or before {upto}")


def resolve(key, today=None):
    """-> {key, year (the year the figures belong to), values, status, verified, source, source_url, partial, carried_fields,
    carried_from, target_year, stale, extra}. `stale` means the figures are for an earlier year than the one in force and are not
    statutory (statutory groups are not indexed, so carrying them forward is correct)."""
    today = today or datetime.date.today()
    r = _resolve(key, today.year)
    r["target_year"] = today.year
    r["stale"] = (r["year"] < today.year or r["partial"]) and r["status"] not in ("statutory",)
    return r


def unconfirmed(year):
    """Entries stored for `year` but not yet confirmed (ignored by resolve()): [{key, status, note}]."""
    try:
        groups = _file(year).get("groups") or {}
    except OSError:
        return []
    return [{"key": k, "status": e.get("status"), "note": e.get("note")} for k, e in groups.items() if e.get("confirmed", True) is False]


_loaded_year = datetime.date.today().year


def year_changed(today=None):
    """True once the calendar year differs from the year the engines last loaded their figures for."""
    return (today or datetime.date.today()).year != _loaded_year


def reload_all(today=None):
    """Refill every engine module's constants for the calendar year in force. Cheap; called from the scheduler when the year changes."""
    global _loaded_year
    import tax, aca, ss, estate, roth, benefits
    clear_cache()
    for m in (tax, aca, ss, estate, roth, benefits):
        m.reload_tables(today)
    _loaded_year = (today or datetime.date.today()).year
    return _loaded_year


# ---- small adapters the engine modules share (JSON has only lists and string keys) ----
def tuples(rows):
    return [tuple(r) for r in rows]


def brackets(d):
    return {k: tuples(v) for k, v in d.items()}


def int_keys(d):
    return {int(k): v for k, v in d.items()}
