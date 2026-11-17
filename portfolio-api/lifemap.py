# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""lifemap.py -- the LIFE MAP engine: which life topics need attention at this stage, and why (design: LIFE-STAGE-DESIGN-2026-11-16.md).

Pure functions, no I/O. Everything that is a judgment (stages, topic names, stage weights, thresholds, wording, which pages a topic lists) is
DATA in a config document (lifemap_default.json, later editable in SETTINGS), so the design can be played with without code. The config may
carry named VARIANTS (partial overrides, deep-merged) so a decision point can be compared side by side with the baseline (see compare()).

A check reads one named source (an existing endpoint's result), tries its rules in order and returns the first match:
    {state, severity, reason, next, ...}  with state in ok | attention | todo | noinfo | na | error.
A reason is a sentence whose figures come from the source; if a figure it needs is missing the check becomes `noinfo`: it never prints a blank
or a guess. No expression in the config is evaluated as code: conditions are lists/dicts handled below.

    score = relevance x urgency x timing (+ pin bonus);   relevance = stage weight + household modifiers (0..3), at least 3 when pinned.
"""
import copy
import datetime
import re

STATES = ("ok", "attention", "todo", "noinfo", "na", "error")
BANNED = re.compile(r"\b(you should|you must|we recommend|i recommend|buy|sell|trim|invest in)\b", re.I)
_MISSING = object()


# ---------------------------------------------------------------- paths and values
def get_path(obj, path):
    """Dotted path into dicts (and list indexes). _MISSING when any step is absent or None."""
    cur = obj
    for part in str(path).split("."):
        if isinstance(cur, dict):
            if part not in cur or cur[part] is None:
                return _MISSING
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return _MISSING
    return cur


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _cmp(op, a, b):
    try:
        if op == "startswith":
            return isinstance(a, str) and a.startswith(str(b))
        if op == "in":
            return a in b
        if op == "containsany":
            return isinstance(a, str) and any(str(w).lower() in a.lower() for w in b)
        return {"==": a == b, "!=": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]
    except (TypeError, KeyError):
        return False


def eval_cond(cond, vars_):
    """True/False for a condition: true, [name, op, value?], {"all": [...]}, {"any": [...]}, {"not": cond}. A name with no value is only
    true for `missing`; every comparison against a missing value is False."""
    if cond is True:
        return True
    if cond is False or cond is None:
        return False
    if isinstance(cond, dict):
        if "all" in cond:
            return all(eval_cond(c, vars_) for c in cond["all"])
        if "any" in cond:
            return any(eval_cond(c, vars_) for c in cond["any"])
        if "not" in cond:
            return not eval_cond(cond["not"], vars_)
        return False
    if isinstance(cond, (list, tuple)) and len(cond) >= 2:
        name, op = cond[0], cond[1]
        val = vars_.get(name, _MISSING)
        if val is None:
            val = _MISSING
        if op == "missing":
            return val is _MISSING
        if op == "present":
            return val is not _MISSING
        if val is _MISSING or len(cond) < 3:
            return False
        return _cmp(op, val, cond[2])
    return False


def resolve_let(let, source, sources=None, ctx=None):
    """name -> value for a check. A value is a dotted path into the source, or {"count"|"sum"|"max"|"min"|"list"|"first"|"sub": ...}; a spec
    may name another source with "from" (the check's own source is the default), so one check can combine two endpoints' results."""
    out = {}
    for name, spec in (let or {}).items():
        if isinstance(spec, dict) and spec.get("from") and sources is not None:
            other = sources.get(spec["from"])
            if not isinstance(other, dict) or other.get("_error"):
                continue
            spec = {k: v for k, v in spec.items() if k != "from"}
            if "path" in spec:
                spec = spec["path"]
            sub = resolve_let({name: spec}, other, None, ctx)
            if name in sub:
                out[name] = sub[name]
            continue
        if isinstance(spec, str):
            v = get_path(source, spec)
            if v is not _MISSING:
                out[name] = v
            continue
        if "sub" in spec:                      # a difference of two values: each a path into the source or {"ctx": name}
            def _val(x):
                return (ctx or {}).get(x["ctx"], _MISSING) if isinstance(x, dict) else get_path(source, x)
            a, b = _val(spec["sub"][0]), _val(spec["sub"][1])
            if _num(a) and _num(b):
                out[name] = a - b
            continue
        kind = next((k for k in ("count", "sum", "max", "min", "list", "first") if k in spec), None)
        if not kind:
            continue
        rows = get_path(source, spec[kind])
        if rows is _MISSING or not isinstance(rows, list):
            continue
        where = spec.get("where")
        sel = [r for r in rows if isinstance(r, dict) and (where is None or eval_cond(where, r))]
        if kind == "count":
            out[name] = len(sel)
            continue
        if kind in ("list", "first"):
            vals = [r.get(spec.get("field")) for r in sel if r.get(spec.get("field")) is not None]
            if kind == "list":
                out[name] = vals
            elif vals:
                out[name] = vals[0]
            continue
        vals = [r.get(spec.get("field")) for r in sel if _num(r.get(spec.get("field")))]
        if not vals:
            if kind == "sum":
                out[name] = 0.0
            continue
        out[name] = {"sum": sum, "max": max, "min": min}[kind](vals)
    return out


def _fmt(v, f):
    if f == "usd":
        return ("-" if v < 0 else "") + "$" + format(int(round(abs(v))), ",") if _num(v) else None
    if f == "n0":
        return format(int(round(v)), ",") if _num(v) else None
    if f == "n1":
        return f"{v:.1f}" if _num(v) else None
    if f == "pct":
        return f"{v:.0f}%" if _num(v) else None
    if f == "year":
        return str(int(v)) if _num(v) else None
    if f == "list":
        return ", ".join(str(x) for x in v) if isinstance(v, list) and v else None
    return str(v)


def render(template, vars_):
    """Fill {name|format} placeholders. None when any placeholder has no usable value (the caller turns that into 'not enough information')."""
    bad = []

    def sub(m):
        name, _, f = m.group(1).partition("|")
        v = vars_.get(name, _MISSING)
        s = None if v is _MISSING else _fmt(v, f or "")
        if s is None:
            bad.append(name)
            return ""
        return s
    text = re.sub(r"\{([^{}]+)\}", sub, template or "")
    return None if bad else text


# ---------------------------------------------------------------- checks
def run_check(check, sources, ctx=None):
    """-> {id, label, state, severity, reason, from, next}. The first rule whose condition holds wins; a missing source is 'could not check'."""
    base = {"id": check.get("id"), "label": check.get("label"), "from": check.get("from") or {}}
    src = sources.get(check.get("source"), _MISSING)
    if src is _MISSING or src is None or (isinstance(src, dict) and src.get("_error")):
        return {**base, "state": "error", "severity": "low", "reason": f"Could not check: {check.get('label')} (the source did not answer).", "next": None}
    vars_ = {**{k: v for k, v in (ctx or {}).items() if v is not None}, **resolve_let(check.get("let"), src, sources, ctx)}
    for rule in check.get("rules") or []:
        if not eval_cond(rule.get("when"), vars_):
            continue
        state, sev = rule.get("state", "ok"), rule.get("severity", "normal")
        reason = render(rule.get("reason"), vars_)
        if reason is None:           # a figure the sentence needs is missing: never print a blank or a guess
            return {**base, "state": "noinfo", "severity": "normal", "reason": f"Not enough information yet to check: {check.get('label')}.",
                    "next": rule.get("next")}
        return {**base, "state": state, "severity": sev, "reason": reason, "next": rule.get("next")}
    return {**base, "state": "na", "severity": "low", "reason": "", "next": None}      # no rule matched: this check does not apply


URGENCY_STATE = {"attention": "attention", "todo": "todo", "noinfo": "noinfo", "error": "error", "ok": "ok"}


def _urgency(cfg, state, severity):
    u = cfg["urgency"]
    if state in ("error", "ok"):
        return float(u[state])
    return float(u[state][severity])


def topic_status(checks):
    """The most urgent check decides: attention > todo > noinfo > error > ok (na checks are ignored). -> (status, deciding check or None)."""
    live = [c for c in checks if c["state"] != "na"]
    if not live:
        return "noinfo", None
    order = {"attention": 0, "todo": 1, "noinfo": 2, "error": 3, "ok": 4}
    sev = {"high": 0, "normal": 1, "low": 2}
    live.sort(key=lambda c: (order[c["state"]], sev.get(c["severity"], 1)))
    return live[0]["state"], live[0]


# ---------------------------------------------------------------- stage, windows, scoring
def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def apply_variant(cfg, name):
    if not name or name == "default":
        return cfg
    over = (cfg.get("variants") or {}).get(name)
    return deep_merge({k: v for k, v in cfg.items() if k != "variants"}, over) if over is not None else cfg


def stage_for(cfg, ctx):
    for st in cfg["stages"]:
        if eval_cond(st.get("when"), ctx):
            return st
    return cfg["stages"][-1]


def _window_active(win, today):
    f, t = win["from"], win["to"]
    md = today.strftime("%m-%d")
    return (f <= md <= t) if f <= t else (md >= f or md <= t)


def score_topics(cfg, stage, ctx, topics_out, pins, today):
    """Adds relevance/urgency/timing/score/band to each status topic dict (in place) and returns the list sorted by score."""
    sid = stage["id"]
    mods = [m for m in cfg.get("modifiers", []) if eval_cond(m.get("when"), ctx)]
    wins = [w for w in cfg.get("windows", []) if _window_active(w, today)]
    win_checks = {c for w in wins for c in w.get("checks", [])}
    pins = set(pins or [])
    stage_ids = [s["id"] for s in cfg["stages"]]
    nxt = stage_ids[stage_ids.index(sid) + 1] if sid in stage_ids and stage_ids.index(sid) + 1 < len(stage_ids) else None
    for t in topics_out:
        if t["kind"] != "status":
            continue
        if not t["checks"]:            # no checks built for this topic in this release: listed, never ranked, never claimed to be fine
            t.update({"status": "unchecked", "band": "later", "pinned": t["id"] in pins, "score": 0.0, "failing": 0,
                      "parts": {"relevance": 0, "base_weight": 0, "modifiers": [], "urgency": 0, "urgency_from": None, "timing": 1.0,
                                "safety_valve": False, "why_weight": None}})
            continue
        w = (cfg["weights"].get(t["id"]) or {})
        base = w.get(sid, 0)
        adds = [(m["id"], m["add"][t["id"]]) for m in mods if t["id"] in (m.get("add") or {})]
        rel = max(0, min(3, base + sum(a for _, a in adds)))
        if t["id"] in pins:
            rel = 3
        status, deciding = topic_status(t["checks"])
        urg = _urgency(cfg, deciding["state"], deciding["severity"]) if deciding else float(cfg["urgency"]["noinfo"]["normal"])
        failing = [c for c in t["checks"] if c["state"] in ("attention", "todo")]
        timed = any(f"{t['id']}.{c['id']}" in win_checks or c["id"] in win_checks for c in failing)
        timing = float(cfg.get("timing_boost", 1.3)) if timed else 1.0
        safety_valve = any(c["state"] == "attention" and c["severity"] == "high" for c in t["checks"])
        if safety_valve:
            rel = max(rel, 2)
        score = rel * urg * timing + (float(cfg.get("pin_bonus", 0.5)) if t["id"] in pins else 0.0)
        next_w = (w.get(nxt, 0) if nxt else 0)
        if status != "ok" and rel >= 2:
            band = "now"
        elif status == "ok" and rel >= 1:
            band = "ontrack"
        elif next_w >= 2 and next_w > base:
            band = "coming"
        else:
            band = "later"
        t.update({"status": status, "band": band, "pinned": t["id"] in pins, "score": round(score, 3), "failing": len(failing),
                  "parts": {"relevance": rel, "base_weight": base, "modifiers": [{"id": i, "add": a} for i, a in adds], "urgency": urg,
                            "urgency_from": (deciding or {}).get("id"), "timing": timing, "safety_valve": safety_valve,
                            "why_weight": w.get("why")}})
    out = [t for t in topics_out if t["kind"] == "status"]
    order = {x["id"]: i for i, x in enumerate(cfg["topics"])}
    out.sort(key=lambda t: (-t["score"], -t["failing"], -t["parts"]["base_weight"], order[t["id"]]))
    return out


# ---------------------------------------------------------------- the whole map
def validate(cfg):
    """-> list of problems (empty = fine). Used before a saved config replaces the default, and by the tests."""
    errs = []
    for k in ("stages", "topics", "weights", "urgency"):
        if k not in cfg:
            errs.append(f"missing '{k}'")
    if errs:
        return errs
    sids = [s.get("id") for s in cfg["stages"]]
    if len(set(sids)) != len(sids):
        errs.append("duplicate stage id")

    def _num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")
    mc = cfg.get("max_cards", 3)
    if not (isinstance(mc, int) and not isinstance(mc, bool) and 1 <= mc <= 9):
        errs.append("max_cards must be a whole number from 1 to 9")
    for k, lo, hi in (("timing_boost", 1.0, 3.0), ("pin_bonus", 0.0, 2.0)):
        if k in cfg and not (_num(cfg[k]) and lo <= cfg[k] <= hi):
            errs.append(f"{k} must be a number from {lo:g} to {hi:g}")
    urg = cfg.get("urgency")
    if not isinstance(urg, dict):
        errs.append("urgency must be a table of numbers")
    else:
        for k, v in urg.items():
            for sub, x in (v.items() if isinstance(v, dict) else [(None, v)]):
                if not (_num(x) and 0 <= x <= 1):
                    errs.append(f"urgency {k}{'/' + str(sub) if sub else ''} must be a number from 0 to 1")
    seen = set()
    for t in cfg["topics"]:
        if t.get("id") in seen:
            errs.append(f"duplicate topic id {t.get('id')}")
        seen.add(t.get("id"))
        if t.get("kind") not in ("status", "explore"):
            errs.append(f"topic {t.get('id')}: kind must be status or explore")
        if t.get("kind") == "status":
            w = cfg["weights"].get(t["id"])
            if not isinstance(w, dict) or any(s not in w for s in sids):
                errs.append(f"weights for {t['id']} must cover every stage")
            elif any(not (isinstance(w[s], int) and 0 <= w[s] <= 3) for s in sids):
                errs.append(f"weights for {t['id']} must be whole numbers 0 to 3")
        texts = [t.get("why", "")] + [r.get("reason", "") for c in t.get("checks", []) for r in c.get("rules", [])]
        for tx in texts:
            m = BANNED.search(tx or "")
            if m:
                errs.append(f"topic {t.get('id')}: wording '{m.group(0)}' reads as advice")
    return errs


def build(cfg, sources, ctx, pins=None, today=None, variant=None, stage_override=None):
    """The full response for GET /api/lifemap. `sources`: name -> endpoint result (or {"_error": ...}); `ctx`: age, target_age, ... values."""
    cfg = apply_variant(cfg, variant)
    today = today or datetime.date.today()
    stage = next((s for s in cfg["stages"] if s["id"] == stage_override), None) if stage_override else None
    stage = stage or stage_for(cfg, ctx)
    cctx = {**ctx, "stage": stage["id"]}                   # checks may read the household context and the stage (for example age, stopped, stage)
    topics = []
    for t in cfg["topics"]:
        d = {"id": t["id"], "name": t["name"], "kind": t["kind"], "why": t.get("why", ""), "pages": t.get("pages", []),
             "stage_line": (t.get("stage_lines") or {}).get(stage["id"]), "checks": []}
        for c in t.get("checks", []) if t["kind"] == "status" else []:
            d["checks"].append(run_check(c, sources, cctx))
        topics.append(d)
    ranked = score_topics(cfg, stage, ctx, topics, pins, today)
    now = [t for t in ranked if t["band"] == "now"]
    ids = [s["id"] for s in cfg["stages"]]
    nxt = cfg["stages"][ids.index(stage["id"]) + 1] if ids.index(stage["id"]) + 1 < len(ids) else None
    return {"config_version": cfg.get("version"), "variant": variant or "default",
            "stage": {"id": stage["id"], "name": stage["name"], "line": stage.get("line"), "age": ctx.get("age"), "target_age": ctx.get("target_age"),
                      "years_to_stop": ctx.get("years_to_stop"), "target_age_is_default": bool(ctx.get("target_age_is_default")),
                      "next": ({"id": nxt["id"], "name": nxt["name"]} if nxt else None)},
            "topics": topics, "order": [t["id"] for t in ranked], "top": [t["id"] for t in now[:int(cfg.get("max_cards", 3))]],
            "bands": {b: [t["id"] for t in ranked if t["band"] == b] for b in ("now", "ontrack", "coming", "later")},
            "note": "Priorities and facts about your own data, not advice. Informational only."}


def compare(cfg, sources, ctx, variants, pins=None, today=None):
    """Side-by-side: for each variant the stage and the top topics with scores (so a decision can be revisited with real data)."""
    out = []
    for v in variants:
        r = build(cfg, sources, ctx, pins=pins, today=today, variant=v)
        sc = {t["id"]: t["score"] for t in r["topics"] if t["kind"] == "status"}
        out.append({"variant": v, "stage": r["stage"]["id"], "top": r["top"], "order": r["order"], "scores": {k: sc[k] for k in r["order"]}})
    return out
