# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
plaid_integration.py -- Plaid as a portfolio data source (TASK-005).

SCOPE (Jason, 2026-10-20): balances, investment holdings and liabilities ONLY. The Transactions
product is never requested or called -- this app is not a Quicken/Monarch replacement, and Plaid
bills Transactions as a per-Item subscription. `PRODUCTS_BY_KIND` is the single list of what Link
may initialize, and a test asserts "transactions" never appears in it.

HOW LINKING WORKS (Plaid "Hosted Link"): the server creates a link_token with `hosted_link: {}`;
Plaid returns a `hosted_link_url` the user opens in any browser (no embedded JS widget, no redirect
URI registration, no public webhook). The app then POLLS /link/token/get until the session shows a
public_token, exchanges it for a long-lived access_token and stores the Item. Session results stay
available at Plaid for six hours after the session ends.

WHY NOT plaid-python: the API surface used here is six JSON POSTs; a thin `requests` client keeps
the dependency list (and the license/CVE surface) unchanged.

SECRETS: client_id/secret live in the app's encrypted secrets store (SETTINGS -> SECRETS); access
tokens live in the encrypted store blob (store.write_json) and are NEVER returned to the browser,
logged, or put in audit detail. Removing an Item calls /item/remove (which stops Plaid billing) and
only then deletes the local token.

