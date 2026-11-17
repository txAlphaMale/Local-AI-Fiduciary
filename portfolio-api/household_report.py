# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""household_report.py -- RV-039 / RV-T5: the quarterly household report.

Pure functions, no I/O. The app passes in the OUTPUTS of its existing engines (net worth and allocation, the retirement projection, the
attention digest, the cash and debt panel, the insurance gap table, the continuity binder, emergency-access status); this module only
arranges them in plain words, notes how old each one is, and compares two reports. It computes nothing new except differences between
two reports. No model text. No account numbers, no per-account-number balances, no secrets: account NAMES are shown with any run of four or
more digits masked.
"""
import re
import time

CLOSING = "Informational only, produced by a personal tool. Not financial, tax or legal advice."   # RV-044 closing line (same text as the timeline report)
STALE_AFTER_S = 3 * 86400          # a source older than this shows its age
SECTIONS = ("net_worth", "where", "plan", "attention", "safety", "if_something_happens")
_DIGITS = re.compile(r"\d{4,}")


def clean_name(name):
    """An account's display name with any account-number-like digit run (4 or more digits) masked."""
    return _DIGITS.sub("••••", str(name or "")).strip()[:80] or "Account"


def _money(v):
    return "—" if v is None else ("-$" if v < 0 else "$") + f"{abs(v):,.0f}"


def _age_note(as_of, now):
    if not as_of:
        return None
    age = now - as_of
    return f"{int(age // 86400)} days old" if age >= STALE_AFTER_S else None


def _signed_accounts(accounts):
    """{clean name: signed balance} for the accounts that count in net worth (debts negative). Used to name what changed between reports."""
    out = {}
    for a in accounts or []:
        if a.get("include_nw") is False:
            continue
        bal = a.get("balance")
        if bal is None:
            continue
        sign = -1.0 if a.get("is_asset") is False else 1.0
        k = clean_name(a.get("name"))
        out[k] = round(out.get(k, 0.0) + sign * float(bal), 2)
    return out


def top_changes(prev_map, now_map, n=3):
    """The n largest changes in signed account balance between two snapshots: [{name, change, was, now, kind}], biggest first."""
    rows = []
    for k in set(prev_map or {}) | set(now_map or {}):
        a, b = (prev_map or {}).get(k), (now_map or {}).get(k)
        ch = round((b or 0.0) - (a or 0.0), 2)
        if abs(ch) < 1:
            continue
        rows.append({"name": k, "change": ch, "was": a, "now": b, "kind": "new" if a is None else ("closed" if b is None else "changed")})
    rows.sort(key=lambda r: -abs(r["change"]))
    return rows[:n]


def _ok(src):
    return isinstance(src, dict) and "_error" not in src and src.get("error") is None


def _carry(prev, key, now, why):
    """A section whose source is down: the previous report's section, marked with its date."""
    old = ((prev or {}).get("sections") or {}).get(key)
    if not old:
        return {"unavailable": True, "reason": why}
    return {**old, "from_last_known": True, "last_known_date": (prev or {}).get("as_of"), "reason": why}


