"""
detections.py -- SEC-4: a small rules engine over the SEC-1/SEC-2 audit log, the "detect" stage of
the log-source -> normalize -> detect -> alert pipeline DEMO-SITE-BUILD-PLAN.md's SIEM framing (§6)
calls for. This is deliberately the SIEM-adjacent half of the GRC-heavy security-portfolio track --
proof that the audit log isn't just a record, something actually WATCHES it.

CONTROL MAPPING:
  - ISO/IEC 27001:2022  A.8.16 (Monitoring activities), A.5.24-A.5.28 (incident management)
  - NIST SP 800-53 Rev5 IR-4 (incident handling), IR-5 (monitoring), IR-6 (reporting),
                        SI-4 (system monitoring)
  - SOC 2 (2017 TSC)    CC7.3-CC7.5 (incident evaluation/response/communication)

ALERT CHANNEL, stated plainly: this is a self-hosted, single-tenant household app with no outbound
alerting infrastructure (no email/Slack/pager integration built anywhere else in the codebase
either). Rather than bolt on a parallel alert store, an alert IS a security event: each rule that
fires calls audit.emit("detection.alert", ...), reusing the SAME encrypted, hash-chained, retention-
governed storage and the SAME redacted viewer (SETTINGS -> SECURITY -> SECURITY AUDIT LOG) already
built for SEC-1/SEC-2. A real managed-SOC deployment would route detection.alert events to a SIEM/
webhook/pager from here; that last-mile integration is a (documented, easy) follow-up, not faked.

RULES (v1 -- deliberately few, chosen to be genuinely demonstrable against this app's own event
vocabulary, not a padded list):
  1. failed_login_burst    -- N failed logins for the same attempted username within a window.
  2. first_seen_ip         -- a successful login from a source IP never seen before for that
                               account (which already has login history).
  3. mfa_disable_then_change -- MFA disabled, then a secret/config/account-management change by
                               the SAME actor shortly after -- the textbook account-takeover
                               sequence (disable the 2nd factor, then change something sensitive).

DEDUP: uses the audit log itself as the source of truth (no separate state file to lose on
restart) -- before emitting a new alert, each rule checks whether a matching detection.alert
already exists for the same (rule, subject) within the rule's own window, via the `resource`
column (e.g. "detection:failed_login_burst:jason"), which is plaintext and cheap to query (see
audit.query_core()) without needing to decrypt every candidate row's `detail`.

TRIGGER: run_detections() is called by a background thread (start_detections_thread(), mirrors
audit.start_retention_thread()/tls.start_renewal_thread()) on a fixed interval, and is also
callable on demand (POST /api/detections/run, app.py) for testing/demo purposes -- same
automatic+manual pattern as retention and ACME renewal elsewhere in this app.
"""
import os
import threading
import datetime

import audit

FAILED_LOGIN_BURST_THRESHOLD = int(os.environ.get("FAILED_LOGIN_BURST_THRESHOLD", "5"))
FAILED_LOGIN_BURST_WINDOW_MIN = int(os.environ.get("FAILED_LOGIN_BURST_WINDOW_MIN", "10"))
MFA_DISABLE_ALERT_WINDOW_MIN = int(os.environ.get("MFA_DISABLE_ALERT_WINDOW_MIN", "30"))
DETECTIONS_INTERVAL_S = int(os.environ.get("DETECTIONS_INTERVAL_S", "300"))

# Event types rule 3 treats as "a sensitive change" following an MFA disable.
_SENSITIVE_CHANGE_TYPES = {
    "secret.changed", "secret.rotated", "config.changed",
    "auth.user.created", "auth.user.deleted", "auth.recovery_key.regenerated",
    "auth.password.changed", "auth.username.changed",
}

RULES = (
    {"id": "failed_login_burst", "label": "Failed-login burst",
     "desc": f"{FAILED_LOGIN_BURST_THRESHOLD}+ failed logins for the same username within "
             f"{FAILED_LOGIN_BURST_WINDOW_MIN} minutes."},
    {"id": "first_seen_ip", "label": "First-seen IP for an account",
     "desc": "A successful login from a source IP never seen before for that account "
             "(informational -- may be legitimate, e.g. a new location)."},
    {"id": "mfa_disable_then_change", "label": "MFA disabled, then a sensitive change",
     "desc": f"MFA disabled, followed within {MFA_DISABLE_ALERT_WINDOW_MIN} minutes by a secret/"
             f"config/account change from the same actor -- the classic account-takeover sequence."},
)


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _minutes_ago_iso(minutes):
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _already_alerted(resource_key, window_ts):
    """True if a detection.alert with this exact resource already exists at/after window_ts --
    the dedup check shared by all three rules. Uses audit.query_core (plaintext columns only, no
    decrypt) since `resource` already encodes everything needed to dedup."""
    hits = audit.query_core(event_type="detection.alert", resource_prefix=resource_key,
                             since_ts=window_ts, limit=1)
    return bool(hits)


def _check_failed_login_burst():
    window_ts = _minutes_ago_iso(FAILED_LOGIN_BURST_WINDOW_MIN)
    rows = audit.query_core(event_type="auth.login.failure", since_ts=window_ts, limit=2000)
    by_user = {}
    for r in rows:
        by_user.setdefault(r["actor_name"] or "-", []).append(r)
    alerts = []
    for username, hits in by_user.items():
        if len(hits) < FAILED_LOGIN_BURST_THRESHOLD:
            continue
        resource_key = f"detection:failed_login_burst:{username}"
        if _already_alerted(resource_key, window_ts):
            continue
        ips = sorted({h["src_ip"] for h in hits if h["src_ip"]})
        audit.emit("detection.alert", outcome="denied", actor_name=username,
                   resource=resource_key,
                   detail={"rule": "failed_login_burst", "username": username,
                           "count": len(hits), "window_minutes": FAILED_LOGIN_BURST_WINDOW_MIN,
                           "source_ips": ips})
        alerts.append(resource_key)
    return alerts


