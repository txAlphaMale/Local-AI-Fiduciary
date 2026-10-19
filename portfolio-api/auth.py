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
import secrets

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
            recovery_key_hash TEXT,
            created INTEGER NOT NULL,
            updated INTEGER NOT NULL
        )""")
        # Migration for databases created before 2026-07-24 (Account Recovery Tier 2) --
        # CREATE TABLE IF NOT EXISTS does not add columns to an already-existing table, and
        # an existing live deployment already has a users row from before this column existed.
        # has_recovery_key()/verify_recovery_key() below treat a NULL hash as "not set" and the
        # setup checklist (SETUP_FIELDS "recovery_key_set") surfaces that as a critical, unfilled
        # item so a migrated-in account doesn't silently stay without one.
        cols = {row["name"] for row in c.execute("PRAGMA table_info(users)").fetchall()}
        if "recovery_key_hash" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN recovery_key_hash TEXT")
        # SEC-5 (2026-07-25) migration: role-based access ("a 2nd household member is a small
        # manual addition later, not a self-serve flow" -- this IS that later addition). DEFAULT
        # 'admin' means Jason's existing live account backfills as admin with no behavior change
        # (an admin can do everything the old single-role model already could) -- only a NEWLY
        # created 'viewer' account is actually restricted, via _security_and_auth_gate (app.py).
        if "role" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'")
        # TASK-001b: per-user session epoch. A session cookie carries the epoch it was minted
        # under ("ep"); the gate rejects a cookie whose epoch no longer matches, so a password
        # change / MFA disable / forced logout kills every OTHER live session (stolen cookies too).
        if "session_epoch" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN session_epoch INTEGER NOT NULL DEFAULT 0")
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
            "backup_codes_remaining": backup_remaining,
            "recovery_key_set": bool(user.get("recovery_key_hash")), "created": user["created"],
            "role": user.get("role") or "admin"}


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def create_user(username, password, role="admin"):
    """Returns (uid, recovery_key) -- the recovery key (2026-07-24, Account Recovery Tier 2) is
    generated immediately so every NEW account has one from day one; the caller is responsible
    for showing it to the user exactly once (same write-only pattern as MFA backup codes) and
    never logging/persisting the plaintext anywhere. Existing accounts created before this
    feature existed migrate in with recovery_key_hash=NULL (see init_db()) and only get one once
    the user explicitly generates it via set_recovery_key() (SETTINGS -> SECURITY, or the
    "recovery_key_set" critical checklist item nudges them there).

    `role` (SEC-5, 2026-07-25) defaults to 'admin' so the original bootstrap-only call site
    (app.py's /api/auth/first-run, which creates THE ONE original account) needs zero changes.
    Only create_viewer() below passes role='viewer' explicitly -- app.py NEVER accepts a `role`
    value from request bodies, so a client can never self-escalate to admin through this
    function; the two legitimate callers each hardcode their own literal role."""
    username = (username or "").strip()
    if not username:
        raise ValueError("username required")
    if not password or len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    if role not in ("admin", "viewer"):
        raise ValueError("invalid role")
    uid = uuid.uuid4().hex[:16]
    now = int(time.time())
    ph = _ph.hash(password)
    with _conn() as c:
        c.execute("INSERT INTO users(id, username, password_hash, totp_enabled, role, created, updated) "
                  "VALUES(?,?,?,0,?,?,?)", (uid, username, ph, role, now, now))
    recovery_key = set_recovery_key(uid)
    return uid, recovery_key


def create_viewer(username, password):
    """SEC-5 (2026-07-25) -- creates an additional, restricted 'viewer' account (a second
    household member who should see the dashboards but not change config/secrets/security
    settings). Thin, self-documenting wrapper around create_user(role='viewer') -- this is the
    ONLY place in the codebase that ever passes role='viewer', so a reviewer can grep this one
    name to find every path that can create a non-admin account. Callable only by an already-
    authenticated admin (enforced in app.py's POST /api/auth/users, not here -- this module has
    no concept of "who is calling," same as every other function in this file)."""
    return create_user(username, password, role="viewer")


def is_admin(user):
    """True if `user` (a row/dict from get_user_by_id/get_user_by_username) has the admin role.
    Existing rows migrated in via init_db()'s ALTER TABLE ... DEFAULT 'admin' are admins --
    nothing that could act as an admin today loses that ability when this column is introduced."""
    return bool(user) and (user.get("role") or "admin") == "admin"


def list_users():
    """All accounts for the admin-only 'HOUSEHOLD ACCOUNTS' management panel -- id/username/role/
    created/mfa_enabled only, never password_hash/totp_secret/backup_codes_json/recovery_key_hash
    (same write-only guarantee as public_user())."""
    with _conn() as c:
        rows = c.execute("SELECT * FROM users ORDER BY created ASC").fetchall()
    return [public_user(_row_to_dict(r)) for r in rows]


def admin_delete_viewer(uid):
    """Deletes a 'viewer' account -- and ONLY a viewer account; raises ValueError if the target
    is 'admin' (there is exactly one admin, the original account owner, and this function is not
    the path to remove them). This is deliberately a DIFFERENT function from the existing
    delete_user() (which /api/auth/reset-account already uses for an authenticated user deleting
    their OWN account, any role, and is unaffected by this change) -- keeping the "delete someone
    ELSE's account" capability structurally incapable of ever targeting the admin removes the
    need for a separate "don't delete the last admin" runtime check entirely: there is only ever
    one admin, and this function's own role check is the guard."""
    user = get_user_by_id(uid)
    if not user:
        raise ValueError("no such account")
    if is_admin(user):
        raise ValueError("cannot delete the admin account this way")
    delete_user(uid)


def transfer_admin_role(current_admin_uid, new_admin_uid):
    """SEC-5 UX gap fix (2026-07-26): swaps the admin role between the current admin and one
    existing viewer, IN PLACE -- same two account rows, same ids, same audit-log actor history.
    Replaces the previous only-option workaround (delete the viewer, recreate them as admin, which
    orphaned their audit history under a dead id and forced a brand-new recovery key for what was
    conceptually the same household member). Atomic single-connection update: there is exactly one
    admin before this call and exactly one admin after it, never zero or two along the way."""
    if current_admin_uid == new_admin_uid:
        raise ValueError("cannot transfer the admin role to yourself")
    admin = get_user_by_id(current_admin_uid)
    target = get_user_by_id(new_admin_uid)
    if not admin or not is_admin(admin):
        raise ValueError("caller is not the current admin")
    if not target:
        raise ValueError("no such account")
    if is_admin(target):
        raise ValueError("target is already admin")
    now = int(time.time())
    with _conn() as c:
        c.execute("UPDATE users SET role='viewer', updated=? WHERE id=?", (now, current_admin_uid))
        c.execute("UPDATE users SET role='admin', updated=? WHERE id=?", (now, new_admin_uid))
    return True


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
        c.execute("UPDATE users SET password_hash=?, updated=?, session_epoch=session_epoch+1 WHERE id=?",
                  (ph, int(time.time()), uid))


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


def session_epoch(uid):
    """Current session epoch for `uid` (0 if unknown). Stamp it into a session at login."""
    user = get_user_by_id(uid)
    return int((user or {}).get("session_epoch") or 0)


def bump_session_epoch(uid):
    """'Sign out everywhere': invalidates every session minted under the old epoch. Returns the new
    epoch. The caller re-stamps its own session if it should stay signed in."""
    with _conn() as c:
        c.execute("UPDATE users SET session_epoch=session_epoch+1, updated=? WHERE id=?",
                  (int(time.time()), uid))
    return session_epoch(uid)


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
# Account Recovery Tier 2 (2026-07-24) — host-CLI recovery key. Tier 1 (recovery/start +
# recovery/reset in app.py) only helps if MFA is still working; this exists for the strictly
# worse case Jason flagged directly: MFA was never enabled (or the device AND backup codes are
# both gone) AND the password is forgotten. Without this, the only "recovery" path left was
# `docker exec` + `rm /data/auth.db` -- host/container access alone silently granting full access
# to the whole financial picture with zero second factor and zero trace. This closes that: the
# host-CLI reset below (`--reset-credentials`) additionally requires a high-entropy secret that
# only ever existed in the user's own hands (shown once, at generation time, never stored except
# as an argon2 hash -- not even recoverable by whoever runs this CLI). Host/container access alone
# now gets you `--wipe-all` only -- a full destructive reset, not a silent takeover of the
# existing account and its data.
# ---------------------------------------------------------------------------
def _gen_recovery_key():
    """20 random bytes -> 32 base32 characters (uppercase, unpadded) -- same spirit as a TOTP
    secret's manual-entry form: transcribable by hand, ~100 bits of entropy."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def set_recovery_key(uid):
    """(Re)generates the account's recovery key and returns the PLAINTEXT once. Only the argon2
    hash is stored -- same write-only guarantee as the password and MFA backup codes. Called both
    from create_user() (every new account gets one immediately) and from the authenticated
    SETTINGS -> SECURITY "regenerate" endpoint (covers accounts migrated in with
    recovery_key_hash=NULL, and lets a user rotate it if they're ever unsure whether an old copy
    is still secret)."""
    key = _gen_recovery_key()
    h = _ph.hash(key)
    with _conn() as c:
        c.execute("UPDATE users SET recovery_key_hash=?, updated=? WHERE id=?", (h, int(time.time()), uid))
    return key


def has_recovery_key(uid):
    user = get_user_by_id(uid)
    return bool(user and user.get("recovery_key_hash"))


def verify_recovery_key(uid, key):
    user = get_user_by_id(uid)
    if not user or not user.get("recovery_key_hash") or not key:
        return False
    try:
        _ph.verify(user["recovery_key_hash"], key)
        return True
    except (VerifyMismatchError, InvalidHash):
        return False
    except Exception:
        return False


def invalidate_recovery_key(uid):
    """Clears the recovery key after it's used to reset credentials (--reset-credentials below)
    -- a successful recovery-key reset is exactly the moment to treat it as spent, the same way a
    backup code is consumed on use, rather than leaving a working master-override key sitting
    around indefinitely. The user has to explicitly generate a new one afterward."""
    with _conn() as c:
        c.execute("UPDATE users SET recovery_key_hash=NULL, updated=? WHERE id=?", (int(time.time()), uid))


# ---------------------------------------------------------------------------
# TOTP MFA — provision (generate, not yet active) -> verify (activates + issues backup codes)
# ---------------------------------------------------------------------------
def _enc_totp(secret):
    """Encrypts the TOTP secret before it lands in SQLite -- the one field in this whole auth
    system that must stay REVERSIBLE (unlike password_hash/backup_codes_json/recovery_key_hash,
    which are correctly one-way hashed) since verifying a code means recomputing it from the raw
    secret. Reuses the app's single shared Fernet key (secrets_crypto.py) rather than a second
    key; lazy import to avoid import-order issues (2026-07-24, "All sensitive information stored
    needs to be encrypted")."""
    from secrets_crypto import encrypt
    return encrypt(secret)


def _dec_totp(secret):
    """Decrypts a stored TOTP secret, with legacy-plaintext fallback for accounts enrolled before
    this module started encrypting (a base32 TOTP secret never parses as a valid Fernet token, so
    decrypt_or_legacy() correctly falls through to returning it as-is). Re-provisioning
    (mfa_provision) transparently upgrades a legacy row to encrypted on next write."""
    if not secret:
        return secret
    from secrets_crypto import decrypt_or_legacy
    return decrypt_or_legacy(secret)


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
                  (_enc_totp(secret), int(time.time()), uid))
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
    totp = pyotp.TOTP(_dec_totp(user["totp_secret"]))
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
    if user.get("totp_secret") and pyotp.TOTP(_dec_totp(user["totp_secret"])).verify(code, valid_window=1):
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
        c.execute("UPDATE users SET totp_enabled=0, totp_secret=NULL, backup_codes_json=NULL, updated=?, "
                  "session_epoch=session_epoch+1 WHERE id=?", (int(time.time()), uid))


def mfa_backup_codes_remaining(uid):
    user = get_user_by_id(uid)
    if not user or not user.get("totp_enabled"):
        return 0
    try:
        return len(json.loads(user.get("backup_codes_json") or "[]"))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Rate limiting — simple in-process sliding window, one independent bucket per use (so hammering
# a job-trigger button can't lock you out of logging in, and vice versa). Deliberately not backed
# by a shared store (Redis etc.): this is a single-instance local app, so in-memory state is fine
# and resets harmlessly on restart. 2026-07-24: generalized from a login-only single bucket
# (`_login_attempts`) to a named-bucket version so `app.py`'s auth gate can also rate-limit the
# heavy job-trigger endpoints (`/api/run-*`, `/api/knowledge/reindex`, `/api/strategist/advise`) —
# `rate_limited(ip)`/`record_login_attempt(ip)`'s original signatures still work unchanged
# (default bucket "login", default limit LOGIN_RATE_LIMIT_PER_MIN), so neither existing call site
# needed to change.
# ---------------------------------------------------------------------------
_rate_buckets = {}   # bucket -> {ip: [unix ts, ...]}


def rate_limited(ip, bucket="login", limit=None):
    lim = LOGIN_RATE_LIMIT_PER_MIN if limit is None else limit
    now = time.time()
    ips = _rate_buckets.setdefault(bucket, {})
    hist = [t for t in ips.get(ip, []) if now - t < 60]
    ips[ip] = hist
    return len(hist) >= lim


def record_attempt(ip, bucket="login"):
    _rate_buckets.setdefault(bucket, {}).setdefault(ip, []).append(time.time())


def record_login_attempt(ip):
    record_attempt(ip, bucket="login")


# ============================================================================
# Host-CLI recovery (2026-07-24, Account Recovery Tier 2) — run this INSIDE the running
# container: `docker compose exec portfolio-api python auth.py --reset-credentials` (or
# `--wipe-all`). Deliberately NOT reachable over HTTP -- this is the fallback for when the web
# app itself can't be used to recover (no session, no working MFA, forgotten password), so it has
# to work independently of app.py/the session gate.
#
# --reset-credentials requires BOTH container/host shell access AND the account's recovery key
# (generated once at account creation, or afterward in SETTINGS -> SECURITY -- never recoverable
# if lost, by design, same as the password itself). This is the one path that gets you back into
# the EXISTING account without destroying anything else.
#
# --wipe-all requires ONLY container/host access (no recovery key) and is genuinely destructive --
# deletes auth.db, store.db, chat.db, and the knowledge index cache, returning the whole app to a
# blank first-run state. This is the intentional trade-off from the original design: host access
# alone must not be enough to silently take over an EXISTING account and its data, but it also
# shouldn't be blocked from resetting a hopelessly-locked-out installation entirely -- someone who
# can already `docker exec` into the container can delete the underlying Docker volume directly
# regardless of anything this script does, so gating --wipe-all further wouldn't add real
# protection, only friction for the legitimate case.
# ============================================================================
def _wipe_all_paths():
    """Same env-var-driven paths store.py/knowledge.py/app.py themselves use (duplicated here,
    not imported, to keep this CLI runnable with only auth.py's own dependencies -- no need to
    pull in requests/ollama/etc. just to delete files)."""
    return [
        AUTH_DB,
        os.environ.get("STORE_DB", "/data/store.db"),
        os.environ.get("CHAT_DB", "/data/chat.db"),
        os.environ.get("KNOWLEDGE_INDEX", "/data/knowledge_index.json"),
    ]


def _cli_reset_credentials():
    import getpass
    init_db()
    if not has_any_user():
        print("No account exists yet -- nothing to reset. Just load the app; it will show the "
              "first-run account-creation screen.")
        return 1
    username = input("Username: ").strip()
    user = get_user_by_username(username)
    if not user:
        print("No such user.")
        return 1
    if not user.get("recovery_key_hash"):
        print("This account has no recovery key configured -- either it was created before this "
              "feature existed and one was never generated in SETTINGS -> SECURITY, or one was "
              "used already (a recovery key is invalidated after a successful reset, same as a "
              "backup code). --reset-credentials is not available without it. The only remaining "
              "option is --wipe-all, which destroys ALL data, not just the login.")
        return 1
    recovery_key = getpass.getpass("Recovery key: ").strip()
    if not verify_recovery_key(user["id"], recovery_key):
        print("Incorrect recovery key.")
        return 1
    new_password = getpass.getpass("New password (min 8 characters): ")
    confirm = getpass.getpass("Confirm new password: ")
    if new_password != confirm:
        print("Passwords do not match.")
        return 1
    try:
        set_password(user["id"], new_password)
    except ValueError as e:
        print(str(e))
        return 1
    mfa_disable(user["id"])              # losing the device is presumably why this path was needed
    invalidate_recovery_key(user["id"])  # spent -- treat like a used backup code, not reusable
    print("Password reset, MFA disabled, and the used recovery key invalidated.")
    print("Log in with the new password. Re-enroll MFA and generate a new recovery key in "
          "SETTINGS -> SECURITY once you're back in -- both are treated as critical setup items.")
    return 0


def _cli_wipe_all():
    print("This deletes ALL data for this deployment: auth.db (login/MFA), store.db (all "
          "financial config/scenarios/history), chat.db (chat history), and the knowledge index "
          "cache (source documents in ./knowledge itself are untouched, but the app will need to "
          "reindex them). This is NOT limited to the login -- there is no undo.")
    confirm = input('Type WIPE (all caps) to confirm: ')
    if confirm != "WIPE":
        print("Aborted -- nothing was deleted.")
        return 1
    removed, missing = [], []
    for p in _wipe_all_paths():
        try:
            os.remove(p)
            removed.append(p)
        except FileNotFoundError:
            missing.append(p)
        except Exception as e:
            print("Could not remove {}: {}".format(p, e))
    print("Removed: {}".format(removed or "(none existed)"))
    if missing:
        print("Already absent: {}".format(missing))
    print("Restart the app now (e.g. `docker compose restart portfolio-api`) -- it will come up "
          "as a completely blank first-run install.")
    return 0


def _cli_main():
    import argparse
    p = argparse.ArgumentParser(
        description="Fiduciary auth.db host-level recovery CLI -- for when a user is locked out "
                    "with no session, no working MFA, and a forgotten password. Run this INSIDE "
                    "the running container: docker compose exec portfolio-api python auth.py <flag>")
    p.add_argument("--reset-credentials", action="store_true",
                   help="Reset the password (and disable MFA) using the one-time recovery key "
                        "issued at account creation. Requires container/host access AND the "
                        "recovery key -- host access alone is NOT sufficient.")
    p.add_argument("--wipe-all", action="store_true",
                   help="Last resort: delete auth.db, store.db, chat.db, and the knowledge index "
                        "cache, returning the whole app to a blank first-run state. Requires ONLY "
                        "container/host access (no recovery key).")
    args = p.parse_args()
    if args.reset_credentials:
        raise SystemExit(_cli_reset_credentials())
    if args.wipe_all:
        raise SystemExit(_cli_wipe_all())
    p.print_help()
    raise SystemExit(0)


if __name__ == "__main__":
    _cli_main()
