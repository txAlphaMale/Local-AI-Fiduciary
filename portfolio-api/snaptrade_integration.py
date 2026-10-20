# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
snaptrade_integration.py -- SnapTrade as a portfolio data source, on the 13.x SDK (2026-10-20 migration).

TWO AUTH MODES (SnapTrade changed this; the old code only knew the second):
  * PERSONAL keys (client ID starts with "PERS-"): your own free key. The user is built into the key --
    requests are signed with the consumer key and carry NO userId/userSecret, and registerUser is not
    available. Brokerages are linked through a Connection Portal URL from loginSnapTradeUser().
  * COMMERCIAL keys: you register a user (snaptrade_register.py) and send userId + userSecret on every
    account-data call.
`make_client()` picks the mode from the client ID (override with SNAPTRADE_AUTH_MODE=personal|commercial)
and returns the SDK client plus the extra keyword arguments account-data calls need in that mode.

SDK 13 differences handled here: the constructor takes `auth=SnapTradeAuth.personal_api_key(...)` /
`commercial_api_key(...)`; the per-account positions call is `get_all_account_positions` (a `{results:[...]}`
payload carrying stocks, ETFs, funds, crypto and options); `list_all_user_accounts` replaces the deprecated
`list_user_accounts`.

SCHEMA NOTE: `list_all_user_accounts` (current) returns display_name / masked_account_number / net_value / connection_id
and NO institution name — names come from the connections list. The deprecated `list_user_accounts` returned name /
number / balance.total / institution_name; the mapping accepts both.

MAPPING FACTS verified against SnapTrade's sandbox broker (test_data/snaptrade_sandbox.json):
  * `cost_basis` is the average cost PER UNIT (total cost = units x cost_basis).
  * An account's `balance.total.amount` = sum(units x price) of the NON-cash-equivalent positions + the
    account's cash. A position flagged `cash_equivalent` (e.g. a money-market fund) is ALREADY inside the
    cash figure, so it is not added again (doing so double-counts it).
  * Option positions are not valued here (contract multiplier/price semantics are unverified); they are
    counted in the per-account note instead of guessed at.