def _check_first_seen_ip():
    # Look at recent successful logins; for each, check whether that (actor, ip) pair has any
    # PRIOR successful login on record. limit=2000 is generous for a household-scale log; a
    # much larger deployment would need this windowed rather than "recent N", but that's not
    # this app's scale.
    rows = audit.query_core(event_type="auth.login.success", limit=2000)
    seen = {}  # actor_name -> set of ips seen so far, built up in chronological order
    alerts = []
    for r in rows:
        actor = r["actor_name"] or "-"
        ip = r["src_ip"] or "-"
        prior_ips = seen.setdefault(actor, set())
        if prior_ips and ip not in prior_ips:
            resource_key = f"detection:first_seen_ip:{actor}:{ip}"
            # Dedup across the whole retained history, not just a short window -- an IP that's
            # ever been alerted-on for this actor shouldn't re-alert every sweep.
            if not _already_alerted(resource_key, "0000-01-01T00:00:00.000000Z"):
                audit.emit("detection.alert", outcome="denied", actor_name=actor,
                           resource=resource_key,
                           detail={"rule": "first_seen_ip", "username": actor, "new_ip": ip,
                                   "known_ips": sorted(prior_ips)})
                alerts.append(resource_key)
        prior_ips.add(ip)
    return alerts


def _check_mfa_disable_then_change():
    disables = audit.query_core(event_type="auth.mfa.disabled", limit=500)
    alerts = []
    for d in disables:
        resource_key = f"detection:mfa_disable_then_change:{d['seq']}"
        if _already_alerted(resource_key, "0000-01-01T00:00:00.000000Z"):
            continue
        window_end = (datetime.datetime.strptime(d["ts"], "%Y-%m-%dT%H:%M:%S.%fZ")
                      + datetime.timedelta(minutes=MFA_DISABLE_ALERT_WINDOW_MIN)
                      ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        # Scan events after the disable, same actor, until the window closes.
        after = audit.query_core(since_ts=d["ts"], limit=2000)
        for ev in after:
            if ev["seq"] <= d["seq"] or ev["ts"] > window_end:
                continue
            if ev["event_type"] in _SENSITIVE_CHANGE_TYPES and ev["actor_name"] == d["actor_name"]:
                audit.emit("detection.alert", outcome="denied", actor_name=d["actor_name"],
                           resource=resource_key,
                           detail={"rule": "mfa_disable_then_change", "username": d["actor_name"],
                                   "mfa_disabled_at": d["ts"], "followed_by": ev["event_type"],
                                   "followed_at": ev["ts"], "minutes_after":
                                       round((datetime.datetime.strptime(ev["ts"], "%Y-%m-%dT%H:%M:%S.%fZ")
                                             - datetime.datetime.strptime(d["ts"], "%Y-%m-%dT%H:%M:%S.%fZ"))
                                             .total_seconds() / 60, 1)})
                alerts.append(resource_key)
                break
    return alerts


def run_detections():
    """Runs all rules once. Returns {"alerts_fired": [...resource_keys...], "rules_checked": n}.
    Never raises -- each rule is individually try/excepted so one broken rule can't block the
    others (mirrors audit.emit's own fail-safe contract)."""
    fired = []
    for fn in (_check_failed_login_burst, _check_first_seen_ip, _check_mfa_disable_then_change):
        try:
            fired.extend(fn())
        except Exception as e:
            print(f"[detections] rule {fn.__name__} failed: {e}", flush=True)
    return {"alerts_fired": fired, "rules_checked": len(RULES)}


def start_detections_thread(interval_s=None):
    """Background daemon thread running run_detections() on a fixed interval. Mirrors
    audit.start_retention_thread()/tls.start_renewal_thread()'s pattern. Called once at app
    startup (app.py)."""
    interval = int(interval_s or DETECTIONS_INTERVAL_S)

    def _loop():
        while True:
            try:
                run_detections()
            except Exception as e:
                print(f"[detections] thread error: {e}", flush=True)
            import time as _time
            _time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True, name="detections")
    t.start()
    return t


if __name__ == "__main__":
    # Smoke test: `python detections.py` -- seeds a failed-login burst + an MFA-disable-then-
    # change sequence directly into a scratch audit DB, then confirms each rule actually fires.
    import tempfile
    d = tempfile.mkdtemp()
    audit.AUDIT_DB = os.path.join(d, "audit.db")
    audit.AUDIT_JSONL = os.path.join(d, "audit.jsonl")
    audit.init_db()

    for i in range(6):
        audit.emit("auth.login.failure", outcome="failure", actor_name="attacker",
                    src_ip="203.0.113.9", resource="/api/auth/login")
    audit.emit("auth.login.success", actor_name="jason", src_ip="192.168.1.10", resource="/api/auth/login")
    audit.emit("auth.login.success", actor_name="jason", src_ip="198.51.100.4", resource="/api/auth/login")
    audit.emit("auth.mfa.disabled", actor_name="jason", resource="/api/auth/mfa/disable")
    audit.emit("secret.changed", actor_name="jason", resource="/api/secrets", detail={"keys": ["X"]})

    result = run_detections()
    print("run_detections:", result)
    alerts = [e for e in audit.read_recent(limit=50) if e["event_type"] == "detection.alert"]
    for a in alerts:
        print(" -", a["resource"], a["detail"])
    print("\nre-running immediately (should fire ZERO new alerts -- dedup):", run_detections())
