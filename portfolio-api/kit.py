"""
kit.py -- the offline Emergency Kit (TASK-002 §3.5). Survives total loss of the server: ONE
passphrase-encrypted *.fidkit (all `emergency` vault items + a binder export + a manifest of sha256s)
plus, UNENCRYPTED beside it, kit_decrypt.py and a printable INSTRUCTIONS.html, zipped for download.

Streaming: items are decrypted one at a time into a tar stream that is chunk-encrypted
(AES-256-GCM, 1 MiB chunks, scrypt key) into a 0600 temp file, so memory stays bounded. The
passphrase is generated here (>=128 bits), returned ONCE to the caller and never stored, logged or
audited -- only metadata (generated_at, item-set hash, count, size) is kept. The kit never contains
`private` or `household` items. Format spec: see the header of kit_decrypt.py.
"""
import base64
import hashlib
import html
import io
import json
import os
import secrets
import struct
import tarfile
import threading
import time
import zipfile

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import store
import vault

MAGIC = b"FIDKIT01"
CHUNK = 1 << 20
SCRYPT_N, SCRYPT_R, SCRYPT_P = 1 << 15, 8, 1
KIT_TMP_DIR = os.environ.get("KIT_TMP_DIR", "/data/tmp")
KIT_MAX_MB = int(os.environ.get("KIT_MAX_MB", "500"))
TOKEN_TTL_S = 600
STALE_ALERT_AFTER_S = 7 * 86400

_tokens = {}            # token -> (path, expires_ts, download_name)
_tok_lock = threading.Lock()


class KitError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


def init_db():
    with store._lock:
        with store._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS vault_kit_meta(
                id INTEGER PRIMARY KEY CHECK (id = 1),
                generated_at REAL, item_hash TEXT, item_count INTEGER, size INTEGER)""")
            c.execute("INSERT OR IGNORE INTO vault_kit_meta(id) VALUES (1)")
    os.makedirs(KIT_TMP_DIR, mode=0o700, exist_ok=True)
    for n in os.listdir(KIT_TMP_DIR):          # leftover temp files from a crash/restart
        if n.startswith("kit-"):
            try:
                os.remove(os.path.join(KIT_TMP_DIR, n))
            except OSError:
                pass


def generate_passphrase():
    """26 base32 characters = 130 bits, grouped 4-4-4-... for reading aloud / writing down."""
    raw = base64.b32encode(secrets.token_bytes(17)).decode().rstrip("=")[:26]
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


# ---------------------------------------------------------------- item set + staleness
def _emergency_rows():
    with store._conn() as c:
        rows = c.execute("SELECT * FROM vault_items WHERE deleted=0 AND visibility='emergency' "
                         "ORDER BY folder, created").fetchall()
    return [dict(r) for r in rows]


def item_set_hash(rows=None):
    rows = _emergency_rows() if rows is None else rows
    h = hashlib.sha256()
    for r in sorted(rows, key=lambda r: r["id"]):
        h.update(f"{r['id']}:{r['sha256_plain']}:{r['visibility']}\n".encode())
    return h.hexdigest()


def meta():
    with store._conn() as c:
        return dict(c.execute("SELECT * FROM vault_kit_meta WHERE id=1").fetchone())


def status(now=None):
    """Admin-facing: {generated_at, item_count, size, current_count, stale, stale_for_s}."""
    now = now or time.time()
    m = meta()
    rows = _emergency_rows()
    cur = item_set_hash(rows)
    never = not m["generated_at"]
    stale = (not never and m["item_hash"] != cur) or (never and bool(rows))
    return {"generated_at": m["generated_at"], "item_count": m["item_count"], "size": m["size"],
            "current_count": len(rows), "never_generated": never, "stale": stale,
            "stale_for_s": (now - m["generated_at"]) if (stale and not never) else None}


def stale_alert(now=None):
    """Text for _alerts_digest when the kit has been stale > 7 days (or emergency items exist but a
    kit was never made and the oldest emergency item is > 7 days old), else None."""
    now = now or time.time()
    s = status(now)
    rows = _emergency_rows()
    if not rows or not s["stale"]:
        return None
    ref = s["generated_at"] or min(r["created"] for r in rows)
    if now - ref < STALE_ALERT_AFTER_S:
        return None
    return ("Emergency Kit is out of date — emergency-visibility documents changed since it was last generated"
            if s["generated_at"] else
            "No Emergency Kit has been generated yet, but emergency-visibility documents are on file")


# ---------------------------------------------------------------- container writer
class _ChunkWriter:
    """File-like sink for tarfile: buffers plaintext, emits AES-GCM chunks. The final chunk is
    flagged in its AAD so truncation is detected on decrypt."""
    def __init__(self, out, key, header, nonce_prefix):
        self.out, self.aes, self.hdr, self.np, self.ctr, self.buf = out, AESGCM(key), header, nonce_prefix, 0, bytearray()

    def _emit(self, data, last):
        ct = self.aes.encrypt(self.np + struct.pack(">Q", self.ctr), bytes(data),
                              MAGIC + self.hdr + (b"\x01" if last else b"\x00"))
        self.out.write(struct.pack(">I", len(ct)) + ct)
        self.ctr += 1

    def write(self, b):
        self.buf.extend(b)
        while len(self.buf) > CHUNK:            # strictly more than a chunk => this one is not the last
            self._emit(self.buf[:CHUNK], False)
            del self.buf[:CHUNK]
        return len(b)

    def close_last(self):
        self._emit(self.buf, True)
        self.buf = bytearray()

    def flush(self):
        pass


def _safe_name(s):
    s = "".join(ch if (ch.isalnum() or ch in " ._-()") else "_" for ch in (s or "file")).strip(" .")
    return s[:120] or "file"


def _binder_text(b):
    lines = ["EMERGENCY BINDER (exported %s)" % time.strftime("%Y-%m-%d %H:%M"), ""]
    lines.append("Executor: %s" % (b.get("executor_name") or "-"))
    lines.append("Will location: %s" % (b.get("will_location") or "-"))
    lines += ["", "KEY CONTACTS"]
    for c in b.get("contacts") or []:
        lines.append("  - %s: %s  %s  %s  %s" % (c.get("role") or "", c.get("name") or "", c.get("phone") or "",
                                                 c.get("email") or "", c.get("notes") or ""))
    lines += ["", "ESTATE DOCUMENTS (last reviewed)"]
    for d in b.get("estate_docs") or []:
        lines.append("  - %s: %s %s" % (d.get("doc_type"), d.get("last_reviewed") or "-", d.get("notes") or ""))
    if b.get("accounts"):
        lines += ["", "ACCOUNT MAP"]
        for a in b["accounts"]:
            lines.append("  - " + json.dumps(a, default=str))
    return "\n".join(lines) + "\n"


def instructions_html(binder, item_count, generated):
    e = html.escape
    contacts = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            e(str(c.get("role") or "")), e(str(c.get("name") or "")), e(str(c.get("phone") or "")),
            e(str(c.get("email") or ""))) for c in (binder.get("contacts") or [])) or \
        "<tr><td colspan=4>No contacts were recorded in the app's Continuity binder.</td></tr>"
    return """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Fiduciary Emergency Kit - instructions</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem;color:#111}
