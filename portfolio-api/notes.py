# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
Decision notes: the household's own record of a question it asked the Fiduciary, the answer it saved, and when to
look at it again.

Why this exists: the chat keeps transcripts, but only the last ~24 turns of ONE session reach the model, and a new
session knows nothing about older ones. A saved note is a small, explicit, human-controlled memory: the person
presses SAVE NOTE on an answer, optionally with a "revisit on" date, and the Fiduciary can read the open notes back
(get_notes tool) in any later session. The chat itself still has NO write tool: it can read notes, never create or
change them.

Storage: a small SQLite file in the data volume. Title, body, question, snapshot and revisit_event are encrypted at
rest with the same Fernet key as the chat transcripts (secrets_crypto); ids, dates and status stay plain so they can
be sorted and queried.
"""
import datetime
import json
import os
import sqlite3
import time

import secrets_crypto

NOTES_DB = os.environ.get("NOTES_DB", "/data/notes.db")
MAX_TITLE = 160
MAX_TEXT = 20000
STATUSES = ("open", "done")


def _conn():
    os.makedirs(os.path.dirname(NOTES_DB) or ".", exist_ok=True)
    c = sqlite3.connect(NOTES_DB, timeout=30)
    try:
        os.chmod(NOTES_DB, 0o600)            # owner only, like store.db / auth.db / audit.db (it was 0644 by default)
    except OSError:
        pass
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY AUTOINCREMENT, created INTEGER, updated INTEGER, "
              "title TEXT, body TEXT, question TEXT, session_id TEXT, snapshot TEXT, revisit_on TEXT, "
              "revisit_event TEXT, status TEXT, reminded_on TEXT)")
    return c


def _enc(v):
    return None if v is None else secrets_crypto.encrypt(v)


def _dec(v):
    return None if v is None else secrets_crypto.decrypt_or_legacy(v)


def _clip(v, n):
    return str(v if v is not None else "").strip()[:n]


def valid_date(s):
    """YYYY-MM-DD or empty. Returns the normalised string, '' for none, or raises ValueError."""
    s = str(s or "").strip()
    if not s:
        return ""
    return datetime.date.fromisoformat(s).isoformat()


def _row(r, with_body=True):
    d = {"id": r["id"], "created": r["created"], "updated": r["updated"], "title": _dec(r["title"]),
         "session_id": r["session_id"], "revisit_on": r["revisit_on"] or "", "status": r["status"],
         "revisit_event": _dec(r["revisit_event"]) or "", "reminded_on": r["reminded_on"] or ""}
    if with_body:
        d["body"] = _dec(r["body"])
        d["question"] = _dec(r["question"])
        try:
            d["snapshot"] = json.loads(_dec(r["snapshot"]) or "{}")
        except ValueError:
            d["snapshot"] = {}
    return d


def add(title, body, question="", session_id="", snapshot=None, revisit_on="", revisit_event=""):
    title, body = _clip(title, MAX_TITLE), _clip(body, MAX_TEXT)
    if not title:
        raise ValueError("a title is required")
    if not body:
        raise ValueError("there is nothing to save")
    revisit_on = valid_date(revisit_on)
    now = int(time.time())
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO notes(created, updated, title, body, question, session_id, snapshot, revisit_on, revisit_event, status, reminded_on) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (now, now, _enc(title), _enc(body), _enc(_clip(question, MAX_TEXT)), _clip(session_id, 40),
             _enc(json.dumps(snapshot or {})), revisit_on, _enc(_clip(revisit_event, 300)), "open", ""))
        return cur.lastrowid


def get(nid):
    with _conn() as c:
        r = c.execute("SELECT * FROM notes WHERE id=?", (int(nid),)).fetchone()
    return _row(r) if r else None


def list_notes(status=None, with_body=False, limit=200):
    q, args = "SELECT * FROM notes", []
    if status in STATUSES:
        q += " WHERE status=?"
        args.append(status)
    q += " ORDER BY (revisit_on='' ) ASC, revisit_on ASC, id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    with _conn() as c:
        rows = c.execute(q, args).fetchall()
    return [_row(r, with_body=with_body) for r in rows]


def update(nid, **fields):
    """Updatable: title, revisit_on, revisit_event, status. Changing the date re-arms the reminder."""
    sets, args = [], []
    if "title" in fields:
        t = _clip(fields["title"], MAX_TITLE)
        if not t:
            raise ValueError("a title is required")
        sets.append("title=?"); args.append(_enc(t))
    if "revisit_on" in fields:
        sets.append("revisit_on=?"); args.append(valid_date(fields["revisit_on"]))
        sets.append("reminded_on=?"); args.append("")
    if "revisit_event" in fields:
        sets.append("revisit_event=?"); args.append(_enc(_clip(fields["revisit_event"], 300)))
    if "status" in fields:
        if fields["status"] not in STATUSES:
            raise ValueError("status must be open or done")
        sets.append("status=?"); args.append(fields["status"])
    if not sets:
        raise ValueError("nothing to update")
    sets.append("updated=?"); args.append(int(time.time()))
    args.append(int(nid))
    with _conn() as c:
        cur = c.execute("UPDATE notes SET " + ", ".join(sets) + " WHERE id=?", args)
        return cur.rowcount > 0


def delete(nid):
    with _conn() as c:
        return c.execute("DELETE FROM notes WHERE id=?", (int(nid),)).rowcount > 0


def due(today=None):
    """Open notes whose revisit date has arrived and that have not been reminded for it yet."""
    today = today or datetime.date.today().isoformat()
    with _conn() as c:
        rows = c.execute("SELECT * FROM notes WHERE status='open' AND revisit_on!='' AND revisit_on<=? "
                         "AND reminded_on!=revisit_on", (today,)).fetchall()
    return [_row(r, with_body=False) for r in rows]


def mark_reminded(nid):
    with _conn() as c:
        c.execute("UPDATE notes SET reminded_on=revisit_on WHERE id=?", (int(nid),))
