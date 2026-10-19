#!/usr/bin/env python3
"""
kit_decrypt.py -- decrypt a Fiduciary Emergency Kit (*.fidkit) with NOTHING but a passphrase.
Needs only Python 3.8+ and the `cryptography` package:   pip install cryptography
Usage:   python kit_decrypt.py <kit.fidkit> <output-folder>      (prompts for the passphrase)

FORMAT (so it can be re-implemented in any language if this script is ever lost):
  bytes 0-7    magic  b"FIDKIT01"
  bytes 8-11   big-endian uint32 H = length of the header
  next H bytes header, UTF-8 JSON: {"v":1,"kdf":"scrypt","n":32768,"r":8,"p":1,
               "salt":<base64>,"np":<base64, 4 bytes>,"chunk":1048576}
  then chunks until end of file; each chunk = big-endian uint32 L, then L bytes of AES-256-GCM
  ciphertext+tag.
  key    = scrypt(passphrase as UTF-8, salt, n, r, p, dklen=32)
  nonce  = np (4 bytes) || chunk counter as big-endian uint64, counter starts at 0
  aad    = b"FIDKIT01" + header bytes + (b"\x01" if this is the LAST chunk else b"\x00")
  Concatenating the decrypted chunks gives a plain TAR archive: manifest.json, binder.json,
  binder.txt and items/<folder>/<file>. Every file's sha256 is listed in manifest.json.
Nothing here talks to the network or to the Fiduciary server.
"""
import base64
import getpass
import hashlib
import json
import os
import struct
import sys
import tarfile
import tempfile

MAGIC = b"FIDKIT01"


def _fail(msg):
    sys.stderr.write("ERROR: " + msg + "\n")
    sys.exit(1)


def decrypt_to_tar(path, passphrase, tar_path):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        _fail("the 'cryptography' package is required:  pip install cryptography")
    with open(path, "rb") as f:
        if f.read(8) != MAGIC:
            _fail("this is not a Fiduciary kit file (bad magic)")
        (hlen,) = struct.unpack(">I", f.read(4))
        hdr = f.read(hlen)
        h = json.loads(hdr.decode("utf-8"))
        if h.get("v") != 1 or h.get("kdf") != "scrypt":
            _fail("unsupported kit version")
        key = hashlib.scrypt(passphrase.encode("utf-8"), salt=base64.b64decode(h["salt"]),
                             n=h["n"], r=h["r"], p=h["p"], dklen=32, maxmem=256 * 1024 * 1024)
        aes = AESGCM(key)
        np_ = base64.b64decode(h["np"])
        counter = 0
        nxt = f.read(4)
        if len(nxt) != 4:
            _fail("kit file is truncated")
        with open(tar_path, "wb") as out:
            while nxt:
                (clen,) = struct.unpack(">I", nxt)
                ct = f.read(clen)
                if len(ct) != clen:
                    _fail("kit file is truncated")
                nxt = f.read(4)
                last = b"\x01" if not nxt else b"\x00"
                try:
                    out.write(aes.decrypt(np_ + struct.pack(">Q", counter), ct, MAGIC + hdr + last))
                except Exception:
                    _fail("wrong passphrase, or the kit file is damaged/tampered")
                counter += 1


def safe_extract(tar_path, outdir):
    os.makedirs(outdir, exist_ok=True)
    root = os.path.realpath(outdir)
    n = 0
    with tarfile.open(tar_path, "r:") as t:
        for m in t:
            if not (m.isfile() or m.isdir()):
                continue                              # no links/devices
            dest = os.path.realpath(os.path.join(root, m.name))
            if dest != root and not dest.startswith(root + os.sep):
                _fail("kit contains an unsafe path: " + m.name)
            if m.isdir():
                os.makedirs(dest, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with t.extractfile(m) as src, open(dest, "wb") as dst:
                while True:
                    b = src.read(1 << 20)
                    if not b:
                        break
                    dst.write(b)
            n += 1
    return n


def verify_manifest(outdir):
    try:
        man = json.load(open(os.path.join(outdir, "manifest.json"), encoding="utf-8"))
    except Exception:
        return None
    bad = 0
    for it in man.get("items", []):
        p = os.path.join(outdir, it["path"])
        try:
            ok = hashlib.sha256(open(p, "rb").read()).hexdigest() == it["sha256"]
        except OSError:
            ok = False
        if not ok:
            bad += 1
            sys.stderr.write("CHECKSUM MISMATCH: " + it["path"] + "\n")
    return bad


def main(argv):
    if len(argv) != 3:
        print(__doc__.split("FORMAT")[0].strip())
        return 2
    kit, outdir = argv[1], argv[2]
    pw = os.environ.get("KIT_PASSPHRASE") or getpass.getpass("Kit passphrase (type it exactly as written): ")
    fd, tmp = tempfile.mkstemp(suffix=".tar")
    os.close(fd)
    try:
        decrypt_to_tar(kit, pw, tmp)
        n = safe_extract(tmp, outdir)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    bad = verify_manifest(outdir)
    print(f"Decrypted {n} file(s) into {outdir}" + ("" if bad is None else
          (" - all checksums OK" if bad == 0 else f" - {bad} CHECKSUM PROBLEM(S)")))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
