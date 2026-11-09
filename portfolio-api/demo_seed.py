# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
demo_seed.py -- synthetic-data seeder + reset harness for a PUBLIC DEMO instance of Fiduciary.

CRITICAL SAFETY MODEL, stated up front: DEMO_MODE is meant for a SEPARATE, DISPOSABLE deployment
(its own /data volume), never toggled on a real household's live instance. But "meant for" isn't
a safety control by itself, so this module enforces it in code, not just in docs:

  - seed_if_needed() (called once at app boot) will REFUSE to create the demo account or touch
    anything if auth.has_any_user() is already True AND no demo marker file exists yet. That
    combination means "this /data directory already has a real account in it" -- exactly the
    scenario of DEMO_MODE being accidentally set on a live personal deployment. It logs a loud
    warning and does nothing further.
  - The demo marker file (DEMO_MARKER_FILE, default /data/.demo_instance) is created ONLY by a
    successful seed_if_needed() run, and is the ONE thing reset_and_reseed() checks before doing
    anything destructive. This means the periodic reset thread can only ever wipe a /data
    directory that THIS module itself already positively confirmed was a fresh demo seed --
    never a directory it hasn't seeded, even if DEMO_MODE=1 is set on it by mistake.

Everything seeded is clearly fake: a published demo login, invented household name, and
real-but-arbitrary tickers (chosen so live market-data calls still work for a realistic-looking
dashboard) with made-up quantities/cost-bases -- no real financial data touches this path.
"""
import os
import csv
import io
import datetime
import threading
import random

import auth
import store
import audit
import knowledge

DEMO_MODE = os.environ.get("DEMO_MODE", "0") == "1"
DEMO_ADMIN_USERNAME = os.environ.get("DEMO_ADMIN_USERNAME", "demo")
DEMO_ADMIN_PASSWORD = os.environ.get("DEMO_ADMIN_PASSWORD", "FiduciaryDemo2026!")
DEMO_MARKER_FILE = os.environ.get("DEMO_MARKER_FILE", "/data/.demo_instance")
DEMO_RESET_INTERVAL_S = int(os.environ.get("DEMO_RESET_INTERVAL_S", str(24 * 3600)))  # daily by default (was 6h until 2026-11-09)

HOLDINGS_CSV = os.environ.get("HOLDINGS_CSV", "/data/holdings.csv")

# Real, liquid tickers (so Finnhub/market-data calls resolve to something real-looking) with
# invented quantities/cost-bases spread across account types. cost_basis is the TOTAL cost of the line (as in
# holdings.csv.example), NOT per share: until 2026-11-09 these were per-share numbers, which made every gain absurd. for a portfolio that exercises most
# of the dashboard's analysis surface (diversification, sector concentration, tax lots, dividends).
SYNTHETIC_HOLDINGS = [
    {"ticker": "VTI",  "name": "Vanguard Total Stock Market ETF", "quantity": 120, "cost_basis": 25200, "account": "Brokerage",  "institution": "Demo Brokerage", "type": "ETF"},
    {"ticker": "VXUS", "name": "Vanguard Total Intl Stock ETF",   "quantity": 200, "cost_basis": 11600,  "account": "Brokerage",  "institution": "Demo Brokerage", "type": "ETF"},
    {"ticker": "BND",  "name": "Vanguard Total Bond Market ETF",  "quantity": 150, "cost_basis": 10800,  "account": "Brokerage",  "institution": "Demo Brokerage", "type": "ETF"},
    {"ticker": "AAPL", "name": "Apple Inc.",                      "quantity": 40,  "cost_basis": 6000, "account": "Brokerage",  "institution": "Demo Brokerage", "type": "Stock"},
    {"ticker": "MSFT", "name": "Microsoft Corp.",                 "quantity": 25,  "cost_basis": 7000, "account": "Brokerage",  "institution": "Demo Brokerage", "type": "Stock"},
    {"ticker": "VOO",  "name": "Vanguard S&P 500 ETF",            "quantity": 60,  "cost_basis": 22800, "account": "Roth IRA",   "institution": "Demo Custodian",  "type": "ETF"},
    {"ticker": "QQQ",  "name": "Invesco QQQ Trust",               "quantity": 30,  "cost_basis": 9900, "account": "Roth IRA",   "institution": "Demo Custodian",  "type": "ETF"},
    {"ticker": "VTI",  "name": "Vanguard Total Stock Market ETF", "quantity": 80,  "cost_basis": 15600, "account": "401(k)",     "institution": "Demo Employer Plan", "type": "ETF"},
    {"ticker": "VBTLX","name": "Vanguard Total Bond Market Fund", "quantity": 500, "cost_basis": 5100,  "account": "401(k)",     "institution": "Demo Employer Plan", "type": "Fund"},
    {"ticker": "SCHD", "name": "Schwab US Dividend Equity ETF",   "quantity": 90,  "cost_basis": 2340,  "account": "HSA",        "institution": "Demo HSA Custodian", "type": "ETF"},
    # 2026-11-09 refresh: funds with NO cost basis (the manual cost-basis feature has a basis entered for the 401(k) line only,
    # so the combined row shows "known for 1 of 2 accounts"), and two cash accounts (emergency-fund tile, net worth).
    {"ticker": "FXAIX", "name": "Fidelity 500 Index Fund",        "quantity": 160, "cost_basis": "",     "account": "401(k)",     "institution": "Demo Employer Plan", "type": "Fund"},
    {"ticker": "FXAIX", "name": "Fidelity 500 Index Fund",        "quantity": 40,  "cost_basis": "",     "account": "Roth IRA",   "institution": "Demo Custodian",  "type": "Fund"},
    {"ticker": "CASH",  "name": "Emergency fund (high-yield savings)", "quantity": 22000, "cost_basis": 22000, "account": "Emergency Fund", "institution": "Demo Bank", "type": "Cash"},
    {"ticker": "CASH",  "name": "Checking",                       "quantity": 6400, "cost_basis": 6400,  "account": "Checking",   "institution": "Demo Bank", "type": "Cash"},
]

CSV_FIELDS = ["ticker", "name", "quantity", "cost_basis", "account", "institution", "type"]


def _write_synthetic_holdings():
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    w.writeheader()
    for row in SYNTHETIC_HOLDINGS:
        w.writerow(row)
    store.atomic_write_text(HOLDINGS_CSV, buf.getvalue())


def _write_marker():
    try:
        os.makedirs(os.path.dirname(DEMO_MARKER_FILE) or ".", exist_ok=True)
        with open(DEMO_MARKER_FILE, "w") as f:
            f.write(datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ") + "\n")
    except Exception as e:
        print(f"[demo_seed] could not write marker file: {e}", flush=True)


def _marker_exists():
    return os.path.exists(DEMO_MARKER_FILE)


def _marker_age_s(now=None):
    """Seconds since this demo was last (re)seeded, from the marker file's mtime; None if there is no marker."""
    try:
        return max(0.0, (now if now is not None else __import__("time").time()) - os.path.getmtime(DEMO_MARKER_FILE))
    except OSError:
        return None


