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
"""
import os

from cryptography.fernet import Fernet, InvalidToken

import store

SECRETS_KEY_FILE = os.environ.get("SECRETS_KEY_FILE", "/data/secrets.key")


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


_FERNET = Fernet(_load_or_create_key())


def encrypt(plaintext):
    """str -> Fernet token (str). Empty/None passes through unchanged -- no point encrypting
    "unset"; callers already treat a falsy value as "not saved"."""
    if not plaintext:
        return plaintext
    return _FERNET.encrypt(str(plaintext).encode()).decode()


def decrypt(token):
    """Fernet token (str) -> original plaintext (str). Raises InvalidToken/ValueError on anything
    that isn't a valid token for THIS key -- callers use decrypt_or_legacy() instead unless they
    specifically want that to propagate."""
    return _FERNET.decrypt(str(token).encode()).decode()


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
    would mangle non-UTF8 binary data. None/empty passes through unchanged."""
    if not data:
        return data
    return _FERNET.encrypt(data)


def decrypt_bytes_or_legacy(data):
    """Best-effort binary decrypt -- same legacy-fallback contract as decrypt_or_legacy(), but
    for raw bytes (knowledge_docs.content) instead of text. A value that fails to decrypt as a
    Fernet token is assumed to be legacy plaintext bytes from before this module covered this
    column, and is returned as-is. Never raises."""
    if not data:
        return data
    try:
        return _FERNET.decrypt(bytes(data))
    except (InvalidToken, ValueError, Exception):
        return data
