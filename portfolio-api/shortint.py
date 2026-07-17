"""
shortint.py — P1/G15 (deferred half): FINRA equity short interest, bi-monthly settlements.

FINRA publishes consolidated short interest for all listed/OTC equities; API access is
FREE with an individual API-console account (developer.finra.org) — client id/secret go
in secrets.json (FINRA_CLIENT_ID / FINRA_CLIENT_SECRET, boot-merged to env). Without
credentials every call returns None and the feature reads "not configured" — no crash.

Signal use is CONTEXT-ONLY (same stance as Form-4/13F): short interest as % of float-ish
volume and days-to-cover flag crowding/squeeze mechanics on holdings and scanner names;
they are not a buy/sell signal by themselves.
"""
import os
import json
import time
import base64

import requests

TOKEN_URL = "https://ews.fip.finra.org/fip/rest/ews/oauth2/access_token"
DATA_URL = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"
_TOK = {"token": None, "exp": 0}


def _creds():
    cid = os.environ.get("FINRA_CLIENT_ID") or ""
    sec = os.environ.get("FINRA_CLIENT_SECRET") or ""
    return (cid.strip(), sec.strip()) if cid.strip() and sec.strip() else None


def configured():
    return _creds() is not None


def _token():
    cr = _creds()
    if not cr:
        return None
    if _TOK["token"] and time.time() < _TOK["exp"] - 60:
        return _TOK["token"]
    try:
        basic = base64.b64encode(f"{cr[0]}:{cr[1]}".encode()).decode()
        r = requests.post(TOKEN_URL, params={"grant_type": "client_credentials"},
                          headers={"Authorization": f"Basic {basic}"}, timeout=20)
        if r.status_code >= 400:
            return None
        j = r.json() or {}
        _TOK["token"] = j.get("access_token")
        _TOK["exp"] = time.time() + float(j.get("expires_in") or 1800)
        return _TOK["token"]
    except Exception:
        return None


def short_interest(sym):
    """Latest two settlements for a symbol → level + change + days-to-cover. None if
    unconfigured/unavailable. FINRA field names per the equity-short-interest metadata."""
    sym = (sym or "").upper().strip()
    tok = _token()
    if not sym or not tok:
        return None
    body = {"limit": 4,
            "compareFilters": [{"fieldName": "symbolCode", "compareType": "EQUAL",
                                "fieldValue": sym}],
            "sortFields": ["-settlementDate"]}
    try:
        r = requests.post(DATA_URL, json=body,
                          headers={"Authorization": f"Bearer {tok}",
                                   "Content-Type": "application/json",
                                   "Accept": "application/json"}, timeout=25)
        if r.status_code >= 400:
            return None
        rows = r.json() or []
    except Exception:
        return None
    if not isinstance(rows, list) or not rows:
        return None
    latest = rows[0]
    prior = rows[1] if len(rows) > 1 else {}

    def _f(row, *names):
        for n in names:
            v = row.get(n)
            if v not in (None, ""):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return None

    cur = _f(latest, "currentShortPositionQuantity", "shortInterestQuantity")
    prev = _f(latest, "previousShortPositionQuantity") or _f(prior, "currentShortPositionQuantity")
    dtc = _f(latest, "daysToCoverQuantity")
    adv = _f(latest, "averageDailyVolumeQuantity")
    if cur is None:
        return None
    chg = round((cur / prev - 1) * 100, 1) if prev else None
    flag = None
    if dtc is not None and dtc >= 8:
        flag = "crowded short (days-to-cover ≥ 8 — squeeze mechanics both ways)"
    elif chg is not None and chg >= 25:
        flag = "short interest jumped ≥ 25% settlement-over-settlement"
    return {"ticker": sym, "settlement_date": latest.get("settlementDate"),
            "short_interest_shares": cur, "prior_shares": prev, "change_pct": chg,
            "days_to_cover": dtc, "avg_daily_volume": adv, "flag": flag,
            "source": "FINRA consolidated short interest (bi-monthly, T+~9 publication lag)",
            "note": "Context-only: crowding/squeeze mechanics, not a directional signal."}
