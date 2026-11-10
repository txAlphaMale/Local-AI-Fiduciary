# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
cashdetect.py -- READ-ONLY heuristics that guess which accounts and holdings are CASH, and whether that cash is "brokerage cash"
(sitting with investments, e.g. a sweep or money-market fund) or "bank cash" (savings, checking, emergency fund).

This module only produces GUESSES with reasons and a confidence, for the admin CASH CHECK panel. Nothing here changes any number
the dashboard or the planning engines use. The aim is to look at real accounts (Monarch, Plaid, SnapTrade, CSV) and tune the rules from
evidence before cash designation is built on top of them.

Signals, strongest first:
  * the security itself: typed Cash, a known money-market ticker, "money market"/"sweep" in its name, or a stable $1.00 NAV;
  * the account: "cash"/"money market"/"sweep" in the name, the provider's account type/subtype;
  * pairing: a cash-looking account whose institution and name stem match an investment account ("WF IRA Cash" next to "WF IRA").
A person's explicit designation (not built yet) will override all of these.
"""
import re

# Money-market (and cash-sweep) funds that brokerages use as the cash position. Not exhaustive: the NAV and name signals catch others.
MONEY_MARKET_TICKERS = frozenset({
    "VMFXX", "VMMXX", "VMRXX", "VUSXX", "VCTXX",                    # Vanguard
    "SPAXX", "FDRXX", "FZFXX", "FZDXX", "FCASH", "FDLXX",           # Fidelity
    "SWVXX", "SNVXX", "SNOXX", "SWTXX", "SWGXX",                    # Schwab
    "TTTXX", "TRPXX", "PRTXX", "PRMXX",                             # T. Rowe Price
    "BRPXX", "CBXXX", "DRTXX", "AGPXX", "GVMXX",                    # assorted government / prime funds
    "WFJXX", "WFFXX", "WBSXX", "WFRXX",                             # Wells Fargo
    "CASH", "USD",                                                  # generic cash placeholders
})
_MM_NAME_RE = re.compile(r"\b(money\s*market|cash\s*(?:management|reserves?|sweep)|sweep|government\s+(?:cash|money)|treasury\s+cash)\b", re.I)
_CASH_ACCT_RE = re.compile(r"\b(cash|money\s*market|sweep|mm|mmf|core\s+position)\b", re.I)
_BANK_ACCT_RE = re.compile(r"\b(savings|checking|emergency|high[- ]yield|hysa|cd)\b", re.I)
# From the first real capture (2026-11-10, Plaid-style names): credit cards named "Blue Cash Everyday" / "Customized Cash Rewards" were being
# read as cash, payment wallets (Venmo, PayPal, Apple Cash) had no pairing, and brokerage cash accounts literally named "BROKERAGE CASH"
# did not pair because the investment accounts are called "WELLSTRADE ...".
_LIABILITY_TYPE_RE = re.compile(r"credit|loan|mortgage|line of credit|liabilit", re.I)
_LIABILITY_NAME_RE = re.compile(r"\b(visa|mastercard|amex|american express card|platinum card|credit card|rewards card|card|mortgage|installment|loan|line of credit)\b", re.I)
_WALLET_RE = re.compile(r"\b(venmo|paypal|apple\s+cash|cash\s*app|zelle|google\s*pay)\b", re.I)
_BROKERAGE_CASH_RE = re.compile(r"\b(brokerage\s+cash|core\s+cash|cash\s+sweep|sweep\s+account|investing\s+cash|investment\s+cash|ira\s+cash)\b", re.I)
_HSA_RE = re.compile(r"\b(hsa|health\s+savings)\b", re.I)
_MASK_RE = re.compile(r"[-–—\s]*\(?\s*(?:xx+|\*+|#)\s*\d{2,6}\)?\s*$", re.I)
_PREFIX_RE = re.compile(r"^(mc|manual)\s+", re.I)
_CASH_WORDS_RE = re.compile(r"\b(cash|money\s*market|sweep|mmf|mm|core\s+position|investing|investment)\b", re.I)


def stem(name):
    """An account name reduced to what identifies the account family: no mask, no 'MC'-style prefix, no cash words."""
    n = _MASK_RE.sub("", str(name or "")).strip()
    n = _PREFIX_RE.sub("", n)
    n = _CASH_WORDS_RE.sub(" ", n)
    return re.sub(r"[^a-z0-9]+", " ", n.lower()).strip()


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def classify_holding(h, account_has_securities):
    """-> {verdict, confidence, reasons[]} for one position. verdict: brokerage_cash | bank_cash | investment."""
    reasons = []
    tkr = str(h.get("ticker") or "").upper()
    name = str(h.get("name") or "")
    typ = str(h.get("type") or "")
    price, day = _num(h.get("price")), _num(h.get("day_pct"))
    score = 0
    if typ.lower() == "cash":
        reasons.append("typed Cash by the provider"); score += 3
    if tkr in MONEY_MARKET_TICKERS:
        reasons.append(f"{tkr} is a known money-market / cash ticker"); score += 3
    if _MM_NAME_RE.search(name):
        reasons.append("security name says money market / sweep"); score += 2
    if price is not None and abs(price - 1.0) < 0.0005 and (day in (None, 0.0)) and typ.lower() != "cash":
        reasons.append("NAV is a stable $1.00"); score += 1
    if score == 0:
        return {"verdict": "investment", "confidence": "high", "reasons": []}
    verdict = "brokerage_cash" if account_has_securities or typ.lower() != "cash" else "bank_cash"
    if typ.lower() == "cash" and not account_has_securities:
        reasons.append("account holds only cash")
    return {"verdict": verdict, "confidence": "high" if score >= 3 else ("medium" if score == 2 else "low"), "reasons": reasons}


def _flags(a):
    """Provider-side state worth knowing when a balance looks wrong (hidden/deactivated/sync off/manual)."""
    out = []
    for key, label in (("hidden", "hidden in the provider's list"), ("deactivated", "deactivated"), ("sync_disabled", "sync switched off"),
                       ("manual", "manually entered")):
        if a.get(key):
            out.append(label)
    return out


def _row(a, **kw):
    base = {"name": a.get("name") or "", "institution": a.get("institution"), "type": a.get("type"), "subtype": a.get("subtype"),
            "holdings_count": a.get("holdings_count") or 0, "balance": a.get("balance"),
            "current_balance": a.get("current_balance"), "last_updated": a.get("last_updated"),
            "data_provider": a.get("data_provider"), "flags": _flags(a)}
    base.update(kw)
    zero = (a.get("balance") in (0, 0.0, None)) and base["holdings_count"] == 0
    if base.get("verdict") not in ("not_cash",) and zero:
        base["reasons"] = list(base.get("reasons") or []) + ["the provider reports a $0 balance: it may be hidden, disconnected, or the institution does not send this balance"]
    if base["flags"]:
        base["reasons"] = list(base.get("reasons") or []) + ["provider state: " + ", ".join(base["flags"])]
    return base


def classify_accounts(accounts, positions):
    """Per-account guesses. `accounts`: [{name, institution, type, subtype, holdings_count, balance, ...}]; `positions` carry
    account + institution. Pairing links a cash-looking account to an investment account of the same institution and name family."""
    sec_accts = {(p.get("institution"), p.get("account")) for p in positions if str(p.get("type") or "").lower() != "cash"}
    by_inst = {}
    for a in accounts:
        by_inst.setdefault(a.get("institution"), []).append(a)
    out = []
    for a in accounts:
        name, inst = a.get("name") or "", a.get("institution")
        typ, sub = str(a.get("type") or ""), str(a.get("subtype") or "")
        liability = (a.get("is_asset") is False) or bool(_LIABILITY_TYPE_RE.search(typ + " " + sub)) or \
            (bool(_LIABILITY_NAME_RE.search(name)) and (inst, name) not in sec_accts and (a.get("holdings_count") or 0) == 0
             and not _BROKERAGE_CASH_RE.search(name))
        if liability:                       # a card or loan is never cash, whatever its name says ("Blue Cash Everyday")
            out.append(_row(a, has_securities=False, verdict="not_cash", confidence="high",
                            reasons=["credit card, loan or other liability"], paired_with=None))
            continue
        reasons, cashy = [], False
        if _CASH_ACCT_RE.search(name):
            reasons.append("account name contains a cash word"); cashy = True
        if typ.lower() in ("cash", "depository", "checking", "savings") or sub.lower() in ("checking", "savings", "cash", "money market", "cash management"):
            reasons.append(f"provider type/subtype is {typ or sub}"); cashy = True
        bank_word = bool(_BANK_ACCT_RE.search(name))
        wallet = bool(_WALLET_RE.search(name))
        hsa = bool(_HSA_RE.search(name))
        if wallet:
            reasons.append("payment wallet (Venmo / PayPal / Apple Cash style)"); cashy = True
        elif hsa and (a.get("holdings_count") or 0) == 0 and (inst, name) not in sec_accts:
            reasons.append("health savings account: its balance may be cash or invested, only you can say"); cashy = True
        elif bank_word:
            reasons.append("account name looks like a bank account"); cashy = True
        explicit = bool(_BROKERAGE_CASH_RE.search(name))
        if explicit:
            reasons.append("the name itself says brokerage / investing cash"); cashy = True
        paired = None
        if cashy:
            my = stem(name)
            best = None
            for o in by_inst.get(inst, []):
                if o is a:
                    continue
                om = stem(o.get("name"))
                invest_like = (o.get("holdings_count") or 0) > 0 or (inst, o.get("name")) in sec_accts or \
                    str(o.get("type") or "").lower() in ("investments", "investment", "brokerage")
                if not (invest_like and om and my):
                    continue
                if om == my:
                    key = (2, len(om))               # same family exactly
                elif om in my or my in om:
                    key = (1, len(om))               # one contains the other: prefer the most specific (longest) match
                else:
                    continue
                if best is None or key > best[0]:
                    best = (key, o.get("name"))
            paired = best[1] if best else None
        if not cashy:
            verdict, conf = "not_cash", "high"       # an investment, loan, card or other account: nothing cash-like about it
        elif wallet:
            verdict, conf = "bank_cash", "medium"
        elif hsa and not paired:
            verdict, conf = "unsure", "low"
        elif explicit and not paired:
            fam = [o.get("name") for o in by_inst.get(inst, []) if o is not a and (
                (o.get("holdings_count") or 0) > 0 or (inst, o.get("name")) in sec_accts)]
            verdict, conf = "brokerage_cash", ("high" if fam else "medium")
            if fam:
                reasons.append(f"{len(fam)} investment account(s) at the same institution (exact match by number is not possible from names)")
        elif paired:
            verdict, conf = "brokerage_cash", "high" if not bank_word else "medium"
            reasons.append(f"same institution and name family as the investment account '{paired}'")
        elif bank_word:
            verdict, conf = "bank_cash", "medium"
        elif (a.get("holdings_count") or 0) == 0:
            verdict, conf = "unsure", "low"
            reasons.append("cash-looking but no matching investment account found: bank cash or a brokerage's cash?")
        else:
            verdict, conf = "brokerage_cash", "medium"
        out.append(_row(a, has_securities=(inst, name) in sec_accts, verdict=verdict, confidence=conf, reasons=reasons, paired_with=paired))
    return out


def report(accounts, positions, counted_brokerage_cash, counted_bank_cash):
    """The full diagnostic: per-account and per-holding guesses, dollar roll-ups, and what the dashboard counts today."""
    sec_accts = {(p.get("institution"), p.get("account")) for p in positions if str(p.get("type") or "").lower() != "cash"}
    acct_rows = classify_accounts(accounts, positions)
    hold_rows = []
    for p in positions:
        c = classify_holding(p, (p.get("institution"), p.get("account")) in sec_accts)
        if c["verdict"] == "investment":
            continue
        hold_rows.append({"account": p.get("account"), "institution": p.get("institution"), "ticker": p.get("ticker"),
                          "name": p.get("name"), "type": p.get("type"), "value": p.get("value"), "price": p.get("price"),
                          **c})
    # roll-up: holdings classified as cash + cash-looking accounts WITHOUT holdings (their balance is the cash)
    guess = {"brokerage_cash": 0.0, "bank_cash": 0.0, "unsure": 0.0}
    for h in hold_rows:
        guess[h["verdict"]] = guess.get(h["verdict"], 0.0) + (h.get("value") or 0.0)
    for a in acct_rows:
        if a["verdict"] in guess and a["holdings_count"] == 0:
            guess[a["verdict"]] += a.get("balance") or 0.0
    return {"accounts": acct_rows, "cash_holdings": hold_rows,
            "guessed_totals": {k: round(v, 2) for k, v in guess.items()},
            "counted_today": {"brokerage_cash": counted_brokerage_cash, "bank_cash": counted_bank_cash},
            "note": "Guesses only. Nothing on the dashboard or in the planning engines changes because of this list; it exists so the rules "
                    "can be checked against your real accounts before a cash-designation feature is built on them."}