def seconds_until_reset(interval_s=None, now=None):
    """How long until the next scheduled reset, measured from the LAST SEED, not from process start. (Measuring from
    process start meant a daily interval could never fire: the ECS task is replaced every weekday morning, so the
    timer restarted before it ran out.) A task that boots after the interval has already passed resets at once."""
    interval = int(interval_s or DEMO_RESET_INTERVAL_S)
    age = _marker_age_s(now)
    return 60 if age is None else max(60, int(interval - age))


def _reindex_knowledge_async():
    """Kick off a full knowledge-base (re)index in a background thread right after a fresh seed.

    Why this exists (2026-07-26, found by Jason on the first real demo walkthrough): the
    knowledge/reference/*.md corpus (public tax/estate/insurance/risk/security reference material,
    all git-tracked and bind-mounted into both the real and demo containers via the same
    ./knowledge:/knowledge:ro mount) is only ever indexed on-demand -- via the SETTINGS -> KNOWLEDGE
    "REINDEX" button, or implicitly the first time a chat persona's search_knowledge tool runs a
    query. Neither happens automatically. Compounding it: the knowledge tables live in the same
    store.db that reset_and_reseed() below deletes wholesale every DEMO_RESET_INTERVAL_S (6h
    default), so even a demo that WAS indexed loses that index on every periodic reset. Net effect:
    a fresh demo (or one visited shortly after a reset) shows "no documents" in the KNOWLEDGE tab
    and gets no citations from search_knowledge, even though the files were there the whole time --
    not a missing-content bug, an indexing-timing gap. Run in a background thread (not inline) so a
    slow embed pass never blocks app boot or the reset thread's own next tick; failures are logged,
    never raised, matching every other best-effort background job in this app.
    """
    def _run():
        try:
            result = knowledge.ensure_index(force=True)
            print(f"[demo_seed] knowledge base indexed for demo: {result}", flush=True)
        except Exception as e:
            print(f"[demo_seed] knowledge reindex failed (non-fatal): {e}", flush=True)
    threading.Thread(target=_run, daemon=True, name="demo-knowledge-reindex").start()


