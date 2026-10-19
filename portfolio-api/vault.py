"""
vault.py -- encrypted document vault (TASK-001 Phase 1, Quicken-parity Q6).

Stores files (will, trust, POA, insurance declarations, IDs ...) encrypted at rest. READ-ONLY
mission is unchanged: the vault stores files and never moves money.

ENVELOPE ENCRYPTION: each file gets a random 256-bit DEK and is encrypted with AES-256-GCM
(blob = 12-byte nonce || ciphertext+tag, AAD = item id so a blob can't be swapped between items).
The DEK is wrapped with secrets_crypto.encrypt_bytes() (the app's one keyring), so key rotation
re-wraps DEKs (rewrap_all) and never re-encrypts the blobs. Clearing wrapped_dek = crypto-shred.
Title and filename are encrypted at rest too; folder/doc_type/dates stay clear so the app can
filter and alert on them.

THREAT MODEL (honest): server-side encryption at rest. Someone with root on the host plus the key
file can decrypt. It protects stolen disks and backups, not a compromised host.

VISIBILITY (the critical piece): app.py's global gate lets viewers do EVERY GET, so every read
path in vault_api.py MUST go through can_read(). private = admin only; household = admin +
viewers; emergency = admin, plus the designated emergency contact once an emergency request is
granted (Phase 2 -- `emergency_granted` is always False in Phase 1).
"""
import hashlib
import io
import os
import secrets
import time
import uuid
import zipfile

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import secrets_crypto
import store

VAULT_DIR = os.environ.get("VAULT_DIR", "/data/vault")
VAULT_MAX_FILE_MB = int(os.environ.get("VAULT_MAX_FILE_MB", "25"))
VAULT_MAX_TOTAL_GB = float(os.environ.get("VAULT_MAX_TOTAL_GB", "5"))

VISIBILITIES = ("private", "household", "emergency")

# Seed folders: match LifeHub and the CONTINUITY binder's estate_docs.
SEED_FOLDERS = (
    ("Estate", "emergency"),             # will, trust, POA, healthcare directive
    ("Insurance", "household"),
    ("Tax", "private"),
    ("Property & Titles", "household"),
    ("IDs", "emergency"),
    ("Medical", "private"),
    ("Accounts & Beneficiaries", "emergency"),
    ("Letter of Instruction", "emergency"),
)

# Types whose bytes may be rendered inline (everything else is attachment-only; HTML/SVG can
# carry script and are not on the allowlist at all).
INLINE_MIMES = ("application/pdf", "image/png", "image/jpeg")


class VaultError(Exception):
    """Validation/policy failure. `status` is the HTTP status the API layer should return."""
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


