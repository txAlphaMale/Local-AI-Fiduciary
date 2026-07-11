"""
thesis.py — per-holding thesis & exit-trigger registry (Fable G14). Exit-discipline-as-code.

The review's exit-discipline gap: the app was rich on what to BUY and how to size, but had no
mechanism for what to SELL — the hardest discipline for a concentrated owner. This module records,
per holding, WHY you own it (the thesis), a snapshot of the fundamentals AT ENTRY, and a set of
PRE-REGISTERED, FALSIFIABLE exit triggers. On each evaluation run the current fundamentals are
checked against those triggers; a tripped trigger is a pre-committed signal to review/exit and
"forces a brief" citing exactly which condition broke — so the sell decision is made in advance,
in the cold light of the original thesis, not in the heat of a drawdown.

Pure logic: the app supplies the current metrics (from the same edgar/valuation/reverse-DCF stack
the scanner uses); this module only decides whether each pre-registered condition is now true.
Falsifiable by construction — every trigger is a concrete metric-vs-threshold comparison.
Decision-support, not advice.
"""

# Metrics a trigger can reference (keys the app's _thesis_metrics supplies).
METRIC_LABELS = {
    "fcf_margin_pct": "FCF margin %", "rev_growth_pct": "revenue growth %",
    "net_margin_pct": "net margin %", "piotroski": "Piotroski F-score",
    "pe_forward": "forward P/E", "implied_growth_pct": "reverse-DCF implied growth %",
    "expected_return_pct": "analyst upside %", "price": "price",
    "accruals_ratio_pct": "accruals ratio % (NI−CFO)/assets — earnings quality",
    "valuation_percentile": "valuation percentile vs peers (0=cheapest…100=richest)",
    "insider_net_buy_pct": "insider net open-market buying % (−100 all selling…+100 all buying)",
}
OPS = ("<", "<=", ">", ">=")


def _cmp(op, cur, thr):
    """True if the exit condition is met, False if not, None if not evaluable."""
    if cur is None or thr is None:
        return None
    if op == "<":
        return cur < thr
    if op == "<=":
        return cur <= thr
    if op == ">":
        return cur > thr
    if op == ">=":
        return cur >= thr
    return None


def resolve_trigger(trigger, metrics, entry):
    """Turn a trigger spec into a concrete evaluated comparison against current metrics.
    Built-in TYPES (falsifiable): fcf_margin_floor, fcf_margin_decay (vs entry), rev_growth_floor,
    implied_growth_ceiling, piotroski_floor, pe_ceiling, drawdown_from_entry, or 'custom'
    ({metric, op, threshold}). Returns {type,label,metric,op,threshold,current,tripped}."""
    t = trigger.get("type", "custom")
    entry = entry or {}
    metric = op = thr = None
    label = trigger.get("label")
    if t == "fcf_margin_floor":
        metric, op, thr = "fcf_margin_pct", "<", trigger.get("threshold")
        label = label or f"FCF margin < {thr}%"
    elif t == "fcf_margin_decay":
        metric, op = "fcf_margin_pct", "<"
        base, dec = entry.get("fcf_margin_pct"), trigger.get("decay_pts")
        thr = (base - dec) if (base is not None and dec is not None) else None
        label = label or f"FCF margin {dec}pt below entry ({base}%)"
    elif t == "rev_growth_floor":
        metric, op, thr = "rev_growth_pct", "<", trigger.get("threshold")
        label = label or f"revenue growth < {thr}%"
    elif t == "implied_growth_ceiling":
        metric, op, thr = "implied_growth_pct", ">", trigger.get("threshold")
        label = label or f"price implies growth > {thr}%/yr (reverse-DCF)"
    elif t == "piotroski_floor":
        metric, op, thr = "piotroski", "<=", trigger.get("threshold", 3)
        label = label or f"Piotroski F ≤ {thr}"
    elif t == "pe_ceiling":
        metric, op, thr = "pe_forward", ">", trigger.get("threshold")
        label = label or f"forward P/E > {thr}"
    elif t == "drawdown_from_entry":
        metric, op = "price", "<"
        ep, dd = entry.get("price"), trigger.get("pct")
        thr = (ep * (1 - dd / 100.0)) if (ep and dd is not None) else None
        label = label or f"price {dd}% below entry (${ep})"
    else:
        metric, op, thr = trigger.get("metric"), trigger.get("op", "<"), trigger.get("threshold")
        label = label or f"{METRIC_LABELS.get(metric, metric)} {op} {thr}"
    cur = metrics.get(metric) if metric else None
    return {"type": t, "label": label, "metric": metric, "op": op,
            "threshold": (round(thr, 2) if isinstance(thr, (int, float)) else thr),
            "current": (round(cur, 2) if isinstance(cur, (int, float)) else cur),
            "tripped": _cmp(op, cur, thr)}


def evaluate_thesis(thesis, metrics):
    """Evaluate every pre-registered trigger for one holding against current metrics.
    Returns the thesis + evaluated triggers + tripped count + a pre-committed action."""
    entry = thesis.get("entry") or {}
    trs = [resolve_trigger(t, metrics or {}, entry) for t in (thesis.get("triggers") or [])]
    tripped = [t for t in trs if t["tripped"] is True]
    unknown = [t for t in trs if t["tripped"] is None]
    if tripped:
        action = f"EXIT REVIEW — {len(tripped)} trigger(s) tripped"
    elif trs:
        action = "thesis intact"
    else:
        action = "no triggers set"
    return {"ticker": thesis.get("ticker"), "thesis": thesis.get("thesis"), "entry": entry,
            "triggers": trs, "tripped_count": len(tripped), "unknown_count": len(unknown),
            "tripped_labels": [t["label"] for t in tripped], "action": action,
            "forces_brief": bool(tripped),
            "created": thesis.get("created"), "updated": thesis.get("updated")}


def default_triggers():
    """A sensible starter set of falsifiable exit triggers for a growth holding."""
    return [
        {"type": "piotroski_floor", "threshold": 3},
        {"type": "fcf_margin_decay", "decay_pts": 10},
        {"type": "implied_growth_ceiling", "threshold": 40},
        {"type": "drawdown_from_entry", "pct": 30},
    ]
