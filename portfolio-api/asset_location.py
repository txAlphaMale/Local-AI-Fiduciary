# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""asset_location.py -- RV-036a / TASK-RV-036a: which holding sits in which kind of account.

Pure functions, no I/O. The app passes in the holdings it already has (value, unrealized gain, yield, fund class mix, security type, industry),
the four bucket totals the bridge panel uses, and the user's marginal rates from tax.py. This module:
  1. lays the holdings out as a grid (security x taxable / pre-tax / Roth / HSA),
  2. works out each holding's tax character from data the app already has (never guessed: 'unknown' when it cannot be told),
  3. estimates the yearly tax drag of the current placement (income held in taxable accounts times the user's marginal rates),
  4. builds ONE comparison placement from a stated rule set, with account sizes fixed (it only swaps what is held where),
  5. prices what moving a taxable holding would cost (its unrealized gain and the tax a sale would trigger) and lists cautions.
Informational arithmetic. Never an instruction to buy or sell.
"""

import re

BUCKETS = ("taxable", "pretax", "roth", "hsa")
UNKNOWN_SHARE_LIMIT = 0.25            # more than this share of taxable value with unknown character: the comparison is withheld

RULES = [
    "Income taxed as ordinary income (interest, bond and REIT distributions) goes to pre-tax accounts first, highest yield first.",
    "Holdings in the highest growth class go to Roth accounts first (the class is a rank from the fund's equity share or the security type, not a forecast of any one company; ties go to the higher yield, then to whatever already sits in Roth, then ticker order).",
    "Low-yield, mostly qualified-dividend equity stays in taxable accounts.",
    "The HSA is left exactly as it is (it is a medical reserve first).",
    "Account sizes are fixed: this only swaps WHAT is held WHERE; it never moves money between account types.",
    "A holding whose tax character or yield is unknown stays where it is.",
]


def _r(v, n=2):
    return None if v is None else round(v, n)


def character(h):
    """-> {'kind': 'ordinary'|'qualified'|'mixed'|'unknown', 'ordinary_share': 0..1|None, 'growth_rank': 0..3, 'basis': text}.
    From the fund's own class mix (SEC N-PORT), the security type and the industry label the app already holds. Nothing else is assumed."""
    typ = str(h.get("type") or "").lower()
    mix = h.get("mix") or None
    ind = str(h.get("industry") or "").lower()
    if typ == "cash":
        return {"kind": "ordinary", "ordinary_share": 1.0, "growth_rank": 0, "basis": "cash earns interest, taxed as ordinary income"}
    if mix:
        eq = float(mix.get("equity") or 0.0)
        non_eq = float(mix.get("bond") or 0.0) + float(mix.get("cash") or 0.0) + float(mix.get("other") or 0.0)
        tot = eq + non_eq
        if tot > 0:
            share = non_eq / tot
            rank = 3 if eq / tot >= 0.7 else (2 if eq / tot >= 0.3 else 0)
            kind = "ordinary" if share >= 0.9 else ("qualified" if share <= 0.1 else "mixed")
            return {"kind": kind, "ordinary_share": round(share, 3), "growth_rank": rank,
                    "basis": f"from the fund's own class mix (SEC N-PORT): {round(share * 100)}% bonds, cash and other, taxed as ordinary income"}
    if h.get("is_stock"):
        if "reit" in ind or "real estate" in ind:
            return {"kind": "ordinary", "ordinary_share": 1.0, "growth_rank": 1, "basis": "real-estate company: distributions are mostly ordinary income"}
        return {"kind": "qualified", "ordinary_share": 0.0, "growth_rank": 3, "basis": "US common stock: dividends are treated as qualified (assumed, holding period not checked)"}
    return {"kind": "unknown", "ordinary_share": None, "growth_rank": 0, "basis": "no fund mix or security class on file"}


def _drag_per_dollar(h, ord_rate, qual_rate):
    """Yearly tax per dollar held in a taxable account, or None when the yield or the character is unknown."""
    y, ch = h.get("yield_pct"), h["char"]
    if y is None or ch["ordinary_share"] is None:
        return None
    s = ch["ordinary_share"]
    return (y / 100.0) * (s * ord_rate + (1.0 - s) * qual_rate)


def build(lines, bucket_totals, ord_rate, qual_rate, ltcg_tax_fn=None, ordinary_taxable=0.0):
    """lines: [{ticker, name, bucket, value, gain, yield_pct, type, mix, industry, is_stock}] one per holding per account; bucket_totals: {bucket: dollars
    incl. balance-only accounts} (the bridge panel's totals); ord_rate / qual_rate: marginal rates (0..1); ltcg_tax_fn(ordinary_taxable, gain) -> tax.
    -> the grid, drag, comparison and cautions."""
    by = {}
    for ln in lines:
        v = float(ln.get("value") or 0.0)
        b = ln.get("bucket")
        if v <= 0 or b not in BUCKETS or not ln.get("ticker"):
            continue
        row = by.setdefault(ln["ticker"], {"ticker": ln["ticker"], "name": ln.get("name"), "values": {k: 0.0 for k in BUCKETS}, "gain_taxable": None,
                                           "yield_pct": ln.get("yield_pct"), "yield_basis": ln.get("yield_basis"), "type": ln.get("type"), "mix": ln.get("mix"), "industry": ln.get("industry"),
                                           "is_stock": bool(ln.get("is_stock"))})
        row["values"][b] += v
        if b == "taxable" and ln.get("gain") is not None:
            row["gain_taxable"] = (row["gain_taxable"] or 0.0) + float(ln["gain"])
        elif b == "taxable":
            row["gain_unknown"] = True
        if row["yield_pct"] is None:
            row["yield_pct"] = ln.get("yield_pct")
            row["yield_basis"] = ln.get("yield_basis")
    rows = sorted(by.values(), key=lambda r: -sum(r["values"].values()))
    for r in rows:
        r["char"] = character(r)
        r["total"] = sum(r["values"].values())
        r["drag_per_dollar"] = _drag_per_dollar(r, ord_rate, qual_rate)
    itemised = {k: sum(r["values"][k] for r in rows) for k in BUCKETS}
    other = {k: _r(max(0.0, (bucket_totals or {}).get(k, itemised[k]) - itemised[k])) for k in BUCKETS}
    column_totals = {k: _r(itemised[k] + other[k]) for k in BUCKETS}

    def drag_of(values_by_ticker):
        tot, unknown = 0.0, 0.0
        for r in rows:
            d = values_by_ticker[r["ticker"]]["taxable"]
            if d <= 0:
                continue
            if r["drag_per_dollar"] is None:
                unknown += d
            else:
                tot += d * r["drag_per_dollar"]
        return tot, unknown
    cur = {r["ticker"]: dict(r["values"]) for r in rows}
    cur_drag, cur_unknown = drag_of(cur)
    taxable_val = itemised["taxable"]
    unknown_share = (cur_unknown / taxable_val) if taxable_val else 0.0
    out = {"rows": [{"ticker": r["ticker"], "name": r["name"], "values": {k: _r(v) for k, v in r["values"].items()}, "total": _r(r["total"]),
                     "yield_pct": r["yield_pct"], "yield_basis": r.get("yield_basis"), "character": r["char"]["kind"], "ordinary_share": r["char"]["ordinary_share"],
                     "character_basis": r["char"]["basis"], "growth_class": r["char"]["growth_rank"],
                     "yearly_drag": _r(r["values"]["taxable"] * r["drag_per_dollar"]) if (r["drag_per_dollar"] is not None and r["values"]["taxable"] > 0) else None}
                    for r in rows],
           "column_totals": column_totals, "other_balances_not_itemised": other,
           "rates": {"ordinary": ord_rate, "qualified": qual_rate},
           "drag": {"yearly_current": _r(cur_drag), "taxable_value_unknown": _r(cur_unknown), "unknown_share": round(unknown_share, 3)}}

    # ---- the comparison ----
    if unknown_share > UNKNOWN_SHARE_LIMIT:
        out["comparison"] = {"status": "withheld", "rules": RULES,
                             "reason": f"{round(unknown_share * 100)}% of the value held in taxable accounts has an unknown tax character (limit {round(UNKNOWN_SHARE_LIMIT * 100)}%), so a comparison would rest on guesses."}
        out["cautions"] = _cautions([])
        return out
    fixed = {k: 0.0 for k in BUCKETS}                 # stays where it is: HSA, and any holding with an unknown character or yield
    mov = []
    for r in rows:
        known = r["drag_per_dollar"] is not None
        for k in BUCKETS:
            v = r["values"][k]
            if v <= 0:
                continue
            if k == "hsa" or not known:
                fixed[k] += v
        if known:
            mv = sum(r["values"][k] for k in ("taxable", "pretax", "roth"))
            if mv > 0:
                mov.append({"t": r["ticker"], "v": mv, "ord_y": (r["yield_pct"] / 100.0) * r["char"]["ordinary_share"], "y": r["yield_pct"] / 100.0, "g": r["char"]["growth_rank"], "roth_now": r["values"]["roth"] / mv})
    cap = {k: max(0.0, sum(r["values"][k] for r in rows) - fixed[k]) for k in ("taxable", "pretax", "roth")}
    prop = {r["ticker"]: {k: (r["values"][k] if (k == "hsa" or r["drag_per_dollar"] is None) else 0.0) for k in BUCKETS} for r in rows}
    left = {m["t"]: m["v"] for m in mov}

    def place(order_key, bucket, limit, only=lambda m: True):
        room = limit
        for m in sorted((m for m in mov if only(m)), key=order_key):
            if room <= 0.005:
                break
            take = min(left[m["t"]], room)
            if take <= 0:
                continue
            prop[m["t"]][bucket] += take
            left[m["t"]] -= take
            room -= take
        return limit - room
    used_pre = place(lambda m: (-m["ord_y"], m["t"]), "pretax", cap["pretax"], only=lambda m: m["ord_y"] > 0)    # only real ordinary-income producers get priority
    used_roth = place(lambda m: (-m["g"], -m["y"], -m["roth_now"], m["t"]), "roth", cap["roth"])          # equal growth: the higher yield gains more from the shelter
    place(lambda m: (m["y"], m["t"]), "taxable", cap["taxable"])
    place(lambda m: (-m["ord_y"], m["t"]), "pretax", cap["pretax"] - used_pre)                                   # whatever room is left takes what is left
    place(lambda m: (-m["g"], m["t"]), "roth", cap["roth"] - used_roth)
    new_drag, _ = drag_of(prop)
    out["comparison"] = {
        "status": "ready", "label": "One common rule of thumb, for study. Not a recommendation.", "rules": RULES,
        "rows": [{"ticker": r["ticker"], "values": {k: _r(prop[r["ticker"]][k]) for k in BUCKETS}} for r in rows],
        "yearly_drag_after": _r(new_drag), "yearly_drag_now": _r(cur_drag), "yearly_difference": _r(cur_drag - new_drag),
        "growth_measure": "A class rank 0-3, not a forecast: 3 = common stock or a fund at least 70% equity; 2 = fund 30-70% equity; 1 = real-estate company; 0 = bonds, cash or unknown. Holdings in the same class are not ranked against each other by growth.",
        "growth_ties": sorted({m["t"] for m in mov if m["g"] == max((x["g"] for x in mov), default=0)}) if len([m for m in mov if m["g"] == max((x["g"] for x in mov), default=0)]) > 1 else [],
        "all_known_yields_zero": all(m["y"] == 0 for m in mov) if mov else False,
        "column_totals_unchanged": all(abs(sum(prop[r["ticker"]][k] for r in rows) - itemised[k]) < 0.5 for k in BUCKETS),
    }
    # ---- what moving would mean ----
    moves, sale_gain, any_gain_unknown, loss_tickers = [], 0.0, False, []
    for r in rows:
        t = r["ticker"]
        d = {k: prop[t][k] - r["values"][k] for k in BUCKETS}
        if all(abs(v) < 1 for v in d.values()):
            continue
        sold_tax = max(0.0, -d["taxable"])
        gain = None
        if sold_tax > 0:
            tv = r["values"]["taxable"]
            if r["gain_taxable"] is not None and not r.get("gain_unknown") and tv > 0:
                gain = r["gain_taxable"] * (sold_tax / tv)
                sale_gain += gain
                if gain < 0:
                    loss_tickers.append(t)
            else:
                any_gain_unknown = True
        moves.append({"ticker": t, "change": {k: _r(v) for k, v in d.items() if abs(v) >= 1}, "taxable_sold": _r(sold_tax) if sold_tax else None,
                      "unrealized_gain_on_sale": _r(gain) if gain is not None else None})
    sale_tax = None
    if ltcg_tax_fn and not any_gain_unknown:
        sale_tax = _r(ltcg_tax_fn(ordinary_taxable, max(0.0, sale_gain)))
    out["comparison"]["moves"] = moves
    out["comparison"]["sale"] = {"gain_realized": _r(sale_gain), "tax_if_sold": sale_tax, "gain_unknown_for_some": any_gain_unknown,
                                 "assumes": "long-term gains stacked on the ordinary income assumed for the tax panel; state tax not included"}
    yd = out["comparison"]["yearly_difference"]
    if sale_tax and yd and yd > 0:
        out["comparison"]["sale"]["years_of_difference_equal_to_sale_tax"] = round(sale_tax / yd, 1)
    out["cautions"] = _cautions(loss_tickers)
    return out


def _cautions(loss_tickers):
    c = ["Moving a holding out of a taxable account means selling it: the gain and the tax above are what that sale would trigger.",
         "An employer plan may not offer the same fund; the comparison ignores what each account is allowed to hold.",
         "Wash-sale rule: a loss sale is disallowed if the same or a substantially identical security is bought, in any of your accounts (IRAs included), within 30 days either side."]
    if loss_tickers:
        c.append("Holdings that would be sold at a loss, where the wash-sale rule matters: " + ", ".join(sorted(set(loss_tickers))) + ".")
    c.append("Informational arithmetic from the app's own figures, not tax advice. Confirm with a CPA.")
    return c


def facts_text(view, question=""):
    """The app's own plain-text account of get_asset_location's result, printed before any model commentary (the model never rewrites a figure).
    Holdings the question names come first; with none named, every holding with a balance is listed. '' when the view is unusable."""
    if not isinstance(view, dict) or not view.get("rows"):
        return ""
    m = lambda v: "unknown" if v is None else ("-$" if v < 0 else "$") + format(abs(round(v)), ",")
    q = str(question or "").upper()
    rows = view["rows"]
    named = [r for r in rows if re.search(r"(?<![A-Z0-9])" + re.escape(str(r["ticker"]).upper()) + r"(?![A-Z0-9])", q)]
    shown = named or rows
    cmp_ = view.get("comparison") or {}
    rule_vals = {r["ticker"]: r["values"] for r in (cmp_.get("rows") or [])} if cmp_.get("status") == "ready" else {}
    L = ["**From the app's asset-location data (copied by the app; the figures never pass through the model):**"]
    L.append("Where each holding sits now (taxable / pre-tax / Roth / HSA), its tax character, yield and yearly tax on its taxable income:")
    for r in shown[:40]:
        v = r["values"]
        y = "yield unknown" if r.get("yield_pct") is None else f"yield {r['yield_pct']:g}%"
        line = f"- **{r['ticker']}**: {m(v['taxable'])} / {m(v['pretax'])} / {m(v['roth'])} / {m(v['hsa'])} · {r.get('character')} · {y} · yearly tax {m(r.get('yearly_drag'))} · growth class {r.get('growth_class')}"
        if r["ticker"] in rule_vals:
            line += f" · Roth under the rule {m(rule_vals[r['ticker']]['roth'])}"
        L.append(line)
    L.append("The rules the comparison uses: " + " ".join(f"({i + 1}) {x}" for i, x in enumerate(cmp_.get("rules") or [])))
    if cmp_.get("status") == "ready":
        L.append(f"Yearly tax on income held in taxable accounts: {m(cmp_.get('yearly_drag_now'))} now, {m(cmp_.get('yearly_drag_after'))} under the rule (difference {m(cmp_.get('yearly_difference'))}). "
                 f"Account sizes unchanged: {'yes' if cmp_.get('column_totals_unchanged') else 'NO'}.")
        if cmp_.get("all_known_yields_zero"):
            L.append("Every known yield is 0, so there is no taxable income to move and the yearly difference is $0.")
        if cmp_.get("growth_measure"):
            L.append("Growth measure: " + cmp_["growth_measure"] + (" Not separated by it: " + ", ".join(cmp_["growth_ties"]) + "." if cmp_.get("growth_ties") else ""))
        sl = cmp_.get("sale") or {}
        L.append(f"Cost of moving the taxable parts: gain {m(sl.get('gain_realized'))}, tax if sold {m(sl.get('tax_if_sold'))}.")
    elif cmp_.get("status") == "withheld":
        L.append("Comparison withheld: " + str(cmp_.get("reason")))
    dg = view.get("drag") or {}
    L.append(f"Unknown: {m(dg.get('taxable_value_unknown'))} of taxable holdings ({round((dg.get('unknown_share') or 0) * 100)}%) has an unknown yield or tax character and is left where it is.")
    L.append("Full grid: PLAN > ASSET LOCATION.")
    return "\n".join(L)


COMMENTARY_RULE = (
    "The app will print the asset-location figures itself, word for word, with your answer. So write ONLY a short commentary (at most 4 sentences) that "
    "answers the question using ONLY the printed facts: the growth measure, the tie, the yields, the stated rules. Do not repeat the figures. "
    "Check the question's premise against the printed facts: if it assumes something they do not show (for example that one holding gets less Roth "
    "than another), say the facts do not show that and say what they do show. Never give reasons the facts do not state (no income-limit, MAGI, step-up or "
    "estate reasons). Give NO instruction to buy, sell or move anything.")
