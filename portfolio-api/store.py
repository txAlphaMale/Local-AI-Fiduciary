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

STORE_DB = os.environ.get("STORE_DB", "/data/store.db")

_lock = threading.Lock()   # SQLite handles its own file locking, but this keeps init_db()
                           # idempotent-safe if ever called from more than one thread at once


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
        try:
            os.chmod(STORE_DB, 0o600)
        except Exception:
            pass


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
        for d in (json.loads(row["value"]) or []):
            c.execute("""INSERT OR REPLACE INTO value_history(date,net_worth,invested_value,spy,funded_ratio)
                         VALUES(?,?,?,?,?)""",
                      (d.get("date"), d.get("net_worth"), d.get("invested_value"), d.get("spy"), d.get("funded_ratio")))


def ensure_strategist_runs_migrated(legacy_path):
    with _conn() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM strategist_runs").fetchone()["n"]
        if n:
            return
        row = c.execute("SELECT value FROM app_state WHERE key=?", (_key_for(legacy_path),)).fetchone()
        if not row:
            return
        for line in (json.loads(row["value"]) or []):
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
                      (rec.get("ts"), rec.get("scenario_id"), rec.get("scenario_name"), rec.get("risk_profile"),
                       rec.get("max_iter"), rec.get("iterations"), json.dumps(rec.get("tool_calls") or {}),
                       rec.get("repeat_calls"), rec.get("trades"), rec.get("search_calls"), rec.get("search_budget"),
                       int(bool(rec.get("nudged"))), int(bool(rec.get("converged"))), rec.get("ended"),
                       rec.get("error"), rec.get("duration_s")))


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
        reg = json.loads(row["value"]) or {}
        scenarios = reg.get("scenarios")
        if not isinstance(scenarios, dict) or not scenarios:
            scenarios = {"default": reg} if (reg.get("transactions") or reg.get("reports")) else {}
        for sid, scen in scenarios.items():
            for tx in (scen.get("transactions") or []):
                c.execute("""INSERT INTO paper_transactions(scenario_id,ts,action,ticker,shares,price,value,recurring)
                             VALUES(?,?,?,?,?,?,?,?)""",
                          (sid, tx.get("ts"), tx.get("action"), tx.get("ticker"), tx.get("shares"),
                           tx.get("price"), tx.get("value"), int(bool(tx.get("recurring")))))
            for rep in (scen.get("reports") or []):
                c.execute("""INSERT INTO paper_reports(scenario_id,ts,total_value,return_pct,summary)
                             VALUES(?,?,?,?,?)""",
                          (sid, rep.get("ts"), rep.get("total_value"), rep.get("return_pct"), rep.get("summary")))


# ---- value_history: typed read/write API (replaces raw read_json_strict/write_json call sites) ----
def value_history_read_all():
    """Ascending by date -- same shape (list of dicts: date/net_worth/invested_value/spy/
    funded_ratio) every existing consumer (_risk_metrics, _vol_regime_signal, performance.py,
    the Portfolio-vs-SPY chart) already expects, so none of them need to change."""
    with _conn() as c:
        rows = c.execute("SELECT date, net_worth, invested_value, spy, funded_ratio "
                         "FROM value_history ORDER BY date ASC").fetchall()
    return [dict(r) for r in rows]


def value_history_today_spy(date):
    """The SPY close already recorded for `date`, if any -- lets the caller skip re-fetching the
    benchmark price when today's row already has one (mirrors the old in-memory `hist[-1]` check
    without needing to load the whole history just to look at one row)."""
    with _conn() as c:
        row = c.execute("SELECT spy FROM value_history WHERE date=?", (date,)).fetchone()
    return row["spy"] if row else None


def value_history_upsert_today(date, net_worth, invested_value, spy):
    """Insert today's row, or overwrite it in place if called again the same day -- same
    idempotent-per-day behavior as the original `hist[-1]=row if same date else append`."""
    with _conn() as c:
        c.execute("""INSERT INTO value_history(date, net_worth, invested_value, spy) VALUES(?,?,?,?)
                     ON CONFLICT(date) DO UPDATE SET net_worth=excluded.net_worth,
                        invested_value=excluded.invested_value, spy=excluded.spy""",
                  (date, net_worth, invested_value, spy))


def value_history_set_funded_ratio(date, funded_ratio):
    """UPDATE-only, never INSERTs -- mirrors the original's "only patch if today's row already
    exists" guard (a funded-ratio snapshot with no matching value-snapshot row silently no-ops,
    same as before)."""
    with _conn() as c:
        c.execute("UPDATE value_history SET funded_ratio=? WHERE date=?", (funded_ratio, date))


# ---- strategist_runs: typed read/write API ----
def strategist_run_log(rec):
    """Appends one telemetry record. No cap in storage (Stage B removes the old
    STRAT_RUN_LOG_MAX-based trim entirely) -- `limit` at read time is now a pure display/API
    cap, never a data-loss one."""
    with _conn() as c:
        c.execute("""INSERT INTO strategist_runs(ts,scenario_id,scenario_name,risk_profile,max_iter,
                        iterations,tool_calls_json,repeat_calls,trades,search_calls,search_budget,
                        nudged,converged,ended,error,duration_s)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (rec.get("ts"), rec.get("scenario_id"), rec.get("scenario_name"), rec.get("risk_profile"),
                   rec.get("max_iter"), rec.get("iterations"), json.dumps(rec.get("tool_calls") or {}),
                   rec.get("repeat_calls"), rec.get("trades"), rec.get("search_calls"), rec.get("search_budget"),
                   int(bool(rec.get("nudged"))), int(bool(rec.get("converged"))), rec.get("ended"),
                   rec.get("error"), rec.get("duration_s")))


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
        r["tool_calls"] = json.loads(r.pop("tool_calls_json") or "{}")
        r["nudged"] = bool(r["nudged"])
        r["converged"] = bool(r["converged"])
    return rows


# ---- paper_transactions / paper_reports: typed read/write API, scoped by scenario_id ----
def paper_tx_append(scenario_id, ts, action, ticker, shares, price, value, recurring=False):
    with _conn() as c:
        c.execute("""INSERT INTO paper_transactions(scenario_id,ts,action,ticker,shares,price,value,recurring)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (scenario_id, ts, action, ticker, shares, price, value, int(bool(recurring))))


