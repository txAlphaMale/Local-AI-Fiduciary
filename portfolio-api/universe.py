# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""universe.py -- S0 of the idea scavenger (design: IDEAS-SCAVENGER-DESIGN.md): every common stock listed on a US exchange.

Sources, all free and needing no API key (so 0 budget-gate calls except one SEC file):
  * Nasdaq Trader symbol directory (nasdaqlisted.txt, otherlisted.txt): exchange, test-issue flag, ETF flag, financial status
  * SEC files/company_tickers_exchange.json: ticker -> CIK (the key into SEC's bulk company facts)
The join is on the ticker. ETFs, test issues, and non-common instruments (warrants, rights, units, preferreds, notes) are
dropped and COUNTED, so the funnel can say how many names went where. A stock with no SEC CIK is kept (cik None): the screen
decides what to do with it. Tickers use SEC's dash form for share classes (BRK-B), matching the rest of the app."""
import os
import re

NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
OTHER_URL = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"
SEC_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"

# otherlisted.txt "Exchange" codes
OTHER_EXCHANGES = {"A": "NYSE American", "N": "NYSE", "P": "NYSE Arca", "Z": "Cboe BZX", "V": "IEX"}
NASDAQ_TIERS = {"Q": "Nasdaq Global Select", "G": "Nasdaq Global", "S": "Nasdaq Capital"}

# Security-name patterns for instruments that are not common stock (or ADR of common stock)
_NOT_COMMON = re.compile(
    r"\b(warrants?|rights?|units?|preferred|pfd|depositary shares|notes? due|senior notes|subordinated|debentures?|"
    r"trust preferred|bond|baby bond|contingent value|when[- ]issued|beneficial interest)\b|%|\bETN\b|\bETF\b|\bFund\b",
    re.I)


def norm_symbol(sym):
    """SEC's form: upper-case, share-class separators as a dash (BRK.B / BRK$B / BRK B -> BRK-B)."""
    return re.sub(r"[.$\s/]+", "-", (sym or "").strip().upper())


def _rows(text, header_start):
    lines = [l for l in (text or "").splitlines() if l.strip()]
    if not lines or not lines[0].startswith(header_start):
        return []
    cols = lines[0].split("|")
    out = []
    for l in lines[1:]:
        if l.startswith("File Creation Time"):
            continue
        parts = l.split("|")
        if len(parts) >= len(cols):
            out.append(dict(zip(cols, parts)))
    return out


def parse_nasdaq_listed(text):
    out = []
    for r in _rows(text, "Symbol"):
        out.append({"symbol": r.get("Symbol", ""), "name": r.get("Security Name", ""),
                    "exchange": NASDAQ_TIERS.get(r.get("Market Category", ""), "Nasdaq"),
                    "test": r.get("Test Issue") == "Y", "etf": r.get("ETF") == "Y",
                    "fin_status": (r.get("Financial Status") or "N").strip() or "N"})
    return out


def parse_other_listed(text):
    out = []
    for r in _rows(text, "ACT Symbol"):
        out.append({"symbol": r.get("NASDAQ Symbol") or r.get("ACT Symbol", ""), "name": r.get("Security Name", ""),
                    "exchange": OTHER_EXCHANGES.get(r.get("Exchange", ""), r.get("Exchange", "")),
                    "test": r.get("Test Issue") == "Y", "etf": r.get("ETF") == "Y", "fin_status": "N"})
    return out


def parse_sec_exchange(obj):
    """{'fields': [cik, name, ticker, exchange], 'data': [[...]]} -> {TICKER: 10-digit CIK string}."""
    fields = (obj or {}).get("fields") or []
    try:
        ic, it = fields.index("cik"), fields.index("ticker")
    except ValueError:
        return {}
    m = {}
    for row in (obj or {}).get("data") or []:
        try:
            t = norm_symbol(row[it])
            if t and t not in m:
                m[t] = str(int(row[ic])).zfill(10)
        except (TypeError, ValueError, IndexError):
            continue
    return m


def build_universe(nasdaq_text, other_text, sec_obj):
    """-> (rows, funnel). rows: [{ticker, name, exchange, cik, fin_status}] for common stocks only."""
    cik_by_t = parse_sec_exchange(sec_obj)
    listed = parse_nasdaq_listed(nasdaq_text) + parse_other_listed(other_text)
    fun = {"listed": len(listed), "test_issues": 0, "etfs": 0, "not_common": 0, "duplicates": 0,
           "stocks": 0, "with_cik": 0, "no_cik": 0}
    seen, rows = set(), []
    for r in listed:
        if r["test"]:
            fun["test_issues"] += 1
            continue
        if r["etf"]:
            fun["etfs"] += 1
            continue
        if _NOT_COMMON.search(r["name"] or ""):
            fun["not_common"] += 1
            continue
        t = norm_symbol(r["symbol"])
        if not t or t in seen:
            fun["duplicates"] += 1
            continue
        seen.add(t)
        cik = cik_by_t.get(t)
        rows.append({"ticker": t, "name": r["name"], "exchange": r["exchange"], "cik": cik, "fin_status": r["fin_status"]})
    fun["stocks"] = len(rows)
    fun["with_cik"] = sum(1 for r in rows if r["cik"])
    fun["no_cik"] = fun["stocks"] - fun["with_cik"]
    return rows, fun


def fetch_sources(sec_get, plain_get, cache_dir):
    """Download the three sources; on a failure fall back to the last saved copy (flagged stale). Raises if a source has
    neither. `sec_get(url)` / `plain_get(url)` return a requests-like response; passed in so the SEC one rides the budget gate."""
    os.makedirs(cache_dir, exist_ok=True)
    out, stale = {}, []
    for key, url, getter in (("nasdaq", NASDAQ_URL, plain_get), ("other", OTHER_URL, plain_get),
                             ("sec", SEC_EXCHANGE_URL, sec_get)):
        path = os.path.join(cache_dir, key + (".json" if key == "sec" else ".txt"))
        text = None
        try:
            r = getter(url)
            if r.status_code == 200 and len(r.content) > 1000:
                text = r.text
                tmp = path + ".part"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(text)
                os.replace(tmp, path)
        except Exception:
            text = None
        if text is None:
            if not os.path.exists(path):
                raise RuntimeError(f"universe source '{key}' unavailable and no saved copy")
            with open(path, encoding="utf-8") as f:
                text = f.read()
            stale.append(key)
        out[key] = text
    return out, stale