Nothing in this module imports app.py (no cycle): app.py injects the credential lookup with
`configure()`, and calls `build_portfolio()` for the normalized provider payload.
"""
import os
import secrets as _secrets
import threading
import time

import requests

import store

PLAID_ITEMS_FILE = os.environ.get("PLAID_ITEMS_FILE", "/data/plaid_items.json")
ENV_URLS = {"sandbox": "https://sandbox.plaid.com", "production": "https://production.plaid.com"}
TIMEOUT = 30
PENDING_TTL_S = 3600
LINK_LIFETIME_S = 1800            # Plaid's default for a link we deliver ourselves

# What each kind of Link session may initialize. `products` restricts which institutions appear, so
# one broad list would hide banks/cards/brokerages; instead the user picks the kind of account. NEVER
# 'transactions'. 'auth' is requested for banks only because Link needs at least one product, and
# Plaid does not bill it until /auth/get is called (which this app never does -- routing/account
# numbers are not needed and are not stored). Balances come from /accounts/get on every Item.
PRODUCTS_BY_KIND = {
    "bank": ["auth"],
    "investments": ["investments"],
    "credit": ["liabilities"],
}
KIND_LABELS = {"bank": "Bank / cash accounts", "investments": "Brokerage & retirement",
               "credit": "Credit cards & loans"}

_cred_getter = lambda: (None, None, "sandbox")   # replaced by configure()
_lock = threading.RLock()


class PlaidError(Exception):
    def __init__(self, message, code=None, status=None, request_id=None):
        super().__init__(message)
        self.code, self.status, self.request_id = code, status, request_id

    @property
    def login_required(self):
        return self.code in ("ITEM_LOGIN_REQUIRED", "PENDING_EXPIRATION", "USER_PERMISSION_REVOKED")


def configure(get_creds):
    """get_creds() -> (client_id, secret, env). Called on every request so saved secrets apply live."""
    global _cred_getter
    _cred_getter = get_creds


def configured():
    cid, sec, _env = _cred_getter()
    return bool(cid and sec)


def environment():
    return _cred_getter()[2] if _cred_getter()[2] in ENV_URLS else "sandbox"


# ---------------------------------------------------------------- HTTP
def _post(path, payload):
    """One Plaid API call. Returns the JSON body; raises PlaidError with Plaid's own error code and
    message (which never contains our credentials). Credentials go in headers, not the body."""
    cid, sec, env = _cred_getter()
    if not (cid and sec):
        raise PlaidError("Plaid is not configured: set the Plaid client ID and secret in SETTINGS -> SECRETS.",
                         code="NOT_CONFIGURED")
    url = ENV_URLS.get(env, ENV_URLS["sandbox"]) + path
    try:
        r = requests.post(url, json=payload, timeout=TIMEOUT,
                          headers={"PLAID-CLIENT-ID": cid, "PLAID-SECRET": sec, "Content-Type": "application/json"})
    except requests.RequestException as e:
        raise PlaidError(f"Could not reach Plaid ({type(e).__name__}).", code="NETWORK")
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 400:
        raise PlaidError(str(body.get("error_message") or f"Plaid returned HTTP {r.status_code}")[:300],
                         code=body.get("error_code") or f"HTTP_{r.status_code}", status=r.status_code,
                         request_id=body.get("request_id"))
    return body


# ---------------------------------------------------------------- API wrappers
def _country_codes():
    return [c.strip().upper() for c in (os.environ.get("PLAID_COUNTRY_CODES") or "US").split(",") if c.strip()]


def products_for(kind):
    env = os.environ.get(f"PLAID_PRODUCTS_{kind.upper()}")           # escape hatch if Plaid changes rules
    prods = [p.strip() for p in env.split(",")] if env else list(PRODUCTS_BY_KIND[kind])
    return [p for p in prods if p and p != "transactions"]            # defence in depth: never transactions


def link_token_create(kind, client_user_id, access_token=None):
    """Hosted Link token. With `access_token` it creates an UPDATE-mode session (re-login) for that
    Item and, per Plaid, omits `products`. Returns {"link_token", "hosted_link_url", "expiration"}."""
    if kind not in PRODUCTS_BY_KIND:
        raise PlaidError("unknown account kind", code="BAD_KIND")
    payload = {"client_name": "Fiduciary", "language": "en", "country_codes": _country_codes(),
               "user": {"client_user_id": client_user_id}, "hosted_link": {"url_lifetime_seconds": LINK_LIFETIME_S}}
    if access_token:
        payload["access_token"] = access_token
    else:
        payload["products"] = products_for(kind)
    b = _post("/link/token/create", payload)
    url = b.get("hosted_link_url")
    if not url:
        raise PlaidError("Plaid did not return a hosted link URL (is Hosted Link enabled for this account?).",
                         code="NO_HOSTED_LINK")
    return {"link_token": b.get("link_token"), "hosted_link_url": url, "expiration": b.get("expiration")}


def link_token_get(link_token):
    """Poll a Hosted Link session. -> {"status": "pending"|"linked"|"exited", "public_tokens": [...],
    "exit": {...}|None}."""
    b = _post("/link/token/get", {"link_token": link_token})
    tokens, exited, finished = [], None, False
    for s in (b.get("link_sessions") or []):
        res = s.get("results") or {}
        for r in (res.get("item_add_results") or []):
            if r.get("public_token"):
                tokens.append(r["public_token"])
        if s.get("exit"):
            exited = s["exit"]
        if s.get("finished_at") or s.get("finished"):
            finished = True
    if tokens:
        return {"status": "linked", "public_tokens": tokens, "exit": None}
    if exited:
        return {"status": "exited", "public_tokens": [], "exit": {"error_code": (exited.get("error") or {}).get("error_code"),
                                                                    "status": exited.get("status")}}
    return {"status": "pending", "public_tokens": [], "exit": None, "finished": finished}


def exchange_public_token(public_token):
    b = _post("/item/public_token/exchange", {"public_token": public_token})
    return b["access_token"], b["item_id"]


def accounts_get(access_token):
    return _post("/accounts/get", {"access_token": access_token})


def holdings_get(access_token):
    return _post("/investments/holdings/get", {"access_token": access_token})


def liabilities_get(access_token):
    return _post("/liabilities/get", {"access_token": access_token})


def item_remove(access_token):
    return _post("/item/remove", {"access_token": access_token})


def institution_name(institution_id):
    if not institution_id:
        return None
    try:
        b = _post("/institutions/get_by_id", {"institution_id": institution_id, "country_codes": _country_codes()})
        return (b.get("institution") or {}).get("name")
    except PlaidError:
        return None


# ---------------------------------------------------------------- item store (encrypted blob)
def _read():
    d = store.read_json(PLAID_ITEMS_FILE, {}) or {}
    d.setdefault("items", [])
    d.setdefault("pending", [])
    return d


def _write(d):
    store.write_json(PLAID_ITEMS_FILE, d)


def list_items():
    with _lock:
        return [dict(i) for i in _read()["items"]]


def public_items():
    """What the browser may see: never the access token."""
    return [{"item_id": i["item_id"], "institution": i.get("institution"), "kind": i.get("kind"),
             "created": i.get("created"), "status": i.get("status", "ok"), "error": i.get("error"),
             "last_ok": i.get("last_ok"), "accounts": i.get("accounts_count")} for i in list_items()]


def start_link(kind, user_id, item_id=None):
    """Create a Hosted Link session (new Item, or update mode for `item_id`). Returns
    {"session_id", "hosted_link_url", "kind"}; the link_token stays server-side."""
    with _lock:
        d = _read()
        access = None
        if item_id:
            it = next((i for i in d["items"] if i["item_id"] == item_id), None)
            if not it:
                raise PlaidError("unknown item", code="NO_SUCH_ITEM")
            access, kind = it["access_token"], it.get("kind") or kind
    t = link_token_create(kind, f"fiduciary-{user_id}", access_token=access)
    sid = _secrets.token_hex(8)
    with _lock:
        d = _read()
        now = time.time()
        d["pending"] = [p for p in d["pending"] if now - p["created"] < PENDING_TTL_S][-9:]
        d["pending"].append({"session_id": sid, "link_token": t["link_token"], "kind": kind, "created": now,
                             "update_item_id": item_id})
        _write(d)
    return {"session_id": sid, "hosted_link_url": t["hosted_link_url"], "kind": kind}


def poll_link(session_id):
    """-> {"status": pending|linked|exited|unknown, "items": [public items added]}. Exchanges any
    public_tokens (new Items) and forgets the session once it is resolved."""
    with _lock:
        d = _read()
        p = next((x for x in d["pending"] if x["session_id"] == session_id), None)
    if not p:
        return {"status": "unknown", "items": []}
    res = link_token_get(p["link_token"])
    if res["status"] == "pending":
        return {"status": "pending", "items": []}
    added = []
    if res["status"] == "linked":
        with _lock:
            d = _read()
            for pt in res["public_tokens"]:
                if p.get("update_item_id"):
                    # update mode: the Item (and token) are unchanged; just clear the login-required flag
                    for i in d["items"]:
                        if i["item_id"] == p["update_item_id"]:
                            i.update(status="ok", error=None)
                            added.append(i)
                    continue
                access, item_id = exchange_public_token(pt)
                accts = accounts_get(access)
                inst_id = (accts.get("item") or {}).get("institution_id")
                rec = {"item_id": item_id, "access_token": access, "institution_id": inst_id,
                       "institution": institution_name(inst_id) or "Institution", "kind": p["kind"],
                       "created": int(time.time()), "status": "ok", "error": None,
                       "accounts_count": len(accts.get("accounts") or [])}
                d["items"] = [i for i in d["items"] if i["item_id"] != item_id] + [rec]
                added.append(rec)
            d["pending"] = [x for x in d["pending"] if x["session_id"] != session_id]
            _write(d)
    else:
        with _lock:
            d = _read()
            d["pending"] = [x for x in d["pending"] if x["session_id"] != session_id]
            _write(d)
    return {"status": res["status"], "exit": res.get("exit"),
            "items": [{"item_id": i["item_id"], "institution": i.get("institution"), "kind": i.get("kind")} for i in added]}


def remove_item(item_id):
    """Tell Plaid first (that is what stops billing), then forget the token. If Plaid says the Item
    is already gone we still delete locally; any other failure keeps the record so it can be retried."""
    with _lock:
        it = next((i for i in _read()["items"] if i["item_id"] == item_id), None)
    if not it:
        raise PlaidError("unknown item", code="NO_SUCH_ITEM")
    try:
        item_remove(it["access_token"])
    except PlaidError as e:
        if e.code not in ("ITEM_NOT_FOUND", "INVALID_ACCESS_TOKEN"):
            raise
    with _lock:
        d = _read()
        d["items"] = [i for i in d["items"] if i["item_id"] != item_id]
        _write(d)
    return True


def mark_item(item_id, status, error=None, ok=False):
    with _lock:
        d = _read()
        for i in d["items"]:
            if i["item_id"] == item_id:
                i["status"], i["error"] = status, (str(error)[:200] if error else None)
                if ok:
                    i["last_ok"] = int(time.time())
        _write(d)


# ---------------------------------------------------------------- pure mapping (no I/O)
_SUBTYPE_LABELS = {
    "checking": "Checking", "savings": "Savings", "cd": "CD", "money market": "Money Market", "hsa": "HSA",
    "cash management": "Cash Management", "paypal": "PayPal", "prepaid": "Prepaid",
    "401k": "401(k)", "401a": "401(a)", "403b": "403(b)", "457b": "457(b)", "ira": "Traditional IRA",
    "roth": "Roth IRA", "roth 401k": "Roth 401(k)", "sep ira": "SEP IRA", "simple ira": "SIMPLE IRA",
    "529": "529", "brokerage": "Brokerage", "pension": "Pension", "retirement": "Retirement",
    "credit card": "Credit Card", "paypal credit": "PayPal Credit", "line of credit": "Line of Credit",
    "home equity": "HELOC", "mortgage": "Mortgage", "student": "Student Loan", "auto": "Auto Loan",
    "personal": "Personal Loan", "loan": "Loan", "consumer": "Consumer Loan", "commercial": "Commercial Loan",
}
_SEC_TYPES = {"equity": "Stock", "etf": "ETF", "mutual fund": "Mutual Fund", "fixed income": "Bond",
              "cash": "Cash", "cryptocurrency": "Crypto", "derivative": "Option", "loan": "Loan", "other": "Other"}


def _f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def subtype_label(a):
    return _SUBTYPE_LABELS.get((a.get("subtype") or "").lower())


# The app classifies tax status from the account NAME (Roth/IRA/401/HSA...). When Plaid's subtype says
# something the name does not, append the subtype label -- but only if the keyword that classification
# needs is missing (so "Plaid 401k" is left alone, while a bare "Brokerage Account" that Plaid says is a
# Roth IRA becomes "Brokerage Account · Roth IRA").
_TAX_KEYWORD = {"roth": "ROTH", "roth 401k": "ROTH", "hsa": "HSA", "ira": "IRA", "sep ira": "IRA",
                "simple ira": "IRA", "401k": "401", "401a": "401", "403b": "403", "457b": "457", "529": "529"}


def account_name(a):
    """'<name>[ · <tax subtype>] (...1234)'."""
    base = (a.get("official_name") or a.get("name") or "Account").strip()
    sub = (a.get("subtype") or "").lower()
    kw, lab = _TAX_KEYWORD.get(sub), subtype_label(a)
    if kw and lab and kw not in base.upper():
        base = f"{base} · {lab}"
    mask = a.get("mask")
    return f"{base} (...{mask})" if mask else base


def account_type_display(a):
    t = a.get("type")
    if t == "investment":
        return "Investments"
    return subtype_label(a) or {"depository": "Cash", "credit": "Credit Card", "loan": "Loan"}.get(t) or "Other"


def is_liability(a):
    return a.get("type") in ("credit", "loan")


def map_item(item, accounts_resp, holdings_resp=None, liabilities_resp=None):
    """Normalize one Item. Returns {"accounts", "positions", "by_account", "inv", "balance_only"} in the
    shape the other providers produce, plus per-account debt defaults (APR / minimum payment) taken
    from /liabilities/get. No network, no globals."""
    inst = item.get("institution") or "Institution"
    liab_by_id = {}
    for kind, rows in ((liabilities_resp or {}).get("liabilities") or {}).items():
        for r in rows or []:
            aid = r.get("account_id")
            apr = min_pay = due = None
            if kind == "credit":
                aprs = r.get("aprs") or []
                purchase = next((x for x in aprs if (x.get("apr_type") or "") == "purchase_apr"), None) or (aprs[0] if aprs else None)
                apr = _f((purchase or {}).get("apr_percentage"))
                min_pay, due = _f(r.get("minimum_payment_amount")), r.get("next_payment_due_date")
            elif kind == "mortgage":
                apr = _f((r.get("interest_rate") or {}).get("percentage"))
                min_pay, due = _f(r.get("next_monthly_payment")), r.get("next_payment_due_date")
            elif kind == "student":
                apr = _f(r.get("interest_rate_percentage"))
                min_pay, due = _f(r.get("minimum_payment_amount")), r.get("next_payment_due_date")
            liab_by_id[aid] = {"apr_pct": apr, "min_payment": min_pay, "next_payment_due": due, "liability_kind": kind}

    secs = {s["security_id"]: s for s in (holdings_resp or {}).get("securities") or [] if s.get("security_id")}
    holds_by_acct = {}
    for h in (holdings_resp or {}).get("holdings") or []:
        holds_by_acct.setdefault(h.get("account_id"), []).append(h)

    accounts, positions, by_account, inv, balance_only = [], [], [], [], []
    for a in accounts_resp.get("accounts") or []:
        bal = a.get("balances") or {}
        cur = _f(bal.get("current"))
        if cur is None:
            cur = _f(bal.get("available"))
        cur = cur or 0.0
        name = account_name(a)
        liab = is_liability(a)
        rec = {"id": "plaid:" + str(a.get("account_id")), "name": name, "institution": inst,
               "type": account_type_display(a), "balance": round(abs(cur) if liab else cur, 2),
               "holdings_count": 0, "is_asset": not liab, "include_nw": True,
               "source": "plaid", "plaid_item_id": item.get("item_id")}
        rec.update({k: v for k, v in (liab_by_id.get(a.get("account_id")) or {}).items() if v is not None})
        hs = holds_by_acct.get(a.get("account_id")) or []
        comp = []
        for h in hs:
            s = secs.get(h.get("security_id")) or {}
            stype = _SEC_TYPES.get((s.get("type") or "").lower(), "Other")
            qty, price = _f(h.get("quantity")), _f(h.get("institution_price"))
            val = _f(h.get("institution_value"))
            if val is None and qty is not None and price is not None:
                val = qty * price
            if not qty or val is None:
                continue
            cash = stype == "Cash" or bool(s.get("is_cash_equivalent") and not s.get("ticker_symbol"))
            tkr = "CASH" if cash else (s.get("ticker_symbol") or "").upper().strip()
            cost = _f(h.get("cost_basis"))               # Plaid: total cost basis for the position
            suspect = cost is None or cost <= 0 or (val > 0 and cost > val * 50)
            cost_total = None if (cash or suspect) else round(cost, 2)
            gain = round(val - cost_total, 2) if cost_total is not None else None
            comp.append({"ticker": tkr or (s.get("name") or "?"), "name": "Cash" if cash else (s.get("name") or tkr),
                         "type": "Cash" if cash else stype, "quantity": round(qty, 4),
                         "price": 1.0 if cash else (price if price is not None else _f(s.get("close_price"))),
                         "value": round(val, 2), "cost_total": cost_total, "gain": gain,
                         "gain_pct": round(gain / cost_total * 100, 2) if (gain is not None and cost_total) else None,
                         "day_pct": None, "suspect": bool(suspect and not cash),
                         # a fund Plaid cannot give a ticker for (typically a 401(k) CIT/collective fund)
                         "is_cit": (not tkr and stype in ("Mutual Fund", "Other"))})
        if comp:
            rec["holdings_count"] = len(comp)
            tv = round(sum(c["value"] for c in comp), 2)
            by_account.append({"account": name, "institution": inst, "value": tv, "balance": rec["balance"], "holdings": comp})
            for c in comp:
                c2 = dict(c); c2["account"], c2["institution"] = name, inst
                positions.append(c2)
            inv.append(rec)
        elif a.get("type") == "investment" and rec["balance"]:
            balance_only.append(rec)
        accounts.append(rec)
    return {"accounts": accounts, "positions": positions, "by_account": by_account, "inv": inv,
            "balance_only": balance_only}


def build_portfolio():
    """Fetch every stored Item and merge. One failing Item never sinks the dashboard (it is recorded
    on the Item and shown in SETTINGS); only 'no connected institutions' or 'every Item failed'
    raises. Returns the kwargs for app._derive() plus the per-item status list."""
    items = list_items()
    if not items:
        raise RuntimeError("Plaid has no connected institutions yet — open SETTINGS → ACCOUNTS → Plaid "
                           "and connect one, or switch the portfolio data source back.")
    merged = {"accounts": [], "positions": [], "by_account": [], "inv": [], "balance_only": []}
    ok_items, errors = 0, []
    for it in items:
        try:
            tok = it["access_token"]
            accts = accounts_get(tok)
            hold = liab = None
            if any(a.get("type") == "investment" for a in accts.get("accounts") or []):
                try:
                    hold = holdings_get(tok)
                except PlaidError as e:           # e.g. product not enabled on this Item
                    if e.login_required:
                        raise
            if any(is_liability(a) for a in accts.get("accounts") or []):
                try:
                    liab = liabilities_get(tok)
                except PlaidError as e:
                    if e.login_required:
                        raise
            m = map_item(it, accts, hold, liab)
            for k in merged:
                merged[k].extend(m[k])
            mark_item(it["item_id"], "ok", ok=True)
            ok_items += 1
        except PlaidError as e:
            errors.append((it, e))
            mark_item(it["item_id"], "login_required" if e.login_required else "error", error=e)
            merged["by_account"].append({"account": it.get("institution") or "Plaid", "institution": it.get("institution"),
                                         "value": 0, "error": ("Needs re-login — SETTINGS → ACCOUNTS → Plaid → RECONNECT"
                                                              if e.login_required else str(e)), "holdings": []})
    if not ok_items:
        raise RuntimeError("Plaid: every connected institution failed — " + "; ".join(
            f"{it.get('institution')}: {e}" for it, e in errors)[:400])
    accts = merged["accounts"]
    assets = round(sum(a["balance"] for a in accts if a["is_asset"]), 2)
    liabs = round(sum(a["balance"] for a in accts if not a["is_asset"]), 2)
    merged.update(net_worth=round(assets - liabs, 2), total_assets=assets, total_liabilities=liabs,
                  item_errors=[{"institution": it.get("institution"), "code": e.code} for it, e in errors])
    return merged
