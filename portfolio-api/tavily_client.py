# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
tavily_client.py -- Tavily news search called DIRECTLY (2026-10-21), replacing the mcpo -> mcp-tavily -> mcp chain.

WHY: news search used to go portfolio-api -> mcpo -> the third-party `mcp-tavily` package (downloaded by uvx at mcpo
start) -> the `mcp` library -> Tavily. Two incidents came from that chain: `mcp` 2.x broke `mcp-tavily` (pinned `mcp<2`
as a workaround) and a missing key left the helper unregistered (HTTP 404 from mcpo). Tavily has a plain documented REST API
(POST https://api.tavily.com/search, Bearer key, `topic: "news"`), so this is one `requests` call with structured JSON
results (title, url, content, published_date, score) -- no helper process, no package download, no parsing of a text blob.
Same thin-client approach as plaid_integration.py: no SDK, no new dependency.

KEY: read on EVERY call (app.py injects the lookup with configure()), so a key saved in SETTINGS -> SECRETS & KEYS applies
at once. With no key, search() says exactly where to add it. (The old mcpo fallback was removed 2026-10-21 after the direct
path was verified live; the `tavily` mcpo entry and its `mcp<2` pin are gone too.)

Nothing here imports app.py. The key is never logged and never appears in an error detail.
"""
import datetime
import requests

API_URL = "https://api.tavily.com/search"
USAGE_URL = "https://api.tavily.com/usage"
_key_getter = lambda: ""


def configure(get_key):
    """get_key() -> the Tavily API key (or ""). Called per request so saved secrets apply live."""
    global _key_getter
    _key_getter = get_key


def configured():
    return bool((_key_getter() or "").strip())


def time_range_for(days):
    d = int(days or 0)
    return "day" if d <= 1 else ("week" if d <= 7 else ("month" if d <= 31 else "year"))


def _payload(query, days, max_results, exact_dates=True):
    p = {"query": query, "topic": "news", "search_depth": "basic", "max_results": max(1, min(int(max_results or 5), 20)),
         "include_answer": False, "include_raw_content": False, "include_images": False}
    if exact_dates and days:
        p["start_date"] = (datetime.date.today() - datetime.timedelta(days=int(days))).isoformat()
    elif days:
        p["time_range"] = time_range_for(days)
    return p


def error_detail(status, body_text=""):
    """One readable sentence per failure: what is wrong, then what to do (shown on RESEARCH -> ALERTS and logged)."""
    snippet = (body_text or "")[:160].replace("\n", " ")
    if status == 401:
        return ("Tavily rejected the API key (HTTP 401). Re-enter it under SETTINGS -> SECRETS & KEYS -> Tavily "
                "(get or check it at app.tavily.com); no restart is needed.")
    if status == 429:
        return "Tavily is rate-limiting requests (HTTP 429). Searches retry on the next news pull."
    if status == 432:
        return ("Your Tavily plan's credit limit is used up (HTTP 432). Check usage at app.tavily.com or upgrade; "
                "cached news keeps showing meanwhile.")
    if status == 433:
        return "Tavily's pay-as-you-go limit was exceeded (HTTP 433). Raise the limit at app.tavily.com."
    if status == 400:
        return f"Tavily rejected the search request (HTTP 400). Detail: {snippet}"
    if status >= 500:
        return f"Tavily is having a problem (HTTP {status}); it retries on the next news pull."
    return f"Tavily search failed (HTTP {status}). Detail: {snippet}"


def search(query, days=14, max_results=5, timeout=30):
    """-> {"ok": bool, "articles": [...], "http": int|None, "detail": str}. Never raises, never includes the key."""
    key = (_key_getter() or "").strip()
    if not key:
        return {"ok": False, "articles": [], "http": None,
                "detail": "No Tavily API key is set. Add it under SETTINGS -> SECRETS & KEYS -> Tavily (applies immediately)."}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        r = requests.post(API_URL, headers=headers, json=_payload(query, days, max_results), timeout=timeout)
        if r.status_code == 400:     # some topic/date combinations reject exact dates: retry once with a relative window
            r = requests.post(API_URL, headers=headers, json=_payload(query, days, max_results, exact_dates=False), timeout=timeout)
        if r.status_code >= 400:
            return {"ok": False, "articles": [], "http": r.status_code, "detail": error_detail(r.status_code, r.text)}
        data = r.json()
    except ValueError:
        return {"ok": False, "articles": [], "http": None, "detail": "Tavily returned something that was not JSON."}
    except requests.RequestException as e:
        return {"ok": False, "articles": [], "http": None,
                "detail": f"Could not reach Tavily ({type(e).__name__}) -- check this machine's internet/DNS."}
    arts = []
    for it in (data.get("results") or []) if isinstance(data, dict) else []:
        if isinstance(it, dict) and (it.get("title") or it.get("url")):
            arts.append({"title": it.get("title") or "", "url": it.get("url"), "content": it.get("content") or "",
                         "published_date": it.get("published_date"), "score": it.get("score")})
    return {"ok": True, "articles": arts, "http": r.status_code, "detail": ""}


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def usage(timeout=15):
    """GET /usage (costs NO credits; Tavily limits it to 10 requests per 10 minutes). Used as the budget gate's
    recovery probe. -> {"ok","http","detail","plan_usage","plan_limit","key_usage","key_limit","paygo"}.
    The response shape is parsed defensively (nested "account"/"key" objects or flat keys); fields that are
    missing come back None -- the caller then cannot tell "exhausted" and must not assume it. Never raises,
    never includes the key."""
    key = (_key_getter() or "").strip()
    out = {"ok": False, "http": None, "detail": "", "plan_usage": None, "plan_limit": None,
           "key_usage": None, "key_limit": None, "paygo": None, "paygo_usage": None, "paygo_limit": None}
    if not key:
        out["detail"] = "No Tavily API key is set."
        return out
    try:
        r = requests.get(USAGE_URL, headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
    except requests.RequestException as e:
        out["detail"] = f"Could not reach Tavily ({type(e).__name__})."
        return out
    out["http"] = r.status_code
    if r.status_code >= 400:
        out["detail"] = error_detail(r.status_code, "")
        return out
    try:
        data = r.json()
    except ValueError:
        out["detail"] = "Tavily /usage returned something that was not JSON."
        return out
    d = data if isinstance(data, dict) else {}
    acct = d.get("account") if isinstance(d.get("account"), dict) else d
    kobj = d.get("key") if isinstance(d.get("key"), dict) else d
    pg_use, pg_lim = _num(acct.get("paygo_usage")), _num(acct.get("paygo_limit"))
    pg = acct.get("paygo") if isinstance(acct.get("paygo"), bool) else (True if (pg_lim or 0) > 0 else
                                                                         (None if pg_lim is None else False))
    out.update(ok=True, plan_usage=_num(acct.get("plan_usage")), plan_limit=_num(acct.get("plan_limit")),
               key_usage=_num(kobj.get("usage")), key_limit=_num(kobj.get("limit")),
               paygo=pg, paygo_usage=pg_use, paygo_limit=pg_lim)
    return out


def as_text(articles, limit=2500):
    """Compact 'Title/URL/Published/Content' blob for the LLM brief prompt (same shape the old mcpo text had)."""
    out = []
    for a in articles:
        out.append(f"Title: {a.get('title','')}\nURL: {a.get('url','')}\n"
                   + (f"Published: {a['published_date']}\n" if a.get("published_date") else "")
                   + f"Content: {(a.get('content') or '')[:500]}\n")
    return "\n".join(out)[:limit]
