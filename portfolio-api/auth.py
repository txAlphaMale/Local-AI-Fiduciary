# ============================================================================
# auth.py — local, single-database (SQLite) authentication: username/password (argon2) +
# optional TOTP MFA (pyotp, RFC 6238 — works with any standard authenticator app: Google
# Authenticator, Authy, 1Password, Bitwarden, etc.) with single-use hashed backup codes.
#
# 2026-07-23c (Jason) — LOCAL-HOSTING-HARDENING-PLAN.md §2/§3, Phase 1. Replaces the old
# trust model (a single ANALYSIS_TOKEN checked on only 23 of 127 endpoints, and even that
# token handed to the client by the page it's meant to protect — see app.py's index()) with
# a real session gate covering the whole app. No external identity provider, no
# multi-tenancy — one household, at most a couple of accounts, same single-user assumption
# the rest of the app already makes everywhere else. Deliberately NOT doing forced password
# rotation (current NIST SP 800-63B guidance recommends against it).
#
# Storage note: this intentionally uses its OWN small SQLite file (AUTH_DB) rather than
# waiting on the full Storage Stage A migration (moving all ~45 JSON config files into one
# database) — Jason asked to start with auth+hardening specifically, and auth only needs
# ONE new table, not the full migration. Stage A can absorb this file later (or not; a
# dedicated credentials database is also a reasonable place to leave it permanently, since
# keeping auth data physically separate from application config is itself a minor hardening
# property, not just a scheduling convenience).
# ============================================================================
import os
import sqlite3
import time
import uuid
import json
import base64
import io

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHash
import pyotp
import qrcode

AUTH_DB = os.environ.get("AUTH_DB", "/data/auth.db")
SESSION_MAX_AGE_DAYS = int(os.environ.get("SESSION_MAX_AGE_DAYS", "30"))
LOGIN_RATE_LIMIT_PER_MIN = int(os.environ.get("LOGIN_RATE_LIMIT_PER_MIN", "8"))
MFA_ISSUER = os.environ.get("MFA_ISSUER", "Fiduciary")

