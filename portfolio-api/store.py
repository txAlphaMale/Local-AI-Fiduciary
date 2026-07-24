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
        try:
            os.chmod(STORE_DB, 0o600)
        except Exception:
            pass


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
