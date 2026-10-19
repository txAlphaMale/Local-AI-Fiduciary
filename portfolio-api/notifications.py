# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
notifications.py -- outbound awareness layer (2026-07-26).

PROBLEM THIS SOLVES: compliance/IR-RUNBOOK.md documented, correctly, that this app has no outbound
paging/email/push integration -- awareness of a security event (or a portfolio-data-source outage)
depended entirely on someone opening SETTINGS -> SECURITY -> SECURITY AUDIT LOG. This module closes
that gap without giving up the app's self-hosted, no-third-party-account posture:

  - Web Push uses the open VAPID standard (RFC 8291/8292) via `pywebpush` -- no OneSignal/Firebase/
    AWS SNS account. The VAPID keypair is generated once and persisted locally, same pattern as
    audit.py's own HMAC key (_load_or_create_hmac_key) or secrets_crypto's Fernet key.
  - Email is a generic SMTP client (stdlib smtplib+email) -- works with a Gmail account (App
    Password), any other provider, or nothing if left unconfigured. No AWS SES / third-party email
    API dependency, so there's no recurring cloud cost tied to notifications specifically.

TRIGGER: audit.emit() calls notify_from_event() as its last step, best-effort (see the try/except
around that call site in audit.py -- a notify failure can never fail or delay the audit write
itself). Actual delivery (SMTP handshake, HTTPS POST to a push service) runs on a background daemon
thread so a slow/unreachable mail server or push endpoint never adds latency to the request that
triggered the underlying event.