# ---------------------------------------------------------------- schema
def init_db():
    with store._lock:
        with store._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS vault_folders(
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                default_visibility TEXT NOT NULL DEFAULT 'private'
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS vault_items(
                id TEXT PRIMARY KEY,
                folder TEXT NOT NULL,
                title TEXT NOT NULL,
                doc_type TEXT,
                filename_enc TEXT NOT NULL,
                mime TEXT NOT NULL,
                size INTEGER NOT NULL,
                sha256_plain TEXT NOT NULL,
                created INTEGER NOT NULL,
                updated INTEGER NOT NULL,
                expires_on TEXT,
                review_by TEXT,
                visibility TEXT NOT NULL DEFAULT 'private',
                wrapped_dek BLOB,
                key_version INTEGER NOT NULL DEFAULT 1,
                deleted INTEGER NOT NULL DEFAULT 0
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_vault_items_folder ON vault_items(folder, deleted)")
            if not c.execute("SELECT 1 FROM vault_folders LIMIT 1").fetchone():
                for name, vis in SEED_FOLDERS:
                    c.execute("INSERT INTO vault_folders(id,name,default_visibility) VALUES(?,?,?)",
                              (uuid.uuid4().hex, name, vis))
    os.makedirs(os.path.join(VAULT_DIR, "blobs"), exist_ok=True)


def _blob_path(item_id):
    return os.path.join(VAULT_DIR, "blobs", f"{item_id}.bin")


# ---------------------------------------------------------------- crypto helpers
def _key_version_of(wrapped):
    """Version prefix of a wrapped DEK ("vN:" when rotated, else 1)."""
    b = bytes(wrapped)
    if b.startswith(b"v") and b":" in b[:6]:
        try:
            return int(b[1:b.index(b":")])
        except ValueError:
            pass
    return 1


def _unwrap_dek(wrapped):
    """Strict unwrap. secrets_crypto.decrypt_bytes_or_legacy() returns its input on failure, so
    insist on a 32-byte result rather than trusting it."""
    if not wrapped:
        raise VaultError("document has been crypto-shredded", 410)
    dek = secrets_crypto.decrypt_bytes_or_legacy(bytes(wrapped))
    if not isinstance(dek, (bytes, bytearray)) or len(dek) != 32:
        raise VaultError("could not unwrap document key", 500)
    return bytes(dek)


def _enc_text(s):
    return secrets_crypto.encrypt(s)


def _dec_text(token):
    try:
        return secrets_crypto.decrypt(token)
    except Exception:
        return ""


# ---------------------------------------------------------------- type allowlist (magic bytes)
_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}


def sniff_mime(data, filename=""):
    """Allowlist by MAGIC BYTES, never by extension. Returns a mime string or None (reject)."""
    head = data[:16]
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in _HEIC_BRANDS:
        return "image/heic"
    if head.startswith(b"PK\x03\x04"):
        try:
            names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
        except Exception:
            return None
        if "[Content_Types].xml" in names:
            if any(n.startswith("word/") for n in names):
                return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            if any(n.startswith("xl/") for n in names):
                return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return None
    # Plain text: valid UTF-8, no NUL bytes, and NOT markup that browsers would execute.
    if data and b"\x00" not in data[:8192]:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        low = text.lstrip()[:200].lower()
        if low.startswith(("<!doctype", "<html", "<svg", "<?xml", "<script")):
            return None
        return "text/plain"
    return None


# ---------------------------------------------------------------- policy
def can_read(user, item, emergency_granted=False):
    """THE single read-authorization decision for vault content. `user` is an auth user dict (or
    None), `item` a row dict from get_item_row(). Every list/meta/download/preview goes through
    this -- the global gate does not protect GETs from viewers."""
    if not user or not item or item.get("deleted"):
        return False
    import auth
    if auth.is_admin(user):
        return True
    vis = item.get("visibility")
    if vis == "household":
        return True
    if vis == "emergency":
        return bool(emergency_granted)
    return False


def _norm_visibility(v, default="private"):
    v = (v or default or "private").strip().lower()
    if v not in VISIBILITIES:
        raise VaultError(f"visibility must be one of {', '.join(VISIBILITIES)}")
    return v


def _norm_date(s, field):
    s = (s or "").strip()
    if not s:
        return None
    try:
        time.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise VaultError(f"{field} must be YYYY-MM-DD")
    return s


def total_bytes():
    with store._conn() as c:
        r = c.execute("SELECT COALESCE(SUM(size),0) AS s FROM vault_items WHERE deleted=0").fetchone()
    return int(r["s"])


def max_file_bytes():
    return VAULT_MAX_FILE_MB * 1024 * 1024


def max_total_bytes():
    return int(VAULT_MAX_TOTAL_GB * 1024 * 1024 * 1024)


# ---------------------------------------------------------------- folders
def list_folders():
    with store._conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id,name,default_visibility FROM vault_folders ORDER BY name").fetchall()]


def _folder_by_name(name):
    with store._conn() as c:
        r = c.execute("SELECT * FROM vault_folders WHERE name=?", (name,)).fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------------- items
def get_item_row(item_id):
    with store._conn() as c:
        r = c.execute("SELECT * FROM vault_items WHERE id=?", (item_id,)).fetchone()
    return dict(r) if r else None


def public_item(row):
    """Metadata safe to return to a caller who passed can_read(): decrypted title/filename, never
    the wrapped DEK."""
    return {
        "id": row["id"], "folder": row["folder"], "title": _dec_text(row["title"]),
        "filename": _dec_text(row["filename_enc"]), "doc_type": row["doc_type"],
        "mime": row["mime"], "size": row["size"], "sha256": row["sha256_plain"],
        "created": row["created"], "updated": row["updated"], "expires_on": row["expires_on"],
        "review_by": row["review_by"], "visibility": row["visibility"],
        "inline_ok": row["mime"] in INLINE_MIMES,
    }


def list_items(user, folder=None, emergency_granted=False):
    q = "SELECT * FROM vault_items WHERE deleted=0"
    args = []
    if folder:
        q += " AND folder=?"
        args.append(folder)
    q += " ORDER BY updated DESC"
    with store._conn() as c:
        rows = [dict(r) for r in c.execute(q, args).fetchall()]
    return [public_item(r) for r in rows if can_read(user, r, emergency_granted)]


def create_item(data, *, filename, folder, title=None, doc_type=None, visibility=None,
                expires_on=None, review_by=None):
    """Validates, encrypts and stores. Raises VaultError. Returns the public item dict."""
    if not data:
        raise VaultError("uploaded file is empty")
    if len(data) > max_file_bytes():
        raise VaultError(f"file exceeds the {VAULT_MAX_FILE_MB} MB per-file limit", 413)
    if total_bytes() + len(data) > max_total_bytes():
        raise VaultError(f"vault is full ({VAULT_MAX_TOTAL_GB:g} GB total limit)", 413)
    fol = _folder_by_name(folder)
    if not fol:
        raise VaultError("unknown folder")
    mime = sniff_mime(data, filename)
    if not mime:
        raise VaultError("file type not allowed (PDF, PNG, JPEG, HEIC, TXT, DOCX, XLSX only)", 415)
    vis = _norm_visibility(visibility, fol["default_visibility"])
    filename = os.path.basename((filename or "").replace("\\", "/")).strip() or "document"
    title = (title or "").strip() or filename
    item_id = uuid.uuid4().hex
    dek = AESGCM.generate_key(bit_length=256)
    nonce = secrets.token_bytes(12)
    blob = nonce + AESGCM(dek).encrypt(nonce, data, item_id.encode())
    wrapped = secrets_crypto.encrypt_bytes(dek)
    now = int(time.time())
    row = {
        "id": item_id, "folder": folder, "title": _enc_text(title[:300]), "doc_type": (doc_type or "").strip()[:60] or None,
        "filename_enc": _enc_text(filename[:300]), "mime": mime, "size": len(data),
        "sha256_plain": hashlib.sha256(data).hexdigest(), "created": now, "updated": now,
        "expires_on": _norm_date(expires_on, "expires_on"), "review_by": _norm_date(review_by, "review_by"),
        "visibility": vis, "wrapped_dek": wrapped, "key_version": _key_version_of(wrapped), "deleted": 0,
    }
    store.atomic_write_bytes(_blob_path(item_id), blob, mode=0o600)
    try:
        with store._conn() as c:
            c.execute("""INSERT INTO vault_items(id,folder,title,doc_type,filename_enc,mime,size,
                sha256_plain,created,updated,expires_on,review_by,visibility,wrapped_dek,key_version,deleted)
                VALUES(:id,:folder,:title,:doc_type,:filename_enc,:mime,:size,:sha256_plain,:created,
                :updated,:expires_on,:review_by,:visibility,:wrapped_dek,:key_version,:deleted)""", row)
    except Exception:
        try:
            os.remove(_blob_path(item_id))
        except OSError:
            pass
        raise
    return public_item(row)


def read_plaintext(row):
    """Decrypt + verify an item. Refuses on any tamper (GCM tag, AAD, or sha256 mismatch)."""
    dek = _unwrap_dek(row["wrapped_dek"])
    try:
        with open(_blob_path(row["id"]), "rb") as f:
            blob = f.read()
    except FileNotFoundError:
        raise VaultError("stored file is missing", 500)
    try:
        data = AESGCM(dek).decrypt(blob[:12], blob[12:], row["id"].encode())
    except Exception:
        raise VaultError("stored file failed integrity check", 500)
    if hashlib.sha256(data).hexdigest() != row["sha256_plain"]:
        raise VaultError("stored file failed integrity check", 500)
    return data


def update_item(item_id, *, title=None, folder=None, doc_type=None, visibility=None,
                expires_on=None, review_by=None):
    row = get_item_row(item_id)
    if not row or row["deleted"]:
        raise VaultError("not found", 404)
    sets, args = ["updated=?"], [int(time.time())]
    if title is not None:
        sets.append("title=?"); args.append(_enc_text(title.strip()[:300] or "document"))
    if folder is not None:
        if not _folder_by_name(folder):
            raise VaultError("unknown folder")
        sets.append("folder=?"); args.append(folder)
    if doc_type is not None:
        sets.append("doc_type=?"); args.append(doc_type.strip()[:60] or None)
    if visibility is not None:
        sets.append("visibility=?"); args.append(_norm_visibility(visibility))
    if expires_on is not None:
        sets.append("expires_on=?"); args.append(_norm_date(expires_on, "expires_on"))
    if review_by is not None:
        sets.append("review_by=?"); args.append(_norm_date(review_by, "review_by"))
    args.append(item_id)
    with store._conn() as c:
        c.execute(f"UPDATE vault_items SET {','.join(sets)} WHERE id=?", args)
    return public_item(get_item_row(item_id))


def delete_item(item_id):
    """Crypto-shred: drop the wrapped DEK, delete the blob, tombstone the row (metadata fields
    that are encrypted stay encrypted)."""
    row = get_item_row(item_id)
    if not row or row["deleted"]:
        raise VaultError("not found", 404)
    with store._conn() as c:
        c.execute("UPDATE vault_items SET deleted=1, wrapped_dek=NULL, updated=? WHERE id=?",
                  (int(time.time()), item_id))
    try:
        os.remove(_blob_path(item_id))
    except OSError:
        pass
    return True


def rewrap_all():
    """After secrets_crypto.rotate_key(): re-wrap every live DEK under the CURRENT key version.
    Blobs are untouched. Returns the number re-wrapped."""
    n = 0
    with store._conn() as c:
        rows = c.execute("SELECT id, wrapped_dek FROM vault_items WHERE deleted=0 "
                         "AND wrapped_dek IS NOT NULL").fetchall()
        for r in rows:
            dek = _unwrap_dek(r["wrapped_dek"])
            new = secrets_crypto.encrypt_bytes(dek)
            c.execute("UPDATE vault_items SET wrapped_dek=?, key_version=? WHERE id=?",
                      (new, _key_version_of(new), r["id"]))
            n += 1
    return n