def seed_if_needed():
    """Called once at app boot. Safe to call even when DEMO_MODE=0 (no-op)."""
    if not DEMO_MODE:
        return {"seeded": False, "reason": "DEMO_MODE not set"}

    if auth.has_any_user() and not _marker_exists():
        # SAFETY GUARD: real account data present, and this directory was never seeded by us --
        # almost certainly DEMO_MODE was set on a real deployment by mistake. Refuse to touch
        # anything and make noise about it, rather than silently doing nothing OR (much worse)
        # silently overwriting something real.
        print("[demo_seed] WARNING: DEMO_MODE=1 but an existing account was found and this /data "
              "directory has no demo marker file — refusing to seed or touch ANYTHING. If this is "
              "genuinely meant to be a fresh demo instance, use an empty /data volume.", flush=True)
        return {"seeded": False, "reason": "existing non-demo account data detected — refused for safety"}

    if auth.has_any_user() and _marker_exists():
        # Already a seeded demo instance (e.g. a restart between reset cycles). If it is older than one reset interval (the
        # ECS task starts each weekday morning, so this is the daily reset), start fresh instead of resuming stale data.
        age = _marker_age_s()
        if age is not None and age >= DEMO_RESET_INTERVAL_S - 3600:
            print(f"[demo_seed] demo data is {int(age // 3600)}h old (interval {DEMO_RESET_INTERVAL_S // 3600}h): resetting at boot.", flush=True)
            return reset_and_reseed()
        return {"seeded": False, "reason": "already seeded"}

    uid, _recovery_key = auth.create_user(DEMO_ADMIN_USERNAME, DEMO_ADMIN_PASSWORD, role="admin")
    _write_synthetic_holdings()
    _write_marker()
    global _FRESH
    _FRESH = True
    try:
        audit.emit("demo.seeded", actor=uid, actor_name=DEMO_ADMIN_USERNAME,
                   resource="demo_seed.seed_if_needed",
                   detail={"holdings_count": len(SYNTHETIC_HOLDINGS)})
    except Exception:
        pass
    _reindex_knowledge_async()
    print(f"[demo_seed] seeded fresh demo instance — login as '{DEMO_ADMIN_USERNAME}'.", flush=True)
    return {"seeded": True, "username": DEMO_ADMIN_USERNAME}