GATING ("don't notify me about an integration I never set up"): notify_from_event() only ever
classifies an event into a notify-worthy category for a curated allowlist of event types (see
_classify) -- routine config.changed/secret.read noise never reaches here at all. The
"integration_outage" category specifically only ever fires from record_integration_result(), which
is itself only called (from app.py's build()) for the ACTIVE PORTFOLIO_PROVIDER -- a household that
runs PORTFOLIO_PROVIDER=csv (no brokerage integration at all) or has never touched SnapTrade never
generates a SnapTrade outage event in the first place, so there's nothing to gate client-side.

SCOPE, stated plainly: email has ONE shared destination address per installation (SMTP_TO_ADDRESS),
controlled by the admin account's own preferences -- this is a household app with one mailbox in
mind, not a multi-tenant product with per-user email delivery. Push, by contrast, is genuinely
per-user AND per-device (a household member's phone and laptop are two separate subscriptions),
since that's a real per-browser browser API, not a shared setting.
"""
import os
import json
import time
import threading
import datetime

import store
import secrets_crypto
import audit
import auth

# ---------------------------------------------------------------------------
# Storage (both files ride store.write_json/read_json_strict -- encrypted at rest automatically,
# same as every other JSON file this app persists since the 2026-07-24 encryption-at-rest sweep).
# ---------------------------------------------------------------------------
NOTIFY_SUBS_FILE = os.environ.get("NOTIFY_SUBS_FILE", "/data/notify_subs.json")   # {uid: [subscription, ...]}
NOTIFY_PREFS_FILE = os.environ.get("NOTIFY_PREFS_FILE", "/data/notify_prefs.json")  # {uid: {push,email,categories}}
VAPID_KEY_FILE = os.environ.get("VAPID_KEY_FILE", "/data/vapid_private_key.pem")
SECRETS_FILE = os.environ.get("SECRETS_FILE", "/data/secrets.json")  # same file app.py's SECRET_SPECS write to

CATEGORIES = {
    "security": "Security alerts (logins, MFA/account changes, admin actions, detections)",
    "integration_outage": "Portfolio data source outages (SnapTrade / Monarch / market data)",
}

INTEGRATION_OUTAGE_THRESHOLD = int(os.environ.get("INTEGRATION_OUTAGE_THRESHOLD", "3"))
INTEGRATION_OUTAGE_WINDOW_MIN = int(os.environ.get("INTEGRATION_OUTAGE_WINDOW_MIN", "15"))

_streak_lock = threading.Lock()
_integration_failure_streak = {}  # provider -> consecutive failure count (in-memory; resets on
                                   # restart -- worst case is one extra alert right after a
                                   # restart, never a missed one, so this is safe to keep simple)


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _minutes_ago_iso(minutes):
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _read_json(path, default=None):
    try:
        return store.read_json_strict(path)
    except Exception:
        return default if default is not None else {}


def _read_subs():
    return _read_json(NOTIFY_SUBS_FILE, {})


def _write_subs(d):
    store.write_json(NOTIFY_SUBS_FILE, d)


def _read_prefs():
    return _read_json(NOTIFY_PREFS_FILE, {})


def _write_prefs(d):
    store.write_json(NOTIFY_PREFS_FILE, d)


def default_prefs():
    """Opt-out model: once a user subscribes a device to push, every category notifies via push
    by default. Email defaults OFF even for the admin until SMTP is actually configured AND
    explicitly enabled -- silence is the safe default for a channel that isn't set up yet."""
    return {"push": True, "email": False,
            "categories": {k: True for k in CATEGORIES}}


def get_prefs(uid):
    p = _read_prefs().get(uid)
    if not p:
        return default_prefs()
    merged = default_prefs()
    merged.update({k: v for k, v in p.items() if k != "categories"})
    merged["categories"].update(p.get("categories") or {})
    return merged


def set_prefs(uid, prefs):
    all_prefs = _read_prefs()
    all_prefs[uid] = {
        "push": bool(prefs.get("push", True)),
        "email": bool(prefs.get("email", False)),
        "categories": {k: bool((prefs.get("categories") or {}).get(k, True)) for k in CATEGORIES},
    }
    _write_prefs(all_prefs)
    return all_prefs[uid]


# ---------------------------------------------------------------------------
# Push subscription management
# ---------------------------------------------------------------------------
def add_subscription(uid, subscription):
    """`subscription` is the raw PushSubscription.toJSON() object from the browser
    (endpoint/keys.p256dh/keys.auth). De-duped on `endpoint` (re-subscribing the same
    browser/device replaces its old entry rather than accumulating dead duplicates)."""
    subs = _read_subs()
    user_subs = [s for s in subs.get(uid, []) if s.get("endpoint") != subscription.get("endpoint")]
    subscription = dict(subscription)
    subscription["added_ts"] = _now_iso()
    user_subs.append(subscription)
    subs[uid] = user_subs
    _write_subs(subs)
    return len(user_subs)


def remove_subscription(uid, endpoint):
    subs = _read_subs()
    user_subs = [s for s in subs.get(uid, []) if s.get("endpoint") != endpoint]
    subs[uid] = user_subs
    _write_subs(subs)


def subscription_count(uid):
    return len(_read_subs().get(uid, []))


# ---------------------------------------------------------------------------
# VAPID keys -- generated once, persisted like audit.py's HMAC key / secrets_crypto's Fernet key.
# ---------------------------------------------------------------------------
def vapid_public_key_b64():
    """Returns the base64url-encoded, uncompressed EC point (the exact format the browser's
    `PushManager.subscribe({applicationServerKey: ...})` expects). Generates+persists the keypair
    on first call if it doesn't exist yet (py_vapid.Vapid.from_file does this itself)."""
    import base64
    from py_vapid import Vapid
    from cryptography.hazmat.primitives import serialization
    os.makedirs(os.path.dirname(VAPID_KEY_FILE) or ".", exist_ok=True)
    v = Vapid.from_file(VAPID_KEY_FILE)
    try:
        os.chmod(VAPID_KEY_FILE, 0o600)
    except Exception:
        pass
    pub_bytes = v.private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return base64.urlsafe_b64encode(pub_bytes).rstrip(b"=").decode("ascii")


def _vapid_claim_email():
    # CONFIG_SPECS-backed (app.py), read the same way app.py's own _knob()/settings reads work --
    # duplicated here (rather than importing app.py, which would be circular) as a plain env>
    # settings-file>default lookup.
    ev = os.environ.get("NOTIFY_VAPID_CONTACT_EMAIL")
    if ev:
        return ev
    try:
        settings = _read_json(os.environ.get("SETTINGS_FILE", "/data/settings.json"), {})
        v = (settings or {}).get("NOTIFY_VAPID_CONTACT_EMAIL")
        if v:
            return v
    except Exception:
        pass
    return "admin@localhost"  # VAPID requires *a* contact -- doesn't need to be deliverable


# ---------------------------------------------------------------------------
# SMTP (generic -- Gmail App Password, any other provider, or unset)
# ---------------------------------------------------------------------------
def _read_app_secrets():
    """Reads the SAME SECRETS_FILE app.py's SECRET_SPECS/_read_secrets()/_write_secrets() use, so
    whatever's saved via SETTINGS -> SECRETS & KEYS (SMTP_* keys) is visible here too. Duplicated
    read logic (rather than importing app.py, which imports this module -- would be circular)."""
    raw = _read_json(SECRETS_FILE, {})
    out = {}
    for k, v in (raw or {}).items():
        try:
            out[k] = secrets_crypto.decrypt_or_legacy(v)
        except Exception:
            out[k] = v
    for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
              "SMTP_FROM_ADDRESS", "SMTP_TO_ADDRESS", "SMTP_USE_TLS"):
        ev = os.environ.get(k)
        if ev:
            out[k] = ev  # env always wins, same precedence as every other secret in this app
    return out


def smtp_configured():
    s = _read_app_secrets()
    return bool(s.get("SMTP_HOST") and s.get("SMTP_USERNAME") and s.get("SMTP_PASSWORD")
                and s.get("SMTP_TO_ADDRESS"))


def send_email_checked(subject, body):
    """(ok, error). Like _send_email but says WHY it failed, in words safe to show an admin (never
    echoes the password). Used by the SMTP test button; _send_email() wraps it for normal alerts."""
    s = _read_app_secrets()
    host = s.get("SMTP_HOST")
    user = s.get("SMTP_USERNAME")
    pw = s.get("SMTP_PASSWORD")
    to_addr = s.get("SMTP_TO_ADDRESS")
    missing = [n for n, v in (("SMTP server", host), ("SMTP username", user), ("SMTP password", pw),
                              ("Send-alerts-to address", to_addr)) if not v]
    if missing:
        return False, "Not configured yet — missing: " + ", ".join(missing) + ". Save them first."
    try:
        port = int(s.get("SMTP_PORT") or "587")
    except ValueError:
        return False, "SMTP port must be a number (587 for Gmail)."
    from_addr = s.get("SMTP_FROM_ADDRESS") or user
    use_tls = (s.get("SMTP_USE_TLS") or "1") != "0"
    import smtplib
    import socket
    from email.mime.text import MIMEText
    msg = MIMEText(body)
    msg["Subject"] = f"[Fiduciary] {subject}"
    msg["From"] = from_addr
    msg["To"] = to_addr
    try:
        with smtplib.SMTP(host, port, timeout=15) as server:
            if use_tls:
                server.starttls()
            server.login(user, pw)
            server.sendmail(from_addr, [to_addr], msg.as_string())
        return True, None
    except smtplib.SMTPAuthenticationError as e:
        hint = (" For Gmail this means the username or App Password is wrong (use a 16-character App Password, "
                "not your login password; 2-Step Verification must be on).") if "gmail" in (host or "").lower() else ""
        return False, f"The mail server rejected the username/password ({e.smtp_code}).{hint}"
    except smtplib.SMTPRecipientsRefused:
        return False, "The server refused the 'send alerts to' address — check it for typos."
    except smtplib.SMTPSenderRefused:
        return False, "The server refused the From address — leave it blank to use the username."
    except smtplib.SMTPNotSupportedError as e:
        return False, f"The server doesn't support what was asked ({e}). Try the STARTTLS setting 1 on port 587."
    except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, ConnectionError) as e:
        return False, f"Could not hold a connection to {host}:{port} ({type(e).__name__}). Check the server, port and STARTTLS setting."
    except socket.gaierror:
        return False, f"Could not find the server '{host}' — check the spelling (Gmail: smtp.gmail.com)."
    except (socket.timeout, TimeoutError):
        return False, f"Timed out reaching {host}:{port} — a firewall or wrong port is likely."
    except Exception as e:
        return False, f"Send failed: {type(e).__name__}: {str(e)[:160]}"


