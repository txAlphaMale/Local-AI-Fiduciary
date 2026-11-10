# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
Per-user interface preferences kept on the server, so a customised layout follows the person to every browser and survives
cleared cookies / site data (2026-11-09, Jason: lost customisations are the most frustrating failure of browser-only storage).

What is stored: the page's own `fid*` preference keys (panel order, graph switches, tab order, theme, calculator inputs, ...) as
opaque strings, per account id. The whole blob is encrypted at rest with the same key as the chat transcripts, because calculator
inputs can contain salary figures. Keys are whitelisted and sizes are capped, so this cannot become a general-purpose store.
Not used on the public demo (its one shared login would leak one visitor's layout to the next).
"""
import json
import os
import re
import sqlite3
import time

import secrets_crypto

PREFS_DB = os.environ.get("PREFS_DB", "/data/prefs.db")
# Everything the page persists about how it looks or what the user last typed. Transient UI state (last sub-tab, a pending research
# ticker) is deliberately NOT synced.
SYNC_KEYS = frozenset({
    "fidDashOrder", "fidGraphs", "fidTabOrder", "fidSubOrder", "fidTheme", "fidCustomThemes", "fidMixView",
    "fidChMode", "fidChWin", "fidChFrom", "fidChTo", "fidCalcIn", "fidCalcWiSel", "fidScenarioId", "fidTourDone",
})
MAX_VALUE = 60_000
MAX_TOTAL = 200_000
_KEY_RE = re.compile(r"^fid[A-Za-z0-9]{1,40}$")


def _conn():
    os.makedirs(os.path.dirname(PREFS_DB) or ".", exist_ok=True)
    c = sqlite3.connect(PREFS_DB, timeout=30)
    c.execute("CREATE TABLE IF NOT EXISTS prefs(uid TEXT PRIMARY KEY, blob TEXT, updated INTEGER)")
    return c


def _load(uid):
    with _conn() as c:
        r = c.execute("SELECT blob FROM prefs WHERE uid=?", (str(uid),)).fetchone()
    if not r or not r[0]:
        return {}
    try:
        d = json.loads(secrets_crypto.decrypt_or_legacy(r[0]) or "{}")
    except ValueError:
        return {}
    return {k: v for k, v in d.items() if k in SYNC_KEYS and isinstance(v, str)} if isinstance(d, dict) else {}


def get(uid):
    return _load(uid)


def update(uid, set_=None, remove=None):
    """Merge `set_` ({key: string}) and drop `remove` ([key]). Returns the stored dict. Raises ValueError on a bad key or size."""
    cur = _load(uid)
    for k, v in (set_ or {}).items():
        if not _KEY_RE.match(str(k)) or k not in SYNC_KEYS:
            raise ValueError(f"preference {str(k)[:40]!r} is not one that is saved")
        if not isinstance(v, str) or len(v) > MAX_VALUE:
            raise ValueError(f"preference {k} must be text of at most {MAX_VALUE} characters")
        cur[k] = v
    for k in remove or []:
        cur.pop(k, None)
    if sum(len(k) + len(v) for k, v in cur.items()) > MAX_TOTAL:
        raise ValueError("saved preferences are too large")
    with _conn() as c:
        c.execute("INSERT INTO prefs(uid, blob, updated) VALUES(?,?,?) ON CONFLICT(uid) DO UPDATE SET blob=excluded.blob, updated=excluded.updated",
                  (str(uid), secrets_crypto.encrypt(json.dumps(cur)), int(time.time())))
    return cur


def delete_user(uid):
    with _conn() as c:
        c.execute("DELETE FROM prefs WHERE uid=?", (str(uid),))
