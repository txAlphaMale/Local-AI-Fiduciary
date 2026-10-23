# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
# ============================================================================
# store.py — Storage Stage A (LOCAL-HOSTING-HARDENING-PLAN.md §4). Consolidates the ~45 flat
# JSON config/cache files this app used to read/write directly (one `open()`/`json.load`/
# `json.dump` per file, scattered across app.py) into ONE SQLite database, via a generic
# key-value table. Mechanical by design: every existing `_read_X()`/`_write_X()` function and
# inline read/write call site keeps its name, signature, and surrounding try/except EXACTLY as
# it was — only the innermost "where do the bytes come from" swapped from a raw file to this
# store. Nothing calling those functions needed to change.
#
# 2026-07-23e (Jason) — "wasn't consolidating file based data stores into the DB on the list?
# ...let's do that now." Buys atomic writes (a SQLite transaction can't leave a half-written,
# corrupt config file the way a killed-mid-write `open(..., "w")` can) and a single-file backup
# story (copy one .db instead of tracking 45 paths). Legacy JSON files are left on disk
# untouched (never deleted) as a historical snapshot/rollback path, but are no longer written to
# once a key has migrated into the store — the store is the sole source of truth going forward.
#
# `read_json_strict(path)` is the load-bearing piece: it mimics `open(path)`'s failure mode
# EXACTLY (raises FileNotFoundError if there's nothing in the store AND nothing on disk) so
# swapping a read call site's `with open(X, encoding="utf-8") as f: ... json.load(f) ...` for
# `... read_json_strict(X) ...` changes NOTHING about the surrounding try/except's behavior —
# same exceptions, same fallback defaults, same everything, just a different data source.
# ============================================================================
import os
import sqlite3
import json
import time
import threading

import market_calendar   # trading-day aging for cached_fetch's staleness window (2026-08-08)

STORE_DB = os.environ.get("STORE_DB", "/data/store.db")

_lock = threading.Lock()   # SQLite handles its own file locking, but this keeps init_db()
                           # idempotent-safe if ever called from more than one thread at once


# ============================================================================
# Encryption at rest (2026-07-24, "All sensitive information stored needs to be encrypted") --
# every sensitive value this module persists routes through _enc()/_dec()/_dec_float() below,
# which lazily import secrets_crypto.py's shared Fernet primitives. LAZY import (inside the
# function body, not at module top) is deliberate, not an oversight: secrets_crypto.py itself
# imports `store` (for atomic_write_bytes, to persist its key file) -- a top-level `import
# secrets_crypto` here would create a circular import that breaks depending on which module
# happens to be imported first. Deferring the import until these functions actually RUN (well
# after both modules have finished loading at app startup) sidesteps that entirely.
# ============================================================================
def _enc(v):
    """Encrypts a value for storage. None passes through (nothing to encrypt); everything else
    is stringified first since Fernet only operates on str/bytes -- needed for the typed numeric
    columns (value_history etc.) as well as the already-string JSON blob columns."""
    if v is None:
        return None
    from secrets_crypto import encrypt
    return encrypt(str(v))


def _dec(v):
    """Decrypts a value that might be a Fernet token (rows written after this module started
    encrypting) or already-plain data (legacy rows from before -- a raw number in a typed SQL
    column, or a plain JSON string in the generic blob table). Never raises; a non-string input
    (already a number, already None) is returned unchanged since there's nothing to decrypt."""
    if v is None or not isinstance(v, str):
        return v
    from secrets_crypto import decrypt_or_legacy
    return decrypt_or_legacy(v)


def _dec_float(v):
    """_dec() plus a float cast for the numeric columns -- handles both a decrypted ciphertext
    (comes back as a string, e.g. "887426.35") and an untouched legacy float (already numeric,
    float() on it is a no-op)."""
    d = _dec(v)
    if d is None:
        return None
    try:
        return float(d)
    except (TypeError, ValueError):
        return d


def _enc_bytes(v):
    """Binary-safe counterpart to _enc() -- for knowledge_docs.content, which is arbitrary raw
    file bytes (PDF/DOCX/etc), not text. Encrypting it via _enc()'s str()-then-encode path would
    mangle non-UTF8 bytes, so this routes through secrets_crypto's bytes-native encrypt_bytes()
    instead."""
    if v is None:
        return None
    from secrets_crypto import encrypt_bytes
    return encrypt_bytes(v)


def _dec_bytes(v):
    """_dec()'s binary counterpart -- decrypts knowledge_docs.content, falling back to the raw
    bytes as-is if they're legacy (pre-encryption) plaintext. Never raises."""
    if v is None:
        return None
    from secrets_crypto import decrypt_bytes_or_legacy
    return decrypt_bytes_or_legacy(v)