def _send_email(subject, body):
    ok, err = send_email_checked(subject, body)
    if not ok and err and not err.startswith("Not configured"):
        print(f"[notifications] email send failed: {err}", flush=True)
    return ok


def _send_push(subscription, title, body):
    from pywebpush import webpush, WebPushException
    try:
        webpush(
            subscription_info=subscription,
            data=json.dumps({"title": title, "body": body}),
            vapid_private_key=VAPID_KEY_FILE,
            vapid_claims={"sub": f"mailto:{_vapid_claim_email()}"},
        )
        return True
    except WebPushException as e:
        # Expected/benign: 404/410 means the browser dropped this subscription (uninstalled,
        # permissions revoked, storage cleared) -- caller removes it so it stops being retried.
        print(f"[notifications] push failed (subscription likely stale): {e}", flush=True)
        return False
    except Exception as e:
        print(f"[notifications] push failed (unexpected): {e}", flush=True)
        return False


# ---------------------------------------------------------------------------
# Integration outage detection -- fed by app.py's build() dispatcher, feeds back into audit.emit()
# (which is what actually triggers delivery, via the hook at the bottom of this file).
# ---------------------------------------------------------------------------
def record_integration_result(provider, ok, error=None):
    """Call after every attempt to reach a portfolio data provider. `provider` should be the
    literal PORTFOLIO_PROVIDER value ("snaptrade"/"monarch") -- "csv" is a local file with nothing
    to have an outage, so callers should simply not call this for csv. Fires ONE detection.alert
    (deduped like every other detections.py rule) once INTEGRATION_OUTAGE_THRESHOLD consecutive
    failures land inside INTEGRATION_OUTAGE_WINDOW_MIN -- a single blip never pages anyone."""
    if not provider or provider == "csv":
        return
    with _streak_lock:
        if ok:
            _integration_failure_streak[provider] = 0
            return
        _integration_failure_streak[provider] = _integration_failure_streak.get(provider, 0) + 1
        streak = _integration_failure_streak[provider]
    if streak < INTEGRATION_OUTAGE_THRESHOLD:
        return
    resource_key = f"detection:integration_outage:{provider}"
    try:
        hits = audit.query_core(event_type="detection.alert", resource_prefix=resource_key,
                                 since_ts=_minutes_ago_iso(INTEGRATION_OUTAGE_WINDOW_MIN), limit=1)
        if hits:
            return  # already alerted within the window -- don't re-fire on every single call
    except Exception:
        pass
    audit.emit("detection.alert", outcome="denied", resource=resource_key,
               detail={"rule": "integration_outage", "provider": provider,
                       "consecutive_failures": streak, "last_error": str(error)[:300]})