def reset_and_reseed():
    """Wipes auth + financial config + audit/chat history and reseeds fresh. ONLY runs its
    destructive step if the demo marker file is present (see module docstring) -- this is the one
    check standing between "periodic demo hygiene" and "accidentally wiping something real."
    Called by the periodic background thread, and safe to call manually for an on-demand reset."""
    if not DEMO_MODE:
        return {"reset": False, "reason": "DEMO_MODE not set"}
    if not _marker_exists():
        print("[demo_seed] WARNING: reset_and_reseed() called but no demo marker file exists — "
              "refusing to wipe anything. Call seed_if_needed() first on a genuinely fresh "
              "instance.", flush=True)
        return {"reset": False, "reason": "no demo marker file — refused for safety"}

    for path_getter in (
        lambda: auth.AUTH_DB,
        lambda: store.STORE_DB if hasattr(store, "STORE_DB") else None,
        lambda: os.environ.get("CHAT_DB", "/data/chat.db"),
        lambda: os.environ.get("AUDIT_DB", "/data/audit.db"),
        lambda: os.environ.get("AUDIT_JSONL", "/data/audit.jsonl"),
        lambda: HOLDINGS_CSV,
    ):
        try:
            p = path_getter()
            if p and os.path.exists(p):
                os.remove(p)
        except Exception as e:
            print(f"[demo_seed] reset: could not remove a data file: {e}", flush=True)

    try:
        auth.init_db()
    except Exception:
        pass
    try:
        audit.init_db()
    except Exception:
        pass
    try:
        store.init_db()
    except Exception:
        pass

    os.remove(DEMO_MARKER_FILE) if _marker_exists() else None
    for _m in (EXTRAS_MARKER_FILE, ANCHOR_MARKER_FILE):
        try:
            os.remove(_m)
        except OSError:
            pass
    result = seed_if_needed()
    extras = seed_extras() if _APP is not None else {"seeded": False, "reason": "app not registered yet (boot-time reset: seeded by the end-of-module hook)"}
    print("[demo_seed] periodic reset complete — fresh demo data seeded.", flush=True)
    return {"reset": True, "reseed": result, "extras": extras}


# ---------------------------------------------------------------------------------------------------------------------
# 2026-11-09 refresh: seed the newer features so the public demo shows them. Everything is synthetic and deterministic.
# These helpers use the app's OWN validators/writers (registered by app.py at the END of the module, once they exist), so
# the stored shapes cannot drift from what the app itself saves. Vault documents are deliberately NOT seeded.
# ---------------------------------------------------------------------------------------------------------------------
EXTRAS_MARKER_FILE = os.environ.get("DEMO_EXTRAS_MARKER_FILE", "/data/.demo_extras")
EXTRAS_VERSION = "2026-11-09c"          # bump to make existing demos start fresh (reset + re-seed) on their next boot
ANCHOR_MARKER_FILE = os.environ.get("DEMO_ANCHOR_MARKER_FILE", "/data/.demo_anchored")
HISTORY_DAYS = 730                      # about two years of daily snapshots
HISTORY_END_NET_WORTH = 214000.0        # where the synthetic series lands (close to what the demo holdings are worth)
HISTORY_END_SPY = 655.0
DEMO_LIABILITIES = [
    {"name": "Demo Visa", "institution": "Demo Bank", "type": "Credit Card", "balance": 2150.00, "rate_pct": 22.9, "min_payment": 65, "pay_in_full": True, "notes": "synthetic demo data"},
    {"name": "Demo Auto Loan", "institution": "Demo Credit Union", "type": "Auto Loan", "balance": 14800.00, "rate_pct": 5.1, "min_payment": 385, "notes": "synthetic demo data"},
    {"name": "Demo Student Loan", "institution": "Demo Servicer", "type": "Student Loan", "balance": 9300.00, "rate_pct": 4.5, "min_payment": 140, "notes": "synthetic demo data"},
]
DEMO_LIABILITIES_TOTAL = sum(r["balance"] for r in DEMO_LIABILITIES)
DEMO_RETIREMENT = {"current_age": 41, "target_age": 60, "annual_expenses_override": 84000, "annual_savings_override": 46000,
                   "expected_real_return_pct": 5.0, "withdrawal_rate_pct": 4.0}
DEMO_INSURANCE = {
    "income_self": 142000.0, "income_spouse": 88000.0, "children": 2, "home_value_override": 465000.0,
    "k401_pretax_pct": 10.0, "k401_match_t1_pct": 6.0, "k401_match_t1_rate": 50.0, "hsa_contribution": 3300.0,
    "roth_ira_contribution_self": 7000.0, "deposit_yield_pct": 4.2,
    "policies": [
        {"kind": "life", "owner": "self", "name": "20-year term (demo)", "coverage": 750000, "premium_annual": 620, "term_expires": "2041-06-30", "notes": "synthetic"},
        {"kind": "disability", "owner": "self", "name": "Employer long-term disability (demo)", "coverage": 6500, "premium_annual": 0, "term_expires": "", "notes": "synthetic"},
        {"kind": "umbrella", "owner": "household", "name": "Umbrella liability (demo)", "coverage": 1000000, "premium_annual": 280, "term_expires": "", "notes": "synthetic"},
    ],
    "ef_accounts": {"Emergency Fund": {"on": True}},
}
_APP = None
_FRESH = False   # True once THIS process has created or reset the base data (so the extras are simply still pending)


