"""
secrets_crypto.py -- the app's ONE shared Fernet encryption primitive, used everywhere sensitive
data lands in SQLite at rest. Originally built (2026-07-24) just for SECRETS_FILE (app.py's
SECRET_SPECS: Finnhub, SnapTrade, Monarch, Tavily, the Claude/LLM provider keys), then EXPANDED
same day (Jason: "All sensitive information stored needs to be encrypted") to cover every other
place real personal/financial data sits in the DB:
  - store.py's generic Storage-Stage-A blob (`app_state.value` -- ~44 files incl. retirement.json/
    insurance.json/accounts.json/policy.json, real income/debt/policy/home-value/estate data) and
    Storage-Stage-B's typed tables (value_history, strategist_runs, paper_transactions/reports).
  - store.py's knowledge/RAG tables (raw uploaded document bytes + extracted chunk text).
  - app.py's chat.db (message content/tool-call results -- a verbatim transcript of financial
    discussions, arguably the single most sensitive table in the app).
  - auth.py's `totp_secret` column -- the one field in the whole auth system that MUST be
    reversible (TOTP verification needs the raw secret to recompute a code) rather than one-way
    hashed like password_hash/backup_codes_json/recovery_key_hash already correctly are.
See each module's own comments for exactly which columns/keys are covered and why (a few -- lookup
keys like knowledge_paths.relpath, or low-individual-sensitivity enum/id columns used in SQL WHERE/
ORDER BY -- are deliberately left plaintext; encrypting a value used as an equality-lookup key
breaks the lookup, since Fernet ciphertext is non-deterministic per encryption).

Before this module existed, every one of the above was plain text in a SQLite TEXT/BLOB column --
protected only by the DB file's 0600 permission bit, no different from any other config blob in
the same table. This module adds real encryption: AES-128-CBC + HMAC via the `cryptography`
package (already a dependency here for TLS cert handling). The store/DB layer itself stays
unaware of encryption -- it just sees longer opaque strings/bytes.

Key bootstrap mirrors SESSION_SECRET's existing pattern in this app (auth.py): if SECRETS_KEY_FILE
doesn't exist yet, a fresh key is generated and persisted (0600) so it survives a restart; if it
already exists, it's reused. ONE key now protects essentially every sensitive thing this app
stores -- back it up alongside the data volume, and treat losing it as equivalent to losing the
data volume itself (every already-encrypted value becomes permanently unrecoverable, app-wide, not
just the SECRETS panel).

This does NOT cover ./mcpo-secrets/mcpo.env (the file Docker Compose reads directly as a second
env_file for the mcpo container) -- that file structurally MUST be plaintext for Compose to inject
its contents into mcpo's environment; there is no decrypt-on-read step Compose can perform. It's
still chmod 0600, same as before, but that's a different (weaker) protection than what this module
gives the DB-stored copy of the same values. Embedding VECTORS (knowledge_chunks.vec) ARE
encrypted here (corpus is explicitly small enough that per-chunk decrypt during search is cheap),
closing what would otherwise be a residual embedding-inversion concern.

SEC-3 (2026-07-25) -- KEY VERSIONING / ROTATION (ISO A.8.24, NIST SC-12/SC-13/SC-28, SOC 2 CC6.1):
originally this module had exactly ONE static key (SECRETS_KEY_FILE), no rotation, and losing that
one file meant every already-encrypted value app-wide became permanently unrecoverable. This adds a
KEYRING: SECRETS_KEY_FILE's key is now "version 1" (loaded EXACTLY as before -- nothing about an
unrotated deployment changes, including its ciphertext FORMAT, see below); rotate_key() generates a
new key as version N+1 and persists it in a separate SECRETS_KEYRING_FILE. Encrypt always uses the
CURRENT version; decrypt looks up whichever version actually produced a given ciphertext. Rotating
does NOT re-encrypt existing data (that would mean touching every consumer table across store.py/
auth.py/knowledge.py/chat.db/audit.py in one risky sweep) -- old ciphertext just keeps decrypting
under its original (still-retained-forever) key version, exactly the standard "envelope encryption"
rotation pattern. A full re-encrypt-everything-under-the-newest-key sweep is a DIFFERENT, larger,
separate maintenance operation, deliberately out of scope here.

BACKWARD COMPATIBILITY (important): a deployment that never rotates (the default, and Jason's
actual state right now) produces IDENTICAL ciphertext to before this feature existed -- no version
prefix at all -- because encrypt() only adds a "vN:" prefix once current_version > 1. Every already-
encrypted value in every table across the whole app, written before this feature shipped, decrypts
exactly as before with ZERO migration step: decrypt() treats any value with no recognized "vN:"
prefix as version 1 (the only key that could ever have produced an unprefixed token). This is the
same "don't touch what already works" discipline as this module's own decrypt_or_legacy() (values
from before encryption existed at all) and audit.py's scheme column (pre-HMAC-upgrade rows).
"""
import os
import json
import time