# ---------------------------------------------------------------------------
# Event classification + delivery (the audit.emit() hook lands here)
# ---------------------------------------------------------------------------
_NOTIFY_EVENT_TYPES = {
    "auth.account.reset", "auth.user.created", "auth.user.deleted", "auth.user.role_changed",
    "auth.recovery.used", "auth.recovery.reset", "auth.mfa.disabled",
}


def _classify(event_type, detail):
    if event_type == "detection.alert":
        rule = (detail or {}).get("rule")
        return "integration_outage" if rule == "integration_outage" else "security"
    if event_type in _NOTIFY_EVENT_TYPES:
        return "security"
    return None


def _format_message(category, event_type, outcome, actor_name, resource, detail):
    detail = detail or {}
    if category == "integration_outage":
        provider = detail.get("provider", "a portfolio provider")
        title = f"{provider} connection is down"
        body = (f"{detail.get('consecutive_failures', '?')} consecutive failures reaching "
                f"{provider}. Last error: {str(detail.get('last_error') or '')[:180]}")
        return title, body
    rule = detail.get("rule")
    if event_type == "detection.alert":
        labels = {
            "failed_login_burst": "Repeated failed logins detected",
            "first_seen_ip": "Login from a new location",
            "mfa_disable_then_change": "MFA disabled, then a sensitive change — possible account takeover",
            "emergency_request_pending_no_admin_activity": "Emergency access request pending and you haven't signed in",
            "emergency_bulk_download": "Many emergency documents downloaded in a short time",
        }
        title = labels.get(rule, f"Security detection: {rule or event_type}")
        body = f"Account: {detail.get('username') or actor_name or 'unknown'}."
        return title, body
    titles = {
        "auth.account.reset": "An account was deleted",
        "auth.user.created": "A new household account was created",
        "auth.user.deleted": "A household account was removed",
        "auth.user.role_changed": "The admin role was transferred",
        "auth.recovery.used": "Account recovery was used",
        "auth.recovery.reset": "A password was reset via account recovery",
        "auth.mfa.disabled": "Two-factor authentication was disabled",
    }
    title = titles.get(event_type, event_type)
    body = f"Actor: {actor_name or 'unknown'}. Outcome: {outcome}."
    return title, body


