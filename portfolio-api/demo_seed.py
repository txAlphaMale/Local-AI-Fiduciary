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

import auth
import store
import audit
import knowledge

DEMO_MODE = os.environ.get("DEMO_MODE", "0") == "1"
DEMO_ADMIN_USERNAME = os.environ.get("DEMO_ADMIN_USERNAME", "demo")
DEMO_ADMIN_PASSWORD = os.environ.get("DEMO_ADMIN_PASSWORD", "FiduciaryDemo2026!")
DEMO_MARKER_FILE = os.environ.get("DEMO_MARKER_FILE", "/data/.demo_instance")
DEMO_RESET_INTERVAL_S = int(os.environ.get("DEMO_RESET_INTERVAL_S", str(6 * 3600)))  # 6h default

HOLDINGS_CSV = os.environ.get("HOLDINGS_CSV", "/data/holdings.csv")

# Real, liquid tickers (so Finnhub/market-data calls resolve to something real-looking) with
# invented quantities/cost-bases spread across account types, for a portfolio that exercises most
# of the dashboard's analysis surface (diversification, sector concentration, tax lots, dividends).
SYNTHETIC_HOLDINGS = [
    {"ticker": "VTI",  "name": "Vanguard Total Stock Market ETF", "quantity": 120, "cost_basis": 210.00, "account": "Brokerage",  "institution": "Demo Brokerage", "type": "ETF"},
    {"ticker": "VXUS", "name": "Vanguard Total Intl Stock ETF",   "quantity": 200, "cost_basis": 58.00,  "account": "Brokerage",  "institution": "Demo Brokerage", "type": "ETF"},
    {"ticker": "BND",  "name": "Vanguard Total Bond Market ETF",  "quantity": 150, "cost_basis": 72.00,  "account": "Brokerage",  "institution": "Demo Brokerage", "type": "ETF"},
    {"ticker": "AAPL", "name": "Apple Inc.",                      "quantity": 40,  "cost_basis": 150.00, "account": "Brokerage",  "institution": "Demo Brokerage", "type": "Stock"},
    {"ticker": "MSFT", "name": "Microsoft Corp.",                 "quantity": 25,  "cost_basis": 280.00, "account": "Brokerage",  "institution": "Demo Brokerage", "type": "Stock"},
    {"ticker": "VOO",  "name": "Vanguard S&P 500 ETF",            "quantity": 60,  "cost_basis": 380.00, "account": "Roth IRA",   "institution": "Demo Custodian",  "type": "ETF"},
    {"ticker": "QQQ",  "name": "Invesco QQQ Trust",               "quantity": 30,  "cost_basis": 330.00, "account": "Roth IRA",   "institution": "Demo Custodian",  "type": "ETF"},
    {"ticker": "VTI",  "name": "Vanguard Total Stock Market ETF", "quantity": 80,  "cost_basis": 195.00, "account": "401(k)",     "institution": "Demo Employer Plan", "type": "ETF"},
    {"ticker": "VBTLX","name": "Vanguard Total Bond Market Fund", "quantity": 500, "cost_basis": 10.20,  "account": "401(k)",     "institution": "Demo Employer Plan", "type": "Fund"},
    {"ticker": "SCHD", "name": "Schwab US Dividend Equity ETF",   "quantity": 90,  "cost_basis": 68.00,  "account": "HSA",        "institution": "Demo HSA Custodian", "type": "ETF"},
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
        # Already a seeded demo instance (e.g. a restart between reset cycles) — nothing to do.
        return {"seeded": False, "reason": "already seeded"}

    uid, _recovery_key = auth.create_user(DEMO_ADMIN_USERNAME, DEMO_ADMIN_PASSWORD, role="admin")
    _write_synthetic_holdings()
    _write_marker()
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
    result = seed_if_needed()
    print("[demo_seed] periodic reset complete — fresh demo data seeded.", flush=True)
    return {"reset": True, "reseed": result}


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
            _time.sleep(interval)
            try:
                reset_and_reseed()
            except Exception as e:
                print(f"[demo_seed] reset thread error: {e}", flush=True)

    t = threading.Thread(target=_loop, daemon=True, name="demo-reset")
    t.start()
    return t
