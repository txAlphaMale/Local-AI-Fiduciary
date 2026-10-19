# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
backup.py -- SEC-7a (Backup & Disaster Recovery), UI-triggered path (2026-07-27).

Native Python implementation of the SAME encryption format scripts/backup.sh (host CLI, via
`openssl enc -aes-256-cbc -pbkdf2 -iter 300000 -salt`) already produces -- deliberately NOT a
different format, so a backup made by clicking a button in the app and one made by the systemd
timer / scripts/backup.sh are fully interchangeable, restorable by either path. Uses the
`cryptography` library (already a pinned dependency, requirements.txt) instead of shelling out to
the `openssl` CLI -- no new binary dependency inside the container, no Docker build-context change
needed to reach scripts/backup.sh (which lives outside portfolio-api/'s build context).

COMPATIBILITY VERIFIED (2026-07-27, before this was wired into any endpoint): a real openssl-
encrypted archive was decrypted with this exact KDF/cipher construction, AND an archive encrypted
with this construction was decrypted by the real `openssl enc -d` CLI -- both directions confirmed
byte-identical round trips. The format: 8-byte literal "Salted__" + 8-byte random salt + AES-256-CBC
ciphertext (PKCS7-padded), key material = PBKDF2-HMAC-SHA256(passphrase, salt, 300_000 iters,
48 bytes) split into a 32-byte key + 16-byte IV -- exactly what `openssl enc -pbkdf2` does with no
`-md` override (SHA256 is its default PBKDF2 PRF).

RESTORE IS DELIBERATELY STAGED, NOT LIVE: stage_restore() extracts into a separate staging
directory, never overwrites the running /data in place. A true one-click "restore over the live
volume while the app is running" would need the app to quiesce open DB connections mid-request and
restart into a restore-mode boot sequence -- meaningfully riskier to get right blind (this module
was written and tested without a live container to verify that flow against), and a wrong restore
is a much worse failure mode than a wrong backup. Completing a real restore today still means: stop
the container, copy the staged files over /data, start the container back up -- same manual final
step scripts/restore.sh always required, just with the decrypt/extract done for you.
"""
import io
import os
import re
import tarfile
import time

from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_PBKDF2_ITERATIONS = 300_000
_MAGIC = b"Salted__"
_NAME_RE = re.compile(r"^fiduciary-backup-(\d{8}T\d{6}Z)\.tar\.gz\.enc$")


def _derive_key_iv(passphrase: bytes, salt: bytes):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=48, salt=salt, iterations=_PBKDF2_ITERATIONS)
    key_iv = kdf.derive(passphrase)
    return key_iv[:32], key_iv[32:48]


def _encrypt(plaintext: bytes, passphrase: str) -> bytes:
    salt = os.urandom(8)
    key, iv = _derive_key_iv(passphrase.encode("utf-8"), salt)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return _MAGIC + salt + ciphertext


def _decrypt(blob: bytes, passphrase: str) -> bytes:
    if blob[:8] != _MAGIC:
        raise ValueError("not a recognized backup file (missing 'Salted__' header) -- wrong file, "
                          "or corrupted")
    salt, ciphertext = blob[8:16], blob[16:]
    key, iv = _derive_key_iv(passphrase.encode("utf-8"), salt)
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    try:
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except Exception as e:
        raise ValueError(f"decrypt failed -- wrong passphrase, or the file is corrupted ({e})")


def _tar_directory(src_dir: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(src_dir, arcname=".")
    return buf.getvalue()


def run_backup(passphrase: str, src_dir: str, out_dir: str, retention_days: int = 0) -> dict:
    """Same output naming/format as scripts/backup.sh: fiduciary-backup-<UTC timestamp>.tar.gz.enc
    + a .sha256 sidecar in out_dir. Returns a manifest dict; raises on any failure (caller decides
    how to surface it -- see app.py's POST /api/backup/run)."""
    if not passphrase:
        raise ValueError("no backup passphrase configured (SETTINGS -> SECRETS -> "
                          "\"Backup passphrase\")")
    if not os.path.isdir(src_dir):
        raise ValueError(f"source directory '{src_dir}' does not exist or is not reachable")
    os.makedirs(out_dir, exist_ok=True)

    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    fname = f"fiduciary-backup-{ts}.tar.gz.enc"
    enc_path = os.path.join(out_dir, fname)

    plaintext_tar = _tar_directory(src_dir)
    encrypted = _encrypt(plaintext_tar, passphrase)

    tmp_path = enc_path + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(encrypted)
    os.replace(tmp_path, enc_path)  # atomic on the same filesystem -- no half-written file visible

    import hashlib
    digest = hashlib.sha256(encrypted).hexdigest()
    with open(enc_path + ".sha256", "w") as f:
        f.write(f"{digest}  {fname}\n")

    pruned = []
    if retention_days and retention_days > 0:
        cutoff = time.time() - retention_days * 86400
        for name in os.listdir(out_dir):
            if not _NAME_RE.match(name.replace(".sha256", "")) and not name.endswith(".tar.gz.enc"):
                continue
            path = os.path.join(out_dir, name)
            if name == fname or name == fname + ".sha256":
                continue  # never prune what we just wrote
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    pruned.append(name)
            except OSError:
                pass

    return {"file": fname, "path": enc_path, "size_bytes": len(encrypted), "sha256": digest,
            "pruned": pruned}


def list_backups(out_dir: str) -> list:
    """Existing archives, newest first. Tolerant of a missing/empty directory (returns [])."""
    if not os.path.isdir(out_dir):
        return []
    out = []
    for name in os.listdir(out_dir):
        m = _NAME_RE.match(name)
        if not m:
            continue
        path = os.path.join(out_dir, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append({"file": name, "size_bytes": st.st_size, "mtime": int(st.st_mtime),
                    "timestamp": m.group(1)})
    out.sort(key=lambda r: r["timestamp"], reverse=True)
    return out


def stage_restore(passphrase: str, archive_path: str, staging_dir: str) -> dict:
    """Decrypts + extracts archive_path into staging_dir (created fresh -- refuses if it already
    exists and is non-empty, same non-clobber safety scripts/restore.sh already has). Does NOT
    touch the live /data -- see this module's docstring for why a true live restore isn't built
    here. Returns the list of extracted file paths (relative) for the caller to show."""
    if not os.path.isfile(archive_path):
        raise ValueError(f"backup file not found: {archive_path}")
    if os.path.isdir(staging_dir) and os.listdir(staging_dir):
        raise ValueError(f"staging directory '{staging_dir}' already exists and is not empty -- "
                          "remove it first or choose a different staging path")

    sidecar = archive_path + ".sha256"
    if os.path.isfile(sidecar):
        import hashlib
        with open(archive_path, "rb") as f:
            actual = hashlib.sha256(f.read()).hexdigest()
        with open(sidecar) as f:
            expected = f.read().split()[0]
        if actual != expected:
            raise ValueError("checksum mismatch -- this backup file may be corrupted")

    with open(archive_path, "rb") as f:
        blob = f.read()
    plaintext_tar = _decrypt(blob, passphrase)

    os.makedirs(staging_dir, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(plaintext_tar), mode="r:gz") as tf:
        # Guard against a maliciously-crafted archive escaping staging_dir via ".." path
        # components -- defense in depth even though these archives are self-produced.
        base = os.path.abspath(staging_dir)
        for member in tf.getmembers():
            member_path = os.path.abspath(os.path.join(staging_dir, member.name))
            if not member_path.startswith(base + os.sep) and member_path != base:
                raise ValueError(f"refusing to extract: unsafe path in archive ({member.name})")
        tf.extractall(staging_dir)

    extracted = []
    for root, _dirs, files in os.walk(staging_dir):
        for fn in files:
            extracted.append(os.path.relpath(os.path.join(root, fn), staging_dir))
    return {"staging_dir": staging_dir, "files": sorted(extracted)}