def notify_from_event(event_type, outcome, actor_name, resource, detail, ts):
    category = _classify(event_type, detail)
    if not category:
        return
    t = threading.Thread(target=_dispatch, args=(category, event_type, outcome, actor_name, resource, detail),
                          daemon=True, name="notify-dispatch")
    t.start()


def _admin_uid():
    try:
        for u in auth.list_users():
            if (u.get("role") or "admin") == "admin":
                return u["id"]
    except Exception:
        pass
    return None


def _dispatch(category, event_type, outcome, actor_name, resource, detail):
    try:
        title, body = _format_message(category, event_type, outcome, actor_name, resource, detail)
        subs = _read_subs()
        prefs_all = _read_prefs()
        changed = False
        for uid, user_subs in list(subs.items()):
            prefs = prefs_all.get(uid) or default_prefs()
            if not prefs.get("push") or not prefs.get("categories", {}).get(category, True):
                continue
            surviving = []
            for sub in user_subs:
                if _send_push(sub, title, body):
                    surviving.append(sub)
                else:
                    changed = True
            subs[uid] = surviving
        if changed:
            _write_subs(subs)

        admin_uid = _admin_uid()
        admin_prefs = (prefs_all.get(admin_uid) if admin_uid else None) or default_prefs()
        if (admin_prefs.get("email") and admin_prefs.get("categories", {}).get(category, True)
                and smtp_configured()):
            _send_email(title, body)
    except Exception as e:
        print(f"[notifications] dispatch failed: {e}", flush=True)


def send_test(uid, is_admin):
    """Fires a synthetic notification through whichever channels the caller has enabled, without
    waiting on a real event -- SETTINGS -> NOTIFICATIONS "SEND TEST" button."""
    results = {"push_attempted": 0, "push_sent": 0, "email_sent": False}
    prefs = get_prefs(uid)
    if prefs.get("push"):
        subs = _read_subs().get(uid, [])
        results["push_attempted"] = len(subs)
        for sub in subs:
            if _send_push(sub, "Fiduciary test notification", "If you can see this, push is working."):
                results["push_sent"] += 1
    if is_admin and prefs.get("email") and smtp_configured():
        results["email_sent"] = _send_email("Test notification",
                                             "If you received this, email notifications are working.")
    return results


