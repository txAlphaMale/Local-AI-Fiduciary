# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
insider.py — SEC Form 4 insider-transaction signal (Fable G15). Context-only.

Corporate insiders (officers, directors, 10% owners) must report trades in their own company's
stock on Form 4 within two business days. Aggregated OPEN-MARKET insider buying/selling is a
widely-followed (never decisive) sentiment read: insiders sell for many reasons — diversification,
taxes, liquidity, scheduled 10b5-1 plans — but rarely buy on the open market except from
conviction, so CLUSTER BUYING (several insiders buying at once) is the higher-signal event, while
selling is noisy. This module parses the Form 4 ownership XML and reduces a window of filings to a
net open-market buy/sell picture.

Pure logic + parsing: the app supplies the raw filing XML (via edgar.recent_form4); this module
only parses and aggregates, so it is fully offline-testable. It deliberately SCORES only
open-market purchases (code P) and sales (code S). Grants/awards (A), option exercises (M), and
tax-withholding dispositions (F) are compensation mechanics, not conviction — they are tallied
separately as context, never in the signal.

CONTEXT-ONLY, never a standalone buy/sell signal. Reverse-lookup of 13F institutional ownership
and short interest are NOT available free from SEC on a per-security basis; those are deferred to a
data provider (the "mini-Bloomberg" MCP evaluation), not fabricated here.
"""

import xml.etree.ElementTree as ET

# Form 4 nonDerivative transaction codes. Only P (open-market purchase) and S (open-market sale)
# carry the conviction signal; the rest are compensation / mechanical and shown as context only.
TRANSACTION_CODES = {
    "P": "Open-market purchase", "S": "Open-market sale",
    "A": "Grant/award (compensation)", "M": "Option/derivative exercise",
    "F": "Shares withheld for taxes", "G": "Gift", "C": "Conversion of derivative",
    "X": "Exercise of in-the-money derivative", "D": "Disposition to the issuer",
    "V": "Voluntary early report", "J": "Other (see footnotes)",
}
SIGNAL_CODES = ("P", "S")   # the two open-market conviction codes


def _ln(tag):
    """Local (namespace-stripped) tag name."""
    return tag.rsplit("}", 1)[-1]


def _first_text(elem, name):
    """Text of the first descendant with local-name == name (or None)."""
    if elem is None:
        return None
    for e in elem.iter():
        if _ln(e.tag) == name and (e.text or "").strip():
            return e.text.strip()
    return None


def _value_of(elem, name):
    """Form 4 wraps most fields as <field><value>X</value></field>; return the inner <value>
    (or the field's own text if it isn't wrapped). None if absent."""
    if elem is None:
        return None
    for e in elem.iter():
        if _ln(e.tag) == name:
            for c in e.iter():
                if _ln(c.tag) == "value" and (c.text or "").strip():
                    return c.text.strip()
            if (e.text or "").strip():
                return e.text.strip()
    return None


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_form4(xml):
    """Parse one Form 4 ownershipDocument XML into
    {issuer, symbol, owner, title, is_officer, is_director, is_ten_pct, date, transactions:[...]}.
    Each transaction: {date, code, code_label, shares, price, value, acquired(bool|None), open_market}.
    Returns None on unparseable/empty input."""
    if not xml:
        return None
    try:
        root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    except Exception:
        return None

    issuer = next((e for e in root.iter() if _ln(e.tag) == "issuer"), None)
    symbol = _value_of(issuer, "issuerTradingSymbol") or _first_text(issuer, "issuerTradingSymbol")
    issuer_name = _first_text(issuer, "issuerName")

    def _flag_of(rel, name):
        v = _first_text(rel, name)
        return str(v).strip().lower() in ("1", "true") if v is not None else False

    # A joint filing lists several reporting owners (a fund and its manager, say); each has its own relationship flags.
    owners = []
    for o in root.iter():
        if _ln(o.tag) != "reportingOwner":
            continue
        rel_o = next((e for e in o.iter() if _ln(e.tag) == "reportingOwnerRelationship"), None)
        owners.append({"name": _first_text(o, "rptOwnerName"), "title": _first_text(rel_o, "officerTitle"),
                       "is_officer": _flag_of(rel_o, "isOfficer"), "is_director": _flag_of(rel_o, "isDirector"),
                       "is_ten_pct": _flag_of(rel_o, "isTenPercentOwner")})
    first = owners[0] if owners else {}
    owner_name, title = first.get("name"), first.get("title")
    is_officer, is_director, is_ten_pct = (bool(first.get("is_officer")), bool(first.get("is_director")),
                                           bool(first.get("is_ten_pct")))

    doc_date = None
    for e in root.iter():
        if _ln(e.tag) == "periodOfReport" and (e.text or "").strip():
            doc_date = e.text.strip()
            break

    txns = []
    for tx in root.iter():
        if _ln(tx.tag) != "nonDerivativeTransaction":
            continue
        date = _value_of(tx, "transactionDate")
        code = (_value_of(tx, "transactionCode") or "").upper() or None
        shares = _f(_value_of(tx, "transactionShares"))
        price = _f(_value_of(tx, "transactionPricePerShare"))
        ad = _value_of(tx, "transactionAcquiredDisposedCode")
        acquired = (ad.strip().upper() == "A") if ad else None
        value = round(shares * price, 2) if (shares is not None and price is not None) else None
        txns.append({"date": date, "code": code,
                     "code_label": TRANSACTION_CODES.get(code, code),
                     "shares": shares, "price": price, "value": value,
                     "acquired": acquired, "open_market": code in SIGNAL_CODES})

    return {"issuer": issuer_name, "symbol": (symbol.upper() if symbol else None),
            "owner": owner_name, "owners": owners, "title": title, "is_officer": is_officer,
            "is_director": is_director, "is_ten_pct": is_ten_pct,
            "date": doc_date, "transactions": txns}


def _filing_actors(f):
    """-> (names of the officers/directors on a parsed Form 4, True if there is at least one, names of the other reporting owners). A filing whose reporting owners are
    all 10% holders (a fund, a parent company) has none: its trades are reported separately and never counted as insiders buying.
    A filing parsed without relationship information (older records) is treated as an insider filing, as before."""
    owners = f.get("owners")
    if owners is None:
        if any(k in f for k in ("is_officer", "is_director", "is_ten_pct")):
            owners = [{"name": f.get("owner"), "is_officer": f.get("is_officer"), "is_director": f.get("is_director")}]
        else:
            n = (f.get("owner") or "").strip()
            return ([n] if n else []), True, []
    ins = [(o.get("name") or "").strip() for o in owners if o.get("is_officer") or o.get("is_director")]
    others = [(o.get("name") or "unnamed holder").strip() for o in owners if not (o.get("is_officer") or o.get("is_director"))]
    return [n for n in ins if n], bool(ins), others


def aggregate(filings, ticker=None, days=None):
    """Reduce a list of parsed Form 4 filings (from parse_form4) to a net OPEN-MARKET buy/sell
    picture. Only codes P (open-market purchase) and S (open-market sale) are scored; other codes
    are tallied as context. Returns a context-only signal dict with a −100..+100 `net_buy_pct`
    (usable as a G14 exit-trigger metric) and a categorical `signal`."""
    filings = [f for f in (filings or []) if f]
    buy_value = sell_value = 0.0
    buy_shares = sell_shares = 0.0
    buy_count = sell_count = 0
    buyers, sellers = set(), set()
    hold = {"buy": {"count": 0, "value": 0.0, "who": set()}, "sell": {"count": 0, "value": 0.0, "who": set()}}   # 10% holders who are neither officer nor director
    other = {}          # non-open-market code -> count (context)
    latest = None
    for f in filings:
        names, is_insider, holder_names = _filing_actors(f)
        for t in f.get("transactions") or []:
            code = (t.get("code") or "").upper()
            d = t.get("date") or f.get("date")
            if d and (latest is None or d > latest):
                latest = d
            if code in ("P", "S") and not is_insider:
                side = hold["buy" if code == "P" else "sell"]       # a fund or other large holder: reported, never scored
                side["count"] += 1
                side["value"] += t.get("value") or 0.0
                side["who"].update(holder_names or ["unnamed holder"])
            elif code == "P":
                buy_count += 1
                buy_value += t.get("value") or 0.0
                buy_shares += t.get("shares") or 0.0
                buyers.update(names)
            elif code == "S":
                sell_count += 1
                sell_value += t.get("value") or 0.0
                sell_shares += t.get("shares") or 0.0
                sellers.update(names)
            elif code:
                other[code] = other.get(code, 0) + 1

    gross = buy_value + sell_value
    net_value = round(buy_value - sell_value, 2)
    net_buy_pct = round(net_value / gross * 100, 1) if gross else None
    n_buyers, n_sellers = len(buyers), len(sellers)

    if buy_count == 0 and sell_count == 0:
        signal = "no open-market activity"
    elif n_buyers >= 2 and net_value > 0:
        signal = "cluster buying"
    elif net_value > 0:
        signal = "net buying"
    elif n_sellers >= 2 and net_value < 0:
        signal = "cluster selling"
    elif net_value < 0:
        signal = "net selling"
    else:
        signal = "balanced"

    return {"ticker": (ticker.upper() if ticker else None),
            "filings": len(filings), "lookback_days": days, "as_of": latest,
            "buy": {"count": buy_count, "insiders": n_buyers,
                    "shares": round(buy_shares, 2), "value": round(buy_value, 2)},
            "sell": {"count": sell_count, "insiders": n_sellers,
                     "shares": round(sell_shares, 2), "value": round(sell_value, 2)},
            "net_value": net_value, "net_buy_pct": net_buy_pct, "signal": signal,
            "holders_not_insiders": {k: {"count": v["count"], "holders": len(v["who"]), "value": round(v["value"], 2)}
                                     for k, v in hold.items()},
            "other_transactions": {TRANSACTION_CODES.get(k, k): v for k, v in sorted(other.items())},
            "note": "Open-market insider purchases (code P) vs sales (code S) only; grants, option "
                    "exercises and tax-withholding are compensation mechanics, shown as context. "
                    "Cluster buying (several insiders at once) is the higher-signal event; selling "
                    "is noisy (diversification/taxes/liquidity/10b5-1 plans). CONTEXT ONLY — never a "
                    "standalone buy/sell signal."}