# ============================================================================
# Atomic flat-file writes (CLOUD-PORTABILITY-ROADMAP.md Phase 2, 2026-07-24) -- for the handful
# of files that are STILL plain files by design rather than SQLite rows (the session-secret
# bootstrap, the per-ticker price-history cache, TLS pending-CSR scratch files): write to a
# `.tmp` sibling then `os.replace()` it into place, so a killed-mid-write process can never leave
# a half-written, corrupt file where a good (or absent) one was expected. Cheap insurance the
# roadmap explicitly calls out as worth doing regardless of whether/when a cloud move happens --
# rolling/restarted deployments make a torn write a real possibility, not just a theoretical one.
# ============================================================================
def atomic_write_text(path, text, mode=None):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def atomic_write_bytes(path, data, mode=None):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def _conn():
    os.makedirs(os.path.dirname(STORE_DB) or ".", exist_ok=True)
    c = sqlite3.connect(STORE_DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _ensure_column(c, table, col, decl):
    """Idempotent ALTER TABLE ADD COLUMN -- SQLite has no 'ADD COLUMN IF NOT EXISTS', so check
    PRAGMA table_info first. Safe to call every boot: no-ops once the column exists. Used for
    additive schema changes to tables that pre-date this helper (2026-07-30, Strategist GOALS)
    rather than reproducing the CREATE TABLE IF NOT EXISTS pattern, which only helps brand-new
    installs -- an existing table's shape is frozen the first time CREATE TABLE IF NOT EXISTS runs
    against it, so a new column needs an explicit ALTER on every pre-existing database."""
    cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
    if col not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db():
    with _lock:
        with _conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS app_state(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )""")
            # ---- Storage Stage B (2026-07-24) ----
            c.execute("""CREATE TABLE IF NOT EXISTS value_history(
                date TEXT PRIMARY KEY,
                net_worth REAL,
                invested_value REAL,
                spy REAL,
                funded_ratio REAL
            )""")
            # 2026-10-23: cost-basis history + completeness metadata. total_cost = sum of KNOWN cost bases; cost_covered_value = the
            # market value of exactly those lines (so value-vs-cost compares like with like); positions = line count that day (used to
            # reject a half-loaded snapshot); excluded = 1 hides a row proven bad (a one-day dip from an incomplete load) from every reader.
            for _col, _decl in (("total_cost", "REAL"), ("cost_covered_value", "REAL"), ("positions", "REAL"),
                                ("excluded", "INTEGER DEFAULT 0")):
                _ensure_column(c, "value_history", _col, _decl)
            c.execute("""CREATE TABLE IF NOT EXISTS strategist_runs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                scenario_id TEXT,
                scenario_name TEXT,
                risk_profile TEXT,
                max_iter INTEGER,
                iterations INTEGER,
                tool_calls_json TEXT,
                repeat_calls INTEGER,
                trades INTEGER,
                search_calls INTEGER,
                search_budget INTEGER,
                nudged INTEGER,
                converged INTEGER,
                ended TEXT,
                error TEXT,
                duration_s REAL
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_strategist_runs_ts ON strategist_runs(ts)")
            c.execute("""CREATE TABLE IF NOT EXISTS paper_transactions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scenario_id TEXT NOT NULL,
                ts INTEGER NOT NULL,
                action TEXT,
                ticker TEXT,
                shares REAL,
                price REAL,
                value REAL,
                recurring INTEGER NOT NULL DEFAULT 0
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_paper_tx_scenario_ts ON paper_transactions(scenario_id, ts)")
            c.execute("""CREATE TABLE IF NOT EXISTS paper_reports(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scenario_id TEXT NOT NULL,
                ts INTEGER NOT NULL,
                total_value REAL,
                return_pct REAL,
                summary TEXT
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_paper_reports_scenario_ts ON paper_reports(scenario_id, ts)")
            # 2026-07-30 (Strategist GOALS) -- snapshot of the goal parameters ACTUALLY active when
            # this report was written, so a report stays interpretable after the scenario's goal is
            # later edited. Encrypted (same sensitivity class as summary/total_value above).
            _ensure_column(c, "paper_reports", "goal_snapshot", "TEXT")
            _ensure_column(c, "strategist_runs", "goal_snapshot", "TEXT")
            # ---- Knowledge/RAG DB-backed storage (2026-07-24h) ----
            c.execute("""CREATE TABLE IF NOT EXISTS knowledge_docs(
                hash TEXT PRIMARY KEY,
                filename TEXT,
                size INTEGER,
                imported_at INTEGER,
                content BLOB
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS knowledge_chunks(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_hash TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL,
                vec TEXT NOT NULL,
                model TEXT NOT NULL
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_doc ON knowledge_chunks(doc_hash)")
            c.execute("""CREATE TABLE IF NOT EXISTS knowledge_paths(
                relpath TEXT PRIMARY KEY,
                hash TEXT,
                mtime INTEGER,
                size INTEGER
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS knowledge_removed(
                hash TEXT PRIMARY KEY,
                filename TEXT,
                removed_at INTEGER NOT NULL
            )""")
            # ---- external_cache (2026-07-30, "last-known-good cache" backlog item, Part 2) ----
            # Generic disk-persisted cache for EVERY external data dependency (EDGAR, Finnhub,
            # Tavily, ...), replacing the plain in-memory module-level dicts each one used to keep
            # separately. Two things that couldn't be fixed with an in-memory dict alone:
            # (1) survives a container restart/redeploy (common during active development, per
            # RESUME-TRACKER's own log of rebuild cycles) -- previously "last known good" only
            # lasted as long as the process did; (2) one shared success/failure discipline instead
            # of six near-identical hand-rolled dicts each getting it slightly differently (Part 1,
            # 2026-07-30 earlier this session, already fixed the worst instance of this
            # inconsistency -- this table is the generalization that was deferred at the time).
            # `source` is a short tag (e.g. "edgar_fundamentals", "tavily"), `key` is whatever that
            # source uses to distinguish cache entries (a ticker, a search query) -- composite
            # primary key so ON CONFLICT can upsert per (source, key) pair.
            c.execute("""CREATE TABLE IF NOT EXISTS external_cache(
                source TEXT NOT NULL,
                key TEXT NOT NULL,
                fetched_at INTEGER NOT NULL,
                value TEXT,
                PRIMARY KEY(source, key)
            )""")
        try:
            os.chmod(STORE_DB, 0o600)
        except Exception:
            pass


# ---- external_cache: generic disk-persisted last-known-good cache, typed read/write API ----
# 2026-07-30: `value` is ENCRYPTED (JSON-encoded then Fernet-encrypted) -- these are third-party
# API responses (company financials, valuation multiples, news snippets), not secrets in the
# credential sense, but still real financial-research detail worth protecting at rest, matching
# this app's blanket "everything sensitive is encrypted" posture established 2026-07-24.
def ext_cache_get(source, key):
    """Returns (fetched_at, value) for the most recent successful fetch of this (source, key), or
    None if nothing has ever been cached. Callers decide for themselves whether the age is
    acceptable to serve as a fallback -- this function just returns what's on disk, no TTL logic
    here (that stays in each caller, same as before -- this table is the FALLBACK tier, not a
    replacement for each call site's own in-memory TTL check)."""
    with _conn() as c:
        row = c.execute("SELECT fetched_at, value FROM external_cache WHERE source=? AND key=?",
                        (source, key)).fetchone()
    if not row:
        return None
    try:
        return row["fetched_at"], json.loads(_dec(row["value"]))
    except Exception:
        return None


def ext_cache_set(source, key, value, fetched_at=None):
    """Upserts the last-known-good value for (source, key). Only ever called on a SUCCESSFUL
    fetch (callers are responsible for that -- this function has no concept of success/failure,
    it just persists whatever it's given)."""
    fetched_at = int(fetched_at) if fetched_at is not None else int(time.time())
    with _conn() as c:
        c.execute("""INSERT INTO external_cache(source,key,fetched_at,value) VALUES(?,?,?,?)
                     ON CONFLICT(source,key) DO UPDATE SET fetched_at=excluded.fetched_at, value=excluded.value""",
                  (source, key, fetched_at, _enc(json.dumps(value))))


# ---- cached_fetch: the shared two-tier helper every external-data call site converts to ----
# 2026-07-30. In-memory hot cache (fast, TTL-gated, cleared on restart) backed by external_cache
# (disk, survives restart). This is the ONE reusable pattern the "last-known-good cache" backlog
# item's Part 1 (earlier this session) explicitly deferred generalizing -- Part 1 hand-fixed
# EDGAR/Finnhub's failure-overwrite bug per call site; this replaces those hand-fixes with one
# shared implementation plus the disk-persistence Part 1 didn't attempt.
_MEM_CACHE = {}   # (source, key) -> (fetched_at, value)


# 2026-08-08 staleness rework. The 2026-07-30 contract equated "this call fell back to the
# last-known-good copy" with "the user is reading stale data", and equated "the source returned
# nothing" with "the source failed". Both were wrong often enough that the RESEARCH -> BRIEFS
# banner sat at 26-of-26 permanently and stopped carrying information:
#
#   * FALLING BACK IS NOT THE SAME AS BEING OUT OF DATE. EDGAR fundamentals are QUARTERLY. A
#     403 on today's refresh, falling back to a copy pulled 20 hours ago, leaves the brief
#     reading exactly the numbers a successful fetch would have returned. Flagging that red
#     teaches the reader to ignore the flag, which costs them the one case that matters.
#     Staleness is now measured against `stale_after` -- how long THIS dataset stays true --
#     not against whether one HTTP call happened to work.
#   * "NOTHING TO REPORT" IS A RESULT, NOT AN OUTAGE. An ETF has no SEC company facts and a
#     quiet week has no news; both used to be scored as fetch failures, so those rows were
#     flagged forever and could never clear. `is_empty` classifies them as successes.
#
# The third state matters just as much and is NEW here: a source that errored with NOTHING
# cached to fall back on is not stale, it is ABSENT -- the brief was built without that input
# entirely. Suppressing that under the narrower stale rule would have re-opened exactly the
# blind spot this provenance work was built to close, so it gets its own `error` flag and its
# own banner rather than being folded into either bucket.
_STALE_AFTER_DEFAULT_FLOOR = 86400      # never call a fallback stale until it has missed a full day


def cached_fetch(source, key, ttl, compute_fn, is_ok=None, is_empty=None, stale_after=None):
    """`compute_fn()` performs the fresh fetch (must not raise -- every current caller already
    catches its own fetch exceptions internally and returns a shaped failure value instead, same
    contract as before this helper existed).

    Classification, in order:
      `is_empty(value)` -- the source answered correctly and the answer is "there is nothing
        here" (no SEC filings for an ETF, no news this week). A SUCCESS: cached, never flagged.
      `is_ok(value)`    -- the fetch produced usable data (default: anything is a success).
      otherwise         -- the fetch FAILED; fall back to the last-known-good copy if one exists.

    `ttl` gates the in-memory hot cache (how often we re-ask). `stale_after` gates the honesty
    flag (how long the data stays true once we cannot re-ask) and should reflect the dataset's
    real cadence -- they are different questions and conflating them is what produced the
    permanently-lit banner. Defaults to max(2*ttl, 1 day).

    `stale_after` is measured in TRADING TIME, not wall-clock (2026-08-08, market_calendar):
    weekend and NYSE-holiday seconds do not age the data, because no fresher value exists while
    the market is shut. A Friday-evening pull read on Saturday morning is 6 hours old, not 15.

    Returns (value, meta), meta =
      {"fetched_at": epoch secs or None,   # when the returned value was actually retrieved
       "age":        secs or None,         # trading-time age -- what `stale` is judged on
       "wall_age":   secs or None,         # plain elapsed time, for display
       "stale":      bool,                 # fell back to a copy older than `stale_after`
       "fallback":   bool,                 # fell back at all (stale or not) -- informational
       "empty":      bool,                 # a valid "nothing here" answer
       "error":      bool}                 # fetch failed AND nothing was cached to fall back on
    """
    is_ok = is_ok or (lambda v: True)
    now = time.time()
    if stale_after is None:
        stale_after = max(2 * ttl, _STALE_AFTER_DEFAULT_FLOOR)

    def _meta(fetched_at, stale=False, fallback=False, empty=False, error=False):
        m = {"fetched_at": fetched_at, "stale": stale, "fallback": fallback,
             "empty": empty, "error": error, "age": None, "wall_age": None}
        if fetched_at is not None:
            m["wall_age"] = max(0.0, now - fetched_at)
            m["age"] = market_calendar.trading_seconds_between(fetched_at, now)
        return m

    hit = _MEM_CACHE.get((source, key))
    if hit and (now - hit[0] < ttl):
        return hit[1], _meta(hit[0])
    res = compute_fn()
    if (is_empty and is_empty(res)) or is_ok(res):
        empty = bool(is_empty and is_empty(res))
        _MEM_CACHE[(source, key)] = (now, res)
        ext_cache_set(source, key, res, now)
        return res, _meta(now, empty=empty)
    # failure this call -- fall back to the last-known-good: memory first (cheaper, this
    # process), then disk (survives a restart -- the whole point of Part 2).
    if not hit:
        disk = ext_cache_get(source, key)
        if disk:
            hit = (disk[0], disk[1])
            _MEM_CACHE[(source, key)] = hit   # rehydrate so this process's later calls skip the disk hit
    if hit:
        aged = market_calendar.trading_seconds_between(hit[0], now) > stale_after
        return hit[1], _meta(hit[0], stale=aged, fallback=True)
    # nothing has ever succeeded for this (source, key), anywhere -- absent, not stale.
    return res, _meta(None, error=True)


# ============================================================================
# Storage Stage B (2026-07-24, LOCAL-HOSTING-HARDENING-PLAN.md, "promote genuinely time-series
# data to real SQLite tables instead of growing JSON arrays with hand-rolled caps"). Covers 3
# datasets that used to live inside Stage A's generic JSON-blob-per-key model:
#   - value_history.json      -> value_history table
#   - strategist_runs.jsonl   -> strategist_runs table
#   - paper.json's per-scenario "transactions"/"reports" arrays -> paper_transactions/paper_reports
#
# Each dataset used a hand-rolled cap purely to bound the blob's parse/serialize cost on every
# read/write (`hist[-2500:]`, `lines[-STRAT_RUN_LOG_MAX:]`, `reports[-26:]`) -- a real table has
# no such cost, so Stage B REMOVES those caps entirely rather than reproducing them as SQL
# LIMITs on write. History that used to be silently discarded is now kept forever; anything that
# only ever wanted "the last N" for display gets that via `ORDER BY ... LIMIT N` at READ time.
#
# Migration is one-time and additive, triggered explicitly by app.py right after each dataset's
# legacy *_FILE path constant is defined (not from inside init_db(), since PAPER_FILE/
# VALUE_HISTORY_FILE/STRAT_RUN_LOG_FILE are all defined well AFTER init_db() already runs at
# app.py's top -- see each ensure_*_migrated() call site in app.py). Each check is "is the new
# table still empty AND does the old Stage-A blob key exist?" -- safe to call on a brand-new
# install (nothing to import, table just starts empty) or a long-lived one (imports once,
# no-ops every time after). The old blob key is left untouched afterward, same "never delete the
# old copy" caution Stage A itself used for the original JSON files.
# ============================================================================
def ensure_value_history_migrated(legacy_path):
    with _conn() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM value_history").fetchone()["n"]
        if n:
            return
        row = c.execute("SELECT value FROM app_state WHERE key=?", (_key_for(legacy_path),)).fetchone()
        if not row:
            return
        for d in (json.loads(_dec(row["value"])) or []):   # 2026-07-24: decrypt the legacy blob first
            c.execute("""INSERT OR REPLACE INTO value_history(date,net_worth,invested_value,spy,funded_ratio)
                         VALUES(?,?,?,?,?)""",
                      (d.get("date"), _enc(d.get("net_worth")), _enc(d.get("invested_value")),
                       _enc(d.get("spy")), _enc(d.get("funded_ratio"))))


def ensure_strategist_runs_migrated(legacy_path):
    with _conn() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM strategist_runs").fetchone()["n"]
        if n:
            return
        row = c.execute("SELECT value FROM app_state WHERE key=?", (_key_for(legacy_path),)).fetchone()
        if not row:
            return
        for line in (json.loads(_dec(row["value"])) or []):   # 2026-07-24: decrypt the legacy blob first
            if not line or not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            c.execute("""INSERT INTO strategist_runs(ts,scenario_id,scenario_name,risk_profile,max_iter,
                            iterations,tool_calls_json,repeat_calls,trades,search_calls,search_budget,
                            nudged,converged,ended,error,duration_s)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (rec.get("ts"), rec.get("scenario_id"), _enc(rec.get("scenario_name")), rec.get("risk_profile"),
                       rec.get("max_iter"), rec.get("iterations"), _enc(json.dumps(rec.get("tool_calls") or {})),
                       rec.get("repeat_calls"), rec.get("trades"), rec.get("search_calls"), rec.get("search_budget"),
                       int(bool(rec.get("nudged"))), int(bool(rec.get("converged"))), rec.get("ended"),
                       _enc(rec.get("error")), rec.get("duration_s")))


def ensure_paper_migrated(legacy_path):
    """Handles BOTH shapes the paper.json blob might still be in at the moment this runs
    (app startup, before the app's own `_read_registry()`-level flat-ledger migration has had a
    chance to run against a live request): the current multi-scenario `{"scenarios": {sid: {...,
    "transactions": [...], "reports": [...]}}}` shape (expected -- this is what any deployment
    that's already run the 2026-07-23 multi-scenario migration will have), or -- defensively, for
    a very old/never-yet-reloaded install -- a flat pre-multi-scenario ledger with `transactions`/
    `reports` sitting at the TOP level with no "scenarios" wrapper at all, treated as one implicit
    "default" scenario (matching `_read_registry()`'s own legacy-migration id/naming)."""
    with _conn() as c:
        n_tx = c.execute("SELECT COUNT(*) AS n FROM paper_transactions").fetchone()["n"]
        n_rep = c.execute("SELECT COUNT(*) AS n FROM paper_reports").fetchone()["n"]
        if n_tx or n_rep:
            return
        row = c.execute("SELECT value FROM app_state WHERE key=?", (_key_for(legacy_path),)).fetchone()
        if not row:
            return
        reg = json.loads(_dec(row["value"])) or {}   # 2026-07-24: decrypt the legacy blob first
        scenarios = reg.get("scenarios")
        if not isinstance(scenarios, dict) or not scenarios:
            scenarios = {"default": reg} if (reg.get("transactions") or reg.get("reports")) else {}
        for sid, scen in scenarios.items():
            for tx in (scen.get("transactions") or []):
                c.execute("""INSERT INTO paper_transactions(scenario_id,ts,action,ticker,shares,price,value,recurring)
                             VALUES(?,?,?,?,?,?,?,?)""",
                          (sid, tx.get("ts"), tx.get("action"), _enc(tx.get("ticker")), _enc(tx.get("shares")),
                           _enc(tx.get("price")), _enc(tx.get("value")), int(bool(tx.get("recurring")))))
            for rep in (scen.get("reports") or []):
                c.execute("""INSERT INTO paper_reports(scenario_id,ts,total_value,return_pct,summary)
                             VALUES(?,?,?,?,?)""",
                          (sid, rep.get("ts"), _enc(rep.get("total_value")), _enc(rep.get("return_pct")),
                           _enc(rep.get("summary"))))


# ---- value_history: typed read/write API (replaces raw read_json_strict/write_json call sites) ----
# 2026-07-24: net_worth/invested_value/spy/funded_ratio are ENCRYPTED (this is a real net-worth
# time series -- one of the more sensitive tables in the app). `date` stays plaintext -- it's the
# primary key and used in every WHERE/ORDER BY, and a bare date with no dollar figure attached
# isn't independently sensitive.
def value_history_read_all(include_excluded=False):
    """Ascending by date -- same shape (list of dicts: date/net_worth/invested_value/spy/
    funded_ratio, plus total_cost/cost_covered_value/positions and `excluded`) every existing consumer (_risk_metrics, _vol_regime_signal,
    performance.py, the Portfolio-vs-SPY chart) already expects. Rows marked excluded (proven-bad snapshots) are hidden unless asked for."""
    with _conn() as c:
        rows = c.execute("SELECT date, net_worth, invested_value, spy, funded_ratio, total_cost, cost_covered_value, positions, excluded "
                         "FROM value_history ORDER BY date ASC").fetchall()
    out = []
    for r in rows:
        if r["excluded"] and not include_excluded:
            continue
        out.append({"date": r["date"], "net_worth": _dec_float(r["net_worth"]),
                    "invested_value": _dec_float(r["invested_value"]), "spy": _dec_float(r["spy"]),
                    "funded_ratio": _dec_float(r["funded_ratio"]), "total_cost": _dec_float(r["total_cost"]),
                    "cost_covered_value": _dec_float(r["cost_covered_value"]), "positions": _dec_float(r["positions"]),
                    "excluded": bool(r["excluded"])})
    return out


def value_history_exclude(dates, flag=1):
    """Mark rows excluded (flag=1) or restore them (flag=0). Never deletes -- reversible."""
    with _conn() as c:
        for d in dates:
            c.execute("UPDATE value_history SET excluded=? WHERE date=?", (1 if flag else 0, d))


def value_history_today_spy(date):
    """The SPY close already recorded for `date`, if any -- lets the caller skip re-fetching the
    benchmark price when today's row already has one (mirrors the old in-memory `hist[-1]` check
    without needing to load the whole history just to look at one row)."""
    with _conn() as c:
        row = c.execute("SELECT spy FROM value_history WHERE date=?", (date,)).fetchone()
    return _dec_float(row["spy"]) if row else None


def value_history_upsert_today(date, net_worth, invested_value, spy, total_cost=None, cost_covered_value=None, positions=None):
    """Insert today's row, or overwrite it in place if called again the same day -- same
    idempotent-per-day behavior as the original `hist[-1]=row if same date else append`."""
    with _conn() as c:
        c.execute("""INSERT INTO value_history(date, net_worth, invested_value, spy, total_cost, cost_covered_value, positions)
                     VALUES(?,?,?,?,?,?,?)
                     ON CONFLICT(date) DO UPDATE SET net_worth=excluded.net_worth,
                        invested_value=excluded.invested_value, spy=excluded.spy, total_cost=excluded.total_cost,
                        cost_covered_value=excluded.cost_covered_value, positions=excluded.positions""",
                  (date, _enc(net_worth), _enc(invested_value), _enc(spy), _enc(total_cost), _enc(cost_covered_value), _enc(positions)))


def value_history_set_funded_ratio(date, funded_ratio):
    """UPDATE-only, never INSERTs -- mirrors the original's "only patch if today's row already
    exists" guard (a funded-ratio snapshot with no matching value-snapshot row silently no-ops,
    same as before)."""
    with _conn() as c:
        c.execute("UPDATE value_history SET funded_ratio=? WHERE date=?", (_enc(funded_ratio), date))


# ---- strategist_runs: typed read/write API ----
# 2026-07-24: scenario_name/tool_calls_json/error are ENCRYPTED (free-text/detail fields that can
# carry real financial specifics via whatever the Strategist actually looked at or said). The
# small enum/numeric telemetry columns (risk_profile, max_iter, iterations, counts, nudged/
# converged flags, duration_s) stay plaintext -- low individual sensitivity, and ts/scenario_id
# need to stay queryable/orderable.
def strategist_run_log(rec):
    """Appends one telemetry record. No cap in storage (Stage B removes the old
    STRAT_RUN_LOG_MAX-based trim entirely) -- `limit` at read time is now a pure display/API
    cap, never a data-loss one. `goal_snapshot` (2026-07-30, Strategist GOALS): the goal-parameter
    dict active for this run, JSON-encoded then encrypted -- same rationale as paper_reports'
    column of the same name."""
    gs = rec.get("goal_snapshot")
    with _conn() as c:
        c.execute("""INSERT INTO strategist_runs(ts,scenario_id,scenario_name,risk_profile,max_iter,
                        iterations,tool_calls_json,repeat_calls,trades,search_calls,search_budget,
                        nudged,converged,ended,error,duration_s,goal_snapshot)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (rec.get("ts"), rec.get("scenario_id"), _enc(rec.get("scenario_name")), rec.get("risk_profile"),
                   rec.get("max_iter"), rec.get("iterations"), _enc(json.dumps(rec.get("tool_calls") or {})),
                   rec.get("repeat_calls"), rec.get("trades"), rec.get("search_calls"), rec.get("search_budget"),
                   int(bool(rec.get("nudged"))), int(bool(rec.get("converged"))), rec.get("ended"),
                   _enc(rec.get("error")), rec.get("duration_s"),
                   _enc(json.dumps(gs)) if gs is not None else None))


def strategist_runs_read(limit=None):
    """Newest-first (matches the old `rows[-N:][::-1]` read-side order). Omit `limit` for the
    entire history."""
    q = "SELECT * FROM strategist_runs ORDER BY ts DESC, id DESC"
    args = ()
    if limit:
        q += " LIMIT ?"
        args = (limit,)
    with _conn() as c:
        rows = [dict(r) for r in c.execute(q, args).fetchall()]
    for r in rows:
        r["scenario_name"] = _dec(r.get("scenario_name"))
        r["error"] = _dec(r.get("error"))
        r["tool_calls"] = json.loads(_dec(r.pop("tool_calls_json")) or "{}")
        r["nudged"] = bool(r["nudged"])
        r["converged"] = bool(r["converged"])
        gs_raw = r.get("goal_snapshot")
        try:
            r["goal_snapshot"] = json.loads(_dec(gs_raw)) if gs_raw is not None else None
        except Exception:
            r["goal_snapshot"] = None
    return rows


# ---- paper_transactions / paper_reports: typed read/write API, scoped by scenario_id ----
# 2026-07-24: ticker/shares/price/value are ENCRYPTED -- even though this is a hypothetical paper
# ledger (no real money moves), the actual dollar amounts and reasoning reflected in trade
# specifics are still real financial detail worth protecting. action/recurring/ts/scenario_id
# stay plaintext (query/order columns, low individual sensitivity).
def paper_tx_append(scenario_id, ts, action, ticker, shares, price, value, recurring=False):
    with _conn() as c:
        c.execute("""INSERT INTO paper_transactions(scenario_id,ts,action,ticker,shares,price,value,recurring)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (scenario_id, ts, action, _enc(ticker), _enc(shares), _enc(price), _enc(value),
                   int(bool(recurring))))


def _paper_tx_row(r):
    d = {"ts": r["ts"], "action": r["action"], "ticker": _dec(r["ticker"]), "shares": _dec_float(r["shares"]),
         "price": _dec_float(r["price"]), "value": _dec_float(r["value"])}
    if r["recurring"]:
        d["recurring"] = True   # OPTIONAL key, only present when true -- matches the original JSON shape exactly
    return d


def paper_tx_all(scenario_id):
    """Full history, oldest-first -- for consumers that need the complete timeline (money-weighted
    IRR's deposit cash-flows, the decision-backtest's buy history), not just a display window."""
    with _conn() as c:
        rows = c.execute("SELECT ts,action,ticker,shares,price,value,recurring FROM paper_transactions "
                         "WHERE scenario_id=? ORDER BY ts ASC, id ASC", (scenario_id,)).fetchall()
    return [_paper_tx_row(r) for r in rows]


def paper_tx_recent(scenario_id, limit=40):
    """Last `limit` transactions, oldest-first among that window -- same shape/order as the old
    `(p.get("transactions") or [])[-40:]` display truncation."""
    with _conn() as c:
        rows = c.execute("SELECT ts,action,ticker,shares,price,value,recurring FROM paper_transactions "
                         "WHERE scenario_id=? ORDER BY ts DESC, id DESC LIMIT ?", (scenario_id, limit)).fetchall()
    return [_paper_tx_row(r) for r in rows][::-1]


# 2026-07-24: total_value/return_pct/summary are ENCRYPTED (real dollar figures + LLM-written
# commentary that can reference actual holdings/reasoning).
def paper_report_append(scenario_id, ts, total_value, return_pct, summary, goal_snapshot=None):
    """`goal_snapshot` (2026-07-30, Strategist GOALS): the scenario's goal-parameter dict at the
    moment this report was logged, JSON-encoded then encrypted -- lets a report stay interpretable
    even if the scenario's goal is edited later. None for reports logged before this existed, or
    for a caller that doesn't pass one (kept optional so existing call sites need no change)."""
    with _conn() as c:
        c.execute("INSERT INTO paper_reports(scenario_id,ts,total_value,return_pct,summary,goal_snapshot) "
                  "VALUES(?,?,?,?,?,?)",
                  (scenario_id, ts, _enc(total_value), _enc(return_pct), _enc(summary),
                   _enc(json.dumps(goal_snapshot)) if goal_snapshot is not None else None))


def _paper_report_row(r):
    gs = None
    raw = r["goal_snapshot"] if "goal_snapshot" in r.keys() else None
    if raw is not None:
        try:
            gs = json.loads(_dec(raw))
        except Exception:
            gs = None
    return {"ts": r["ts"], "total_value": _dec_float(r["total_value"]),
            "return_pct": _dec_float(r["return_pct"]), "summary": _dec(r["summary"]), "goal_snapshot": gs}


def paper_reports_all(scenario_id):
    with _conn() as c:
        rows = c.execute("SELECT ts,total_value,return_pct,summary,goal_snapshot FROM paper_reports "
                         "WHERE scenario_id=? ORDER BY ts ASC, id ASC", (scenario_id,)).fetchall()
    return [_paper_report_row(r) for r in rows]


def paper_reports_recent(scenario_id, limit=12):
    with _conn() as c:
        rows = c.execute("SELECT ts,total_value,return_pct,summary,goal_snapshot FROM paper_reports "
                         "WHERE scenario_id=? ORDER BY ts DESC, id DESC LIMIT ?", (scenario_id, limit)).fetchall()
    out = [_paper_report_row(r) for r in rows]
    return out[::-1]


def paper_reports_count(scenario_id):
    with _conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM paper_reports WHERE scenario_id=?", (scenario_id,)).fetchone()["n"]


def paper_scenario_wipe(scenario_id):
    """Deletes ALL transactions/reports for one scenario -- used wherever a scenario's history is
    replaced (reset, seed) or the scenario itself is permanently deleted. Without this, a
    deleted/reset scenario_id's old rows would sit as harmless-but-permanent orphaned garbage
    (nothing ever queries them again since no scenario references that id) rather than actually
    being cleaned up."""
    with _conn() as c:
        c.execute("DELETE FROM paper_transactions WHERE scenario_id=?", (scenario_id,))
        c.execute("DELETE FROM paper_reports WHERE scenario_id=?", (scenario_id,))


def _key_for(path):
    """The store key is the file's basename (e.g. '/data/settings.json' -> 'settings.json') —
    stable, human-readable in the DB, and every *_FILE default in app.py already resolves to a
    unique basename under /data/, so collisions aren't a practical concern. If a deployment ever
    overrides two different *_FILE env vars to the SAME basename in different directories,
    they'd collide here — not a configuration anyone has, and cheap to special-case later if it
    ever comes up."""
    return os.path.basename(path)


def read_json_strict(path):
    """Returns the parsed JSON for `path`'s key. Checks the store first; if the key isn't there
    yet, falls back to reading the legacy on-disk file ONCE (migrating it into the store so this
    fallback is never needed again for that key), and raises FileNotFoundError — exactly what
    `open(path)` itself would do — if there's genuinely nothing in either place. Never partially
    succeeds: either you get valid parsed JSON, or an exception, same as the `open()` + `json.load()`
    pair it replaces.

    2026-07-24: the stored value is decrypted here (transparently, with a legacy-plaintext
    fallback via _dec() -- every one of the ~44 files behind this function, incl. real financial
    data like retirement.json/insurance.json/accounts.json, was plaintext in the DB before this)."""
    key = _key_for(path)
    with _conn() as c:
        row = c.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
    if row is not None:
        return json.loads(_dec(row["value"]))
    with open(path, encoding="utf-8") as f:   # legacy fallback -- first read after migrating TO this store
        data = json.load(f)
    write_json(path, data)   # persist into the store (encrypted) so next time skips straight to the row above
    return data


def read_json(path, default=None):
    """Convenience wrapper for NEW call sites that don't want a try/except of their own —
    returns `default` instead of raising. Existing migrated call sites all use
    read_json_strict() + their OWN pre-existing try/except instead, to preserve their exact
    original fallback behavior rather than centralizing it here."""
    try:
        return read_json_strict(path)
    except Exception:
        return default


def write_json(path, data):
    """Writes `data` (anything json-serializable) under `path`'s key. This is now the ONLY
    place that persists it — the legacy file at `path` is deliberately left untouched (not
    updated, not deleted), so it stays as a frozen snapshot of whatever it last held.

    2026-07-24: encrypted at rest (_enc()) -- the whole serialized blob, not per-field, mirroring
    exactly how app.py's SECRET_SPECS values already worked before this was generalized to every
    other file behind this function. (SECRETS_FILE specifically ends up double-encrypted -- each
    value individually by secrets_crypto in app.py, then this whole outer blob again here --
    which is harmless, just slightly redundant for that one file, in exchange for not needing a
    special case to skip it.)"""
    key = _key_for(path)
    value = _enc(json.dumps(data, ensure_ascii=False))
    now = int(time.time())
    with _conn() as c:
        c.execute("""INSERT INTO app_state(key, value, updated_at) VALUES(?,?,?)
                     ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                  (key, value, now))


def read_lines_strict(path):
    """For the two append-log files (VERIFIER_LOG_FILE, STRAT_RUN_LOG_FILE) that store a capped
    list of independent JSON-line strings rather than one JSON blob — same store, represented as
    a JSON array of strings under one key instead of a text file read via splitlines(). Mirrors
    open(path).read().splitlines() -- raises FileNotFoundError if nothing exists anywhere yet."""
    key = _key_for(path)
    with _conn() as c:
        row = c.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
    if row is not None:
        return json.loads(_dec(row["value"]))
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    write_lines(path, lines)
    return lines


def write_lines(path, lines):
    key = _key_for(path)
    value = _enc(json.dumps(list(lines), ensure_ascii=False))
    now = int(time.time())
    with _conn() as c:
        c.execute("""INSERT INTO app_state(key, value, updated_at) VALUES(?,?,?)
                     ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                  (key, value, now))


def migrated_keys():
    """For diagnostics/verification only — every key currently in the store."""
    with _conn() as c:
        return sorted(r["key"] for r in c.execute("SELECT key FROM app_state").fetchall())


# ============================================================================
# Knowledge/RAG DB-backed storage (2026-07-24h, LOCAL-HOSTING-HARDENING-PLAN.md, design
# finalized 2026-07-23g). Replaces `knowledge.py`'s old flat `/data/knowledge_index.json` blob
# (path+mtime/size keyed) with 4 real tables, content-hash (SHA-256) keyed:
#   - knowledge_docs     -- one row per unique document CONTENT (the hash is the primary key,
#                           not the filename) — raw bytes stored as a BLOB so re-embedding never
#                           needs the host folder to still have the file, and so a future
#                           cloud-packaged deployment with no host filesystem access still works.
#   - knowledge_chunks   -- the embedded chunks for a doc_hash (text + JSON-encoded vector +
#                           the embed model used, so a model change can be detected per-chunk).
#   - knowledge_paths    -- CURRENT filesystem state: which relpath currently points at which
#                           hash, with the mtime/size last seen there. Lets a rename (same
#                           content, new path) skip a re-embed (hash unchanged) while a genuine
#                           content edit (new hash under the same path) is still detected.
#   - knowledge_removed  -- retention of hashes explicitly removed via the new in-app delete
#                           action, so a reappearance of that same content on a later reindex
#                           can be flagged as "reimported" instead of silently re-added.
# The DB is now the source of truth for what's SEARCHABLE — the (still read-only) `/knowledge`
# folder is only the INTAKE mechanism `knowledge.py`'s reindex scans to feed imports in. Deleting
# a file from the folder no longer removes it from the index by itself (a deliberate behavior
# change from the old design) — only the explicit in-app removal action does that.
# ============================================================================
def knowledge_doc_get(h):
    with _conn() as c:
        row = c.execute("SELECT hash, filename, size, imported_at FROM knowledge_docs WHERE hash=?",
                        (h,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["filename"] = _dec(d["filename"])
    return d


def knowledge_doc_get_content(h):
    """The raw imported bytes for a doc — used to re-extract/re-chunk without needing the file
    to still exist on disk (e.g. after a model change forces a re-embed)."""
    with _conn() as c:
        row = c.execute("SELECT content FROM knowledge_docs WHERE hash=?", (h,)).fetchone()
    return _dec_bytes(row["content"]) if row else None


def knowledge_doc_upsert(h, filename, size, imported_at, content):
    with _conn() as c:
        c.execute("""INSERT INTO knowledge_docs(hash, filename, size, imported_at, content)
                     VALUES(?,?,?,?,?)
                     ON CONFLICT(hash) DO UPDATE SET filename=excluded.filename""",
                  (h, _enc(filename), size, imported_at, _enc_bytes(content)))


def knowledge_docs_all():
    """Metadata only (no content blob) — for the status/UI listing. filename is encrypted at
    rest, so this can't ORDER BY filename in SQL anymore (ciphertext sort order is meaningless) --
    sorted in Python after decrypting instead."""
    with _conn() as c:
        rows = c.execute("SELECT hash, filename, size, imported_at FROM knowledge_docs").fetchall()
    out = [dict(r) for r in rows]
    for d in out:
        d["filename"] = _dec(d["filename"])
    out.sort(key=lambda d: (d["filename"] or ""))
    return out


def knowledge_doc_delete(h, filename=None):
    """Removes a document + its chunks + its path mapping(s), and records the hash into
    knowledge_removed so a later reindex can flag its reappearance rather than silently
    re-adding it (Jason, 2026-07-23g: surface a reimport either way it cuts — bug magnet if
    meant to retire permanently, safety net if the wrong doc was deleted by mistake)."""
    with _conn() as c:
        row = c.execute("SELECT filename FROM knowledge_docs WHERE hash=?", (h,)).fetchone()
        fn = filename or (_dec(row["filename"]) if row else None)
        c.execute("DELETE FROM knowledge_chunks WHERE doc_hash=?", (h,))
        c.execute("DELETE FROM knowledge_docs WHERE hash=?", (h,))
        # Deliberately NOT deleting from knowledge_paths here: a path's (mtime, size) signature
        # still matching what's on disk is what lets a non-force reindex (e.g. the one `search()`
        # triggers on every query) recognize "nothing changed here" and skip re-scanning the file
        # -- which is what keeps a delete actually STICK until an explicit reindex, instead of the
        # very next search silently re-importing it (the path row still pointing at this now-gone
        # hash makes THIS path a no-op for an unchanged file; only force=True -- the REINDEX button
        # -- re-examines it regardless, correctly rediscovering + flagging it as reimported).
        c.execute("""INSERT INTO knowledge_removed(hash, filename, removed_at) VALUES(?,?,?)
                     ON CONFLICT(hash) DO UPDATE SET filename=excluded.filename,
                        removed_at=excluded.removed_at""",
                  (h, _enc(fn), int(time.time())))
    return fn


def knowledge_removed_get(h):
    with _conn() as c:
        row = c.execute("SELECT hash, filename, removed_at FROM knowledge_removed WHERE hash=?",
                        (h,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["filename"] = _dec(d["filename"])
    return d


def knowledge_removed_clear(h):
    """Called once a previously-removed hash is reimported — it's active again, so it shouldn't
    keep claiming 'reimported' on every subsequent reindex pass."""
    with _conn() as c:
        c.execute("DELETE FROM knowledge_removed WHERE hash=?", (h,))


def knowledge_chunks_replace(doc_hash, model, chunks):
    """`chunks`: iterable of (chunk_index, text, vec_list). Replaces ALL chunks for this hash —
    used both for a fresh import and for a forced re-embed (e.g. EMBED_MODEL changed). Both
    `text` (the actual document content, chunked) and `vec` (the embedding vector -- corpus is
    small enough that per-chunk decrypt during search is cheap, see secrets_crypto.py) are
    encrypted; doc_hash/chunk_index/model stay plain (lookup/join keys, low sensitivity)."""
    with _conn() as c:
        c.execute("DELETE FROM knowledge_chunks WHERE doc_hash=?", (doc_hash,))
        c.executemany("INSERT INTO knowledge_chunks(doc_hash, chunk_index, text, vec, model) "
                      "VALUES(?,?,?,?,?)",
                      [(doc_hash, ci, _enc(txt), _enc(json.dumps(vec)), model)
                       for (ci, txt, vec) in chunks])


def knowledge_chunk_count(doc_hash):
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM knowledge_chunks WHERE doc_hash=?",
                        (doc_hash,)).fetchone()
    return row["n"] if row else 0


def knowledge_chunks_all():
    """Every chunk across every doc, joined with its current filename — for `search()`'s
    cosine-similarity scan (corpus is small; a full Python scan is still the right call here,
    same as the old design, just reading from SQL instead of one JSON blob)."""
    with _conn() as c:
        rows = c.execute("""SELECT kc.doc_hash, kc.chunk_index, kc.text, kc.vec, kc.model,
                                    kd.filename
                             FROM knowledge_chunks kc
                             JOIN knowledge_docs kd ON kd.hash = kc.doc_hash""").fetchall()
    return [{"hash": r["doc_hash"], "chunk_index": r["chunk_index"], "text": _dec(r["text"]),
             "vec": json.loads(_dec(r["vec"])), "model": r["model"], "file": _dec(r["filename"])}
            for r in rows]


def knowledge_path_get(relpath):
    with _conn() as c:
        row = c.execute("SELECT relpath, hash, mtime, size FROM knowledge_paths WHERE relpath=?",
                        (relpath,)).fetchone()
    return dict(row) if row else None


def knowledge_path_upsert(relpath, h, mtime, size):
    with _conn() as c:
        c.execute("""INSERT INTO knowledge_paths(relpath, hash, mtime, size) VALUES(?,?,?,?)
                     ON CONFLICT(relpath) DO UPDATE SET hash=excluded.hash,
                        mtime=excluded.mtime, size=excluded.size""",
                  (relpath, h, mtime, size))


def knowledge_path_delete(relpath):
    with _conn() as c:
        c.execute("DELETE FROM knowledge_paths WHERE relpath=?", (relpath,))


def knowledge_paths_all():
    with _conn() as c:
        rows = c.execute("SELECT relpath, hash, mtime, size FROM knowledge_paths").fetchall()
    return {r["relpath"]: {"hash": r["hash"], "mtime": r["mtime"], "size": r["size"]} for r in rows}
