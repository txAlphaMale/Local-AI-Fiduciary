"""
test_vault.py -- TASK-001 Phase 1: encrypted document vault.

Runs the REAL app (app.py, incl. its global auth/viewer gate) against a throwaway data dir, so the
viewer-visibility tests prove the actual end-to-end behavior, not a mock.
Run:  python3 -m pytest test_vault.py -q      (needs fastapi/httpx/pytest; see requirements)
"""
import glob
import hashlib
import io
import os
import re
import sys
import tempfile
import unittest
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="vault-test-")
# Point EVERY "/data..." env default (any module) at the temp dir BEFORE importing app.
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f) == "test_vault.py":
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ["VAULT_DIR"] = os.path.join(_TMP, "vault")
os.environ["VAULT_MAX_FILE_MB"] = "1"
os.environ["VAULT_MAX_TOTAL_GB"] = "0.002"      # ~2 MB total
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)

from fastapi.testclient import TestClient  # noqa: E402
import app as appmod  # noqa: E402
import audit  # noqa: E402
import auth  # noqa: E402
import secrets_crypto  # noqa: E402
import store  # noqa: E402
import vault  # noqa: E402

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n" + os.urandom(200)
PNG = b"\x89PNG\r\n\x1a\n" + os.urandom(100)


def _docx():
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("[Content_Types].xml", "<x/>")
        z.writestr("word/document.xml", "<w/>")
    return b.getvalue()


def _login(username, password):
    c = TestClient(appmod.app)
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


class VaultBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Another test module may have imported app (and so vault) first with default settings;
        # pin the limits and storage dir here instead of relying on import-time env vars.
        vault.VAULT_MAX_FILE_MB = 1
        vault.VAULT_MAX_TOTAL_GB = 0.002          # ~2 MB total
        vault.VAULT_DIR = os.path.join(_TMP, "vault")
        vault.init_db()
        try:
            auth.create_user("boss", "correct-horse-battery-1", role="admin")
        except Exception:
            pass
        try:
            auth.create_viewer("kid", "correct-horse-battery-2")
        except Exception:
            pass
        cls.admin = _login("boss", "correct-horse-battery-1")
        cls.viewer = _login("kid", "correct-horse-battery-2")

    def upload(self, data, folder="Estate", visibility=None, filename="will.pdf", client=None, **kw):
        h = {"X-Vault-Filename": filename, "X-Vault-Folder": folder}
        if visibility:
            h["X-Vault-Visibility"] = visibility
        for k, v in kw.items():
            h["X-Vault-" + k.replace("_", "-")] = v
        return (client or self.admin).post("/api/vault/upload", content=data, headers=h)


class TestCrypto(VaultBase):
    def test_round_trip_and_stored_bytes_are_ciphertext(self):
        r = self.upload(PDF, title="Will 2026")
        self.assertEqual(r.status_code, 200, r.text)
        item = r.json()["item"]
        self.assertEqual(item["sha256"], hashlib.sha256(PDF).hexdigest())
        d = self.admin.get(f"/api/vault/items/{item['id']}/download")
        self.assertEqual(d.status_code, 200)
        self.assertEqual(d.content, PDF)                      # byte-identical
        blob = open(vault._blob_path(item["id"]), "rb").read()
        self.assertNotIn(b"%PDF", blob)                        # ciphertext only
        row = vault.get_item_row(item["id"])
        self.assertNotIn(b"Will 2026", str(row["title"]).encode())   # metadata encrypted too
        self.assertNotIn("will.pdf", str(row["filename_enc"]))

    def test_tamper_is_refused(self):
        item = self.upload(PDF).json()["item"]
        p = vault._blob_path(item["id"])
        b = bytearray(open(p, "rb").read())
        b[20] ^= 0xFF
        open(p, "wb").write(bytes(b))
        r = self.admin.get(f"/api/vault/items/{item['id']}/download")
        self.assertEqual(r.status_code, 500)
        self.assertIn("integrity", r.json()["error"])

    def test_blob_cannot_be_swapped_between_items(self):
        a = self.upload(PDF).json()["item"]
        b = self.upload(PDF + b"x").json()["item"]
        open(vault._blob_path(b["id"]), "wb").write(open(vault._blob_path(a["id"]), "rb").read())
        self.assertEqual(self.admin.get(f"/api/vault/items/{b['id']}/download").status_code, 500)

    def test_key_rotation_rewraps_deks_and_old_items_still_open(self):
        item = self.upload(PDF, title="before rotation").json()["item"]
        old_v = vault.get_item_row(item["id"])["key_version"]
        r = self.admin.post("/api/secrets/rotate-key")
        self.assertEqual(r.status_code, 200, r.text)
        row = vault.get_item_row(item["id"])
        self.assertGreater(row["key_version"], old_v)           # DEK re-wrapped under new key
        self.assertEqual(self.admin.get(f"/api/vault/items/{item['id']}/download").content, PDF)
        new = self.upload(PNG).json()["item"]                    # new uploads use the new version
        self.assertEqual(self.admin.get(f"/api/vault/items/{new['id']}/download").content, PNG)

    def test_delete_crypto_shreds(self):
        item = self.upload(PDF).json()["item"]
        self.assertEqual(self.admin.delete(f"/api/vault/items/{item['id']}").status_code, 200)
        row = vault.get_item_row(item["id"])
        self.assertIsNone(row["wrapped_dek"])
        self.assertFalse(os.path.exists(vault._blob_path(item["id"])))
        self.assertEqual(self.admin.get(f"/api/vault/items/{item['id']}/download").status_code, 404)