code,pre{background:#f1f1f1;padding:.15rem .35rem;border-radius:4px}pre{padding:.7rem;overflow:auto}
table{border-collapse:collapse;width:100%%}td,th{border:1px solid #999;padding:.35rem .5rem;text-align:left}
h1{font-size:1.5rem}h2{font-size:1.15rem;margin-top:1.6rem}</style></head><body>
<h1>Fiduciary Emergency Kit</h1>
<p>Generated %(gen)s. Contains <b>%(n)d</b> document(s) marked <i>emergency</i>, plus a binder of contacts and
estate details. It is encrypted: you need the <b>kit passphrase</b>, which is NOT in this folder. Ask whoever
gave you this kit, check a sealed envelope, or contact the attorney named below.</p>
<h2>1. Open the documents (any Windows, Mac or Linux computer, no internet needed after step a)</h2>
<ol><li>Install Python 3 (python.org) and run <code>pip install cryptography</code> (needs internet once).</li>
<li>Put <code>kit_decrypt.py</code> and the <code>.fidkit</code> file in one folder.</li>
<li>Run <pre>python kit_decrypt.py YOUR-KIT-FILE.fidkit decrypted</pre> and type the passphrase exactly as written.
A <code>decrypted</code> folder appears with your documents under <code>items/</code> and a readable
<code>binder.txt</code>. The script checks every file against a stored checksum.</li></ol>
<h2>2. Who to call</h2>
<table><tr><th>Role</th><th>Name</th><th>Phone</th><th>Email</th></tr>%(contacts)s</table>
<p>Executor: %(exec)s &nbsp;|&nbsp; Will location: %(will)s</p>
<h2>3. If the Fiduciary app itself needs to be brought back</h2>
<p>The app runs in Docker on the household computer. Its encrypted backups (<code>fiduciary-backup-*.tar.gz.enc</code>,
in the <code>backups</code> folder) restore with <code>scripts/restore.sh</code> using the <b>backup passphrase</b> (separate
from this kit's), then <code>docker compose up -d</code>. The app's encryption key file is <code>secrets.key</code> inside
the backup - without it vault files cannot be opened. If this is unfamiliar, hand this page to a technical friend; the
documents themselves do not need the app (step 1).</p>
<h2>4. Keep this safe</h2><p>Anyone with the passphrase and the kit file can read everything in it. Store the passphrase
separately from the file.</p></body></html>""" % {
        "gen": e(generated), "n": item_count, "contacts": contacts,
        "exec": e(str(binder.get("executor_name") or "-")), "will": e(str(binder.get("will_location") or "-"))}


# ---------------------------------------------------------------- build / deliver
def build(binder, passphrase=None):
    """Build the kit zip. Returns {"passphrase", "download_token", "item_count", "size", "name"}.
    The passphrase is returned to the caller exactly once and kept nowhere."""
    if passphrase is not None and len(passphrase) < 16:
        raise KitError("a custom passphrase must be at least 16 characters")
    pw = passphrase or generate_passphrase()
    rows = _emergency_rows()
    if not rows:
        raise KitError("there are no emergency-visibility documents to put in a kit", 409)
    if sum(r["size"] for r in rows) > KIT_MAX_MB * 1024 * 1024:
        raise KitError(f"emergency documents exceed the {KIT_MAX_MB} MB kit size cap", 413)
    os.makedirs(KIT_TMP_DIR, mode=0o700, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    salt, nonce_prefix = secrets.token_bytes(16), secrets.token_bytes(4)
    header = json.dumps({"v": 1, "kdf": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
                         "salt": base64.b64encode(salt).decode(), "np": base64.b64encode(nonce_prefix).decode(),
                         "chunk": CHUNK}, sort_keys=True, separators=(",", ":")).encode()
    key = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32,
                         maxmem=256 * 1024 * 1024)
    kit_path = os.path.join(KIT_TMP_DIR, f"kit-{secrets.token_hex(6)}.fidkit")
    zip_path = os.path.join(KIT_TMP_DIR, f"kit-{secrets.token_hex(6)}.zip")
    try:
        fd = os.open(kit_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(MAGIC + struct.pack(">I", len(header)) + header)
            w = _ChunkWriter(out, key, header, nonce_prefix)
            with tarfile.open(fileobj=w, mode="w|") as tar:
                manifest, used = [], set()
                for r in rows:
                    data = vault.read_plaintext(r)            # integrity-checked; in memory one item at a time
                    it = vault.public_item(r)
                    name = _safe_name(it["filename"] or it["title"])
                    path = f"items/{_safe_name(it['folder'])}/{name}"
                    n = 1
                    while path in used:
                        n += 1
                        path = f"items/{_safe_name(it['folder'])}/{n}-{name}"
                    used.add(path)
                    manifest.append({"path": path, "title": it["title"], "folder": it["folder"],
                                     "doc_type": it["doc_type"], "mime": it["mime"], "size": len(data),
                                     "sha256": hashlib.sha256(data).hexdigest(), "expires_on": it["expires_on"]})
                    ti = tarfile.TarInfo(path)
                    ti.size, ti.mtime = len(data), int(time.time())
                    tar.addfile(ti, io.BytesIO(data))
                    del data
                for fname, blob in (
                        ("manifest.json", json.dumps({"generated": stamp, "items": manifest}, indent=2).encode()),
                        ("binder.json", json.dumps(binder, indent=2, default=str).encode()),
                        ("binder.txt", _binder_text(binder).encode())):
                    ti = tarfile.TarInfo(fname)
                    ti.size, ti.mtime = len(blob), int(time.time())
                    tar.addfile(ti, io.BytesIO(blob))
            w.close_last()
        zname = f"fiduciary-emergency-kit-{stamp}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
            z.write(kit_path, f"fiduciary-emergency-{stamp}.fidkit")
            z.write(os.path.join(os.path.dirname(os.path.abspath(__file__)), "kit_decrypt.py"), "kit_decrypt.py")
            z.writestr("INSTRUCTIONS.html", instructions_html(binder, len(rows), time.strftime("%Y-%m-%d %H:%M")))
        os.chmod(zip_path, 0o600)
        size = os.path.getsize(zip_path)
    finally:
        try:
            os.remove(kit_path)
        except OSError:
            pass
    with store._conn() as c:
        c.execute("UPDATE vault_kit_meta SET generated_at=?, item_hash=?, item_count=?, size=? WHERE id=1",
                  (time.time(), item_set_hash(rows), len(rows), size))
    token = secrets.token_urlsafe(32)
    with _tok_lock:
        _purge_expired()
        _tokens[token] = (zip_path, time.time() + TOKEN_TTL_S, zname)
    return {"passphrase": pw, "download_token": token, "item_count": len(rows), "size": size, "name": zname}


def _purge_expired():
    now = time.time()
    for t, (p, exp, _n) in list(_tokens.items()):
        if exp < now:
            _tokens.pop(t, None)
            try:
                os.remove(p)
            except OSError:
                pass


def claim(token):
    """Single-use: returns (path, download_name) and invalidates the token, or None."""
    with _tok_lock:
        _purge_expired()
        rec = _tokens.pop(token, None)
    if not rec:
        return None
    return rec[0], rec[2]
