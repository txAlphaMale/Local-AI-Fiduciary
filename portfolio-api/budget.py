# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
budget.py -- API budget gate: know when a provider is out of quota / rate-limited, PAUSE, PROBE, RECOVER.

Why (Jason 2026-11-12, idea-scavenger design): sweeping the US-listed universe in tranches will make
thousands of calls. Without a gate, a quota or rate-limit failure looks like "this company has no
data" and the sweep would hammer a dead provider (or, worse, blacklist good companies).

Contract
  check(name, batch=False, max_wait=0) -> {"ok": bool, "wait": seconds, "reason": str|None, "deferred": bool}
      Call BEFORE every external request. ok=False means "do not call now": the caller must treat the
      request as DEFERRED (retry later), never as "no data". Interactive callers (page, chat, daily
      jobs) pass batch=False; the scavenger passes batch=True and gets a lower per-minute pace plus an
      optional daily soft budget so it can never use a provider's whole quota.
  report(name, status=None, retry_after=None, ok=None)
      Call AFTER the request with the HTTP status (None = network error). Classifies it:
        quota code  (e.g. Tavily 432/433/434) -> PAUSED (kind "quota"), backoff 1h doubling to 24h
        rate code   (429; SEC also 403)       -> PAUSED (kind "rate"), max(Retry-After, 60s doubling to 1h)
        5 consecutive network/5xx failures    -> PAUSED (kind "outage"), 5 min doubling to 1h
        success                               -> OPEN, backoff reset
        other 4xx (400/401/403 on APIs where it means "not on your plan") -> ignored, never pauses
  Recovery is half-open: when the pause expires the gate runs the provider's free probe (Tavily GET
  /usage costs no credits); with no probe it lets exactly ONE trial call through. A passing probe or a
  successful trial re-opens the gate; a failure extends the pause with a longer backoff.