from cryptography.fernet import Fernet, InvalidToken

import store

SECRETS_KEY_FILE = os.environ.get("SECRETS_KEY_FILE", "/data/secrets.key")
SECRETS_KEYRING_FILE = os.environ.get("SECRETS_KEYRING_FILE", "/data/secrets_keyring.json")


def _load_or_create_key():
    try:
        with open(SECRETS_KEY_FILE, "rb") as f:
            key = f.read().strip()
            if key:
                return key
    except FileNotFoundError:
        pass
    key = Fernet.generate_key()
    store.atomic_write_bytes(SECRETS_KEY_FILE, key, mode=0o600)
    return key


def _load_keyring_meta():
    """Versions >= 2 only -- version 1 always comes from SECRETS_KEY_FILE (see module docstring),
    never duplicated into this file. Returns {"current_version": int, "keys": {"2": "<b64>", ...},
    "created_at": {"1": ts, "2": ts, ...}}. Missing/corrupt file -> a fresh, unrotated structure
    (current_version=1, no extra keys) -- this is the state of every deployment that predates
    SEC-3, so a missing keyring file is the EXPECTED default, not an error."""
    try:
        with open(SECRETS_KEYRING_FILE, encoding="utf-8") as f:
            meta = json.loads(f.read())
        if isinstance(meta, dict) and "current_version" in meta:
            return meta
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[secrets_crypto] keyring file unreadable, treating as unrotated: {e}", flush=True)
    return {"current_version": 1, "keys": {}, "created_at": {}}


def _save_keyring_meta(meta):
    store.atomic_write_bytes(
        SECRETS_KEYRING_FILE, json.dumps(meta, indent=2).encode("utf-8"), mode=0o600)


def _build_keyring():
    """{version_int: Fernet(key_bytes)} -- version 1 from SECRETS_KEY_FILE always, plus whatever
    rotate_key() has added since. Rebuilt once at import and again after every rotate_key() call
    (rotation is rare -- an admin-triggered action -- so rebuilding the whole dict each time is
    simpler and cheap, not a hot path needing incremental updates)."""
    meta = _load_keyring_meta()
    ring = {1: Fernet(_load_or_create_key())}
    for v_str, b64key in (meta.get("keys") or {}).items():
        try:
            ring[int(v_str)] = Fernet(b64key.encode() if isinstance(b64key, str) else b64key)
        except Exception as e:
            print(f"[secrets_crypto] keyring version {v_str} unusable, skipping: {e}", flush=True)
    return ring, int(meta.get("current_version") or 1)


_KEYRING, _CURRENT_VERSION = _build_keyring()
_PREFIX_RE_MIN_VERSION = 2  # version 1 is never prefixed -- see module docstring


def rotate_key():
    """Generates a new Fernet key as version (current max + 1), persists it into
    SECRETS_KEYRING_FILE, and makes it the current version for all FUTURE encrypt() calls. Prior
    versions are kept forever (never deleted) so everything already encrypted keeps decrypting.
    Returns the new version number. Callers (app.py's admin-only POST /api/secrets/rotate-key)
    are responsible for audit-logging this as a `secret.rotated` event."""
    meta = _load_keyring_meta()
    existing_versions = [1] + [int(v) for v in (meta.get("keys") or {})]
    new_version = max(existing_versions) + 1
    new_key = Fernet.generate_key()
    meta.setdefault("keys", {})[str(new_version)] = new_key.decode()
    meta.setdefault("created_at", {})[str(new_version)] = int(time.time())
    meta["current_version"] = new_version
    _save_keyring_meta(meta)
    global _KEYRING, _CURRENT_VERSION
    _KEYRING, _CURRENT_VERSION = _build_keyring()
    return new_version