def register_app(mod):
    """app.py calls this at the very end of the module, once every helper the extras use has been defined."""
    global _APP
    _APP = mod


def history_rows(today=None, days=HISTORY_DAYS):
    """Deterministic synthetic daily snapshots ending YESTERDAY (the app writes today's own row). A fixed random seed
    gives the same picture after every reset; the series is rescaled so it lands near the demo's real value, which
    avoids a cliff when the first genuine snapshot arrives."""
    today = today or datetime.date.today()
    rnd = random.Random(20261109)
    start = today - datetime.timedelta(days=days)
    spy, inv, cost = 520.0, 118000.0, 99000.0
    cash0, liab0 = 21000.0, 36000.0
    raw = []
    for i in range(days):
        d = start + datetime.timedelta(days=i)
        m = 0.00045 + 0.0085 * rnd.gauss(0, 1)                       # daily market return
        spy *= (1 + m)
        inv *= (1 + 0.9 * m + 0.002 * rnd.gauss(0, 1))
        cash = cash0 + (cash0 * 0.35) * i / days                     # cash grows slowly
        if d.day in (1, 15):                                          # twice-monthly contributions
            inv += 1500.0
            cost += 1500.0
        liab = liab0 - (liab0 - DEMO_LIABILITIES_TOTAL) * i / days   # loans paid down to today's balances
        raw.append((d, inv, cost, spy, cash, liab))
    d_end, inv_e, cost_e, spy_e, cash_e, liab_e = raw[-1]
    f_inv = (HISTORY_END_NET_WORTH - cash_e + liab_e) / inv_e         # land the net worth on target
    f_spy = HISTORY_END_SPY / spy_e
    rows = []
    for d, inv, cost, spy, cash, liab in raw:
        iv = inv * f_inv
        rows.append({"date": d.isoformat(), "net_worth": round(iv + cash - liab, 2), "invested_value": round(iv, 2),
                     "spy": round(spy * f_spy, 2), "total_cost": round(cost * f_inv, 2), "cost_covered_value": round(iv * 0.92, 2)})
    return rows


def _seed_history(today=None):
    rows = history_rows(today)
    for r in rows:
        store.value_history_upsert_today(r["date"], r["net_worth"], r["invested_value"], r["spy"],
                                         r["total_cost"], r["cost_covered_value"], None)
    return len(rows)


def _seed_all(app_mod, today=None):
    """Writes every extra through the app's own helpers. Raises on any validation error so a bad seed is loud."""
    today = today or datetime.date.today()
    n = _seed_history(today)
    clean, errors = app_mod._validate_manual_liability_rows([dict(r) for r in DEMO_LIABILITIES])
    if errors:
        raise ValueError(f"demo liabilities invalid: {errors}")
    app_mod._write_manual_liability_rows(clean)
    as_of = (today - datetime.timedelta(days=45)).isoformat()
    app_mod._basis_save([{"institution": "Demo Employer Plan", "account": "401(k)", "ticker": "FXAIX", "avg_cost": 118.40,
                          "qty_at_entry": 160, "as_of": as_of, "note": "from the plan's annual statement (synthetic)"}])
    store.write_json(app_mod.RETIRE_FILE, {**app_mod._read_retire(), **DEMO_RETIREMENT})
    cfg = app_mod._read_insurance_cfg()                     # AFTER the liabilities: they stored their rates in this same file
    cfg.update(DEMO_INSURANCE)
    store.write_json(app_mod.INSURANCE_FILE, cfg)
    try:
        app_mod._cache["data"], app_mod._cache["ts"] = None, 0   # next /api/portfolio rebuilds with the new data
    except Exception:
        pass
    return {"history_days": n, "liabilities": len(clean)}