def _paper_tx_row(r):
    d = {"ts": r["ts"], "action": r["action"], "ticker": r["ticker"], "shares": r["shares"],
         "price": r["price"], "value": r["value"]}
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


def paper_report_append(scenario_id, ts, total_value, return_pct, summary):
    with _conn() as c:
        c.execute("INSERT INTO paper_reports(scenario_id,ts,total_value,return_pct,summary) VALUES(?,?,?,?,?)",
                  (scenario_id, ts, total_value, return_pct, summary))


def paper_reports_all(scenario_id):
    with _conn() as c:
        rows = c.execute("SELECT ts,total_value,return_pct,summary FROM paper_reports "
                         "WHERE scenario_id=? ORDER BY ts ASC, id ASC", (scenario_id,)).fetchall()
    return [dict(r) for r in rows]


def paper_reports_recent(scenario_id, limit=12):
    with _conn() as c:
        rows = c.execute("SELECT ts,total_value,return_pct,summary FROM paper_reports "
                         "WHERE scenario_id=? ORDER BY ts DESC, id DESC LIMIT ?", (scenario_id, limit)).fetchall()
    return [dict(r) for r in rows][::-1]


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
    pair it replaces."""
    key = _key_for(path)
    with _conn() as c:
        row = c.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
    if row is not None:
        return json.loads(row["value"])
    with open(path, encoding="utf-8") as f:   # legacy fallback -- first read after migrating TO this store
        data = json.load(f)
    write_json(path, data)   # persist into the store so next time skips straight to the row above
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
    updated, not deleted), so it stays as a frozen snapshot of whatever it last held."""
    key = _key_for(path)
    value = json.dumps(data, ensure_ascii=False)
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
        return json.loads(row["value"])
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    write_lines(path, lines)
    return lines


def write_lines(path, lines):
    key = _key_for(path)
    value = json.dumps(list(lines), ensure_ascii=False)
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
    return dict(row) if row else None


def knowledge_doc_get_content(h):
    """The raw imported bytes for a doc — used to re-extract/re-chunk without needing the file
    to still exist on disk (e.g. after a model change forces a re-embed)."""
    with _conn() as c:
        row = c.execute("SELECT content FROM knowledge_docs WHERE hash=?", (h,)).fetchone()
    return row["content"] if row else None


def knowledge_doc_upsert(h, filename, size, imported_at, content):
    with _conn() as c:
        c.execute("""INSERT INTO knowledge_docs(hash, filename, size, imported_at, content)
                     VALUES(?,?,?,?,?)
                     ON CONFLICT(hash) DO UPDATE SET filename=excluded.filename""",
                  (h, filename, size, imported_at, content))


def knowledge_docs_all():
    """Metadata only (no content blob) — for the status/UI listing."""
    with _conn() as c:
        rows = c.execute("SELECT hash, filename, size, imported_at FROM knowledge_docs "
                         "ORDER BY filename").fetchall()
    return [dict(r) for r in rows]


def knowledge_doc_delete(h, filename=None):
    """Removes a document + its chunks + its path mapping(s), and records the hash into
    knowledge_removed so a later reindex can flag its reappearance rather than silently
    re-adding it (Jason, 2026-07-23g: surface a reimport either way it cuts — bug magnet if
    meant to retire permanently, safety net if the wrong doc was deleted by mistake)."""
    with _conn() as c:
        row = c.execute("SELECT filename FROM knowledge_docs WHERE hash=?", (h,)).fetchone()
        fn = filename or (row["filename"] if row else None)
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
                  (h, fn, int(time.time())))
    return fn


def knowledge_removed_get(h):
    with _conn() as c:
        row = c.execute("SELECT hash, filename, removed_at FROM knowledge_removed WHERE hash=?",
                        (h,)).fetchone()
    return dict(row) if row else None


def knowledge_removed_clear(h):
    """Called once a previously-removed hash is reimported — it's active again, so it shouldn't
    keep claiming 'reimported' on every subsequent reindex pass."""
    with _conn() as c:
        c.execute("DELETE FROM knowledge_removed WHERE hash=?", (h,))


def knowledge_chunks_replace(doc_hash, model, chunks):
    """`chunks`: iterable of (chunk_index, text, vec_list). Replaces ALL chunks for this hash —
    used both for a fresh import and for a forced re-embed (e.g. EMBED_MODEL changed)."""
    with _conn() as c:
        c.execute("DELETE FROM knowledge_chunks WHERE doc_hash=?", (doc_hash,))
        c.executemany("INSERT INTO knowledge_chunks(doc_hash, chunk_index, text, vec, model) "
                      "VALUES(?,?,?,?,?)",
                      [(doc_hash, ci, txt, json.dumps(vec), model) for (ci, txt, vec) in chunks])


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
    return [{"hash": r["doc_hash"], "chunk_index": r["chunk_index"], "text": r["text"],
             "vec": json.loads(r["vec"]), "model": r["model"], "file": r["filename"]}
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
