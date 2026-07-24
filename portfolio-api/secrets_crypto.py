"""
secrets_crypto.py -- encryption at rest for the values stored in SECRETS_FILE (app.py's
SECRET_SPECS: Finnhub, SnapTrade, Monarch, Tavily, the Claude/LLM provider keys, etc.)

Before this module existed, `store.write_json(SECRETS_FILE, stored)` wrote those values as plain
JSON text into a SQLite TEXT column -- protected only by the store DB file's 0600 permission bit,
no different from any other config blob in the same table. That's fine for non-sensitive config
but not for actual credentials (a brokerage login password, an LLM API key) in a wealth-management
app. This module adds real encryption: each value is Fernet-encrypted (AES-128-CBC + HMAC, via the
`cryptography` package already a dependency here for TLS cert handling) before it reaches
store.write_json, and decrypted after store.read_json_strict returns it. The store/DB layer
itself stays completely unaware of encryption -- it just sees longer opaque strings.

Key bootstrap mirrors SESSION_SECRET's existing pattern in this app (auth.py): if SECRETS_KEY_FILE
doesn't exist yet, a fresh key is generated and persisted (0600) so it survives a restart; if it
already exists, it's reused. Losing this key file makes every already-encrypted value permanently
unrecoverable -- back it up alongside the data volume, same as you would the store DB itself.

This does NOT cover ./mcpo-secrets/mcpo.env (the file Docker Compose reads directly as a second
env_file for the mcpo container) -- that file structurally MUST be plaintext for Compose to inject
its contents into mcpo's environment; there is no decrypt-on-read step Compose can perform. It's
still chmod 0600, same as before, but that's a different (weaker) protection than what this module
gives the DB-stored copy of the same values.
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