def anchor_history(today_iso, net_worth, invested_value, spy, total_cost, cost_covered_value):
    """DEMO_MODE only, called by the app just BEFORE it stores the first real snapshot of a freshly seeded demo. The synthetic
    series is a stand-in with its own price level; this rescales each series by one constant (real value / the synthetic
    value on the last synthetic day) so the real snapshot continues it exactly, whatever prices the demo sees today.
    The shape of the history is unchanged. Runs once per seed (marker); a reset re-arms it."""
    if not DEMO_MODE or not _extras_current():
        return False
    try:
        with open(ANCHOR_MARKER_FILE) as f:
            if f.read().strip() == EXTRAS_VERSION:
                return False
    except OSError:
        pass
    rows = [r for r in store.value_history_read_all() if (r.get("date") or "") < today_iso]
    if not rows:
        return False
    last = rows[-1]
    factors = {}
    for k, real in (("net_worth", net_worth), ("invested_value", invested_value), ("spy", spy),
                    ("total_cost", total_cost), ("cost_covered_value", cost_covered_value)):
        try:
            f = float(real) / float(last[k])
        except (TypeError, ValueError, ZeroDivisionError, KeyError):
            continue
        if 0.2 <= f <= 5.0:                      # ignore a nonsensical ratio (a half-loaded portfolio) rather than distort the history
            factors[k] = f
    for r in rows:
        v = {k: (round(float(r[k]) * factors[k], 2) if k in factors and r.get(k) is not None else r.get(k))
             for k in ("net_worth", "invested_value", "spy", "total_cost", "cost_covered_value")}
        store.value_history_upsert_today(r["date"], v["net_worth"], v["invested_value"], v["spy"], v["total_cost"],
                                         v["cost_covered_value"], r.get("positions"))
    try:
        with open(ANCHOR_MARKER_FILE, "w") as f:
            f.write(EXTRAS_VERSION + "\n")
    except OSError:
        pass
    print(f"[demo_seed] synthetic history anchored to today's snapshot: {sorted(factors)}", flush=True)
    return True


def _extras_current():
    try:
        with open(EXTRAS_MARKER_FILE) as f:
            return f.read().strip() == EXTRAS_VERSION
    except OSError:
        return False


def seed_extras(force=False):
    """DEMO_MODE only, and only on a directory this module already marked as a demo (same safety model as the base seed)."""
    if not DEMO_MODE:
        return {"seeded": False, "reason": "DEMO_MODE not set"}
    if _APP is None:
        return {"seeded": False, "reason": "app not registered"}
    if not _marker_exists():
        return {"seeded": False, "reason": "no demo marker file: refusing to write anything"}
    if _extras_current() and not force:
        return {"seeded": False, "reason": "extras already current"}
    if not _FRESH and not force:
        # a demo seeded by an OLDER release (no extras, or an older extras version): its base data (holdings) is stale too,
        # so start fresh instead of layering new history over old holdings. reset_and_reseed() seeds the extras itself.
        print("[demo_seed] demo data comes from an older release: starting fresh.", flush=True)
        return (reset_and_reseed() or {}).get("extras") or {"seeded": False, "reason": "reset did not seed extras"}
    try:
        res = _seed_all(_APP)
        os.makedirs(os.path.dirname(EXTRAS_MARKER_FILE) or ".", exist_ok=True)
        with open(EXTRAS_MARKER_FILE, "w") as f:
            f.write(EXTRAS_VERSION + "\n")
        print(f"[demo_seed] extras seeded: {res}", flush=True)
        return {"seeded": True, **res}
    except Exception as e:
        print(f"[demo_seed] extras seeding failed (non-fatal): {e}", flush=True)
        return {"seeded": False, "reason": f"error: {e}"}


def start_demo_reset_thread(interval_s=None):
    """Mirrors audit.start_retention_thread()/detections.start_detections_thread()'s pattern.
    No-op (returns None, starts no thread) unless DEMO_MODE=1."""
    if not DEMO_MODE:
        return None
    import threading
    import time as _time
    interval = int(interval_s or DEMO_RESET_INTERVAL_S)

    def _loop():
        while True:
            _time.sleep(seconds_until_reset(interval))
            try:
                reset_and_reseed()
            except Exception as e:
                print(f"[demo_seed] reset thread error: {e}", flush=True)

    t = threading.Thread(target=_loop, daemon=True, name="demo-reset")
    t.start()
    return t
