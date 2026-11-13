# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""ticker_state.py -- one row per US-listed stock for the idea scavenger (design section 4).

state: new | active | reconsider | blacklist. Public market data only (no personal data), kept in store.db.
Only a SUCCESSFUL screen changes a row; a failed or deferred run leaves every row untouched (callers simply do not write).
`last_changed` moves only when the state itself changes, so "what changed since X" is a cheap query."""
import json
import time

import store

STATES = ("new", "active", "reconsider", "blacklist")


def ensure():
    with store._lock:
        with store._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS ticker_state(
                ticker TEXT PRIMARY KEY, cik TEXT, name TEXT, exchange TEXT,
                state TEXT NOT NULL, reason TEXT, reason_kind TEXT, check_set_by TEXT, next_check TEXT,
                first_seen TEXT, last_seen TEXT, last_changed TEXT, last_signal TEXT,
                s1 TEXT, scores TEXT)""")
            c.execute("CREATE INDEX IF NOT EXISTS ix_ticker_state_state ON ticker_state(state)")
            store._ensure_column(c, "ticker_state", "s3", "TEXT")          # signals (analyst, insider, short interest), phase 4c
            store._ensure_column(c, "ticker_state", "s2", "TEXT")          # market data (price, size, yield, beta, sector), phase 4b


def _row(r):
    d = dict(r)
    for k in ("s1", "scores", "s2", "s3"):
        try:
            d[k] = json.loads(d[k]) if d.get(k) else None
        except Exception:
            d[k] = None
    return d


def get_all_states():
    ensure()
    with store._conn() as c:
        return {r["ticker"]: (r["state"], r["first_seen"], r["last_changed"])
                for r in c.execute("SELECT ticker,state,first_seen,last_changed FROM ticker_state")}


def upsert_many(rows, today):
    """rows: dicts with ticker, cik, name, exchange, state, reason, reason_kind, check_set_by, next_check, s1, scores.
    Returns {'added': n, 'changed': n, 'unchanged': n}."""
    ensure()
    prior = get_all_states()
    added = changed = same = 0
    with store._lock:
        with store._conn() as c:
            for r in rows:
                p = prior.get(r["ticker"])
                first = p[1] if p else today
                lc = today if (not p or p[0] != r["state"]) else (p[2] or today)
                if not p:
                    added += 1
                elif p[0] != r["state"]:
                    changed += 1
                else:
                    same += 1
                c.execute("""INSERT INTO ticker_state(ticker,cik,name,exchange,state,reason,reason_kind,check_set_by,next_check,
                    first_seen,last_seen,last_changed,s1,scores) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(ticker) DO UPDATE SET cik=excluded.cik,name=excluded.name,exchange=excluded.exchange,
                    state=excluded.state,reason=excluded.reason,reason_kind=excluded.reason_kind,
                    check_set_by=excluded.check_set_by,next_check=excluded.next_check,last_seen=excluded.last_seen,
                    last_changed=excluded.last_changed,s1=excluded.s1,scores=excluded.scores""",
                          (r["ticker"], r.get("cik"), r.get("name"), r.get("exchange"), r["state"], r.get("reason"),
                           r.get("reason_kind"), r.get("check_set_by") or "rule", r.get("next_check"), first, today, lc,
                           json.dumps(r.get("s1")), json.dumps(r.get("scores"))))
    return {"added": added, "changed": changed, "unchanged": same}


def counts():
    ensure()
    with store._conn() as c:
        return {r["state"]: r["n"] for r in c.execute("SELECT state, COUNT(*) n FROM ticker_state GROUP BY state")}


def reason_counts(limit=12):
    ensure()
    with store._conn() as c:
        return [{"state": r["state"], "kind": r["reason_kind"], "n": r["n"]} for r in c.execute(
            "SELECT state, reason_kind, COUNT(*) n FROM ticker_state GROUP BY state, reason_kind ORDER BY n DESC LIMIT ?",
            (limit,))]


def list_rows(state=None, q=None, limit=50, offset=0):
    ensure()
    sql, args = "SELECT * FROM ticker_state", []
    where = []
    if state in STATES:
        where.append("state=?")
        args.append(state)
    if q:
        where.append("(ticker LIKE ? OR name LIKE ?)")
        args += [q.upper() + "%", "%" + q + "%"]
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ticker LIMIT ? OFFSET ?"
    args += [max(1, min(int(limit), 500)), max(0, int(offset))]
    with store._conn() as c:
        return [_row(r) for r in c.execute(sql, args)]


def total():
    return sum(counts().values())


def screened_rows(states):
    """Every row in the given states with its parsed s1/scores (for sleeve evaluation; a few thousand rows, read once per request)."""
    ensure()
    marks = ",".join("?" for _ in states)
    with store._conn() as c:
        return [_row(r) for r in c.execute(
            f"SELECT ticker,cik,name,exchange,state,reason_kind,s1,scores,s2,s3 FROM ticker_state WHERE state IN ({marks}) AND s1 IS NOT NULL AND s1 != 'null'",
            list(states))]


def set_s2(ticker, record):
    """Store a market-data record. Never touches state, reason or last_changed: market data does not move a ticker between states."""
    ensure()
    with store._lock:
        with store._conn() as c:
            c.execute("UPDATE ticker_state SET s2=? WHERE ticker=?", (json.dumps(record), ticker))