def build(src, now=None, prev=None, config=None):
    """-> the report dict. `src` keys: data, retirement, alerts, cashdebt, insurance, continuity, emergency (each an engine's own output, or a
    dict with `_error` when it failed); `prev` = the previous stored report; `config` = {household_name, show_emergency_contact}."""
    now = int(now or time.time())
    cfg = config or {}
    secs, srcs, notes = {}, {}, []

    def mark(key, panel, tab, subtab, as_of):
        srcs[key] = {"panel": panel, "tab": tab, "subtab": subtab, "as_of": as_of, "age_note": _age_note(as_of, now)}

    # 2. net worth and what changed
    d = src.get("data")
    if _ok(d) and d.get("net_worth") is not None:
        snap = _signed_accounts(d.get("accounts"))
        sec = {"net_worth": d.get("net_worth"), "assets": d.get("total_assets"), "debts": d.get("total_liabilities")}
        pv = ((prev or {}).get("sections") or {}).get("net_worth") if prev else None
        if pv and not pv.get("unavailable") and pv.get("net_worth") is not None:
            sec["change"] = round(d["net_worth"] - pv["net_worth"], 2)
            sec["assets_change"] = None if (pv.get("assets") is None or d.get("total_assets") is None) else round(d["total_assets"] - pv["assets"], 2)
            sec["debts_change"] = None if (pv.get("debts") is None or d.get("total_liabilities") is None) else round(d["total_liabilities"] - pv["debts"], 2)
            sec["since"] = prev.get("as_of")
            sec["biggest_changes"] = top_changes((prev or {}).get("accounts_snapshot"), snap)
        else:
            sec["first_report"] = True
        secs["net_worth"] = sec
        mark("net_worth", "Dashboard / Overview", "dashboard", None, d.get("generated_at"))
        # 3. where the money is
        inst, kind, debts = {}, {}, 0.0
        for a_ in d.get("accounts") or []:
            if a_.get("include_nw") is False or a_.get("balance") is None:
                continue
            if a_.get("is_asset") is False:
                debts += float(a_["balance"])
                continue
            i_, k_ = clean_name(a_.get("institution") or "Other"), str(a_.get("type") or "Other").strip().title() or "Other"
            inst[i_] = inst.get(i_, 0.0) + float(a_["balance"])
            kind[k_] = kind.get(k_, 0.0) + float(a_["balance"])
        top = lambda m, n: [{"label": k, "value": round(v, 2)} for k, v in sorted(m.items(), key=lambda kv: -kv[1])[:n]]
        secs["where"] = {"by_institution": top(inst, 8), "by_kind": top(kind, 10), "debts": round(debts, 2)}
        mark("where", "Accounts (balances by institution and kind)", "positions", None, d.get("generated_at"))
        accounts_snapshot = snap
    else:
        secs["net_worth"] = _carry(prev, "net_worth", now, "net worth data was not available")
        secs["where"] = _carry(prev, "where", now, "holdings data was not available")
        accounts_snapshot = (prev or {}).get("accounts_snapshot") or {}
        notes.append("Net worth and holdings could not be read when this report was made; they show the last known values and their date.")

    # 4. plan status, one sentence each
    r = src.get("retirement")
    if _ok(r):
        mc, fr = r.get("monte_carlo") or {}, r.get("funded_ratio") or {}
        hv = mc.get("holdings_volatility") or {}
        lines = []
        if r.get("years_to_fi") is not None:
            lines.append(f"At the current savings and return assumptions, about {r['years_to_fi']:.0f} years to financial independence" + (f" (around {r['fi_year']})." if r.get("fi_year") else "."))
        if fr.get("funded_ratio") is not None:
            lines.append(f"The plan is {fr['funded_ratio'] * 100:.0f}% funded; counting essential spending only it is {((fr.get('essential_funded_ratio') or 0) * 100):.0f}%.")
        if mc.get("success_pct") is not None:
            lines.append(f"Retirement odds from the Monte Carlo simulation: the money lasts in {mc['success_pct']:.0f}% of simulated markets (assumed volatility {mc.get('volatility_pct')}%).")
        if hv.get("available") and hv.get("success_pct") is not None:
            lines.append(f"Using the volatility your own holdings have shown ({hv.get('measured_pct')}% a year), the money lasts in {hv['success_pct']:.0f}% of simulated markets.")
        secs["plan"] = {"lines": lines or ["The plan panel has no figures yet; set your birth year and expenses in PLAN > ASSUMPTIONS."]}
        mark("plan", "PLAN > FI · MONTE CARLO", "plan", "fi", r.get("generated_at"))
    else:
        secs["plan"] = _carry(prev, "plan", now, "the retirement projection was not available")
        notes.append("The retirement projection could not be read; the plan section shows the last known values and their date.")

    # 5. what needs attention (described, not instructed)
    al = src.get("alerts")
    if _ok(al):
        items = [{"level": a.get("level"), "text": str(a.get("message") or "")[:300]} for a in (al.get("alerts") or [])[:8]]
        secs["attention"] = {"items": items, "counts": al.get("counts"), "none": not items}
        mark("attention", "ATTENTION", "insights", "attention", al.get("generated_at"))
    else:
        secs["attention"] = _carry(prev, "attention", now, "the attention list was not available")

    # 6. safety net
    cd, ins = src.get("cashdebt"), src.get("insurance")
    safety = {}
    ef = (cd or {}).get("emergency_fund") if _ok(cd) else None
    if ef:
        safety["emergency_fund"] = {"months_covered": ef.get("months_covered"), "target_months": ef.get("target_months"), "status": ef.get("status"), "gap_dollars": ef.get("gap_dollars")}
        mark("safety", "CASH · DEBT", "plan", "cashdebt", cd.get("generated_at"))
    if _ok(ins):
        safety["insurance_gaps"] = [{"line": g.get("line"), "need": g.get("need"), "in_force": g.get("in_force"), "gap": g.get("gap"), "status": g.get("status")}
                                    for g in (ins.get("gap_table") or []) if g.get("status") not in (None, "ok", "covered")][:6]
        srcs.setdefault("safety", {"panel": "INSURANCE", "tab": "plan", "subtab": "insurance", "as_of": ins.get("generated_at"), "age_note": None})
    secs["safety"] = safety if safety else _carry(prev, "safety", now, "the cash and insurance panels were not available")

    # 7. if something happens (documents and contacts only: no account numbers, no balances per account, no secrets)
    co, em = src.get("continuity"), src.get("emergency")
    if _ok(co):
        contacts = [{"name": str(c.get("name") or "")[:60], "role": str(c.get("role") or c.get("type") or "")[:40]} for c in (co.get("contacts") or []) if isinstance(c, dict)][:10]
        sec = {"executor": co.get("executor_name"), "will_location": co.get("will_location"), "contacts": contacts,
               "binder": "PLAN > ESTATE has the continuity binder (what passes how, contacts, document review dates). The document vault is PLAN > VAULT.",
               "documents_due": [x.get("doc_type") for x in (co.get("doc_currency") or []) if x.get("status") not in (None, "ok", "current")][:6],
               "beneficiaries": co.get("beneficiary_summary")}
        if cfg.get("show_emergency_contact") and _ok(em) and (em.get("config") or {}).get("enabled"):
            sec["emergency_access"] = "Emergency access is switched on; the emergency contact is named in SETTINGS > EMERGENCY ACCESS."
        secs["if_something_happens"] = sec
        mark("if_something_happens", "PLAN > ESTATE (continuity binder)", "plan", "estate", co.get("generated_at"))
    else:
        secs["if_something_happens"] = _carry(prev, "if_something_happens", now, "the continuity binder was not available")

    name = (cfg.get("household_name") or "").strip() or "Household"
    return {"id": f"r{now}", "generated_at": now, "as_of": now, "household_name": name[:80],
            "period": {"from": (prev or {}).get("as_of"), "to": now}, "sections": secs, "sources": srcs, "notes": notes,
            "accounts_snapshot": accounts_snapshot, "closing": CLOSING,
            "estimate_label": "Any performance or projection figure here is an estimate from the app's own models, not a measured result."}