This module imports the SDK lazily and never imports app.py.
"""
import os
import re

PERSONAL_PREFIX = "PERS-"


class SnapTradeConfigError(RuntimeError):
    """Missing/invalid configuration (clear message for the dashboard)."""


def auth_mode(client_id):
    override = (os.environ.get("SNAPTRADE_AUTH_MODE") or "").strip().lower()
    if override in ("personal", "commercial"):
        return override
    return "personal" if (client_id or "").startswith(PERSONAL_PREFIX) else "commercial"


def make_client(client_id, consumer_key, user_id=None, user_secret=None):
    """-> (client, user_kwargs, mode). user_kwargs is {} in personal mode, else {user_id, user_secret}."""
    if not client_id or not consumer_key:
        raise SnapTradeConfigError("SnapTrade not configured: set SNAPTRADE_CLIENT_ID and SNAPTRADE_CONSUMER_KEY "
                                   "(SETTINGS → SECRETS & KEYS → Portfolio (SnapTrade)).")
    mode = auth_mode(client_id)
    from snaptrade_client import SnapTrade, SnapTradeAuth     # pip: snaptrade-python-sdk (>= 13)
    if mode == "personal":
        client = SnapTrade(auth=SnapTradeAuth.personal_api_key(consumer_key=consumer_key, client_id=client_id))
        return client, {}, mode
    if not user_id or not user_secret:
        raise SnapTradeConfigError("SnapTrade (commercial keys) also needs SNAPTRADE_USER_ID and SNAPTRADE_USER_SECRET — "
                                   "run snaptrade_register.py once to create them. (Personal keys, which start with "
                                   f"'{PERSONAL_PREFIX}', need no user.)")
    client = SnapTrade(auth=SnapTradeAuth.commercial_api_key(consumer_key=consumer_key, client_id=client_id))
    return client, {"user_id": user_id, "user_secret": user_secret}, mode


def _body(resp):
    return getattr(resp, "body", resp)


def _f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- network fetch
def fetch_raw(client, user_kwargs):
    """-> {"accounts": [...], "positions": {account_id: payload}, "balances": {account_id: list},
    "errors": {account_id: message}}. One failing account never sinks the rest."""
    ai = client.account_information
    listed = _body(ai.list_all_user_accounts(**user_kwargs))
    accounts = listed.get("results") if isinstance(listed, dict) else listed
    out = {"accounts": list(accounts or []), "positions": {}, "balances": {}, "errors": {}, "connections": []}
    try:                                   # brokerage names + "needs re-authorization" flags (best effort)
        out["connections"] = list(_body(client.connections.list_brokerage_authorizations(**user_kwargs)) or [])
    except Exception:
        pass
    for a in out["accounts"]:
        aid = a.get("id")
        try:
            out["positions"][aid] = _body(ai.get_all_account_positions(account_id=aid, **user_kwargs))
            out["balances"][aid] = _body(ai.get_user_account_balance(account_id=aid, **user_kwargs))
        except Exception as e:                      # SDK ApiException etc. -- record and move on
            out["errors"][aid] = f"{type(e).__name__}: {str(getattr(e, 'body', e))[:160]}"
    return out


def health(client, user_kwargs):
    """Cheap connection check (two list calls, no positions): what SnapTrade says is linked right now.
    -> {"ok", "n_accounts", "accounts":[{name, institution, total}], "connections":[{name, disabled}], "problems":[...]}.
    Raises on auth/network failure -- the caller turns that into a message."""
    raw = fetch_accounts_and_connections(client, user_kwargs)
    conns = {c.get("id"): c for c in raw["connections"] if isinstance(c, dict)}
    accounts = [{"name": account_display_name(a), "institution": _institution(a, conns), "total": account_total(a)}
                for a in raw["accounts"]]
    cons = [{"name": ((c.get("brokerage") or {}).get("name") or c.get("name") or "Brokerage"),
             "disabled": bool(c.get("disabled"))} for c in conns.values()]
    problems = []
    if not accounts:
        problems.append("Connected to SnapTrade, but no brokerage accounts are linked yet — click CONNECT A BROKERAGE and finish the login in the SnapTrade window.")
    for c in cons:
        if c["disabled"]:
            problems.append(f"{c['name']}: the connection is disabled — reconnect it (CONNECT A BROKERAGE).")
    return {"ok": bool(accounts) and not any(c["disabled"] for c in cons), "n_accounts": len(accounts),
            "accounts": accounts, "connections": cons, "problems": problems}


def fetch_accounts_and_connections(client, user_kwargs):
    listed = _body(client.account_information.list_all_user_accounts(**user_kwargs))
    accounts = listed.get("results") if isinstance(listed, dict) else listed
    try:
        conns = list(_body(client.connections.list_brokerage_authorizations(**user_kwargs)) or [])
    except Exception:
        conns = []
    return {"accounts": list(accounts or []), "connections": conns}


def portal_url(client, user_kwargs, sandbox=False):
    """A one-time Connection Portal link (expires in ~5 minutes) for linking a brokerage — or SnapTrade's
    simulated SANDBOX broker for testing."""
    kw = dict(user_kwargs, connection_type="read")
    if sandbox:
        kw["broker"] = "SANDBOX"
    return _body(client.authentication.login_snap_trade_user(**kw)).get("redirectURI")


# ---------------------------------------------------------------- pure mapping (no I/O)
_TYPES = {"stock": "Stock", "etf": "ETF", "crypto": "Crypto", "mutualfund": "Mutual Fund", "bond": "Bond",
          "cryptocurrency": "Crypto"}


def account_display_name(a):
    """'<name>[ · <raw_type>] (...<last4>)'. The app classifies tax status from the NAME (Roth/IRA/401/HSA),
    so the brokerage's raw account type is appended when the name does not already say it.
    Accepts both account schemas: list_all_user_accounts (display_name, masked_account_number) and the
    deprecated list_user_accounts (name, number)."""
    name = (a.get("display_name") or a.get("name") or a.get("raw_type") or "Account").strip()
    raw = (a.get("raw_type") or "").strip()
    if raw and raw.lower() not in name.lower():
        name = f"{name} · {raw}"
    num = re.sub(r"[^A-Za-z0-9]", "", str(a.get("masked_account_number") or a.get("number") or ""))
    return f"{name} (...{num[-4:]})" if num else name


def account_total(a):
    """Account value, from either schema: net_value.amount (new) or balance.total.amount (deprecated call)."""
    nv = (a.get("net_value") or {}).get("amount")
    if nv is None:
        nv = ((a.get("balance") or {}).get("total") or {}).get("amount")
    return _f(nv) or 0.0


def _institution(a, conns):
    c = conns.get(a.get("connection_id") or a.get("brokerage_authorization")) or {}
    return ((c.get("brokerage") or {}).get("name") or c.get("name") or a.get("institution_name") or "Brokerage")


def map_portfolio(accounts, positions_by_id, balances_by_id, errors=None, connections=None):
    """Normalize SnapTrade accounts into the shape the other providers produce. Returns
    {"accounts","positions","by_account","inv","balance_only","net_worth","total_assets","notes"}."""
    errors = errors or {}
    conns = {c.get("id"): c for c in (connections or []) if isinstance(c, dict)}
    out_accounts, positions, by_account, inv, balance_only, notes = [], [], [], [], [], []
    for a in accounts:
        aid = a.get("id")
        inst = _institution(a, conns)
        name = account_display_name(a)
        total = account_total(a)
        if (conns.get(a.get("connection_id")) or {}).get("disabled"):
            notes.append(f"{inst}: the connection is disabled — reconnect it (SETTINGS → ACCOUNTS → SnapTrade → CONNECT)")
        rec = {"id": "snaptrade:" + str(aid), "name": name, "institution": inst, "type": "Investments",
               "balance": round(total, 2), "holdings_count": 0, "is_asset": True, "include_nw": True,
               "source": "snaptrade"}
        out_accounts.append(rec)
        if aid in errors:
            by_account.append({"account": name, "institution": inst, "value": round(total, 2), "balance": round(total, 2),
                               "error": errors[aid], "holdings": []})
            continue
        comp, skipped_opts = [], 0
        for p in (positions_by_id.get(aid) or {}).get("results") or []:
            ins = p.get("instrument") or {}
            kind = str(ins.get("kind") or "").lower()
            if p.get("cash_equivalent"):
                continue                              # already inside the account's cash figure
            if "option" in kind:
                skipped_opts += 1
                continue
            units, price = _f(p.get("units")), _f(p.get("price"))
            if not units or price is None:
                continue
            value = round(units * price, 2)
            sym = (ins.get("symbol") or ins.get("raw_symbol") or "")
            sym = sym.get("symbol") if isinstance(sym, dict) else sym
            sym = str(sym or "").upper().strip()
            per_unit = _f(p.get("cost_basis"))
            cost_total = round(units * per_unit, 2) if (per_unit is not None and per_unit > 0) else None
            suspect = cost_total is None or (value > 0 and cost_total > value * 50)
            if suspect:
                cost_total = None
            gain = round(value - cost_total, 2) if cost_total is not None else None
            comp.append({"ticker": sym or (ins.get("description") or "?"), "name": ins.get("description") or sym,
                         "type": _TYPES.get(kind) or (kind.title() if kind else "Other"),
                         "quantity": round(units, 6), "price": price, "value": value, "cost_total": cost_total,
                         "gain": gain, "gain_pct": round(gain / cost_total * 100, 2) if (gain is not None and cost_total) else None,
                         "day_pct": None, "suspect": bool(suspect), "is_cit": False})
        cash = sum(_f(b.get("cash")) or 0.0 for b in (balances_by_id.get(aid) or [])
                   if ((b.get("currency") or {}).get("code") or "USD") == "USD")
        if cash > 0:
            comp.append({"ticker": "CASH", "name": "Cash", "type": "Cash", "quantity": round(cash, 2), "price": 1.0,
                         "value": round(cash, 2), "cost_total": None, "gain": None, "gain_pct": None, "day_pct": None,
                         "suspect": False, "is_cit": False})
        if skipped_opts:
            notes.append(f"{name}: {skipped_opts} option position(s) not valued")
        if comp:
            rec["holdings_count"] = len(comp)
            by_account.append({"account": name, "institution": inst, "value": round(sum(c["value"] for c in comp), 2),
                               "balance": round(total, 2), "holdings": comp,
                               **({"note": f"{skipped_opts} option position(s) not valued"} if skipped_opts else {})})
            for c in comp:
                c2 = dict(c); c2["account"], c2["institution"] = name, inst
                positions.append(c2)
            inv.append(rec)
        elif total:
            balance_only.append(rec)
    net = round(sum(r["balance"] for r in out_accounts), 2)
    return {"accounts": out_accounts, "positions": positions, "by_account": by_account, "inv": inv,
            "balance_only": balance_only, "net_worth": net, "total_assets": net, "notes": notes}


def build_portfolio(client_id, consumer_key, user_id=None, user_secret=None, client_factory=None):
    """Fetch + map. Raises SnapTradeConfigError for config problems and RuntimeError when nothing could be read.
    `client_factory` lets tests inject a fake SDK client."""
    client, user_kwargs, mode = (client_factory or make_client)(client_id, consumer_key, user_id, user_secret)
    raw = fetch_raw(client, user_kwargs)
    if not raw["accounts"]:
        raise RuntimeError("SnapTrade returned no accounts yet — link a brokerage (SETTINGS → ACCOUNTS → SnapTrade → "
                           "CONNECT), or pick another portfolio data source.")
    if raw["errors"] and len(raw["errors"]) == len(raw["accounts"]):
        raise RuntimeError("SnapTrade: every account failed to load — " + "; ".join(raw["errors"].values())[:300])
    m = map_portfolio(raw["accounts"], raw["positions"], raw["balances"], raw["errors"], raw.get("connections"))
    m["mode"] = mode
    return m