def key_status():
    """For the SETTINGS -> SECURITY UI: {"current_version": N, "versions": [1, 2, ...],
    "created_at": {"1": ts_or_None, "2": ts, ...}}. Version 1's creation time isn't tracked (it
    predates this feature and SECRETS_KEY_FILE itself carries no timestamp), reported as None."""
    meta = _load_keyring_meta()
    versions = sorted(_KEYRING.keys())
    created = {"1": None}
    created.update(meta.get("created_at") or {})
    return {"current_version": _CURRENT_VERSION, "versions": versions,
            "created_at": {str(v): created.get(str(v)) for v in versions}}


def _versioned_token(raw_token_str):
    """encrypt()'s output for the CURRENT version: unprefixed if current_version==1 (byte-for-byte
    what this module always produced, for the overwhelming common case of a never-rotated
    deployment), else "vN:" + token."""
    if _CURRENT_VERSION == 1:
        return raw_token_str
    return f"v{_CURRENT_VERSION}:{raw_token_str}"


def _parse_version(token_str):
    """(version, token_without_prefix) for a stored value. No recognized "vN:" prefix -> version 1
    (the only key that could have produced an unprefixed token -- see module docstring)."""
    if token_str.startswith("v") and ":" in token_str[:6]:
        prefix, _, rest = token_str.partition(":")
        try:
            v = int(prefix[1:])
            if v in _KEYRING:
                return v, rest
        except ValueError:
            pass
    return 1, token_str


def encrypt(plaintext):
    """str -> versioned Fernet token (str). Empty/None passes through unchanged -- no point
    encrypting "unset"; callers already treat a falsy value as "not saved"."""
    if not plaintext:
        return plaintext
    token = _KEYRING[_CURRENT_VERSION].encrypt(str(plaintext).encode()).decode()
    return _versioned_token(token)


def decrypt(token):
    """Versioned Fernet token (str) -> original plaintext (str). Raises InvalidToken/ValueError on
    anything that isn't a valid token for its (implied or explicit) key version -- callers use
    decrypt_or_legacy() instead unless they specifically want that to propagate."""
    v, raw = _parse_version(str(token))
    return _KEYRING[v].decrypt(raw.encode()).decode()


def decrypt_or_legacy(value):
    """Best-effort decrypt for values that might predate this module (saved back when
    SECRETS_FILE held plain JSON text, no encryption at all). A value that fails to decrypt as a
    Fernet token is assumed to be one of those legacy plaintext values and is returned as-is --
    the caller is expected to re-save (re-encrypt) it the next time anything in that store gets
    written, so it's upgraded transparently on first touch rather than requiring a separate
    migration step. Never raises."""
    if not value:
        return value
    try:
        return decrypt(value)
    except (InvalidToken, ValueError, Exception):
        return value


def encrypt_bytes(data):
    """Binary-safe counterpart to encrypt() -- for knowledge_docs.content, which is arbitrary
    raw file bytes (PDF/DOCX/etc), not text. Fernet.encrypt/decrypt already operate on bytes
    natively; the str-based encrypt()/decrypt() above just add an extra encode/decode step that
    would mangle non-UTF8 binary data. None/empty passes through unchanged. Versioned the same way
    as encrypt() -- the "vN:" prefix (when current_version > 1) is plain ASCII, safe to prepend to
    Fernet's own base64 (ASCII) output without corrupting anything."""
    if not data:
        return data
    token = _KEYRING[_CURRENT_VERSION].encrypt(data)
    if _CURRENT_VERSION == 1:
        return token
    return f"v{_CURRENT_VERSION}:".encode() + token


def decrypt_bytes_or_legacy(data):
    """Best-effort binary decrypt -- same legacy-fallback contract as decrypt_or_legacy(), but
    for raw bytes (knowledge_docs.content) instead of text. A value that fails to decrypt as a
    Fernet token is assumed to be legacy plaintext bytes from before this module covered this
    column, and is returned as-is. Never raises."""
    if not data:
        return data
    try:
        data_b = bytes(data)
        if data_b.startswith(b"v") and b":" in data_b[:6]:
            prefix, _, raw = data_b.partition(b":")
            v = int(prefix[1:])
            if v in _KEYRING:
                return _KEYRING[v].decrypt(raw)
        return _KEYRING[1].decrypt(data_b)
    except (InvalidToken, ValueError, Exception):
        return data
