# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
emergency.py -- break-glass emergency access for the document vault (TASK-002, Vault Phase 2).

A designated household member (the "designee", an existing VIEWER account) can reach
`emergency`-visibility vault items -- or take over as admin -- only after a time-delayed request
the admin could have denied. Every step is audited and notifies the admin on every channel.
Ships DISABLED. Read-only mission unchanged: nothing here moves money.

STATE (all in store.db, so grants/denials survive a restart):
  emergency_config   singleton: enabled, designee_uid, wait_seconds, grant_type (view|takeover),
                     view_grant_days, last_test_ok, pending_json (a cooling-down change)
  emergency_requests pending -> denied | granted | cancelled;  granted -> revoked | expired;
                     'granting' is an internal crash-safe step for takeover only.
At most ONE open (pending/granting/granted) request exists at a time (partial unique index).

TIME: every clock read goes through _now(); tests call set_clock() to time-travel.

COOL-DOWN: a change that WIDENS access (enable, new designee, shorter wait, view->takeover, longer
view grant) takes effect COOLDOWN_S (24 h) after it is made, and is notified immediately, so a
hijacked admin session can't instantly re-aim the feature. Reductions apply at once.

TEST MODE: EMERGENCY_ALLOW_SHORT_WAIT=1 (default OFF; never set it on a real deployment) allows a
wait in seconds, and skips the 24 h wait floor, the 24 h cool-down and the notification gate, so the
whole flow can be exercised in minutes in a scratch container. is_test_mode() is surfaced in the UI.
"""
import json
import os
import threading
import time

import audit
import auth
import notifications
import store

ALLOW_SHORT_WAIT = os.environ.get("EMERGENCY_ALLOW_SHORT_WAIT", "0").lower() in ("1", "true", "yes", "on")
CHECK_INTERVAL_S = int(os.environ.get("EMERGENCY_CHECK_INTERVAL_S", "900"))
DEFAULT_WAIT_S = 168 * 3600
MIN_WAIT_S = 24 * 3600
COOLDOWN_S = 24 * 3600
TEST_NOTIFY_MAX_AGE_S = 24 * 3600
GRANT_TYPES = ("view", "takeover")
OPEN = ("pending", "granting", "granted")

_clock = time.time
_lock = threading.RLock()


def _now():
    return _clock()


def set_clock(fn=None):
    """Tests only: inject a time source (None restores the real clock)."""
    global _clock
    _clock = fn or time.time


def is_test_mode():
    return ALLOW_SHORT_WAIT


class EmergencyError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


# ---------------------------------------------------------------- schema
def init_db():
    with store._lock:
        with store._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS emergency_config(
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER NOT NULL DEFAULT 0,
                designee_uid TEXT,
                wait_seconds INTEGER NOT NULL DEFAULT 604800,
                grant_type TEXT NOT NULL DEFAULT 'view',
                view_grant_days INTEGER NOT NULL DEFAULT 30,
                last_test_ok REAL,
                pending_json TEXT,
                updated REAL NOT NULL DEFAULT 0
            )""")
            c.execute("INSERT OR IGNORE INTO emergency_config(id) VALUES (1)")
            c.execute("""CREATE TABLE IF NOT EXISTS emergency_requests(
                id TEXT PRIMARY KEY,
                slot INTEGER NOT NULL DEFAULT 1,
                designee_uid TEXT NOT NULL,
                requested_at REAL NOT NULL,
                status TEXT NOT NULL,
                decided_at REAL,
                decided_by TEXT,
                grant_type TEXT,
                grant_expires_at REAL,
                wait_seconds INTEGER NOT NULL,
                reminder_flags TEXT NOT NULL DEFAULT '{}'
            )""")
            c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_emergency_one_open
                ON emergency_requests(slot) WHERE status IN ('pending','granting','granted')""")


# ---------------------------------------------------------------- config
def _cfg_row():
    with store._conn() as c:
        return dict(c.execute("SELECT * FROM emergency_config WHERE id=1").fetchone())


def _public_cfg(r):
    return {"enabled": bool(r["enabled"]), "designee_uid": r["designee_uid"],
            "wait_seconds": r["wait_seconds"], "wait_hours": round(r["wait_seconds"] / 3600, 4),
            "grant_type": r["grant_type"], "view_grant_days": r["view_grant_days"]}


def _apply_due_pending():
    """Promote a cooled-down widening change into the active config (idempotent)."""
    with _lock:
        r = _cfg_row()
        if not r["pending_json"]:
            return r
        p = json.loads(r["pending_json"])
        if _now() < p["effective_at"]:
            return r
        new = p["config"]
        with store._conn() as c:
            c.execute("""UPDATE emergency_config SET enabled=?, designee_uid=?, wait_seconds=?, grant_type=?,
                         view_grant_days=?, pending_json=NULL, updated=? WHERE id=1""",
                      (int(new["enabled"]), new["designee_uid"], new["wait_seconds"], new["grant_type"],
                       new["view_grant_days"], _now()))
        if r["designee_uid"] != new["designee_uid"] or not new["enabled"]:
            _close_open("designee changed or feature disabled")
        _audit("emergency.config_changed", detail={"applied_after_cooldown": True, **new})
        _notify("emergency.config_changed", {"summary": "A scheduled emergency-access change has taken effect.",
                                              **new})
        return _cfg_row()


def get_config():
    return _public_cfg(_apply_due_pending())


def _designee_problem(uid, grant_type):
    """None if `uid` can be the designee for `grant_type`, else a human reason."""
    u = auth.get_user_by_id(uid) if uid else None
    if not u:
        return "designee account does not exist"
    pu = auth.public_user(u)
    if pu["role"] != "viewer" or auth.is_admin(u):
        return "designee must be a viewer account (not the admin)"
    if grant_type == "takeover":
        if not pu["mfa_enabled"]:
            return "takeover requires the designee to have MFA enabled"
        if not pu["recovery_key_set"]:
            return "takeover requires the designee to have a recovery key set"
    return None


def _admin():
    for u in auth.list_users():
        if u.get("role") == "admin":
            return u
    return None


def _channels_ok():
    a = _admin()
    return bool(a and notifications.subscription_count(a["id"]) >= 1) or notifications.smtp_configured()


def mark_test_ok():
    with store._conn() as c:
        c.execute("UPDATE emergency_config SET last_test_ok=? WHERE id=1", (_now(),))


def _is_widening(cur, new):
    if not new["enabled"]:
        return False
    if not cur["enabled"]:
        return True
    return (new["designee_uid"] != cur["designee_uid"] or new["wait_seconds"] < cur["wait_seconds"]
            or (new["grant_type"] == "takeover" and cur["grant_type"] != "takeover")
            or new["view_grant_days"] > cur["view_grant_days"])


def update_config(admin, body):
    """Admin-only (caller enforces). Validates server-side, fail closed. Returns
    {"config", "scheduled": None | {"effective_at", "config"}}."""
    with _lock:
        cur_row = _apply_due_pending()
        cur = _public_cfg(cur_row)
        new = dict(cur)
        if "enabled" in body:
            new["enabled"] = bool(body["enabled"])
        if "designee_uid" in body:
            new["designee_uid"] = (body["designee_uid"] or None)
        if "grant_type" in body:
            if body["grant_type"] not in GRANT_TYPES:
                raise EmergencyError("grant_type must be 'view' or 'takeover'")
            new["grant_type"] = body["grant_type"]
        if "view_grant_days" in body:
            try:
                d = int(body["view_grant_days"])
            except (TypeError, ValueError):
                raise EmergencyError("view_grant_days must be a whole number of days")
            if not 1 <= d <= 365:
                raise EmergencyError("view_grant_days must be between 1 and 365")
            new["view_grant_days"] = d
        if "wait_seconds" in body:
            if not ALLOW_SHORT_WAIT:
                raise EmergencyError("wait_seconds is only accepted when EMERGENCY_ALLOW_SHORT_WAIT=1 (test mode)")
            try:
                w = int(body["wait_seconds"])
            except (TypeError, ValueError):
                raise EmergencyError("wait_seconds must be a whole number")
            if w < 5:
                raise EmergencyError("wait_seconds must be at least 5")
            new["wait_seconds"] = w
        elif "wait_hours" in body:
            try:
                w = int(round(float(body["wait_hours"]) * 3600))
            except (TypeError, ValueError):
                raise EmergencyError("wait_hours must be a number")
            if w < MIN_WAIT_S:
                raise EmergencyError("wait_hours must be at least 24")
            if w > 90 * 86400:
                raise EmergencyError("wait_hours must be at most 2160 (90 days)")
            new["wait_seconds"] = w
        if new["enabled"]:
            if not new["designee_uid"]:
                raise EmergencyError("choose a designee before enabling")
            prob = _designee_problem(new["designee_uid"], new["grant_type"])
            if prob:
                raise EmergencyError(prob)
            if not ALLOW_SHORT_WAIT:
                if not _channels_ok():
                    raise EmergencyError("cannot enable: you need at least one push subscription or working SMTP "
                                         "so you can actually be reached during the wait", 409)
                t = _cfg_row()["last_test_ok"]
                if not t or _now() - t > TEST_NOTIFY_MAX_AGE_S:
                    raise EmergencyError("cannot enable: send a successful test notification (within the last "
                                         "24 hours) first", 409)
        new["wait_seconds"] = max(new["wait_seconds"], 1)
        widening = _is_widening(cur, new)
        scheduled = None
        if widening and not ALLOW_SHORT_WAIT:
            eff = _now() + COOLDOWN_S
            with store._conn() as c:
                c.execute("UPDATE emergency_config SET pending_json=?, updated=? WHERE id=1",
                          (json.dumps({"effective_at": eff, "config": new}), _now()))
            scheduled = {"effective_at": eff, "config": new}
            _audit("emergency.config_changed", actor=admin,
                   detail={"scheduled": True, "effective_at": eff, **new})
            _notify("emergency.config_changed", {
                "summary": "An emergency-access change that WIDENS access was scheduled and takes effect in "
                           "24 hours. If you did not do this, disable emergency access now.", **new})
        else:
            with store._conn() as c:
                c.execute("""UPDATE emergency_config SET enabled=?, designee_uid=?, wait_seconds=?, grant_type=?,
                             view_grant_days=?, pending_json=NULL, updated=? WHERE id=1""",
                          (int(new["enabled"]), new["designee_uid"], new["wait_seconds"], new["grant_type"],
                           new["view_grant_days"], _now()))
            _audit("emergency.config_changed", actor=admin, detail={"scheduled": False, **new})
            _notify("emergency.config_changed", {"summary": "Emergency-access settings were changed.", **new})
            if not new["enabled"] or (cur["designee_uid"] != new["designee_uid"]):
                _close_open("config disabled or designee changed", by=admin["id"])
        return {"config": get_config(), "scheduled": scheduled}


# ---------------------------------------------------------------- audit / notify glue
def _audit(event, actor=None, detail=None, outcome="success"):
    kw = {}
    if actor:
        kw = {"actor": actor["id"], "actor_name": actor.get("username")}
    audit.emit(event, outcome=outcome, resource="/api/emergency", detail=detail or {}, **kw)


def _notify(event, detail, to_designee_uid=None, extra_admin_uids=None):
    """Always-on 'emergency' category (never muted by prefs): every admin push subscription plus the
    shared SMTP address. Never raises -- a notification failure must not break the state machine."""
    try:
        notifications.dispatch_emergency(event, detail, to_uid=to_designee_uid,
                                         extra_uids=extra_admin_uids or [])
    except Exception as e:
        print(f"[emergency] notify failed: {e}", flush=True)


def _name(uid):
    u = auth.get_user_by_id(uid) if uid else None
    return (u or {}).get("username") or "the designee"


# ---------------------------------------------------------------- requests
def _row(req_id=None, where="status IN ('pending','granting','granted')"):
    with store._conn() as c:
        if req_id:
            r = c.execute("SELECT * FROM emergency_requests WHERE id=?", (req_id,)).fetchone()
        else:
            r = c.execute(f"SELECT * FROM emergency_requests WHERE {where} ORDER BY requested_at DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def open_request():
    return _row()


def history(limit=50):
    with store._conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM emergency_requests ORDER BY requested_at DESC LIMIT ?", (limit,)).fetchall()]


def _grants_at(req):
    return req["requested_at"] + req["wait_seconds"]


def is_designee(user):
    cfg = get_config()
    return bool(user and cfg["designee_uid"] and user.get("id") == cfg["designee_uid"]
                and (user.get("role") or "admin") == "viewer")


def request_access(user):
    with _lock:
        cfg = get_config()
        if not (user and cfg["designee_uid"] and user["id"] == cfg["designee_uid"]):
            raise EmergencyError("only the designated emergency contact can request emergency access", 403)
        if (user.get("role") or "admin") != "viewer":
            raise EmergencyError("only the designated emergency contact can request emergency access", 403)
        if not cfg["enabled"]:
            raise EmergencyError("emergency access is not enabled", 409)
        prob = _designee_problem(user["id"], cfg["grant_type"])
        if prob:
            raise EmergencyError(prob, 409)
        if open_request():
            raise EmergencyError("an emergency request is already open", 409)
        rid = os.urandom(8).hex()
        now = _now()
        try:
            with store._conn() as c:
                c.execute("""INSERT INTO emergency_requests(id,designee_uid,requested_at,status,grant_type,
                             wait_seconds) VALUES(?,?,?,?,?,?)""",
                          (rid, user["id"], now, "pending", cfg["grant_type"], cfg["wait_seconds"]))
        except Exception:
            raise EmergencyError("an emergency request is already open", 409)
        req = _row(rid)
        d = {"request_id": rid, "designee": user["username"], "grant_type": cfg["grant_type"],
             "grants_at": _grants_at(req), "wait_seconds": cfg["wait_seconds"]}
        _audit("emergency.requested", actor=user, detail=d)
        _notify("emergency.requested", d, to_designee_uid=user["id"])
        return req


def _set_status(req, new_status, by=None, expect=("pending",), **extra):
    """Compare-and-set status transition. Returns True only for the single winner."""
    sets = ["status=?", "decided_at=?", "decided_by=?"]
    args = [new_status, _now(), by]
    for k, v in extra.items():
        sets.append(f"{k}=?")
        args.append(v)
    args += [req["id"]]
    q = f"UPDATE emergency_requests SET {','.join(sets)} WHERE id=? AND status IN ({','.join('?' * len(expect))})"
    with store._conn() as c:
        cur = c.execute(q, args + list(expect))
        return cur.rowcount == 1


def cancel(user):
    with _lock:
        req = open_request()
        if not req or req["status"] != "pending" or req["designee_uid"] != (user or {}).get("id"):
            raise EmergencyError("no pending request of yours to cancel", 404)
        if _set_status(req, "cancelled", by=user["id"]):
            _audit("emergency.cancelled", actor=user, detail={"request_id": req["id"]})
            _notify("emergency.cancelled", {"request_id": req["id"], "designee": user["username"]},
                    to_designee_uid=user["id"])
        return _row(req["id"])


def deny(admin):
    with _lock:
        req = open_request()
        if not req or req["status"] != "pending":
            raise EmergencyError("no pending request to deny", 404)
        if _set_status(req, "denied", by=admin["id"]):
            d = {"request_id": req["id"], "designee": _name(req["designee_uid"])}
            _audit("emergency.denied", actor=admin, detail=d)
            _notify("emergency.denied", d, to_designee_uid=req["designee_uid"])
        return _row(req["id"])


def approve(admin):
    """Early grant."""
    with _lock:
        req = open_request()
        if not req or req["status"] != "pending":
            raise EmergencyError("no pending request to approve", 404)
        return _grant(req, by=admin, early=True)


def revoke(admin):
    with _lock:
        req = open_request()
        if not req or req["status"] != "granted":
            raise EmergencyError("no active grant to revoke", 404)
        if req["grant_type"] == "takeover":
            raise EmergencyError("a takeover grant cannot be revoked (the admin role has already moved); "
                                 "use the new admin's account-transfer instead", 409)
        if _set_status(req, "revoked", by=admin["id"], expect=("granted",)):
            d = {"request_id": req["id"], "designee": _name(req["designee_uid"])}
            _audit("emergency.revoked", actor=admin, detail=d)
            _notify("emergency.revoked", d, to_designee_uid=req["designee_uid"])
        return _row(req["id"])


def _grant(req, by=None, early=False):
    cfg = get_config()
    gt = req["grant_type"] or cfg["grant_type"]
    d = {"request_id": req["id"], "designee": _name(req["designee_uid"]), "grant_type": gt, "early": early}
    if gt == "view":
        exp = _now() + cfg["view_grant_days"] * 86400
        if not _set_status(req, "granted", by=(by or {}).get("id"), expect=("pending",), grant_expires_at=exp):
            return _row(req["id"])
        d["grant_expires_at"] = exp
        _audit("emergency.granted", actor=by, detail=d)
        _notify("emergency.granted", d, to_designee_uid=req["designee_uid"])
        return _row(req["id"])
    # takeover: 'granting' is the single-winner claim; the role swap is idempotent so a crash or a
    # second thread run between the steps can never swap twice (see _finish_takeover).
    if not _set_status(req, "granting", by=(by or {}).get("id"), expect=("pending",)):
        return _row(req["id"])
    return _finish_takeover(_row(req["id"]), by)


def _finish_takeover(req, by=None):
    designee = auth.get_user_by_id(req["designee_uid"])
    old_admin = _admin()
    d = {"request_id": req["id"], "designee": _name(req["designee_uid"]), "grant_type": "takeover"}
    if designee and auth.is_admin(designee):
        pass                                  # swap already happened (crash recovery) -- just finish
    elif designee and old_admin:
        try:
            auth.transfer_admin_role(old_admin["id"], designee["id"])
        except ValueError as e:
            _set_status(req, "cancelled", by=None, expect=("granting",))
            _audit("emergency.takeover_failed", detail={**d, "reason": str(e)}, outcome="failure")
            _notify("emergency.auto_disabled", {**d, "reason": f"takeover could not complete: {e}"})
            return _row(req["id"])
    else:
        _set_status(req, "cancelled", by=None, expect=("granting",))
        return _row(req["id"])
    if _set_status(req, "granted", by=(by or {}).get("id"), expect=("granting",)):
        with store._conn() as c:                # feature auto-disables after a takeover
            c.execute("UPDATE emergency_config SET enabled=0, pending_json=NULL, updated=? WHERE id=1", (_now(),))
        _audit("emergency.granted", actor=by, detail=d)
        _audit("auth.user.role_changed", detail={"reason": "emergency_takeover", "new_admin": d["designee"]})
        _notify("emergency.granted", {**d, "summary": "Admin role TRANSFERRED to the designee."},
                to_designee_uid=req["designee_uid"],
                extra_admin_uids=[old_admin["id"]] if old_admin else [])
    return _row(req["id"])


def _close_open(reason, by=None):
    """Cancel a pending request / revoke a live view grant. (Takeover grants are final.)"""
    req = open_request()
    if not req:
        return
    if req["status"] == "pending":
        _set_status(req, "cancelled", by=by, expect=("pending",))
    elif req["status"] == "granted" and req["grant_type"] != "takeover":
        _set_status(req, "revoked", by=by, expect=("granted",))
    else:
        return
    d = {"request_id": req["id"], "reason": reason, "designee": _name(req["designee_uid"])}
    _audit("emergency.cancelled" if req["status"] == "pending" else "emergency.revoked", detail=d)


def revalidate(reason="periodic check", force=False):
    """If the configured designee is no longer valid (deleted, role changed, MFA/recovery key gone),
    disable the feature, close any open request, audit and notify. `force=True` (used when the ADMIN
    role itself moves) disables regardless: the configuration was made by a different admin.
    Returns True if it disabled."""
    with _lock:
        r = _cfg_row()
        if not r["enabled"]:
            return False
        req = open_request()
        if req and req["status"] == "granting":
            return False                       # takeover mid-flight; tick() completes it
        if req and req["status"] == "granted" and req["grant_type"] == "takeover":
            return False
        prob = _designee_problem(r["designee_uid"], r["grant_type"])
        if force and not prob:
            prob = "the admin role changed hands; the new admin must review and re-enable emergency access"
        if not prob:
            return False
        with store._conn() as c:
            c.execute("UPDATE emergency_config SET enabled=0, pending_json=NULL, updated=? WHERE id=1", (_now(),))
        _close_open(f"auto-disabled: {prob}")
        d = {"reason": prob, "trigger": reason}
        _audit("emergency.auto_disabled", detail=d, outcome="denied")
        _notify("emergency.auto_disabled", d)
        return True


# ---------------------------------------------------------------- read-side grant check
def emergency_granted(user):
    """THE grant condition: True iff `user` is the designee of a granted, unexpired VIEW grant.
    Evaluated against SQLite on every call -- no cache -- so deny/revoke/expiry bite on the very
    next request."""
    if not user:
        return False
    with store._conn() as c:
        r = c.execute("""SELECT 1 FROM emergency_requests WHERE status='granted' AND grant_type='view'
                         AND designee_uid=? AND grant_expires_at > ?""", (user.get("id"), _now())).fetchone()
    return r is not None


# ---------------------------------------------------------------- scheduler
def tick():
    """One pass of the state machine (the thread calls this every CHECK_INTERVAL_S; tests call it
    directly after set_clock()). Idempotent."""
    out = {"granted": [], "expired": [], "reminders": [], "disabled": False}
    with _lock:
        _apply_due_pending()
        req = open_request()
        if req and req["status"] == "granting":
            _finish_takeover(req)
            return out
        if revalidate("scheduled check"):
            out["disabled"] = True
            return out
        req = open_request()
        if not req:
            return out
        now = _now()
        if req["status"] == "pending":
            cfg = get_config()
            if cfg["enabled"] and now >= _grants_at(req):
                _grant(req)
                out["granted"].append(req["id"])
            else:
                flags = json.loads(req["reminder_flags"] or "{}")
                frac = (now - req["requested_at"]) / max(req["wait_seconds"], 1)
                for key, thr in (("r50", 0.5), ("r90", 0.9)):
                    if frac >= thr and not flags.get(key):
                        flags[key] = True
                        with store._conn() as c:
                            c.execute("UPDATE emergency_requests SET reminder_flags=? WHERE id=?",
                                      (json.dumps(flags), req["id"]))
                        d = {"request_id": req["id"], "designee": _name(req["designee_uid"]),
                             "grants_at": _grants_at(req), "percent": int(thr * 100)}
                        _audit("emergency.reminder", detail=d)
                        _notify("emergency.reminder", d)
                        out["reminders"].append(key)
        elif req["status"] == "granted" and req["grant_type"] == "view" and now >= (req["grant_expires_at"] or 0):
            if _set_status(req, "expired", expect=("granted",)):
                d = {"request_id": req["id"], "designee": _name(req["designee_uid"])}
                _audit("emergency.expired", detail=d)
                _notify("emergency.expired", d, to_designee_uid=req["designee_uid"])
                out["expired"].append(req["id"])
    return out


def start_emergency_thread(interval_s=None):
    interval = int(interval_s or CHECK_INTERVAL_S)

    def _loop():
        while True:
            try:
                tick()
            except Exception as e:
                print(f"[emergency] thread error: {e}", flush=True)
            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True, name="emergency")
    t.start()
    return t


# ---------------------------------------------------------------- status views (role-scoped)
def status_for(user):
    """Role-scoped status. Admin: everything. Designee: only their own request state. Anyone else:
    {"is_designee": False}. The API layer must call this -- never return raw rows."""
    if user and auth.is_admin(user):
        cfg = get_config()
        r = _cfg_row()
        pend = json.loads(r["pending_json"]) if r["pending_json"] else None
        req = open_request()
        return {"role": "admin", "config": cfg, "test_mode": is_test_mode(),
                "pending_change": pend, "open_request": _req_view(req),
                "history": [_req_view(h) for h in history()],
                "viewers": [{"id": u["id"], "username": u["username"], "mfa_enabled": u["mfa_enabled"],
                             "recovery_key_set": u["recovery_key_set"]}
                            for u in auth.list_users() if u.get("role") == "viewer"],
                "channels": {"push": bool(_admin() and notifications.subscription_count(_admin()["id"]) >= 1),
                             "smtp": notifications.smtp_configured(),
                             "last_test_ok": r["last_test_ok"]}}
    if not is_designee(user):
        return {"is_designee": False}
    cfg = get_config()
    req = open_request()
    last = None
    with store._conn() as c:
        row = c.execute("SELECT * FROM emergency_requests WHERE designee_uid=? ORDER BY requested_at DESC LIMIT 1",
                        (user["id"],)).fetchone()
        last = dict(row) if row else None
    cur = req or last
    return {"is_designee": True, "enabled": cfg["enabled"], "wait_seconds": cfg["wait_seconds"],
            "grant_type": cfg["grant_type"],
            "status": cur["status"] if cur else None,
            "requested_at": cur["requested_at"] if cur else None,
            "grants_at": _grants_at(cur) if cur and cur["status"] == "pending" else None,
            "grant_expires_at": cur["grant_expires_at"] if cur else None,
            "granted_now": emergency_granted(user), "now": _now()}


def _req_view(r):
    if not r:
        return None
    if r["decided_by"]:
        dby = _name(r["decided_by"])
    elif r["decided_at"]:
        dby = "automatic"                       # the scheduler (wait elapsed / expiry / auto-disable)
    else:
        dby = None
    return {"id": r["id"], "designee_uid": r["designee_uid"], "designee": _name(r["designee_uid"]),
            "requested_at": r["requested_at"], "status": r["status"], "decided_at": r["decided_at"],
            "decided_by": dby,
            "grant_type": r["grant_type"], "grant_expires_at": r["grant_expires_at"],
            "grants_at": _grants_at(r), "wait_seconds": r["wait_seconds"]}