State is persisted (paused state survives a restart); the sliding-window call counters are not.
No network and no app imports here: the clock, persistence and probes are injected, so it unit-tests offline.
"""
import datetime
import threading
import time

PROVIDERS = {
    # hard_per_min: the most we ever send interactively (provider limit minus a margin);
    # batch_per_min: the scavenger's lower pace; batch_daily: default daily soft budget for batch callers.
    # tavily: 1,000 free credits/cycle, then pay-as-you-go at $0.008/credit (Jason enabled PAYGO 2026-11-12, with an ACCOUNT-side cap of 3,000 paid credits = 4,000/month). The app
    # keeps its OWN dollar cap (default_hard_cap_usd) because an account-side cap is unconfirmed, and the scavenger
    # (batch) is held to the FREE allowance unless Jason raises batch_cycle_credits.
    "tavily":  {"label": "Tavily news search", "hard_per_min": 90, "batch_per_min": 30, "batch_daily": 10,
                "quota_codes": (432, 433, 434), "rate_codes": (429,),
                "free_allowance": 1000, "usd_per_credit": 0.008, "default_hard_cap_usd": 8.0},
    "finnhub": {"label": "Finnhub market data", "hard_per_min": 55, "batch_per_min": 45, "batch_daily": None,
                "quota_codes": (), "rate_codes": (429,)},
    "sec":     {"label": "SEC EDGAR", "hard_per_min": 450, "batch_per_min": 300, "batch_daily": None,
                "quota_codes": (), "rate_codes": (403, 429)},
}
OUTAGE_AFTER = 5            # consecutive network/5xx failures before an "outage" pause
RATE_BASE, RATE_MAX = 60, 3600
QUOTA_BASE, QUOTA_MAX = 3600, 86400
OUTAGE_BASE, OUTAGE_MAX = 300, 3600
TRIAL_TIMEOUT = 60          # a half-open trial that never reports back must not wedge the gate


def _fmt(sec):
    sec = int(max(0, sec))
    if sec >= 86400:
        return f"{sec // 86400}d {sec % 86400 // 3600}h"
    if sec >= 3600:
        return f"{sec // 3600}h {sec % 3600 // 60}m"
    if sec >= 60:
        return f"{sec // 60}m"
    return f"{sec}s"


class Budget:
    def __init__(self, clock=time.time, load=None, save=None, providers=None):
        self._clock = clock
        self._load, self._save = load, save
        self._lock = threading.Lock()
        self._cfg = providers or PROVIDERS
        self._probes = {}
        self._last_forced = {}
        self._st = {n: self._fresh() for n in self._cfg}
        self._binfo = {n: [] for n in self._cfg}      # parallel to _win: (tag, is_batch) per call
        self._win = {n: [] for n in self._cfg}        # call timestamps, last 60 s (not persisted)
        self._restore()

    # ---------------------------------------------------------------- state
    @staticmethod
    def _fresh():
        return {"state": "open", "kind": None, "reason": None, "paused_until": 0, "level": 0, "since": None,
                "last_ok": None, "last_error": None, "consec_fail": 0, "trial_at": 0,
                "day": 0, "day_count": 0, "batch_daily": None, "renewal": None, "usage": None,
                "hard_cap_usd": None, "batch_cycle_credits": None, "usage_try_at": 0,
                "cycle": {"start": 0, "credits": 0, "by_source": {}}, "last_cycle": None}

    _PERSIST = ("state", "kind", "reason", "paused_until", "level", "since", "last_ok", "last_error",
                "consec_fail", "day", "day_count", "batch_daily", "renewal", "usage",
                "hard_cap_usd", "batch_cycle_credits", "usage_try_at", "cycle", "last_cycle")

    def _restore(self):
        try:
            saved = (self._load() or {}) if self._load else {}
        except Exception:
            saved = {}
        for n, s in (saved or {}).items():
            if n in self._st and isinstance(s, dict):
                for k in self._PERSIST:
                    if k in s:
                        self._st[n][k] = s[k]
                if self._st[n]["state"] == "probing":      # restart mid-trial: fall back to paused (expired -> trial again)
                    self._st[n]["state"] = "paused"

    def _persist(self):
        if not self._save:
            return
        try:
            self._save({n: {k: s[k] for k in self._PERSIST} for n, s in self._st.items()})
        except Exception:
            pass                                             # persistence is best-effort; the gate still works

    def register_probe(self, name, fn):
        """fn() -> {"state": "ok"|"quota"|"rate"|"unknown", "detail": str, "usage": dict|None}. Must not raise."""
        self._probes[name] = fn

    # ---------------------------------------------------------------- pausing
    def _pause(self, name, kind, secs, reason, now):
        s = self._st[name]
        renewal = s.get("renewal")
        until = now + secs
        if kind == "quota" and renewal and renewal > now:
            until = min(until, renewal)                     # known renewal date: never wait past it
        s.update(state="paused", kind=kind, reason=reason, paused_until=until, since=s.get("since") or now,
                 level=s["level"] + 1, trial_at=0)
        self._persist()

    def _open(self, name, now):
        s = self._st[name]
        s.update(state="open", kind=None, reason=None, paused_until=0, level=0, since=None, last_ok=now,
                 consec_fail=0, trial_at=0)
        self._persist()

    # ---------------------------------------------------------------- spend cycle (providers with a free allowance)
    @staticmethod
    def _cycle_start(now, renewal):
        """Epoch of the most recent billing-cycle start <= now. Anchored on the renewal date's day of month
        (clamped to 28 so every month has it); calendar months when no renewal date is set."""
        anchor = min(28, datetime.datetime.fromtimestamp(renewal, datetime.timezone.utc).day) if renewal else 1
        d = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
        y, m = d.year, d.month
        if d.day < anchor:
            y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        return datetime.datetime(y, m, anchor, tzinfo=datetime.timezone.utc).timestamp()

    def _roll_cycle(self, name, now):
        s = self._st[name]
        start = self._cycle_start(now, s.get("renewal"))
        if s["cycle"].get("start") != start:
            if s["cycle"].get("start"):
                s["last_cycle"] = dict(s["cycle"], end=start)
            s["cycle"] = {"start": start, "credits": 0, "by_source": {}}
            s["usage_try_at"] = 0                              # re-read the provider's own counter for the new cycle
            self._persist()

    def _hard_cap_credits(self, name):
        cfg, s = self._cfg[name], self._st[name]
        usd = s["hard_cap_usd"] if s["hard_cap_usd"] is not None else cfg.get("default_hard_cap_usd", 0.0)
        return cfg["free_allowance"] + int(round(usd / cfg["usd_per_credit"]))

    def _apply_usage(self, name, usage, now):
        """A provider usage snapshot is authoritative: never let the local credit estimate sit BELOW it."""
        s = self._st[name]
        s["usage"] = {**usage, "at": now}
        pu = usage.get("plan_usage")
        if self._cfg[name].get("free_allowance") and isinstance(pu, (int, float)):
            self._roll_cycle(name, now)
            s["cycle"]["credits"] = max(s["cycle"]["credits"], int(pu))

    # ---------------------------------------------------------------- the two calls every client makes
    def check(self, name, batch=False, max_wait=0, tag=None):
        deadline = self._clock() + max(0, max_wait)
        while True:
            res = self._check_once(name, batch, tag)
            if res["ok"] or res.get("pace") is None or self._clock() + res["wait"] > deadline:
                res.pop("pace", None)
                return res
            time.sleep(min(res["wait"], 5))                  # pacing only: wait for the 60 s window to free a slot

    def _check_once(self, name, batch, tag=None):
        if name not in self._st:
            return {"ok": True, "wait": 0, "reason": None, "deferred": False}
        cfg = self._cfg[name]
        probe_now = False
        with self._lock:
            now = self._clock()
            s = self._st[name]
            if s["state"] in ("paused", "probing"):
                if now < s["paused_until"]:
                    return self._deny(name, s["paused_until"] - now, "paused")
                if s["state"] == "probing" and now - s["trial_at"] < TRIAL_TIMEOUT:
                    return self._deny(name, 5, "a recovery trial is in flight")
                if name in self._probes:
                    s["state"], s["trial_at"] = "probing", now
                    probe_now = True
                else:                                        # half-open: this caller IS the single trial call
                    s["state"], s["trial_at"] = "probing", now
                    self._persist()
                    self._count(name, now, batch, tag)
                    return {"ok": True, "wait": 0, "reason": None, "deferred": False, "trial": True}
        if probe_now:
            out = self._run_probe(name)
            with self._lock:
                now = self._clock()
                if out.get("usage"):
                    self._apply_usage(name, out["usage"], now)
                if out.get("state") == "ok":
                    self._open(name, now)
                else:
                    kind = {"quota": "quota", "rate": "rate"}.get(out.get("state"), "outage")
                    self._st[name]["state"] = "paused"
                    self._pause(name, kind, self._backoff(name, kind), out.get("detail") or "probe failed", now)
                    return self._deny(name, self._st[name]["paused_until"] - now, "paused")
        if cfg.get("free_allowance") and name in self._probes:
            with self._lock:
                now = self._clock()
                self._roll_cycle(name, now)
                due = now - self._st[name]["usage_try_at"] >= 3600
                if due:
                    self._st[name]["usage_try_at"] = now
            if due:                                           # hourly, free: keeps the credit estimate honest + spots exhaustion early
                out = self._run_probe(name)
                with self._lock:
                    now = self._clock()
                    if out.get("usage"):
                        self._apply_usage(name, out["usage"], now)
                    if out.get("state") == "quota":
                        self._st[name]["state"] = "paused"
                        self._pause(name, "quota", self._backoff(name, "quota"), out.get("detail") or "quota used up", now)
                        return self._deny(name, self._st[name]["paused_until"] - now, "paused")
                    self._persist()
        with self._lock:
            now = self._clock()
            s = self._st[name]
            if cfg.get("free_allowance"):
                self._roll_cycle(name, now)
                cyc, nxt = s["cycle"]["credits"], None
                cap = self._hard_cap_credits(name)
                nxt = self._cycle_start(now + 32 * 86400, s.get("renewal")) - now
                if cyc >= cap:
                    usd = s["hard_cap_usd"] if s["hard_cap_usd"] is not None else cfg.get("default_hard_cap_usd", 0.0)
                    return self._deny(name, nxt, f"monthly spend cap reached (${usd:g} of pay-as-you-go beyond the "
                                                 f"{cfg['free_allowance']} free credits; raise it in the budget settings)")
                if batch:
                    bc = s["batch_cycle_credits"] if s["batch_cycle_credits"] is not None else cfg["free_allowance"]
                    if cyc >= bc:
                        return self._deny(name, nxt, f"the scavenger is held to {bc} credits per cycle ({cyc} used)")
            self._prune(name, now)
            used = len(self._win[name])
            lim = cfg["hard_per_min"]
            if batch:
                # Background work has its OWN lane (batch_per_min) so interactive bursts cannot starve it, but the
                # combined total still stays under the provider ceiling (hard_per_min + 3 spare, below Finnhub's 60).
                bused = sum(1 for e in self._binfo[name] if e[1])
                over = bused >= cfg["batch_per_min"] or used >= lim + 3
                lim = cfg["batch_per_min"] if bused >= cfg["batch_per_min"] else lim + 3
            else:
                over = used >= lim
            if over:
                wait = max(1.0, 60 - (now - self._win[name][0]))
                r = self._deny(name, wait, f"pacing ({used}/{lim} calls in the last minute)")
                r["pace"] = True
                return r
            if batch:
                cap = s["batch_daily"] if s["batch_daily"] is not None else cfg["batch_daily"]
                day = int(now // 86400)
                if s["day"] != day:
                    s["day"], s["day_count"] = day, 0
                if cap is not None and s["day_count"] >= cap:
                    return self._deny(name, (day + 1) * 86400 - now, f"daily soft budget used ({cap} calls)")
                s["day_count"] += 1
                if s["day_count"] % 20 == 0:
                    self._persist()                              # so a restart does not zero the idea-hunting counter
            self._count(name, now, batch, tag)
            return {"ok": True, "wait": 0, "reason": None, "deferred": False}

    def _run_probe(self, name):
        try:
            return self._probes[name]() or {"state": "unknown"}
        except Exception as e:
            return {"state": "unknown", "detail": f"probe error ({type(e).__name__})"}

    def _backoff(self, name, kind):
        base, mx = {"quota": (QUOTA_BASE, QUOTA_MAX), "rate": (RATE_BASE, RATE_MAX)}.get(kind, (OUTAGE_BASE, OUTAGE_MAX))
        return min(base * (2 ** max(0, self._st[name]["level"])), mx)

    def _deny(self, name, wait, why):
        s = self._st[name]
        label = self._cfg[name]["label"]
        reason = (f"{label} is paused ({s['kind']}: {s['reason']}); next check in {_fmt(wait)}."
                  if why == "paused" else f"{label} deferred: {why}.")
        return {"ok": False, "wait": wait, "reason": reason, "deferred": True}

    def _callers(self, name):
        out = {}
        for tag, b in self._binfo[name]:
            k = ("batch " if b else "") + (tag or "?")
            out[k] = out.get(k, 0) + 1
        return out

    def _prune(self, name, now):
        w = self._win[name]
        while w and now - w[0] >= 60:
            w.pop(0)
            if self._binfo[name]:
                self._binfo[name].pop(0)

    def _count(self, name, now, batch=False, tag=None):
        self._prune(name, now)
        self._win[name].append(now)
        self._binfo[name].append((tag, bool(batch)))

    def report(self, name, status=None, retry_after=None, ok=None, source="other", credits=1):
        if name not in self._st:
            return
        cfg = self._cfg[name]
        with self._lock:
            now = self._clock()
            s = self._st[name]
            if ok is None:
                ok = status is not None and 200 <= status < 300
            if ok:
                if cfg.get("free_allowance"):                    # log WHAT consumed credits (Tavily's own breakdown is paid-plan-only)
                    self._roll_cycle(name, now)
                    s["cycle"]["credits"] += credits
                    bs = s["cycle"]["by_source"]
                    bs[source] = bs.get(source, 0) + credits
                    self._persist()
                s["consec_fail"] = 0
                if s["state"] != "open" or s["level"]:
                    self._open(name, now)
                else:
                    s["last_ok"] = now
                return
            if status in cfg["quota_codes"]:
                s["last_error"] = {"at": now, "http": status}
                self._pause(name, "quota", self._backoff(name, "quota"),
                            f"HTTP {status}: plan credit limit reached", now)
            elif status in cfg["rate_codes"]:
                s["last_error"] = {"at": now, "http": status}
                secs = max(float(retry_after or 0), self._backoff(name, "rate"))
                self._pause(name, "rate", min(secs, RATE_MAX), f"HTTP {status}: rate limit", now)
            elif status is None or status >= 500:
                s["last_error"] = {"at": now, "http": status}
                s["consec_fail"] += 1
                if s["consec_fail"] >= OUTAGE_AFTER or s["state"] == "probing":
                    self._pause(name, "outage", self._backoff(name, "outage"),
                                f"{s['consec_fail']} failures in a row (network or server error)", now)
            # any other 4xx (400, 401, 403 "not on your plan") never pauses the provider

    # ---------------------------------------------------------------- settings + status
    def configure(self, name, batch_daily="keep", renewal="keep", hard_cap_usd="keep", batch_cycle_credits="keep"):
        if name not in self._st:
            raise KeyError(name)
        with self._lock:
            s = self._st[name]
            if batch_daily != "keep":
                s["batch_daily"] = None if batch_daily is None else max(0, int(batch_daily))
            if renewal != "keep":
                s["renewal"] = None if renewal is None else float(renewal)
            if hard_cap_usd != "keep":
                s["hard_cap_usd"] = None if hard_cap_usd is None else max(0.0, float(hard_cap_usd))
            if batch_cycle_credits != "keep":
                s["batch_cycle_credits"] = None if batch_cycle_credits is None else max(0, int(batch_cycle_credits))
            self._persist()

    def force_probe(self, name):
        """Run the provider's free probe now (used by the status panel's TEST button). -> the probe result."""
        if name not in self._probes:
            return {"state": "unknown", "detail": "no free probe for this provider"}
        with self._lock:
            now = self._clock()
            if now - self._last_forced.get(name, -1e9) < 60:       # Tavily allows 10 /usage calls per 10 min
                return {"state": "unknown", "detail": "wait a minute between probes"}
            self._last_forced[name] = now
        out = self._run_probe(name)
        with self._lock:
            now = self._clock()
            if out.get("usage"):
                self._apply_usage(name, out["usage"], now)
            if out.get("state") == "ok" and self._st[name]["state"] != "open":
                self._open(name, now)
            else:
                self._persist()
        return out

    def _spend(self, name, now):
        cfg, s = self._cfg[name], self._st[name]
        if not cfg.get("free_allowance"):
            return None
        self._roll_cycle(name, now)
        cyc = s["cycle"]["credits"]
        free, ucost = cfg["free_allowance"], cfg["usd_per_credit"]
        cap_c = self._hard_cap_credits(name)
        return {"cycle_start": s["cycle"]["start"], "credits_used": cyc, "free_allowance": free,
                "over_free": max(0, cyc - free), "est_overage_usd": round(max(0, cyc - free) * ucost, 2),
                "hard_cap_usd": s["hard_cap_usd"] if s["hard_cap_usd"] is not None else cfg.get("default_hard_cap_usd"),
                "credits_left_before_cap": max(0, cap_c - cyc),
                "batch_cycle_credits": s["batch_cycle_credits"] if s["batch_cycle_credits"] is not None else free,
                "by_source": dict(s["cycle"]["by_source"]), "last_cycle": s["last_cycle"]}

    def status(self):
        with self._lock:
            now = self._clock()
            rows = []
            for n, cfg in self._cfg.items():
                s = self._st[n]
                self._prune(n, now)
                day_count = s["day_count"] if s["day"] == int(now // 86400) else 0
                rows.append({
                    "provider": n, "label": cfg["label"], "state": s["state"], "kind": s["kind"],
                    "reason": s["reason"], "paused_until": s["paused_until"] or None,
                    "resumes_in": _fmt(s["paused_until"] - now) if s["state"] != "open" and s["paused_until"] > now else None,
                    "backoff_level": s["level"], "calls_last_minute": len(self._win[n]), "recent_callers": self._callers(n),
                    "interactive_per_min": cfg["hard_per_min"], "batch_per_min": cfg["batch_per_min"],
                    "batch_calls_today": day_count,
                    "batch_daily_budget": s["batch_daily"] if s["batch_daily"] is not None else cfg["batch_daily"],
                    "renewal": s["renewal"], "usage": s["usage"], "last_ok": s["last_ok"],
                    "last_error": s["last_error"], "has_probe": n in self._probes,
                    "spend": self._spend(n, now)})
            return rows
