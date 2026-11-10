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
        base["reasons"] = list(base.get("reasons") or []) + ["the provider reports a $0 balance: it may be hidden, disconnected, or the institution does not send the current balance. In Monarch, open the account and try turning on 'Use available balance instead of current balance' (Wells Fargo brokerage cash accounts need it)"]
    if base["flags"]:
        base["reasons"] = list(base.get("reasons") or []) + ["provider state: " + ", ".join(base["flags"])]
    return base


DESIGNATION_VERDICT = {"investment": "not_cash", "brokerage_cash": "brokerage_cash", "bank_cash": "bank_cash", "exclude": "excluded"}
DESIGNATION_LABEL = {"investment": "Investment", "brokerage_cash": "Brokerage cash", "bank_cash": "Bank cash", "exclude": "Exclude from widgets"}


def desig_key(institution, name):
    return f"{institution or ''}||{name or ''}"


def classify_accounts(accounts, positions, designations=None):
    """Per-account verdicts: the rules' guess, overridden by the person's own designation when there is one. A designation always wins and
    is high confidence. Brokerage cash / Exclude only make sense for an account with no holdings of its own (its balance is the thing being
    placed); on an account that lists holdings they are ignored with a note, because its holdings already feed the widgets line by line."""
    rows = _guess_accounts(accounts, positions)
    if not designations:
        return rows
    for a, r in zip(accounts, rows):
        d = designations.get(desig_key(a.get("institution"), a.get("name")))
        r["designation"] = None
        if not d:
            continue
        if (a.get("holdings_count") or 0) > 0 and d in ("brokerage_cash", "exclude"):
            r["reasons"] = list(r["reasons"]) + [f"you set '{DESIGNATION_LABEL[d]}', but this account lists holdings of its own, so it is ignored (its holdings already feed the widgets)"]
            continue
        r["guess"] = r["verdict"]
        r["designation"] = d
        r["verdict"], r["confidence"] = DESIGNATION_VERDICT[d], "high"
        r["reasons"] = [f"set by you: {DESIGNATION_LABEL[d]}"] + [f"the rules had guessed: {r['guess']} ({'; '.join(r['reasons']) or 'no cash signals'})"]
    return rows


def left_out_accounts(accounts, positions, designations):
    """Holdings-less accounts the person sent to Exclude or Bank cash: their balance stays in net worth but is left out of the donuts."""
    out = []
    for a, r in zip(accounts, classify_accounts(accounts, positions, designations)):
        if r.get("designation") in ("exclude", "bank_cash") and (a.get("holdings_count") or 0) == 0:
            out.append(a)
    return out


def _guess_accounts(accounts, positions):
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
        hsa = bool(_HSA_RE.search(name)) or bool(_HSA_RE.search(sub)) or sub.lower() == "hsa"
        if hsa and (a.get("holdings_count") or 0) == 0 and (inst, name) not in sec_accts:
            # A health savings account is its own thing (2026-11-10, Jason): a MEDICAL RESERVE first, a retirement asset only for the part above
            # foreseeable healthcare costs and only from 65. It is never brokerage cash or bank cash, whatever its name or a paired account
            # says, and its balance stays in net worth. The person's own designation (applied afterwards) can still override this.
            out.append(_row(a, has_securities=False, verdict="hsa", confidence="high",
                            reasons=["health savings account: a medical reserve, not cash and not an ordinary investment account "
                                     "(counted in net worth; kept in its own bucket by the retirement planners)"], paired_with=None))
            continue
        if wallet:
            reasons.append("payment wallet (Venmo / PayPal / Apple Cash style)"); cashy = True
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


def counted_brokerage_cash_accounts(accounts, positions, designations=None):
    """The accounts whose BALANCE feeds the dashboard's 'Cash (in brokerage accounts)' slice: cash accounts with no holdings of their own
    (Monarch lists a brokerage's cash sweep as a separate account) that the rules are highly confident belong to a brokerage. Only high
    confidence counts; medium/low guesses, bank cash, unsure, wallets, liabilities and deactivated accounts never do. Returns the account dicts."""
    out = []
    for a, r in zip(accounts, classify_accounts(accounts, positions, designations)):
        if (r["verdict"] == "brokerage_cash" and r["confidence"] == "high" and r["holdings_count"] == 0
                and (a.get("balance") or 0) > 0 and not a.get("deactivated")):
            out.append(a)
    return out