def to_text(rep, fmt_date=lambda t: time.strftime("%Y-%m-%d", time.localtime(t))):
    """Plain-text copy of the report (COPY REPORT). Ends with the closing line."""
    S, L = rep.get("sections") or {}, []
    L += [f"{rep.get('household_name', 'Household')}: quarterly household report", f"Report date {fmt_date(rep['generated_at'])}; figures as of {fmt_date(rep['as_of'])}"
          + (f"; covers {fmt_date(rep['period']['from'])} to {fmt_date(rep['period']['to'])}" if (rep.get("period") or {}).get("from") else "; first report, no earlier one to compare"), ""]

    def stale(sec):
        return f"  (last known values from {fmt_date(sec['last_known_date'])}: {sec.get('reason')})" if sec.get("from_last_known") else (f"  ({sec.get('reason')})" if sec.get("unavailable") else "")
    n = S.get("net_worth") or {}
    L.append("NET WORTH" + stale(n))
    if not n.get("unavailable"):
        L.append(f"  Now {_money(n.get('net_worth'))}: assets {_money(n.get('assets'))}, debts {_money(n.get('debts'))}.")
        if n.get("change") is not None:
            L.append(f"  Change since the last report: {_money(n['change'])} (assets {_money(n.get('assets_change'))}, debts {_money(n.get('debts_change'))}).")
            for c in n.get("biggest_changes") or []:
                L.append(f"    {c['name']}: {_money(c['change'])}" + (" (new)" if c["kind"] == "new" else " (closed)" if c["kind"] == "closed" else ""))
    w = S.get("where") or {}
    L += ["", "WHERE THE MONEY IS" + stale(w)]
    for x in w.get("by_institution") or []:
        L.append(f"  {x['label']}: {_money(x['value'])}")
    for x in w.get("by_kind") or []:
        L.append(f"  by kind, {x['label']}: {_money(x['value'])}")
    p = S.get("plan") or {}
    L += ["", "PLAN STATUS" + stale(p)] + [f"  {t}" for t in p.get("lines") or []]
    a = S.get("attention") or {}
    L += ["", "NEEDS ATTENTION" + stale(a)] + ([f"  {i['text']}" for i in a.get("items") or []] if not a.get("none") else ["  Nothing flagged right now."])
    s = S.get("safety") or {}
    L += ["", "SAFETY NET" + stale(s)]
    ef = s.get("emergency_fund")
    if ef:
        L.append(f"  Emergency fund: {ef.get('months_covered')} months covered against a target of {ef.get('target_months')} ({ef.get('status')}).")
    for g in s.get("insurance_gaps") or []:
        L.append(f"  Protection: {g['line']}: need {_money(g['need'])}, in force {_money(g['in_force'])}, gap {_money(g['gap'])}.")
    i = S.get("if_something_happens") or {}
    L += ["", "IF SOMETHING HAPPENS" + stale(i)]
    if i and not i.get("unavailable"):
        L.append(f"  Executor: {i.get('executor') or 'not recorded'}. Will kept: {i.get('will_location') or 'not recorded'}.")
        for c in i.get("contacts") or []:
            L.append(f"  Contact: {c['name']}" + (f" ({c['role']})" if c["role"] else ""))
        L.append("  " + str(i.get("binder")))
        if i.get("emergency_access"):
            L.append("  " + i["emergency_access"])
    for nt in rep.get("notes") or []:
        L.append("")
        L.append(nt)
    L += ["", rep.get("estimate_label", ""), "", rep.get("closing", CLOSING)]
    return "\n".join(L)
