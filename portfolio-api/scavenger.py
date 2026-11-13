# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""scavenger.py -- the nightly "universe" job: S0 (who is listed) + S1 (quant screen) -> ticker_state. No API quota used.

Steady state is cheap on purpose: SEC refreshes its bulk file about 3 a.m. Eastern. The job looks at the file's
Last-Modified (one HEAD request); unchanged means nothing to do. When it did change, the 1.4 GB zip is streamed to disk,
screened, and deleted. A failed download or screen leaves every ticker's state as it was."""
import datetime
import os
import time

import requests

import edgar
import scavenge_screen
import store
import ticker_state
import universe

UNIVERSE_DIR = os.environ.get("UNIVERSE_DIR", "/data/universe")
META_FILE = os.environ.get("UNIVERSE_META_FILE", "/data/universe_meta.json")
FACTS_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
UA_PLAIN = {"User-Agent": "Mozilla/5.0 (compatible; local-fiduciary)"}


def read_meta():
    try:
        return store.read_json_strict(META_FILE) or {}
    except Exception:
        return {}


def _sec_get(url, **kw):
    return edgar._get(url, headers=edgar._headers(), timeout=kw.pop("timeout", 60), **kw)


def _plain_get(url):
    return requests.get(url, headers=UA_PLAIN, timeout=60)


def _download_zip(dest, last_modified_seen, force):
    """-> (path or None, last_modified). None when SEC's file is unchanged since the last screen."""
    h = _sec_get(FACTS_URL, stream=True, timeout=(30, 120))
    try:
        if h.status_code != 200:
            raise RuntimeError(f"SEC bulk file returned HTTP {h.status_code}")
        lm = h.headers.get("Last-Modified")
        if lm and lm == last_modified_seen and not force:
            return None, lm
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            for chunk in h.iter_content(1 << 20):
                if chunk:
                    f.write(chunk)
        os.replace(tmp, dest)
        return dest, lm
    finally:
        h.close()


def run_universe(force=False, today=None, sec_get=None, plain_get=None, download=None):
    """The job body. Returns a summary dict (also saved to META_FILE for GET /api/universe)."""
    t0 = time.time()
    today = today or datetime.date.today()
    meta = read_meta()
    force = force or meta.get("screen_version") != scavenge_screen.SCREEN_VERSION   # new rules: re-screen once
    with edgar.batch_mode():                      # background pace, and counted as idea-hunting calls on the gate
        srcs, stale = universe.fetch_sources(sec_get or _sec_get, plain_get or _plain_get, UNIVERSE_DIR)
    import json as _json
    rows, funnel = universe.build_universe(srcs["nasdaq"], srcs["other"], _json.loads(srcs["sec"]))
    if not rows:
        raise RuntimeError("universe came back empty; leaving ticker states untouched")
    dest = os.path.join(UNIVERSE_DIR, "companyfacts.zip")
    with edgar.batch_mode():
        path, lm = (download or _download_zip)(dest, meta.get("facts_last_modified"), force)
    summary = {"ran_at": int(time.time()), "date": today.isoformat(), "funnel": funnel, "stale_sources": stale,
               "facts_last_modified": lm, "screened": False}
    if path is None:
        summary["note"] = "SEC's bulk file has not changed since the last screen; nothing to do."
        summary["seconds"] = round(time.time() - t0, 1)
        store.write_json(META_FILE, {**meta, "facts_last_modified": lm, "last_check": summary,
                                     "screen_version": meta.get("screen_version")})
        return summary
    try:
        out_rows, stats = scavenge_screen.screen_zip(path, rows, today)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    moved = ticker_state.upsert_many(out_rows, today.isoformat())
    summary.update({"screened": True, "screen": stats, "changes": moved, "counts": ticker_state.counts(),
                    "seconds": round(time.time() - t0, 1)})
    store.write_json(META_FILE, {"facts_last_modified": lm, "last_check": summary, "last_screen": summary,
                                 "screen_version": scavenge_screen.SCREEN_VERSION})
    return summary
