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

    owner = next((e for e in root.iter() if _ln(e.tag) == "reportingOwner"), None)
    owner_name = _first_text(owner, "rptOwnerName")
    rel = next((e for e in root.iter() if _ln(e.tag) == "reportingOwnerRelationship"), None)

    def _flag(name):
        v = _first_text(rel, name)
        return str(v).strip().lower() in ("1", "true") if v is not None else False

    is_officer = _flag("isOfficer")
    is_director = _flag("isDirector")
    is_ten_pct = _flag("isTenPercentOwner")
    title = _first_text(rel, "officerTitle")

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
            "owner": owner_name, "title": title, "is_officer": is_officer,
            "is_director": is_director, "is_ten_pct": is_ten_pct,
            "date": doc_date, "transactions": txns}


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
    other = {}          # non-open-market code -> count (context)
    latest = None
    for f in filings:
        who = (f.get("owner") or "").strip()
        for t in f.get("transactions") or []:
            code = (t.get("code") or "").upper()
            d = t.get("date") or f.get("date")
            if d and (latest is None or d > latest):
                latest = d
            if code == "P":
                buy_count += 1
                buy_value += t.get("value") or 0.0
                buy_shares += t.get("shares") or 0.0
                if who:
                    buyers.add(who)
            elif code == "S":
                sell_count += 1
                sell_value += t.get("value") or 0.0
                sell_shares += t.get("shares") or 0.0
                if who:
                    sellers.add(who)
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
            "other_transactions": {TRANSACTION_CODES.get(k, k): v for k, v in sorted(other.items())},
            "note": "Open-market insider purchases (code P) vs sales (code S) only; grants, option "
                    "exercises and tax-withholding are compensation mechanics, shown as context. "
                    "Cluster buying (several insiders at once) is the higher-signal event; selling "
                    "is noisy (diversification/taxes/liquidity/10b5-1 plans). CONTEXT ONLY — never a "
                    "standalone buy/sell signal."}
