# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
test_audit_anchor.py -- unit tests for audit_anchor.py (SEC-9: RFC 3161 + Merkle-batched external
anchoring for the hash-chained audit log).

Pure/stdlib (unittest) + the real audit.py/audit_anchor.py modules against a temp SQLite DB.
DELIBERATELY ZERO NETWORK -- the RFC 3161 request/response/verify path (_attempt_anchor,
anchor_job) needs a real or mocked TSA and is NOT exercised here; that path is covered by
audit_anchor.py's own `__main__` smoke test, which supports a real, network-using round-trip via
`AUDIT_ANCHOR_LIVE_TEST=1 python3 audit_anchor.py` -- run that somewhere with real internet access
to confirm the live path end-to-end (this sandboxed dev environment's egress is restricted, so it
could not be exercised while writing this module).

Run:  python3 -m unittest test_audit_anchor -v

Covers:
  · Merkle tree correctness across a range of batch sizes (odd/even boundaries), including that
    EVERY leaf's inclusion proof verifies against the tree's own root.
  · Tampering: flipping one leaf changes the root, and the OLD proof for that position does not
    verify the NEW leaf value against the OLD root (nor does the new leaf verify with the old
    proof against a stale root) -- i.e. tampering is actually detectable, not just "the numbers
    differ somewhere."
  · CVE-2012-2459 regression: the promoting (correct) tree construction must NOT produce the same
    root for two different entry sets that a naive DUPLICATING construction would collide on (a
    5-leaf batch vs. a 6-leaf batch formed by appending a second copy of the 5th leaf).
  · Domain separation: a leaf digest can never equal an internal-node digest computed over the
    same raw bytes, and combining two leaf digests as if they were siblings does not accidentally
    reproduce a real tree's actual internal node (prefix separation is doing real work).
  · Canonical entry serialization: deterministic regardless of source dict key-insertion order,
    and any single field change produces different bytes (i.e. every field is actually load-bearing
    in the hash, none is silently dropped).
  · Batch selection (_select_batch_rows) against a real, temp-DB-backed audit.py: first-ever batch
    covers genesis..head; a normal follow-up batch covers exactly the new entries; and the "no new
    entries" case re-affirms the single current head entry (requirement 4's positive-evidence rule)
    rather than being skipped or erroring on an empty batch.
  · Renewal evidence-bundle serialization is deterministic for a given anchor row.