def cash_breakdown(accounts, positions, designations=None):
    """How the dashboard's allocation/mix widgets treat cash, decided in ONE place (the widgets and CASH CHECK both use it).
      brokerage_lines: Cash-typed lines inside an account that holds securities OR inside an investment-type account that currently holds
                       only cash (e.g. a brokerage account waiting to invest);
      bank_lines:      Cash-typed lines in bank-style accounts (savings, checking, emergency fund, provider type Cash);
      account_lines:   holdings-less accounts the rules are highly confident are a brokerage's cash sweep (balance counts);
      equivalent_lines: money-market / sweep FUNDS typed as a fund (a Vanguard money-market fund) that count as brokerage cash.
    Each line is {account, institution, ticker, name, value, why}; totals are rounded dollars."""
    rows = {(r["institution"], r["name"]): r for r in classify_accounts(accounts, positions, designations)}
    sec = {(p.get("institution"), p.get("account")) for p in positions if str(p.get("type") or "").lower() != "cash"}
    brokerage, bank, equiv = [], [], []
    for p in positions:
        key = (p.get("institution"), p.get("account"))
        line = {"account": p.get("account"), "institution": p.get("institution"), "ticker": p.get("ticker"), "name": p.get("name"),
                "value": p.get("value") or 0.0}
        if str(p.get("type") or "").lower() == "cash":
            r = rows.get(key)
            if r and r.get("designation") == "bank_cash":
                bank.append(dict(line, why="you designated this account as bank cash"))
            elif r and r.get("designation") == "investment":
                brokerage.append(dict(line, why="you designated this account as an investment account"))
            elif key in sec:
                brokerage.append(dict(line, why="cash line in an account that also holds securities"))
            elif r and r["verdict"] == "bank_cash":
                bank.append(dict(line, why="account looks like a bank account (savings, checking, emergency fund)"))
            elif r and r["verdict"] in ("not_cash", "brokerage_cash") and str(r.get("type") or "").lower() in ("investments", "investment", "brokerage"):
                brokerage.append(dict(line, why="an investment account that currently holds only cash"))
            else:
                bank.append(dict(line, why="cash-only account with no sign of being a brokerage"))
        else:
            h = classify_holding(p, True)
            if h["verdict"] == "brokerage_cash" and h["confidence"] in ("high", "medium"):
                equiv.append(dict(line, why="; ".join(h["reasons"])))
    accts = []
    for a in counted_brokerage_cash_accounts(accounts, positions, designations):
        mine = (designations or {}).get(desig_key(a.get("institution"), a.get("name"))) == "brokerage_cash"
        accts.append({"account": a.get("name"), "institution": a.get("institution"), "ticker": None, "name": a.get("name"),
                      "value": a.get("balance") or 0.0,
                      "why": "you designated this account as brokerage cash" if mine else "highly likely a brokerage cash sweep listed as its own account"})
    tot = lambda L: round(sum(x["value"] or 0.0 for x in L), 2)
    return {"brokerage_lines": brokerage, "bank_lines": bank, "account_lines": accts, "equivalent_lines": equiv,
            "brokerage_cash": round(tot(brokerage) + tot(accts) + tot(equiv), 2), "bank_cash": tot(bank),
            "brokerage_cash_accounts": tot(accts), "cash_equiv_value": tot(equiv),
            "cash_equiv_tickers": sorted({x["ticker"] for x in equiv if x["ticker"]})}


def report(accounts, positions, counted_brokerage_cash, counted_bank_cash, designations=None):
    """The full diagnostic: per-account and per-holding guesses, dollar roll-ups, and what the dashboard counts today."""
    sec_accts = {(p.get("institution"), p.get("account")) for p in positions if str(p.get("type") or "").lower() != "cash"}
    acct_rows = classify_accounts(accounts, positions, designations)
    counted = {id(a) for a in counted_brokerage_cash_accounts(accounts, positions, designations)}
    for a, r in zip(accounts, acct_rows):
        r["counted_in_widgets"] = id(a) in counted
        if r["counted_in_widgets"]:
            r["reasons"] = list(r["reasons"]) + ["its balance is counted in the dashboard's 'Cash (in brokerage accounts)' slice"]
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
    return {"accounts": acct_rows, "cash_holdings": hold_rows, "widget_cash": cash_breakdown(accounts, positions, designations),
            "guessed_totals": {k: round(v, 2) for k, v in guess.items()},
            "counted_today": {"brokerage_cash": counted_brokerage_cash, "bank_cash": counted_bank_cash},
            "note": "Guesses only. Nothing on the dashboard or in the planning engines changes because of this list; it exists so the rules "
                    "can be checked against your real accounts before a cash-designation feature is built on them."}