def s2_index():
    """{ticker: {'fetched_at', 'available'}} for every row that has market data."""
    ensure()
    out = {}
    with store._conn() as c:
        for r in c.execute("SELECT ticker, s2 FROM ticker_state WHERE s2 IS NOT NULL"):
            try:
                d = json.loads(r["s2"])
                out[r["ticker"]] = {"fetched_at": d.get("fetched_at"), "available": d.get("available", True), "v": d.get("v")}
            except Exception:
                continue
    return out


def set_s3(ticker, group, record):
    """Store one signals group ('analyst', 'insider' or 'short') for a ticker, leaving the other groups and the state alone."""
    ensure()
    with store._lock:
        with store._conn() as c:
            r = c.execute("SELECT s3 FROM ticker_state WHERE ticker=?", (ticker,)).fetchone()
            try:
                cur = json.loads(r["s3"]) if (r and r["s3"]) else {}
            except Exception:
                cur = {}
            cur[group] = record
            c.execute("UPDATE ticker_state SET s3=? WHERE ticker=?", (json.dumps(cur), ticker))


def s3_index(group):
    """{ticker: {'fetched_at', 'available'}} for rows that have this signals group."""
    ensure()
    out = {}
    with store._conn() as c:
        for r in c.execute("SELECT ticker, s3 FROM ticker_state WHERE s3 IS NOT NULL"):
            try:
                g = (json.loads(r["s3"]) or {}).get(group)
            except Exception:
                continue
            if g:
                out[r["ticker"]] = {"fetched_at": g.get("fetched_at"), "available": g.get("available", True), "v": g.get("v")}
    return out


# ---- scout notes and the pick log (phase 4d) ----
def ensure_scout():
    with store._lock:
        with store._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS scout_notes(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, created_at INTEGER NOT NULL, sleeves TEXT, why TEXT,
                conviction INTEGER, note TEXT NOT NULL, inputs TEXT, model TEXT)""")
            c.execute("CREATE INDEX IF NOT EXISTS ix_scout_notes_ticker ON scout_notes(ticker, created_at)")
            c.execute("""CREATE TABLE IF NOT EXISTS scout_picks(
                ticker TEXT NOT NULL, picked_on TEXT NOT NULL, sleeves TEXT, conviction INTEGER, price REAL, spy REAL, note_id INTEGER,
                scored TEXT, PRIMARY KEY(ticker, picked_on))""")


def add_note(ticker, note, inputs, sleeves, why, model, price, spy, today, now=None):
    """Save a scout note and log it as a pick (entry price and SPY level that day) for the later track-record scoring."""
    ensure_scout()
    now = int(now or __import__("time").time())
    with store._lock:
        with store._conn() as c:
            cur = c.execute("INSERT INTO scout_notes(ticker,created_at,sleeves,why,conviction,note,inputs,model) VALUES(?,?,?,?,?,?,?,?)",
                            (ticker, now, json.dumps(sleeves), why, note.get("conviction"), json.dumps(note), json.dumps(inputs), model))
            nid = cur.lastrowid
            c.execute("INSERT OR REPLACE INTO scout_picks(ticker,picked_on,sleeves,conviction,price,spy,note_id,scored) VALUES(?,?,?,?,?,?,?,NULL)",
                      (ticker, today, json.dumps(sleeves), note.get("conviction"), price, spy, nid))
    return nid


def notes_index():
    """{ticker: {'created_at', 'inputs', 'conviction'}} for the newest note of each ticker."""
    ensure_scout()
    out = {}
    with store._conn() as c:
        for r in c.execute("SELECT ticker, created_at, conviction, inputs FROM scout_notes ORDER BY created_at ASC"):
            try:
                inp = json.loads(r["inputs"]) if r["inputs"] else {}
            except Exception:
                inp = {}
            out[r["ticker"]] = {"created_at": r["created_at"], "conviction": r["conviction"], "inputs": inp}
    return out


def scout_briefs():
    """{ticker: {'conviction','created_at','thesis'}} newest note each, for the sleeve tables."""
    ensure_scout()
    out = {}
    with store._conn() as c:
        for r in c.execute("SELECT ticker, created_at, conviction, note FROM scout_notes ORDER BY created_at ASC"):
            try:
                th = (json.loads(r["note"]) or {}).get("thesis", "")
            except Exception:
                th = ""
            out[r["ticker"]] = {"conviction": r["conviction"], "created_at": r["created_at"], "thesis": th[:160]}
    return out


def get_note(ticker):
    ensure_scout()
    with store._conn() as c:
        r = c.execute("SELECT * FROM scout_notes WHERE ticker=? ORDER BY created_at DESC LIMIT 1", (ticker,)).fetchone()
    if not r:
        return None
    d = dict(r)
    for k in ("note", "inputs", "sleeves"):
        try:
            d[k] = json.loads(d[k]) if d.get(k) else None
        except Exception:
            d[k] = None
    return d


def list_notes(limit=30):
    ensure_scout()
    with store._conn() as c:
        rows = c.execute("SELECT id,ticker,created_at,conviction,sleeves,why,model FROM scout_notes ORDER BY created_at DESC LIMIT ?",
                         (max(1, min(int(limit), 200)),)).fetchall()
    return [{**dict(r), "sleeves": json.loads(r["sleeves"] or "[]")} for r in rows]


def picks(limit=500):
    ensure_scout()
    with store._conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM scout_picks ORDER BY picked_on DESC LIMIT ?", (limit,))]