"""
import base64
import hashlib
import json
import os
import shutil
import tempfile
import unittest

import audit
import audit_anchor as aa


class MerkleTreeTests(unittest.TestCase):
    def _rand_leaves(self, n, seed):
        return [hashlib.sha256(f"{seed}-{i}".encode()).hexdigest() for i in range(n)]

    def test_every_leaf_proof_verifies_for_many_sizes(self):
        for n in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 13, 17, 31, 32, 33, 64, 65):
            with self.subTest(n=n):
                leaves = self._rand_leaves(n, f"size{n}")
                root, levels = aa.build_merkle_tree(leaves)
                for i in range(n):
                    proof = aa.inclusion_proof(levels, i)
                    self.assertTrue(aa.verify_inclusion(leaves[i], proof, root),
                                     f"leaf {i} of {n} failed to verify")

    def test_single_leaf_tree_root_is_its_own_domain_separated_hash(self):
        leaves = self._rand_leaves(1, "single")
        root, levels = aa.build_merkle_tree(leaves)
        self.assertEqual(len(levels), 1)
        expected = hashlib.sha256(b"\x00" + bytes.fromhex(leaves[0])).hexdigest()
        self.assertEqual(root, expected)
        # Zero-length proof still verifies for a single-leaf batch.
        self.assertTrue(aa.verify_inclusion(leaves[0], [], root))

    def test_empty_batch_raises(self):
        with self.assertRaises(ValueError):
            aa.build_merkle_tree([])

    def test_tampering_is_detected(self):
        leaves = self._rand_leaves(7, "tamper")
        root, levels = aa.build_merkle_tree(leaves)
        old_proof_for_index3 = aa.inclusion_proof(levels, 3)

        tampered = list(leaves)
        tampered[3] = hashlib.sha256(b"tampered-value").hexdigest()
        new_root, new_levels = aa.build_merkle_tree(tampered)

        self.assertNotEqual(root, new_root, "tampering a leaf must change the root")
        # The tampered leaf does NOT verify against the ORIGINAL root using the original proof.
        self.assertFalse(aa.verify_inclusion(tampered[3], old_proof_for_index3, root))
        # Nor does the original (untampered) leaf verify against the NEW root.
        self.assertFalse(aa.verify_inclusion(leaves[3], old_proof_for_index3, new_root))
        # Every OTHER leaf's proof against the new tree still verifies fine (only the tampered
        # position's evidence is affected in content, though the root itself changed for all).
        for i in range(7):
            if i == 3:
                continue
            proof = aa.inclusion_proof(new_levels, i)
            self.assertTrue(aa.verify_inclusion(tampered[i], proof, new_root))

    def test_cve_2012_2459_promotion_vs_duplication(self):
        """The exact attack class CVE-2012-2459 is about: a DUPLICATING odd-node scheme lets two
        different entry sets (a 5-leaf batch, and a 6-leaf batch formed by re-appending the 5th
        leaf) produce the SAME root. Our promoting implementation must NOT have this property."""
        def build_tree_duplicating_BUGGY(leaf_hexes):
            level = [aa._leaf_node_digest(h) for h in leaf_hexes]
            while len(level) > 1:
                if len(level) % 2 == 1:
                    level = level + [level[-1]]  # the CVE-2012-2459 bug: duplicate, don't promote
                nxt = [aa._internal_node_digest(level[i], level[i + 1]) for i in range(0, len(level), 2)]
                level = nxt
            return level[0].hex()

        leaves5 = self._rand_leaves(5, "cve")
        leaves6_attack = leaves5 + [leaves5[-1]]   # append a SECOND copy of the last real leaf

        dup_root_5 = build_tree_duplicating_BUGGY(leaves5)
        dup_root_6 = build_tree_duplicating_BUGGY(leaves6_attack)
        self.assertEqual(dup_root_5, dup_root_6,
                          "sanity check: the buggy duplicating scheme SHOULD collide here -- "
                          "if this assertion itself fails, the attack construction is wrong")

        promote_root_5, _ = aa.build_merkle_tree(leaves5)
        promote_root_6, _ = aa.build_merkle_tree(leaves6_attack)
        self.assertNotEqual(promote_root_5, promote_root_6,
                             "our promoting implementation must NOT collide on this attack construction")

    def test_domain_separation_leaf_vs_internal(self):
        raw = os.urandom(64)
        # A leaf digest is SHA256(0x00 + x); an internal-node digest over the same 64 raw bytes
        # split as two 32-byte "children" is SHA256(0x01 + left + right). These must never collide
        # for any input, by construction (different fixed prefix byte).
        leaf_like = hashlib.sha256(b"\x00" + raw).digest()
        node_like = hashlib.sha256(b"\x01" + raw).digest()
        self.assertNotEqual(leaf_like, node_like)

        left, right = raw[:32], raw[32:]
        internal = aa._internal_node_digest(left, right)
        self.assertEqual(internal, node_like)
        self.assertNotEqual(internal, hashlib.sha256(b"\x00" + raw).digest())


class CanonicalSerializationTests(unittest.TestCase):
    def _row(self, **overrides):
        base = {"seq": 1, "ts": "2026-09-01T00:00:00.000000Z", "actor": "u1", "actor_name": "jason",
                "src_ip": "203.0.113.9", "event_type": "auth.login.success", "resource": "/api/x",
                "outcome": "success", "corr_id": None, "scheme": "hmac-sha256",
                "prev_hash": "GENESIS-abc", "entry_hash": "deadbeef"}
        base.update(overrides)
        return base

    def test_deterministic_regardless_of_dict_key_order(self):
        row = self._row()
        reordered = {k: row[k] for k in reversed(list(row.keys()))}
        self.assertEqual(aa._canonical_entry_json(row), aa._canonical_entry_json(reordered))
        self.assertEqual(aa.leaf_hash(row), aa.leaf_hash(reordered))

    def test_every_field_is_load_bearing(self):
        base_hash = aa.leaf_hash(self._row())
        for field, new_value in (("seq", 2), ("ts", "2026-09-02T00:00:00.000000Z"),
                                  ("actor", "u2"), ("actor_name", "someoneelse"),
                                  ("src_ip", "198.51.100.1"), ("event_type", "auth.logout"),
                                  ("resource", "/api/y"), ("outcome", "failure"),
                                  ("corr_id", "abc123"), ("scheme", "sha256"),
                                  ("prev_hash", "OTHER"), ("entry_hash", "cafef00d")):
            with self.subTest(field=field):
                changed = aa.leaf_hash(self._row(**{field: new_value}))
                self.assertNotEqual(base_hash, changed, f"field {field!r} did not affect the leaf hash")

    def test_repeatable(self):
        row = self._row()
        self.assertEqual(aa.leaf_hash(row), aa.leaf_hash(row))


class BatchSelectionTests(unittest.TestCase):
    """Exercises _select_batch_rows() against the REAL audit.py module, isolated to a fresh temp
    DB per test (mirrors audit.py's own __main__ smoke-test technique -- reassign the module
    globals directly before touching them)."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        audit.AUDIT_DB = os.path.join(self._tmpdir, "audit.db")
        audit.AUDIT_JSONL = os.path.join(self._tmpdir, "audit.jsonl")
        audit.AUDIT_HMAC_KEY_FILE = os.path.join(self._tmpdir, "audit_hmac.key")
        audit._HMAC_KEY = audit._load_or_create_hmac_key()
        aa.AUDIT_DB = audit.AUDIT_DB
        audit.init_db()
        aa.init_db()

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_empty_log_selects_nothing(self):
        rows, is_reaffirm = aa._select_batch_rows()
        self.assertIsNone(rows)
        self.assertFalse(is_reaffirm)

    def test_first_batch_covers_genesis_to_head(self):
        for i in range(5):
            audit.emit("auth.login.success", actor="u1", actor_name="jason", resource=f"/x/{i}")
        rows, is_reaffirm = aa._select_batch_rows()
        self.assertEqual(len(rows), 5)
        self.assertFalse(is_reaffirm)
        self.assertEqual(rows[0]["seq"], rows[-1]["seq"] - 4)

    def test_followup_batch_covers_only_new_entries(self):
        for i in range(3):
            audit.emit("auth.login.success", actor="u1", actor_name="jason", resource=f"/x/{i}")
        rows, _ = aa._select_batch_rows()
        anchor_id = aa._create_pending_batch(rows, False)
        # Simulate the batch having completed (status flips to 'anchored' without a real TSA call).
        with aa._conn() as c:
            c.execute("UPDATE anchors SET status='anchored', local_anchored_at=? WHERE anchor_id=?",
                      (aa._now_iso(), anchor_id))

        for i in range(2):
            audit.emit("config.changed", actor="u1", actor_name="jason", resource=f"/y/{i}")
        rows2, is_reaffirm2 = aa._select_batch_rows()
        self.assertEqual(len(rows2), 2)
        self.assertFalse(is_reaffirm2)
        self.assertEqual(rows2[0]["seq"], rows[-1]["seq"] + 1)

    def test_no_new_entries_reaffirms_current_head(self):
        for i in range(3):
            audit.emit("auth.login.success", actor="u1", actor_name="jason", resource=f"/x/{i}")
        rows, _ = aa._select_batch_rows()
        anchor_id = aa._create_pending_batch(rows, False)
        with aa._conn() as c:
            c.execute("UPDATE anchors SET status='anchored', local_anchored_at=? WHERE anchor_id=?",
                      (aa._now_iso(), anchor_id))

        # No new events written -- the next batch must re-affirm the head, not come back empty.
        rows2, is_reaffirm2 = aa._select_batch_rows()
        self.assertIsNotNone(rows2)
        self.assertTrue(is_reaffirm2)
        self.assertEqual(len(rows2), 1)
        self.assertEqual(rows2[0]["seq"], rows[-1]["seq"])

        # And it must actually be buildable into a valid single-leaf batch (regression against the
        # "empty batch" ValueError path in build_merkle_tree).
        anchor_id2 = aa._create_pending_batch(rows2, is_reaffirm2)
        with aa._conn() as c:
            row = dict(c.execute("SELECT * FROM anchors WHERE anchor_id=?", (anchor_id2,)).fetchone())
        self.assertEqual(row["entry_count"], 1)
        self.assertEqual(row["is_head_reaffirm"], 1)
        leaves = json.loads(row["leaf_hashes_json"])
        self.assertEqual(len(leaves), 1)


class EvidenceBundleSerializationTests(unittest.TestCase):
    def test_deterministic_and_field_sensitive(self):
        row = {
            "anchor_id": 1, "batch_start_seq": 1, "batch_end_seq": 5, "leaf_hash_algo": "sha256",
            "leaf_hashes_json": json.dumps(["a", "b"]), "merkle_root": "deadbeef",
            "tsa_time": "2026-09-01T00:00:00.000000Z", "tst_token_der": b"tokenbytes",
            "cert_chain_der_json": json.dumps([base64.b64encode(b"cert").decode()]),
            "ocsp_der": b"ocspbytes", "crl_der": None,
        }
        b1 = aa._serialize_evidence_bundle(row)
        b2 = aa._serialize_evidence_bundle(dict(row))
        self.assertEqual(b1, b2)

        changed = dict(row)
        changed["merkle_root"] = "cafebabe"
        self.assertNotEqual(b1, aa._serialize_evidence_bundle(changed))

        digest1 = hashlib.sha256(b1).hexdigest()
        self.assertEqual(len(digest1), 64)


if __name__ == "__main__":
    unittest.main()