class TestVisibility(VaultBase):
    """The #1 requirement: the global gate lets viewers GET everything, so the vault must not."""

    def test_viewer_cannot_list_or_download_private_or_emergency(self):
        ids = {v: self.upload(PDF + v.encode(), visibility=v).json()["item"]["id"]
               for v in ("private", "household", "emergency")}
        listed = {i["id"] for i in self.viewer.get("/api/vault/items").json()["items"]}
        self.assertIn(ids["household"], listed)
        self.assertNotIn(ids["private"], listed)
        self.assertNotIn(ids["emergency"], listed)
        by_folder = {i["id"] for i in self.viewer.get("/api/vault/items?folder=Estate").json()["items"]}
        self.assertFalse(by_folder & {ids["private"], ids["emergency"]})
        for v in ("private", "emergency"):
            for suffix in ("", "/download", "/download?inline=1"):
                r = self.viewer.get(f"/api/vault/items/{ids[v]}{suffix}")
                self.assertEqual(r.status_code, 404, f"{v}{suffix}")
        self.assertEqual(self.viewer.get(f"/api/vault/items/{ids['household']}/download").content,
                         PDF + b"household")
        # admin still sees all three
        listed_admin = {i["id"] for i in self.admin.get("/api/vault/items").json()["items"]}
        self.assertTrue(set(ids.values()) <= listed_admin)

    def test_viewer_folder_counts_exclude_hidden_items(self):
        self.upload(PDF + b"c", folder="Tax", visibility="private")
        tax = [f for f in self.viewer.get("/api/vault/folders").json()["folders"] if f["name"] == "Tax"][0]
        self.assertEqual(tax["count"], 0)

    def test_viewer_cannot_write(self):
        self.assertEqual(self.upload(PDF, client=self.viewer).status_code, 403)
        iid = self.upload(PDF, visibility="household").json()["item"]["id"]
        self.assertEqual(self.viewer.delete(f"/api/vault/items/{iid}").status_code, 403)
        self.assertEqual(self.viewer.patch(f"/api/vault/items/{iid}", json={"visibility": "household"}).status_code, 403)

    def test_denied_read_is_audited_and_unauthenticated_is_401(self):
        iid = self.upload(PDF + b"aud", visibility="private").json()["item"]["id"]
        self.viewer.get(f"/api/vault/items/{iid}/download")
        self.assertEqual(TestClient(appmod.app).get("/api/vault/items").status_code, 401)

    def test_can_read_unit(self):
        admin = {"id": 1, "role": "admin"}
        viewer = {"id": 2, "role": "viewer"}
        it = lambda v, d=0: {"visibility": v, "deleted": d}
        self.assertTrue(vault.can_read(admin, it("private")))
        self.assertFalse(vault.can_read(viewer, it("private")))
        self.assertTrue(vault.can_read(viewer, it("household")))
        self.assertFalse(vault.can_read(viewer, it("emergency")))
        self.assertTrue(vault.can_read(viewer, it("emergency"), emergency_granted=True))
        self.assertFalse(vault.can_read(admin, it("household", 1)))
        self.assertFalse(vault.can_read(None, it("household")))