# ---------------------------------------------------------------------------
# TASK-002: the always-on 'emergency' category. Deliberately NOT in CATEGORIES (so it can't be
# muted from SETTINGS -> NOTIFICATIONS) and NOT routed through _classify/_dispatch (which fan out
# to every subscriber and honor prefs). Delivered to EVERY push subscription of the admin, plus the
# shared SMTP address, regardless of any preference -- if the admin can't be reached, the time delay
# protects nothing. `to_uid` (the designee) additionally gets a push acknowledgement of their own
# request's progress; `extra_uids` lets a takeover notify the OUTGOING admin too.
# ---------------------------------------------------------------------------
def _fmt_when(ts):
    try:
        import datetime as _dt
        return _dt.datetime.fromtimestamp(float(ts)).strftime("%a %b %d %Y %H:%M")
    except Exception:
        return "the scheduled time"


def _format_emergency(event, d):
    who = d.get("designee") or "the designee"
    t = {
        "emergency.requested": (f"Emergency access requested by {who} — it will be granted on "
                                f"{_fmt_when(d.get('grants_at'))} unless you deny it",
                                "Open SETTINGS → IAM → EMERGENCY ACCESS and press DENY if this is not expected."),
        "emergency.reminder": (f"Emergency access request from {who} is {d.get('percent', '?')}% of the way to "
                               f"being granted ({_fmt_when(d.get('grants_at'))})",
                               "Open SETTINGS → IAM → EMERGENCY ACCESS to deny it, or approve it early."),
        "emergency.denied": (f"Emergency access request from {who} was DENIED", "No access was granted."),
        "emergency.granted": (f"Emergency access GRANTED to {who} ({d.get('grant_type', 'view')})",
                              d.get("summary") or "They can now open the emergency-visibility vault documents."),
        "emergency.revoked": (f"Emergency access for {who} was REVOKED", "Their access ended immediately."),
        "emergency.expired": (f"Emergency view access for {who} expired", "They can request again if needed."),
        "emergency.cancelled": (f"Emergency access request from {who} was cancelled", "No access was granted."),
        "emergency.config_changed": ("Emergency-access settings changed",
                                     d.get("summary") or "Review SETTINGS → IAM → EMERGENCY ACCESS."),
        "emergency.auto_disabled": ("Emergency access was automatically DISABLED",
                                    f"Reason: {d.get('reason', 'the designee is no longer valid')}. "
                                    "Review SETTINGS → IAM → EMERGENCY ACCESS."),
    }
    return t.get(event, (event, json.dumps({k: v for k, v in d.items() if k != "passphrase"})[:200]))


def dispatch_emergency(event, detail, to_uid=None, extra_uids=None):
    """Synchronous (so state-machine tests are deterministic). Returns
    {"push_sent": n, "email_sent": bool}. Never raises."""
    out = {"push_sent": 0, "email_sent": False}
    try:
        title, body = _format_emergency(event, detail or {})
        subs = _read_subs()
        targets = set(extra_uids or [])
        admin_uid = _admin_uid()
        if admin_uid:
            targets.add(admin_uid)
        for uid in targets:                              # prefs are IGNORED on purpose
            for sub in subs.get(uid, []):
                if _send_push(sub, title, body):
                    out["push_sent"] += 1
        if to_uid and to_uid not in targets:
            for sub in subs.get(to_uid, []):             # acknowledgement to the requester
                _send_push(sub, title, body)
        if smtp_configured():
            out["email_sent"] = bool(_send_email(title, body))
    except Exception as e:
        print(f"[notifications] emergency dispatch failed: {e}", flush=True)
    return out