_ph = PasswordHasher()


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def _conn():
    os.makedirs(os.path.dirname(AUTH_DB) or ".", exist_ok=True)
    c = sqlite3.connect(AUTH_DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            totp_secret TEXT,
            totp_enabled INTEGER NOT NULL DEFAULT 0,
            backup_codes_json TEXT,
            created INTEGER NOT NULL,
            updated INTEGER NOT NULL
        )""")
    try:
        os.chmod(AUTH_DB, 0o600)
    except Exception:
        pass


def has_any_user():
    with _conn() as c:
        return c.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def user_count():
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    return row["n"] if row else 0


def _row_to_dict(row):
    return dict(row) if row else None


def get_user_by_id(uid):
    with _conn() as c:
        return _row_to_dict(c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())


def get_user_by_username(username):
    username = (username or "").strip()
    with _conn() as c:
        return _row_to_dict(c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone())


def public_user(user):
    """Never includes password_hash, totp_secret, or backup_codes_json — those never leave
    this module. Mirrors the write-only convention the /api/secrets endpoints already use.
    `backup_codes_remaining` (2026-07-24) is a count derived from the already-loaded row, not a
    second query — surfaced in SETTINGS -> SECURITY so a user gets a warning before they're down
    to their last backup code, instead of discovering it locked out (RESUME-TRACKER.md
    2026-07-24 gap: mfa_backup_codes_remaining() existed but nothing called it)."""
    if not user:
        return None
    backup_remaining = None
    if user.get("totp_enabled"):
        try:
            backup_remaining = len(json.loads(user.get("backup_codes_json") or "[]"))
        except Exception:
            backup_remaining = None
    return {"id": user["id"], "username": user["username"], "mfa_enabled": bool(user["totp_enabled"]),
            "backup_codes_remaining": backup_remaining, "created": user["created"]}


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def create_user(username, password):
    username = (username or "").strip()
    if not username:
        raise ValueError("username required")
    if not password or len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    uid = uuid.uuid4().hex[:16]
    now = int(time.time())
    ph = _ph.hash(password)
    with _conn() as c:
        c.execute("INSERT INTO users(id, username, password_hash, totp_enabled, created, updated) "
                  "VALUES(?,?,?,0,?,?)", (uid, username, ph, now, now))
    return uid


def verify_password(user, password):
    if not user or not password:
        return False
    try:
        _ph.verify(user["password_hash"], password)
        return True
    except (VerifyMismatchError, InvalidHash):
        return False
    except Exception:
        return False


def set_password(uid, new_password):
    if not new_password or len(new_password) < 8:
        raise ValueError("password must be at least 8 characters")
    ph = _ph.hash(new_password)
    with _conn() as c:
        c.execute("UPDATE users SET password_hash=?, updated=? WHERE id=?", (ph, int(time.time()), uid))


def set_username(uid, new_username):
    """Rename the account (2026-07-24 backlog — Jason's "change the name of the single user
    account" case). Uniqueness check is defensive, not load-bearing today: create_user() refuses
    a 2nd account entirely, so there is currently never a second row to collide with — but this
    keeps the function correct if that single-account restriction is ever relaxed later (the
    hardening plan already floats "a 2nd household member is a small manual addition later")."""
    new_username = (new_username or "").strip()
    if not new_username:
        raise ValueError("username required")
    if len(new_username) > 64:
        raise ValueError("username must be 64 characters or fewer")
    with _conn() as c:
        clash = c.execute("SELECT id FROM users WHERE username=? AND id<>?", (new_username, uid)).fetchone()
        if clash:
            raise ValueError("that username is already in use")
        c.execute("UPDATE users SET username=?, updated=? WHERE id=?", (new_username, int(time.time()), uid))


def delete_user(uid):
    """Deletes the account row entirely — auth.has_any_user() goes back to False if this was the
    only account, so the app returns to the first-run "CREATE YOUR ACCOUNT" screen on next load.
    2026-07-24 (Jason): an AUTHENTICATED, in-app reset of auth.db specifically (e.g. to re-test
    onboarding, or rename the account by recreating it) — deliberately scoped to ONLY this table.
    Distinct from the still-unbuilt Account Recovery Tier 2 (`--wipe-all`), which is an
    UNAUTHENTICATED host-CLI fallback gated on a separate offline recovery key and is far more
    destructive (also clears store.db/chat.db/the knowledge index). The caller (app.py) is
    responsible for clearing request.session after this returns."""
    with _conn() as c:
        c.execute("DELETE FROM users WHERE id=?", (uid,))


# ---------------------------------------------------------------------------
# TOTP MFA — provision (generate, not yet active) -> verify (activates + issues backup codes)
# ---------------------------------------------------------------------------
def mfa_provision(uid):
    """Generates a NEW TOTP secret and stores it un-enabled (a half-finished setup never
    grants MFA-bypass — mfa_check_code() only trusts totp_enabled=1 rows). Returns
    (secret, otpauth_uri, qr_data_uri) for the setup screen to render."""
    user = get_user_by_id(uid)
    if not user:
        raise ValueError("no such user")
    secret = pyotp.random_base32()
    with _conn() as c:
        c.execute("UPDATE users SET totp_secret=?, totp_enabled=0, backup_codes_json=NULL, updated=? WHERE id=?",
                  (secret, int(time.time()), uid))
    uri = pyotp.totp.TOTP(secret).provisioning_uri(name=user["username"], issuer_name=MFA_ISSUER)
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    return secret, uri, qr_data_uri


def mfa_verify_and_enable(uid, code):
    """Confirms the user's authenticator app is actually producing valid codes for the
    provisioned secret, THEN flips totp_enabled on and issues one-time backup codes (shown to
    the user exactly once by the caller — never retrievable again, same write-only pattern as
    the password hash). Returns (ok, backup_codes_or_None)."""
    user = get_user_by_id(uid)
    if not user or not user.get("totp_secret"):
        return False, None
    totp = pyotp.TOTP(user["totp_secret"])
    if not totp.verify(str(code or "").strip(), valid_window=1):
        return False, None
    codes = [uuid.uuid4().hex[:10] for _ in range(10)]
    hashed = [_ph.hash(c) for c in codes]
    with _conn() as c:
        c.execute("UPDATE users SET totp_enabled=1, backup_codes_json=?, updated=? WHERE id=?",
                  (json.dumps(hashed), int(time.time()), uid))
    return True, codes


def mfa_check_code(uid, code):
    """True if MFA isn't enabled (nothing to check) OR `code` is a valid current TOTP code OR
    a valid, still-unused backup code (which is consumed on match)."""
    user = get_user_by_id(uid)
    if not user or not user.get("totp_enabled"):
        return True
    code = str(code or "").strip()
    if not code:
        return False
    if user.get("totp_secret") and pyotp.TOTP(user["totp_secret"]).verify(code, valid_window=1):
        return True
    try:
        hashed = json.loads(user.get("backup_codes_json") or "[]")
    except Exception:
        hashed = []
    for i, h in enumerate(hashed):
        try:
            _ph.verify(h, code)
        except Exception:
            continue
        hashed.pop(i)   # single-use — consume it
        with _conn() as c:
            c.execute("UPDATE users SET backup_codes_json=? WHERE id=?", (json.dumps(hashed), uid))
        return True
    return False


def mfa_disable(uid):
    with _conn() as c:
        c.execute("UPDATE users SET totp_enabled=0, totp_secret=NULL, backup_codes_json=NULL, updated=? "
                  "WHERE id=?", (int(time.time()), uid))


def mfa_backup_codes_remaining(uid):
    user = get_user_by_id(uid)
    if not user or not user.get("totp_enabled"):
        return 0
    try:
        return len(json.loads(user.get("backup_codes_json") or "[]"))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Login rate limiting — simple in-process sliding window. Deliberately not backed by a
# shared store (Redis etc.): this is a single-instance local app, so in-memory state is fine
# and resets harmlessly on restart.
# ---------------------------------------------------------------------------
_login_attempts = {}   # ip -> [unix ts, ...]


def rate_limited(ip):
    now = time.time()
    hist = [t for t in _login_attempts.get(ip, []) if now - t < 60]
    _login_attempts[ip] = hist
    return len(hist) >= LOGIN_RATE_LIMIT_PER_MIN


def record_login_attempt(ip):
    _login_attempts.setdefault(ip, []).append(time.time())