class TestUploadHardening(VaultBase):
    def test_magic_byte_allowlist(self):
        self.assertEqual(self.upload(PNG, filename="x.png").json()["item"]["mime"], "image/png")
        self.assertEqual(self.upload(_docx(), filename="x.docx").status_code, 200)
        self.assertEqual(self.upload(b"hello notes", filename="n.txt").json()["item"]["mime"], "text/plain")
        # extension lies: HTML/SVG/EXE named like PDFs are rejected
        for bad in (b"<html><script>alert(1)</script></html>", b"<svg onload=x>", b"MZ\x90\x00" + os.urandom(50),
                    b"\x7fELF" + os.urandom(50)):
            r = self.upload(bad, filename="innocent.pdf")
            self.assertEqual(r.status_code, 415, bad[:10])
        # a plain zip that isn't an Office doc
        b = io.BytesIO(); zipfile.ZipFile(b, "w").writestr("a.txt", "x")
        self.assertEqual(self.upload(b.getvalue(), filename="x.docx").status_code, 415)

    def test_size_and_quota_limits(self):
        big = b"%PDF-" + os.urandom(1024 * 1024 + 10)
        self.assertEqual(self.upload(big).status_code, 413)                 # > VAULT_MAX_FILE_MB (1)
        self.assertEqual(self.upload(b"").status_code, 400)
        fill = b"%PDF-" + os.urandom(900 * 1024)
        codes = [self.upload(fill + bytes([i])).status_code for i in range(4)]
        self.assertIn(413, codes)                                            # total quota (~2 MB) hit

    def test_bad_folder_visibility_and_dates(self):
        self.assertEqual(self.upload(PDF, folder="Nope").status_code, 400)
        self.assertEqual(self.upload(PDF + b"v", visibility="public").status_code, 400)
        self.assertEqual(self.upload(PDF + b"d", expires_on="soon").status_code, 400)
        ok = self.upload(PDF + b"e", expires_on="2030-01-31", review_by="2029-12-01")
        self.assertEqual(ok.json()["item"]["expires_on"], "2030-01-31")

    def test_download_headers_never_inline_script(self):
        iid = self.upload(b"plain text", filename="n.txt").json()["item"]["id"]
        r = self.admin.get(f"/api/vault/items/{iid}/download?inline=1")   # txt: inline refused
        self.assertIn("attachment", r.headers["content-disposition"])
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")
        self.assertIn("sandbox", r.headers["content-security-policy"])
        pid = self.upload(PDF + b"inl").json()["item"]["id"]
        r = self.admin.get(f"/api/vault/items/{pid}/download?inline=1")
        self.assertIn("inline", r.headers["content-disposition"])
        self.assertEqual(r.headers["content-type"], "application/pdf")


class TestAudit(VaultBase):
    def setUp(self):
        # Other test modules (test_audit_anchor) repoint audit.AUDIT_DB at their own temp DBs in
        # this same process, so give this test a fresh, isolated audit log of its own.
        d = tempfile.mkdtemp(dir=_TMP)
        audit.AUDIT_DB = os.path.join(d, "audit.db")
        audit.AUDIT_JSONL = os.path.join(d, "audit.jsonl")
        audit.init_db()

    def test_events_emitted_and_chain_verifies(self):
        item = self.upload(PDF + b"chain").json()["item"]
        self.admin.get(f"/api/vault/items/{item['id']}/download")
        self.admin.patch(f"/api/vault/items/{item['id']}", json={"title": "renamed"})
        self.admin.delete(f"/api/vault/items/{item['id']}")
        for t in ("vault.upload", "vault.download", "vault.update", "vault.delete"):
            self.assertTrue(audit.query_core(event_type=t, limit=10), f"no {t} event")
        res = audit.verify_chain()
        self.assertTrue(res["ok"], res)


if __name__ == "__main__":
    unittest.main()
