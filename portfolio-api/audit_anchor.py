# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
audit_anchor.py -- SEC-9: RFC 3161 trusted timestamping with Merkle-batched external anchoring for
the hash-chained audit log (audit.py).

WHY: audit.py's HMAC hash chain (SEC-2) proves INTERNAL consistency -- if any stored event is
altered or deleted outside the documented retention exception, verify_chain() detects it. What it
CANNOT prove is WHEN the chain existed. An actor with full write access to this app's data
directory (a stolen backup, a compromised host, a malicious admin) could regenerate the ENTIRE
chain from scratch with different content, including a fresh AUDIT_HMAC_KEY_FILE, and the
regenerated chain would verify perfectly cleanly -- verify_chain() has no way to know the
difference between "this chain has existed since 2026-07-25" and "this chain was fabricated five
minutes ago to look that way." External anchoring closes exactly that gap: an independent third
party (a Time-Stamp Authority, RFC 3161) attests "this specific digest existed at this specific
time," and that attestation is cryptographically signed by a party who never had write access to
this app's data at all.

DESIGN, top to bottom:

  1. MERKLE BATCH BUILDER -- every audit-log entry since the last successful anchor becomes a leaf
     (SHA-256 over a fixed, versioned canonical JSON serialization -- see LEAF_SCHEMA_VERSION and
     _canonical_entry_json()). Leaves are paired up pairwise into a tree; an ODD node at any level
     is PROMOTED UNCHANGED to the next level, never duplicated (CVE-2012-2459: duplicating the last
     node of an odd level lets two DIFFERENT entry sets produce the SAME root -- e.g. a 5-leaf batch
     and a 6-leaf batch with the 5th leaf appended a second time -- which breaks the tree's binding
     to a unique, specific set of entries; promotion closes that off entirely, see build_merkle_tree
     docstring). Both leaf and internal nodes are hashed with a domain-separation prefix (0x00 vs
     0x01, the RFC 6962/Certificate-Transparency convention) so a leaf hash can never be replayed as
     a valid internal node or vice versa -- defense-in-depth beyond what CVE-2012-2459 alone
     requires. Every leaf's inclusion proof (ordered sibling hashes + left/right position) can be
     recomputed from the tree at proof time; what gets PERSISTED per anchor is just the ordered
     leaf-hash array, which is enough to deterministically rebuild the whole tree (and hence every
     proof) forever -- see ANCHOR STORAGE below for why that matters after a retention purge.

  2. RFC 3161 CLIENT (rfc3161-client, Trail of Bits) -- builds a TimeStampReq over the Merkle root
     (SHA-256 imprint, random nonce, certReq=true), POSTs it to a configured TSA, parses the
     TimeStampToken (CMS SignedData), and verifies it BEFORE anything is persisted: imprint matches
     our root, nonce matches what we sent, the CMS signature is valid, and the signing cert chains
     to a trusted root. IMPORTANT semantic note on this library's API: `.data(x)` takes the RAW
     bytes to be timestamped and HASHES THEM ITSELF with whatever `.hash_algorithm(...)` was set --
     it is NOT given an already-computed digest. We pass the raw 32 Merkle-root bytes; the library
     (and the TSA) hash them once more with SHA-256 to form the actual RFC 3161 messageImprint. That
     is exactly the intended protocol shape (the TSA is timestamping "our document", which happens
     to itself already be a hash) and it satisfies the constraint below: the TSA never receives
     anything but 32 bytes of hash material, never record content. We ALSO independently recompute
     SHA-256(root_bytes) ourselves and compare it to the token's own claimed imprint before trusting
     anything the verifier library concludes -- belt-and-suspenders against a misbehaving TSA.

     Trust root: if TSA_*_ROOT_CA_FILE is configured, that pinned PEM is the trust anchor (the
     right answer for production -- see CONFIG below). Otherwise we fall back to trust-on-first-use:
     the self-signed certificate found in the TSA's own returned chain (cert_req=true asks the TSA
     to embed its full chain). TOFU is weaker -- documented as such, not oversold -- but still means
     an anchor can't be silently fabricated without also compromising that specific TSA's signing
     key, which is already a huge step up from "an actor with DB write access alone."

  3. ANCHOR STORAGE -- one row per anchor, holding literally everything needed to re-verify it
     OFFLINE, forever, with no dependency on this app's config still existing or a network call
     still working: the ordered leaf-hash array (not the entries themselves -- a later retention
     purge of audit.py's events table must never invalidate an already-issued anchor's evidence),
     the Merkle root, the raw TimeStampToken DER bytes AND the full raw TimeStampResp DER bytes AS
     RECEIVED, the TSA's FULL certificate chain AS RECEIVED, whichever root certificate(s) were
     actually trusted at anchor time (so a later config change or lost pinned-root file can't orphan
     an old anchor's verifiability), an OCSP response (or CRL, as fallback) captured AT ANCHOR TIME,
     and the TSA-asserted time kept in a SEPARATE column from our own local clock's time. The cert-
     chain-and-revocation-data part is the one everyone forgets: an RFC 3161 token verifies against
     the signing cert as it was valid AT SIGNING TIME, not "currently" -- a cert that's long expired
     (or been revoked) by the time someone checks years later is exactly the normal, expected case,
     and without archiving what its validity looked like back then, a perfectly good token becomes
     practically unverifiable once its cert has cycled out of anyone's live trust store.

  4. TWO SEPARATE SCHEDULED JOBS, deliberately NOT coupled:
       - anchor_job() -- fixed daily schedule, UNCONDITIONAL. Retries any already-queued pending
         batch first (a TSA outage must never lose a batch -- see constraint below); only builds a
         new batch if nothing is currently pending. If there are no new entries since the last
         anchor, it still anchors the unchanged head as its own single-leaf batch -- POSITIVE
         evidence that nothing changed, not just silence.
       - verify_job() -- independent, more frequent by default. Recomputes audit.py's own internal
         chain, AND recomputes every completed anchor's Merkle root from its archived leaf hashes,
         AND re-verifies the archived token/cert-chain -- all offline, no network. This is the
         DETECTION path: the anchor job produces evidence, this job is what actually notices if
         something doesn't match, and alerts (via audit.emit, same pattern as SEC-4's
         detection.alert) on any mismatch.

  5. VERIFICATION API -- verify_entry(seq) and verify_range(start, end), each returning a
     structured result naming exactly which check failed (leaf recompute, inclusion proof, stored-
     root match, token/cert-chain re-verification) rather than a bare true/false.

  6. LONG-TERM VALIDATION -- _maybe_renew_stale_anchors(), run as part of the daily anchor job,
     re-anchors an archived evidence bundle with a FRESH RFC 3161 token once it's older than
     ANCHOR_RENEWAL_INTERVAL_DAYS (default 365), before the original signing cert or SHA-256 itself
     has any realistic chance of weakening. This is an ERS-STYLE renewal (RFC 4998 / ETSI archival
     timestamp renewal in SPIRIT), deliberately NOT full RFC 4998 ASN.1 Evidence Record syntax --
     that's a large, separate surface area (asn1crypto.tsp does model Evidence/ArchiveTimeStamp
     primitives, if a future upgrade wants full spec compliance). What we actually need -- proof
     that a given evidence bundle already existed before today -- is achieved just as soundly by:
     canonically serialize the ENTIRE original anchor's evidence (root, token, cert chain,
     revocation data -- see _serialize_evidence_bundle), hash it, and get a brand-new TSA token over
     THAT hash, linked back via the anchor_renewals table. The original merkle_root/leaf_hashes stay
     the thing actually being proven at every step; each renewal just re-attests "this exact
     original bundle already existed" using whatever crypto is strong today.

     Hash agility: every anchor row stores its own leaf_hash_algo + leaf_schema_version rather than
     assuming SHA-256/v1 forever. All recompute paths (build_merkle_tree, leaf_hash,
     verify_inclusion) take an algo parameter and dispatch through _LEAF_DOMAIN_SEP, so adding a new
     algorithm later is a one-entry addition to that registry, not a breaking change to every
     already-issued anchor.

CONSTRAINTS enforced throughout this module:
  * The TSA receives ONLY a hash (the Merkle root's bytes) -- never record content, never an
    identifier of any kind.
  * Anchor failure NEVER blocks writes to the audit log -- this module never touches audit.emit()'s
    write path; a stuck/unreachable TSA just means a batch stays 'pending' and gets retried by the
    next scheduled anchor_job() run (or an admin's manual "anchor now"), forever, without dropping
    anything.
  * Nothing is persisted until the RFC 3161 response has been fully verified -- see _attempt_anchor.
  * Every function in this module follows audit.py's own SAFETY CONTRACT: best-effort, logs to
    stderr, never raises into a caller that isn't expecting it (the two background job functions and
    every read/verify function catch broadly; only the low-level TSA-request helper raises, and only
    to its own immediate caller, which always catches it).
"""
import os
import json
import time
import base64
import hashlib
import sqlite3
import datetime
import threading

import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509 import ocsp
from cryptography.x509.oid import ExtendedKeyUsageOID, AuthorityInformationAccessOID

from rfc3161_client import (
    HashAlgorithm, TimestampRequestBuilder, decode_timestamp_response,
    VerifierBuilder, VerificationError, PKIStatus,
)

import audit

class AnchorError(Exception):
    """Internal aggregation error for the TSA request/verify pipeline. `stage` is one of
    'request' (network/parsing) or 'verify' (cryptographic/trust failure), used to keep
    last_error messages actionable rather than a bare traceback string."""
    def __init__(self, stage, message):
        super().__init__(message)
        self.stage = stage


# ---------------------------------------------------------------------------
# CONFIG -- read LAZILY (functions, not module-level constants), same reason as audit.py's own
# _trust_proxy_headers(): app.py imports this module before it runs settings.json -> os.environ
# boot-merge (the mechanism CONFIG_SPECS/the SETTINGS -> ADVANCED KNOBS UI relies on). A module-
# level constant here would permanently freeze in whatever os.environ held at import time and
# silently ignore a UI-saved value for the process's whole life.
# ---------------------------------------------------------------------------
def _tsa_urls():
    primary = os.environ.get("TSA_PRIMARY_URL") or "https://freetsa.org/tsr"
    fallback = os.environ.get("TSA_FALLBACK_URL") or ""
    return [u for u in (primary, fallback) if u]


def _root_ca_file_for(url):
    primary = os.environ.get("TSA_PRIMARY_URL") or "https://freetsa.org/tsr"
    if url == primary:
        return os.environ.get("TSA_PRIMARY_ROOT_CA_FILE") or ""
    return os.environ.get("TSA_FALLBACK_ROOT_CA_FILE") or ""


def _policy_oid():
    return os.environ.get("TSA_POLICY_OID") or ""


def _anchor_interval_hours():
    try:
        return max(0.1, float(os.environ.get("ANCHOR_INTERVAL_HOURS") or "24"))
    except (TypeError, ValueError):
        return 24.0


def _verify_interval_hours():
    try:
        return max(0.1, float(os.environ.get("ANCHOR_VERIFY_INTERVAL_HOURS") or "6"))
    except (TypeError, ValueError):
        return 6.0


def _renewal_interval_days():
    try:
        return max(1, int(os.environ.get("ANCHOR_RENEWAL_INTERVAL_DAYS") or "365"))
    except (TypeError, ValueError):
        return 365


AUDIT_DB = audit.AUDIT_DB  # same physical file as audit.py's events table -- see init_db() below

_lock = threading.Lock()
LEAF_SCHEMA_VERSION = 1
EVIDENCE_BUNDLE_SCHEMA_VERSION = 1
_BACKOFF_SCHEDULE = (2, 4, 8)   # seconds; retry-with-backoff within one TSA URL before failing over


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _dt_to_iso(dt):
    """TimeStampTokenInfo.gen_time -> our standard ISO string. Defensive about tz-naive input
    (ASN.1 GeneralizedTime is UTC by construction, but don't trust that blindly)."""
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except Exception:
        return str(dt)


def _conn():
    c = sqlite3.connect(AUDIT_DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    """Adds this module's two tables to the SAME physical file as audit.py's events table (same
    directory, same 0600 protection, no separate DB to keep in sync/backed-up) -- called once at
    app startup, alongside audit.init_db(). Safe if it fails; every function below degrades to a
    no-op/logged-failure rather than crashing a request path."""
    try:
        with _conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS anchors(
                anchor_id           INTEGER PRIMARY KEY AUTOINCREMENT,
                status              TEXT NOT NULL,              -- 'pending' | 'anchored'
                batch_start_seq     INTEGER NOT NULL,
                batch_end_seq       INTEGER NOT NULL,
                entry_count         INTEGER NOT NULL,
                is_head_reaffirm    INTEGER NOT NULL DEFAULT 0, -- 1 = "no new entries" positive-evidence batch
                leaf_hash_algo      TEXT NOT NULL,               -- hash-agility identifier (req. 6)
                leaf_schema_version INTEGER NOT NULL,
                leaf_hashes_json    TEXT NOT NULL,               -- ordered hex leaf hashes -- enough to
                                                                  -- deterministically rebuild the whole
                                                                  -- tree + every inclusion proof forever,
                                                                  -- independent of audit.py's events table
                                                                  -- (which a later retention purge may
                                                                  -- have since deleted the source rows from)
                merkle_root         TEXT NOT NULL,
                local_built_at      TEXT NOT NULL,               -- our clock, batch built (pre-TSA)
                nonce               TEXT,                        -- decimal string (can exceed int64)
                tsa_url_used        TEXT,
                tsa_time            TEXT,                        -- TSA-asserted gen_time (separate from ours)
                local_anchored_at   TEXT,                        -- our clock, token received+verified
                tst_token_der       BLOB,                        -- raw TimeStampToken bytes, AS RECEIVED
                tsp_response_der    BLOB,                        -- raw full TimeStampResp bytes, AS RECEIVED
                cert_chain_der_json TEXT,                        -- JSON array of base64 DER certs, AS RECEIVED
                root_certs_der_json TEXT,                        -- JSON array of base64 DER certs actually
                                                                  -- TRUSTED at anchor time (pinned or TOFU)
                root_trust_basis    TEXT,                        -- 'pinned' | 'tofu'
                ocsp_der            BLOB,                        -- OCSP response captured AT ANCHOR TIME
                crl_der             BLOB,                        -- CRL captured AT ANCHOR TIME (OCSP fallback)
                revocation_status   TEXT,                        -- 'good'|'revoked'|'unknown'|'unchecked'
                revocation_checked_at TEXT,
                revocation_note     TEXT,
                attempts            INTEGER NOT NULL DEFAULT 0,
                last_error          TEXT,
                policy_oid          TEXT,
                scheme_note         TEXT                         -- self-describing scheme tag, for future migrations
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS ix_anchors_status ON anchors(status)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_anchors_range ON anchors(batch_start_seq, batch_end_seq)")
            c.execute("""CREATE TABLE IF NOT EXISTS anchor_renewals(
                renewal_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                original_anchor_id INTEGER NOT NULL,   -- the anchor whose evidence is being kept alive
                prior_anchor_id    INTEGER NOT NULL,    -- the anchor (or previous renewal target) renewed THIS time
                new_anchor_id      INTEGER NOT NULL,    -- the fresh anchors row created to timestamp the renewal digest
                renewal_digest     TEXT NOT NULL,        -- hex sha256 over the canonical evidence-bundle serialization
                created_at         TEXT NOT NULL
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS ix_renewals_orig ON anchor_renewals(original_anchor_id)")
        try:
            os.chmod(AUDIT_DB, 0o600)
        except Exception:
            pass
    except Exception as e:
        print(f"[audit_anchor] init_db failed (external anchoring disabled this run): {e}", flush=True)


# ---------------------------------------------------------------------------
# CANONICAL ENTRY SERIALIZATION + MERKLE TREE. Fixed field order/encoding, versioned, NEVER changed
# in place after release -- see LEAF_SCHEMA_VERSION docstring below. This is the part that "breaks
# forever" if it drifts, so it's kept as small, explicit, and dependency-free as possible: plain
# stdlib json with sort_keys+fixed separators (canonical by construction, no hand-rolled delimiter
# joining that could collide with field content) and plain hashlib.
# ---------------------------------------------------------------------------
def _canonical_entry_json(row):
    """Fixed field set/order/encoding for one audit-log row's Merkle leaf. NEVER change field
    names, presence, types, JSON options (sort_keys/ensure_ascii/separators), or what
    LEAF_SCHEMA_VERSION=1 means, after release -- an already-issued anchor's leaf_hashes_json must
    stay byte-for-byte recomputable from the SAME row content forever, or every archived inclusion
    proof silently stops verifying. A future change adds a NEW LEAF_SCHEMA_VERSION and keeps this
    function able to reproduce the OLD version's bytes for rows anchored under it (mirrors
    audit.py's own CURRENT_SCHEME / legacy-scheme handling in _entry_hash()).

    Deliberately includes `entry_hash` (and `prev_hash`) rather than re-deriving a binding to
    ts/actor/src_ip/event_type/resource/outcome/corr_id/detail from scratch: audit.py's emit()
    already computes entry_hash as an HMAC over exactly those fields (detail included, via its own
    hash) chained to prev_hash, using a key this module never has access to. Re-hashing the raw
    fields here would duplicate that binding while adding new encoding pitfalls (None-handling,
    unicode edge cases, a second place field-order could drift) without adding independent
    security value, since entry_hash itself can't be forged without AUDIT_HMAC_KEY_FILE. Including
    the plaintext core fields alongside it is still worthwhile: it means a Merkle proof alone (leaf
    + siblings + root) is human-legible evidence of WHAT the entry said, not just an opaque token,
    without requiring a verifier to also trust/recompute the internal HMAC chain to make sense of
    it. seq is included as the row's own stable position; scheme is included so leaf_hash() output
    for a pre-SEC-2 plain-SHA256-scheme row is distinguishable from a post-upgrade HMAC-scheme row.
    """
    obj = {
        "v": LEAF_SCHEMA_VERSION,
        "seq": row["seq"],
        "ts": row["ts"],
        "actor": row["actor"],
        "actor_name": row["actor_name"],
        "src_ip": row["src_ip"],
        "event_type": row["event_type"],
        "resource": row["resource"],
        "outcome": row["outcome"],
        "corr_id": row["corr_id"],
        "scheme": row["scheme"],
        "prev_hash": row["prev_hash"],
        "entry_hash": row["entry_hash"],
    }
    return json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


# Domain-separation registry -- RFC 6962 (Certificate Transparency) convention: a leaf hash and an
# internal-node hash can NEVER collide by construction, because they're computed with different
# fixed prefix bytes. This is defense-in-depth ON TOP OF the odd-node-promotion fix below, not a
# substitute for it -- the two protect against different attacks (second-preimage / leaf-vs-node
# ambiguity, vs. CVE-2012-2459's duplicate-node ambiguity). Hash agility (requirement 6): every
# anchor row records its own leaf_hash_algo; a future algorithm is a new entry here, and every
# recompute path below takes an explicit `algo` parameter rather than assuming sha256 forever --
# ALREADY-ISSUED anchors keep using whatever algo they were built with.
_LEAF_DOMAIN_SEP = {
    "sha256": {"leaf": b"\x00", "node": b"\x01", "hashfn": hashlib.sha256},
}


def _hashfn(algo):
    try:
        return _LEAF_DOMAIN_SEP[algo]["hashfn"]
    except KeyError:
        raise ValueError(f"unsupported leaf_hash_algo {algo!r}")


def leaf_hash(row, algo="sha256"):
    """SHA-256 (or whatever `algo` names) over the canonical serialization of one audit-log row.
    This is the UN-domain-separated leaf digest, stored (as ordered hex strings) in an anchor's
    leaf_hashes_json -- domain separation is applied only when the digest enters the TREE (see
    _leaf_node_digest), keeping the stored value a plain, portable SHA-256 hex string."""
    return _hashfn(algo)(_canonical_entry_json(row)).hexdigest()


def _leaf_node_digest(leaf_hex, algo="sha256"):
    d = _LEAF_DOMAIN_SEP[algo]
    return d["hashfn"](d["leaf"] + bytes.fromhex(leaf_hex)).digest()


def _internal_node_digest(left, right, algo="sha256"):
    d = _LEAF_DOMAIN_SEP[algo]
    return d["hashfn"](d["node"] + left + right).digest()


def build_merkle_tree(leaf_hexes, algo="sha256"):
    """Build a Merkle tree over an ORDERED list of hex leaf hashes. Returns (root_hex, levels)
    where levels[0] is the (domain-separated) leaf level and levels[-1] == [root_digest].

    An ODD node at any level is PROMOTED UNCHANGED to the next level rather than being paired with
    a duplicate of itself (CVE-2012-2459). Duplication lets an attacker produce the SAME root from
    TWO DIFFERENT entry sets -- e.g. a 5-entry batch and a 6-entry batch formed by appending a
    second copy of the 5th entry both hash to the same root under a duplicating scheme, because the
    duplicated pair's combined hash is identical either way. That breaks the one property a Merkle
    root exists to provide: binding to a SPECIFIC, unique sequence of entries. Promotion closes it
    off completely -- an odd node's hash reappears bit-for-bit at the next level instead of being
    combined with a copy of itself, so no re-arrangement or duplication of the real entry set can
    ever reproduce the same root (see test_audit_anchor.py for a regression test constructing this
    exact attack against both a promoting and a (deliberately buggy) duplicating implementation)."""
    if not leaf_hexes:
        raise ValueError("cannot build a Merkle tree over zero entries")
    level = [_leaf_node_digest(h, algo) for h in leaf_hexes]
    levels = [level]
    while len(level) > 1:
        nxt = []
        i, n = 0, len(level)
        while i < n:
            if i + 1 < n:
                nxt.append(_internal_node_digest(level[i], level[i + 1], algo))
                i += 2
            else:
                nxt.append(level[i])   # promote the unpaired node UNCHANGED -- never duplicate it
                i += 1
        levels.append(nxt)
        level = nxt
    return level[0].hex(), levels


def inclusion_proof(levels, index):
    """Ordered sibling-hash + left/right-position list proving `levels[0][index]` is included
    under `levels[-1][0]` (the root). When a node at some level has no sibling (it was promoted,
    not paired), no proof step is emitted for that level -- the running hash simply carries forward
    unchanged, exactly mirroring how build_merkle_tree treated it."""
    proof = []
    idx = index
    for level in levels[:-1]:
        n = len(level)
        if idx % 2 == 0:
            sib = idx + 1
            if sib < n:
                proof.append({"hash": level[sib].hex(), "pos": "right"})
            # else: this node was promoted at this level -- no sibling, nothing to record
        else:
            sib = idx - 1
            proof.append({"hash": level[sib].hex(), "pos": "left"})
        idx //= 2
    return proof


def verify_inclusion(leaf_hex, proof, root_hex, algo="sha256"):
    """Recompute the root from a leaf hash + its inclusion proof and compare to `root_hex`. This
    is the ENTIRE trust-critical recomputation for "is this one entry really in this batch" --
    deliberately kept to plain stdlib hashlib operations with no external dependency."""
    cur = _leaf_node_digest(leaf_hex, algo)
    for step in proof:
        sib = bytes.fromhex(step["hash"])
        if step["pos"] == "right":
            cur = _internal_node_digest(cur, sib, algo)
        elif step["pos"] == "left":
            cur = _internal_node_digest(sib, cur, algo)
        else:
            raise ValueError(f"bad inclusion-proof step position {step.get('pos')!r}")
    return cur.hex() == root_hex


# ---------------------------------------------------------------------------
# REVOCATION CAPTURE (OCSP, falling back to CRL) -- best-effort, at anchor time. Never blocks an
# otherwise-successful anchor: the RFC 3161 token's own signature is independently valid regardless
# of whether we could reach an OCSP responder right now, and refusing to anchor over a flaky
# revocation-check network path would defeat "never drop a batch." What we DO guarantee is that the
# attempt (and its outcome, including failure) is recorded on the anchor row -- a documented gap is
# honest; a silently-missing check is not.
# ---------------------------------------------------------------------------
def _leaf_and_issuer_certs(certs):
    """Identify the TSA's own signing cert (by the id-kp-timeStamping EKU, RFC 3161 2.3) and its
    direct issuer within a parsed certificate list, for OCSP/CRL lookups."""
    leaf = None
    for c in certs:
        try:
            eku = c.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
        except x509.ExtensionNotFound:
            continue
        if ExtendedKeyUsageOID.TIME_STAMPING in eku.value:
            leaf = c
            break
    if leaf is None:
        return None, None
    issuer = None
    for c in certs:
        if c is not leaf and c.subject == leaf.issuer:
            issuer = c
            break
    return leaf, issuer


def _fetch_ocsp(leaf, issuer, timeout=10):
    try:
        aia = leaf.extensions.get_extension_for_class(x509.AuthorityInformationAccess)
    except x509.ExtensionNotFound:
        return None, "no_aia_extension"
    ocsp_url = None
    for desc in aia.value:
        if desc.access_method == AuthorityInformationAccessOID.OCSP:
            ocsp_url = desc.access_location.value
            break
    if not ocsp_url:
        return None, "no_ocsp_url_in_aia"
    try:
        req = ocsp.OCSPRequestBuilder().add_certificate(leaf, issuer, hashes.SHA256()).build()
        resp = requests.post(ocsp_url, data=req.public_bytes(Encoding.DER),
                              headers={"Content-Type": "application/ocsp-request"}, timeout=timeout)
        resp.raise_for_status()
        return resp.content, None
    except Exception as e:
        return None, f"ocsp_fetch_failed: {e}"


def _fetch_crl(leaf, timeout=15):
    try:
        cdp = leaf.extensions.get_extension_for_class(x509.CRLDistributionPoints)
    except x509.ExtensionNotFound:
        return None, "no_crl_distribution_point"
    for dp in cdp.value:
        if not dp.full_name:
            continue
        for name in dp.full_name:
            if isinstance(name, x509.UniformResourceIdentifier):
                try:
                    resp = requests.get(name.value, timeout=timeout)
                    resp.raise_for_status()
                    return resp.content, None
                except Exception:
                    continue
    return None, "crl_fetch_failed_all_urls"


def _capture_revocation(cert_der_list):
    """Returns (ocsp_der, crl_der, revocation_status, revocation_note). At most one of
    ocsp_der/crl_der is set (OCSP preferred; CRL is the documented fallback). Never raises."""
    try:
        certs = [x509.load_der_x509_certificate(c) for c in cert_der_list]
    except Exception as e:
        return None, None, "unchecked", f"cert parse failed: {e}"
    leaf, issuer = _leaf_and_issuer_certs(certs)
    if leaf is None or issuer is None:
        return None, None, "unchecked", "could not identify TSA leaf/issuer cert pair for revocation lookup"

    ocsp_der, ocsp_err = _fetch_ocsp(leaf, issuer)
    if ocsp_der:
        try:
            parsed = ocsp.load_der_ocsp_response(ocsp_der)
            if parsed.response_status != ocsp.OCSPResponseStatus.SUCCESSFUL:
                return ocsp_der, None, "unchecked", f"ocsp_response_status={parsed.response_status}"
            status = {
                ocsp.OCSPCertStatus.GOOD: "good",
                ocsp.OCSPCertStatus.REVOKED: "revoked",
                ocsp.OCSPCertStatus.UNKNOWN: "unknown",
            }.get(parsed.certificate_status, "unknown")
            return ocsp_der, None, status, "ocsp"
        except Exception as e:
            ocsp_err = f"ocsp_parse_failed: {e}"

    crl_der, crl_err = _fetch_crl(leaf)
    if crl_der:
        try:
            crl = x509.load_der_x509_crl(crl_der)
            revoked = crl.get_revoked_certificate_by_serial_number(leaf.serial_number)
            status = "revoked" if revoked is not None else "good"
        except Exception:
            status = "unknown"
        return None, crl_der, status, f"ocsp unavailable ({ocsp_err}); used CRL fallback"

    return None, None, "unchecked", f"ocsp failed ({ocsp_err}); crl failed ({crl_err})"


# ---------------------------------------------------------------------------
# RFC 3161 TSA CLIENT -- build request, POST, parse response, verify BEFORE anything is stored.
# ---------------------------------------------------------------------------
def _try_tsa_urls(root_bytes, urls, timeout=25):
    """Try each configured TSA URL in order; within each URL, retry with backoff on a transient
    failure (_BACKOFF_SCHEDULE). Returns (url_used, req, tsr, response_bytes) on the FIRST success.
    Raises AnchorError aggregating every attempt's failure reason if every URL/attempt fails --
    the caller (_attempt_anchor) always catches this and leaves the batch 'pending' for the next
    scheduled run rather than dropping it (constraint: never drop a batch over TSA unreachability)."""
    errors = []
    for url in urls:
        for attempt, backoff in enumerate((0,) + _BACKOFF_SCHEDULE):
            if backoff:
                time.sleep(backoff)
            try:
                req = (TimestampRequestBuilder()
                       .data(root_bytes)
                       .hash_algorithm(HashAlgorithm.SHA256)
                       .nonce(nonce=True)
                       .cert_request(cert_request=True)
                       .build())
                resp = requests.post(url, data=req.as_bytes(),
                                      headers={"Content-Type": "application/timestamp-query"},
                                      timeout=timeout)
                resp.raise_for_status()
                tsr = decode_timestamp_response(resp.content)
                return url, req, tsr, resp.content
            except Exception as e:
                errors.append(f"{url} attempt {attempt + 1}: {e}")
    raise AnchorError("request", "all configured TSA endpoints failed -- " + " | ".join(errors))


def _determine_root_certs(tsr, pinned_root_file):
    """Trust-root resolution for a FRESH (not-yet-stored) TSA response. Pinned file wins when
    configured (the right answer for production -- see .env.example); otherwise falls back to
    trust-on-first-use against the TSA's own returned chain (requires cert_req=true and the TSA
    actually including a self-signed root -- some public TSAs only return leaf+intermediate,
    expecting a pre-installed trust store; if so, this fails CLOSED with an actionable message
    rather than silently trusting an incomplete chain)."""
    if pinned_root_file:
        try:
            with open(pinned_root_file, "rb") as f:
                pem = f.read()
            return [x509.load_pem_x509_certificate(pem)], True, "pinned"
        except Exception as e:
            raise AnchorError("verify", f"configured root CA file {pinned_root_file!r} could not be loaded: {e}")
    try:
        certs = [x509.load_der_x509_certificate(c) for c in tsr.signed_data.certificates]
    except Exception as e:
        raise AnchorError("verify", f"could not parse certificates in TSA response: {e}")
    self_signed = [c for c in certs if c.issuer == c.subject]
    if not self_signed:
        raise AnchorError("verify",
            "TSA response did not include a self-signed root certificate and no pinned "
            "TSA_*_ROOT_CA_FILE is configured for this endpoint -- cannot establish trust. "
            "Configure a pinned root CA PEM (see .env.example) or obtain the TSA's root "
            "certificate out-of-band and set TSA_PRIMARY_ROOT_CA_FILE / TSA_FALLBACK_ROOT_CA_FILE.")
    return self_signed, False, "tofu"


# ---------------------------------------------------------------------------
# BATCH SELECTION + PERSISTENCE
# ---------------------------------------------------------------------------
def _last_anchor_end_seq():
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT batch_end_seq FROM anchors WHERE status='anchored' AND is_head_reaffirm=0 "
                "ORDER BY anchor_id DESC LIMIT 1"
            ).fetchone()
        if row is not None:
            return row["batch_end_seq"]
        # Fall back to considering a head-reaffirm anchor too, in case every real batch has since
        # been superseded by reaffirms (extremely idle log) -- still correct: batch_end_seq==head.
        with _conn() as c:
            row = c.execute(
                "SELECT batch_end_seq FROM anchors WHERE status='anchored' ORDER BY anchor_id DESC LIMIT 1"
            ).fetchone()
        return row["batch_end_seq"] if row else None
    except Exception as e:
        print(f"[audit_anchor] _last_anchor_end_seq failed: {e}", flush=True)
        return None


def _pending_anchors():
    try:
        with _conn() as c:
            rows = c.execute("SELECT * FROM anchors WHERE status='pending' ORDER BY anchor_id ASC").fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[audit_anchor] _pending_anchors failed: {e}", flush=True)
        return []


def _select_batch_rows():
    """Decide what the NEXT new batch should cover. Returns (rows, is_head_reaffirm) or (None,
    False) if there's nothing to anchor yet (brand-new install, zero events).

    Requirement 4's "anchor the unchanged head anyway": when nothing new has happened since the
    last anchor, the batch becomes a single-leaf batch over just the current head entry (re-
    affirmed) rather than being skipped -- each day's anchor is then positive evidence the log
    still ends exactly there, not merely the absence of a signal."""
    head, genesis = audit.head_seq()
    if head is None:
        return None, False
    last = _last_anchor_end_seq()
    if last is None:
        start, end, is_reaffirm = genesis, head, False
    elif head > last:
        start, end, is_reaffirm = last + 1, head, False
    else:
        start, end, is_reaffirm = head, head, True
    rows = audit.fetch_range(start, end)
    if not rows:
        return None, False
    return rows, is_reaffirm


def _create_pending_batch(rows, is_reaffirm=False):
    leaves = [leaf_hash(r) for r in rows]
    root, _levels = build_merkle_tree(leaves)
    now = _now_iso()
    with _lock:
        with _conn() as c:
            cur = c.execute(
                """INSERT INTO anchors
                   (status, batch_start_seq, batch_end_seq, entry_count, is_head_reaffirm,
                    leaf_hash_algo, leaf_schema_version, leaf_hashes_json, merkle_root,
                    local_built_at, attempts, scheme_note)
                   VALUES ('pending', ?, ?, ?, ?, 'sha256', ?, ?, ?, ?, 0, ?)""",
                (rows[0]["seq"], rows[-1]["seq"], len(rows), 1 if is_reaffirm else 0,
                 LEAF_SCHEMA_VERSION, json.dumps(leaves), root, now,
                 "rfc3161+merkle-sha256-domainsep-v1"),
            )
            return cur.lastrowid


def _mark_attempt_failed(anchor_id, err):
    err = str(err)[:2000]
    with _lock:
        with _conn() as c:
            c.execute("UPDATE anchors SET attempts=attempts+1, last_error=? WHERE anchor_id=?",
                      (err, anchor_id))
    print(f"[audit_anchor] anchor #{anchor_id} attempt failed: {err}", flush=True)


def _attempt_anchor(anchor_row):
    """Try to complete ONE pending anchor row: get a TSA token over its already-fixed merkle_root,
    verify everything BEFORE storing anything, then persist the full evidence bundle. Returns True
    on success, False on failure (row stays 'pending', last_error/attempts updated -- picked up
    again by the next anchor_job() run or a manual "anchor now")."""
    root_hex = anchor_row["merkle_root"]
    root_bytes = bytes.fromhex(root_hex)

    try:
        url, req, tsr, resp_bytes = _try_tsa_urls(root_bytes, _tsa_urls())
    except AnchorError as e:
        _mark_attempt_failed(anchor_row["anchor_id"], str(e))
        return False

    # Defense-in-depth: independently confirm the token's own claimed imprint really is
    # SHA-256(root_bytes) before trusting anything the verifier library concludes -- catches a
    # misbehaving/rogue TSA that silently used a different algorithm or a different digest.
    expected_imprint = hashlib.sha256(root_bytes).digest()
    if tsr.tst_info.message_imprint.message != expected_imprint:
        _mark_attempt_failed(anchor_row["anchor_id"], f"imprint mismatch in response from {url}")
        return False

    try:
        if PKIStatus(tsr.status) != PKIStatus.GRANTED:
            raise AnchorError("verify", f"PKIStatus not GRANTED ({tsr.status_string}) from {url}")
        root_certs, _used_pinned, trust_basis = _determine_root_certs(tsr, _root_ca_file_for(url))
        vb = VerifierBuilder.from_request(req)   # binds nonce + policy from OUR own request
        for rc in root_certs:
            vb = vb.add_root_certificate(rc)
        policy_oid_str = _policy_oid()
        verifier = vb.build()
        verifier.verify_message(tsr, root_bytes)   # raises VerificationError on ANY failure
    except (AnchorError, VerificationError) as e:
        _mark_attempt_failed(anchor_row["anchor_id"], f"verification failed from {url}: {e}")
        return False
    except Exception as e:
        _mark_attempt_failed(anchor_row["anchor_id"], f"unexpected verify error from {url}: {e}")
        return False

    # Verified -- ONLY NOW do we persist anything (constraint: verify BEFORE storing).
    cert_der_list = list(tsr.signed_data.certificates)
    ocsp_der, crl_der, revocation_status, revocation_note = _capture_revocation(cert_der_list)
    tsa_time_iso = _dt_to_iso(tsr.tst_info.gen_time)
    root_certs_der_b64 = [base64.b64encode(rc.public_bytes(Encoding.DER)).decode() for rc in root_certs]
    cert_chain_b64 = [base64.b64encode(c_).decode() for c_ in cert_der_list]

    with _lock:
        with _conn() as c:
            c.execute(
                """UPDATE anchors SET
                     status='anchored', tsa_url_used=?, tsa_time=?, local_anchored_at=?,
                     nonce=?, tst_token_der=?, tsp_response_der=?, cert_chain_der_json=?,
                     root_certs_der_json=?, root_trust_basis=?, ocsp_der=?, crl_der=?,
                     revocation_status=?, revocation_checked_at=?, revocation_note=?,
                     policy_oid=?, attempts=attempts+1, last_error=NULL
                   WHERE anchor_id=?""",
                (url, tsa_time_iso, _now_iso(), str(req.nonce),
                 sqlite3.Binary(tsr.time_stamp_token()), sqlite3.Binary(resp_bytes),
                 json.dumps(cert_chain_b64), json.dumps(root_certs_der_b64), trust_basis,
                 sqlite3.Binary(ocsp_der) if ocsp_der else None,
                 sqlite3.Binary(crl_der) if crl_der else None,
                 revocation_status, _now_iso(), revocation_note,
                 policy_oid_str or None, anchor_row["anchor_id"]),
            )
    audit.emit("audit.anchor.completed", actor_name="system", resource="audit.anchor",
               detail={"anchor_id": anchor_row["anchor_id"], "batch_start_seq": anchor_row["batch_start_seq"],
                       "batch_end_seq": anchor_row["batch_end_seq"], "entry_count": anchor_row["entry_count"],
                       "is_head_reaffirm": bool(anchor_row["is_head_reaffirm"]), "merkle_root": root_hex,
                       "tsa_url": url, "tsa_time": tsa_time_iso, "root_trust_basis": trust_basis,
                       "revocation_status": revocation_status})
    return True


# ---------------------------------------------------------------------------
# THE TWO SCHEDULED JOBS (requirement 4) -- deliberately independent of each other.
# ---------------------------------------------------------------------------
def anchor_job():
    """ANCHOR JOB: fixed daily schedule (start_anchor_thread), UNCONDITIONAL. Retries every already
    -queued pending batch first -- a TSA outage never loses a batch, it just stays queued and gets
    retried here on the next run (or via a manual "anchor now"). Only builds a brand-new batch if
    nothing is currently pending, so a sustained multi-day TSA outage doesn't pile up a backlog of
    redundant pending batches for the same growing tail of entries. Also sweeps for any anchor due
    for its annual renewal (requirement 6) -- folded into this job rather than a third thread,
    since it's the same "daily maintenance" cadence and keeps the scheduled-job surface to the two
    the spec calls for."""
    outcome = {"retried": 0, "retried_ok": 0, "new_batch": None, "new_batch_ok": None, "renewed": []}
    for pending in _pending_anchors():
        outcome["retried"] += 1
        if _attempt_anchor(pending):
            outcome["retried_ok"] += 1

    if not _pending_anchors():
        rows, is_reaffirm = _select_batch_rows()
        if rows:
            anchor_id = _create_pending_batch(rows, is_reaffirm)
            outcome["new_batch"] = anchor_id
            with _conn() as c:
                row = dict(c.execute("SELECT * FROM anchors WHERE anchor_id=?", (anchor_id,)).fetchone())
            outcome["new_batch_ok"] = _attempt_anchor(row)

    outcome["renewed"] = _maybe_renew_stale_anchors()
    return outcome


def _reverify_anchor(row):
    """Fully offline re-verification of ONE already-anchored row, using only what's archived on
    the row itself: recompute the Merkle root from leaf_hashes_json, recompute the expected
    imprint, re-parse the archived TimeStampResp bytes, and re-run the same signature/trust-chain
    verification against the ROOT CERT(S) ARCHIVED AT ANCHOR TIME (not live config -- a later
    config change or a lost pinned-root file can never orphan an old anchor's verifiability).
    Returns (ok: bool, reason: str|None)."""
    try:
        leaves = json.loads(row["leaf_hashes_json"])
        algo = row["leaf_hash_algo"] or "sha256"
        recomputed_root, _levels = build_merkle_tree(leaves, algo)
        if recomputed_root != row["merkle_root"]:
            return False, "merkle_root_mismatch"

        tsp_bytes = row["tsp_response_der"]
        if not tsp_bytes:
            return False, "no_archived_response"
        tsr = decode_timestamp_response(bytes(tsp_bytes))

        root_bytes = bytes.fromhex(row["merkle_root"])
        expected_imprint = hashlib.sha256(root_bytes).digest()
        if tsr.tst_info.message_imprint.message != expected_imprint:
            return False, "imprint_mismatch"

        root_certs_der = json.loads(row["root_certs_der_json"] or "[]")
        if not root_certs_der:
            return False, "no_archived_root_cert"
        root_certs = [x509.load_der_x509_certificate(base64.b64decode(b)) for b in root_certs_der]

        vb = VerifierBuilder()
        for rc in root_certs:
            vb = vb.add_root_certificate(rc)
        if row["nonce"]:
            try:
                vb = vb.nonce(int(row["nonce"]))
            except (TypeError, ValueError):
                pass
        verifier = vb.build()
        verifier.verify_message(tsr, root_bytes)
        return True, None
    except VerificationError as e:
        return False, f"token_verification_failed: {e}"
    except Exception as e:
        return False, f"reverify_error: {e}"


def verify_job():
    """VERIFY JOB: independent, more frequent than the anchor job by default (start_verify_thread).
    This is the DETECTION path (as distinct from the anchor job, which only ever produces new
    evidence): recomputes audit.py's own internal HMAC chain, AND re-verifies every completed
    anchor purely from its archived evidence (no network, no live config dependency), and alerts
    via audit.emit on ANY mismatch -- same pattern as SEC-4's detection.alert, reusing this same
    hash-chained/anchored storage for its own findings."""
    result = {"chain_ok": None, "anchors_checked": 0, "anchors_failed": [], "checked_at": _now_iso()}
    try:
        chain = audit.verify_chain()
        result["chain_ok"] = chain.get("ok")
        if not chain.get("ok"):
            audit.emit("audit.anchor.verify.failed", outcome="failure", actor_name="system",
                       resource="audit.chain", detail={"reason": "internal_chain_broken", "detail": chain})
    except Exception as e:
        result["chain_ok"] = False
        result["chain_error"] = str(e)

    try:
        with _conn() as c:
            rows = c.execute("SELECT * FROM anchors WHERE status='anchored' ORDER BY anchor_id ASC").fetchall()
        for r in rows:
            r = dict(r)
            result["anchors_checked"] += 1
            ok, reason = _reverify_anchor(r)
            if not ok:
                result["anchors_failed"].append({"anchor_id": r["anchor_id"], "reason": reason})
                audit.emit("audit.anchor.verify.failed", outcome="failure", actor_name="system",
                           resource="audit.anchor", detail={"anchor_id": r["anchor_id"], "reason": reason})
    except Exception as e:
        result["scan_error"] = str(e)

    if result["chain_ok"] and not result["anchors_failed"]:
        audit.emit("audit.anchor.verify.ok", actor_name="system", resource="audit.anchor",
                   detail={"anchors_checked": result["anchors_checked"]})
    return result


# ---------------------------------------------------------------------------
# VERIFICATION API (requirement 5) -- structured, actionable results, never raises.
# ---------------------------------------------------------------------------
def verify_entry(seq):
    """Prove ONE audit-log entry's inclusion in an externally anchored batch. Recomputes the
    entry's Merkle leaf from its CURRENT stored content, walks the inclusion proof to the anchor's
    root, confirms that root matches what was actually timestamped, and re-verifies the archived
    RFC 3161 token. Returns a structured result with a per-check breakdown and the TSA-asserted
    time -- never raises."""
    seq = int(seq)
    out = {"entry_seq": seq, "ok": False, "anchor_id": None, "tsa_time": None,
           "checks": {}, "reason": None}
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT * FROM anchors WHERE status='anchored' AND batch_start_seq<=? AND batch_end_seq>=? "
                "ORDER BY anchor_id DESC LIMIT 1", (seq, seq)
            ).fetchone()
        if not row:
            out["reason"] = "not_yet_anchored"
            return out
        row = dict(row)
        out["anchor_id"] = row["anchor_id"]
        out["tsa_time"] = row["tsa_time"]
        out["merkle_root"] = row["merkle_root"]

        entry_rows = audit.fetch_range(seq, seq)
        if not entry_rows:
            out["checks"]["leaf_recomputable"] = False
            out["reason"] = "entry_not_found_or_purged"
            return out
        entry_row = entry_rows[0]
        algo = row["leaf_hash_algo"] or "sha256"
        recomputed_leaf = leaf_hash(entry_row, algo)
        out["checks"]["leaf_recomputable"] = True

        leaves = json.loads(row["leaf_hashes_json"])
        index = seq - row["batch_start_seq"]
        if not (0 <= index < len(leaves) and leaves[index] == recomputed_leaf):
            # Defensive fallback -- a purge can't happen mid-recent-batch in practice, but don't
            # assume positional alignment is guaranteed if it ever did.
            try:
                index = leaves.index(recomputed_leaf)
            except ValueError:
                out["checks"]["leaf_matches_stored_batch"] = False
                out["reason"] = "leaf_mismatch"
                return out
        out["checks"]["leaf_matches_stored_batch"] = True

        root_hex, levels = build_merkle_tree(leaves, algo)
        out["checks"]["batch_root_matches_stored"] = (root_hex == row["merkle_root"])
        proof = inclusion_proof(levels, index)
        inclusion_ok = verify_inclusion(recomputed_leaf, proof, row["merkle_root"], algo)
        out["checks"]["inclusion_proof_valid"] = inclusion_ok
        out["inclusion_proof"] = proof

        token_ok, token_reason = _reverify_anchor(row)
        out["checks"]["tsa_token_valid"] = token_ok

        out["ok"] = bool(out["checks"]["batch_root_matches_stored"] and inclusion_ok and token_ok)
        if not out["ok"]:
            out["reason"] = token_reason if not token_ok else (
                "batch_root_mismatch" if not out["checks"]["batch_root_matches_stored"] else "inclusion_proof_invalid")
        return out
    except Exception as e:
        out["reason"] = f"error: {e}"
        return out


def verify_range(start, end):
    """Same as verify_entry but over a whole seq range: the internal chain check, plus EVERY
    anchor that covers any part of [start, end] re-verified, plus any sub-range not yet covered by
    an anchor called out explicitly (not itself a failure -- just informational, since the most
    recent entries may not have been anchored yet)."""
    start, end = int(start), int(end)
    out = {"start": start, "end": end, "chain": None, "anchors": [], "ok": True}
    try:
        chain = audit.verify_chain()
        out["chain"] = chain
        if not chain.get("ok"):
            out["ok"] = False
    except Exception as e:
        out["chain"] = {"ok": False, "error": str(e)}
        out["ok"] = False

    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT * FROM anchors WHERE status='anchored' AND batch_end_seq>=? AND batch_start_seq<=? "
                "ORDER BY anchor_id ASC", (start, end)
            ).fetchall()
        covered = set()
        for r in rows:
            r = dict(r)
            token_ok, reason = _reverify_anchor(r)
            out["anchors"].append({
                "anchor_id": r["anchor_id"], "batch_start_seq": r["batch_start_seq"],
                "batch_end_seq": r["batch_end_seq"], "merkle_root": r["merkle_root"],
                "tsa_time": r["tsa_time"], "token_valid": token_ok, "reason": reason,
            })
            if not token_ok:
                out["ok"] = False
            covered.update(range(max(r["batch_start_seq"], start), min(r["batch_end_seq"], end) + 1))
        uncovered = sorted(set(range(start, end + 1)) - covered)
        if uncovered:
            out["uncovered_entries_sample"] = uncovered[:50]
            out["uncovered_count"] = len(uncovered)
    except Exception as e:
        out["anchors_error"] = str(e)
        out["ok"] = False
    return out


# ---------------------------------------------------------------------------
# LONG-TERM VALIDATION (requirement 6) -- annual renewal with fresh crypto. See module docstring
# "LONG-TERM VALIDATION" for why this is a deliberately-simplified ERS-style scheme and not full
# RFC 4998 ASN.1 Evidence Record syntax.
# ---------------------------------------------------------------------------
def _serialize_evidence_bundle(anchor_row):
    """Canonical, fixed-field serialization of everything archived for one anchor -- the exact
    input a renewal re-timestamps. Same never-change-after-release discipline as
    _canonical_entry_json (see EVIDENCE_BUNDLE_SCHEMA_VERSION)."""
    obj = {
        "v": EVIDENCE_BUNDLE_SCHEMA_VERSION,
        "anchor_id": anchor_row["anchor_id"],
        "batch_start_seq": anchor_row["batch_start_seq"],
        "batch_end_seq": anchor_row["batch_end_seq"],
        "leaf_hash_algo": anchor_row["leaf_hash_algo"],
        "leaf_hashes_json": anchor_row["leaf_hashes_json"],
        "merkle_root": anchor_row["merkle_root"],
        "tsa_time": anchor_row["tsa_time"],
        "tst_token_der_b64": base64.b64encode(bytes(anchor_row["tst_token_der"] or b"")).decode(),
        "cert_chain_der_json": anchor_row["cert_chain_der_json"],
        "ocsp_der_b64": base64.b64encode(bytes(anchor_row["ocsp_der"] or b"")).decode(),
        "crl_der_b64": base64.b64encode(bytes(anchor_row["crl_der"] or b"")).decode(),
    }
    return json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _needs_renewal(anchor_row, cutoff_days):
    try:
        with _conn() as c:
            latest = c.execute(
                "SELECT created_at FROM anchor_renewals WHERE original_anchor_id=? "
                "ORDER BY renewal_id DESC LIMIT 1", (anchor_row["anchor_id"],)
            ).fetchone()
        basis_ts = latest["created_at"] if latest else anchor_row["local_anchored_at"]
        if not basis_ts:
            return False
        basis_dt = datetime.datetime.strptime(basis_ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=datetime.timezone.utc)
        age_days = (datetime.datetime.now(datetime.timezone.utc) - basis_dt).days
        return age_days >= cutoff_days
    except Exception:
        return False


def _renew_anchor(anchor_row):
    """Build+anchor a fresh batch over a digest of anchor_row's FULL archived evidence bundle, and
    record the link in anchor_renewals. Reuses the ordinary pending -> _attempt_anchor pipeline
    (same TSA client, same verify-before-store) -- the bundle digest is treated as a single leaf,
    tree-built exactly like a normal batch, so _reverify_anchor's recompute path works unmodified
    for a renewal row too."""
    bundle = _serialize_evidence_bundle(anchor_row)
    bundle_digest = hashlib.sha256(bundle).hexdigest()
    root_hex, _levels = build_merkle_tree([bundle_digest])
    now = _now_iso()
    with _lock:
        with _conn() as c:
            cur = c.execute(
                """INSERT INTO anchors
                   (status, batch_start_seq, batch_end_seq, entry_count, is_head_reaffirm,
                    leaf_hash_algo, leaf_schema_version, leaf_hashes_json, merkle_root,
                    local_built_at, attempts, scheme_note)
                   VALUES ('pending', ?, ?, 0, 0, 'sha256', ?, ?, ?, ?, 0, ?)""",
                (anchor_row["batch_start_seq"], anchor_row["batch_end_seq"], LEAF_SCHEMA_VERSION,
                 json.dumps([bundle_digest]), root_hex, now,
                 f"renewal-of-anchor-{anchor_row['anchor_id']}"),
            )
            new_anchor_id = cur.lastrowid

    with _conn() as c:
        new_row = dict(c.execute("SELECT * FROM anchors WHERE anchor_id=?", (new_anchor_id,)).fetchone())
    ok = _attempt_anchor(new_row)
    if ok:
        with _lock:
            with _conn() as c:
                c.execute(
                    """INSERT INTO anchor_renewals
                       (original_anchor_id, prior_anchor_id, new_anchor_id, renewal_digest, created_at)
                       VALUES (?,?,?,?,?)""",
                    (anchor_row["anchor_id"], anchor_row["anchor_id"], new_anchor_id, bundle_digest, now),
                )
        audit.emit("audit.anchor.renewed", actor_name="system", resource="audit.anchor",
                   detail={"original_anchor_id": anchor_row["anchor_id"], "new_anchor_id": new_anchor_id,
                           "bundle_digest": bundle_digest})
    return ok


def _maybe_renew_stale_anchors():
    cutoff_days = _renewal_interval_days()
    renewed = []
    try:
        with _conn() as c:
            rows = c.execute("SELECT * FROM anchors WHERE status='anchored'").fetchall()
        for r in rows:
            r = dict(r)
            if _needs_renewal(r, cutoff_days):
                if _renew_anchor(r):
                    renewed.append(r["anchor_id"])
    except Exception as e:
        print(f"[audit_anchor] renewal sweep failed: {e}", flush=True)
    return renewed


# ---------------------------------------------------------------------------
# BACKGROUND THREADS -- mirrors audit.py's own start_retention_thread() pattern exactly.
# ---------------------------------------------------------------------------
def start_anchor_thread(interval_hours=None):
    interval = float(interval_hours if interval_hours is not None else _anchor_interval_hours())

    def _loop():
        while True:
            try:
                anchor_job()
            except Exception as e:
                print(f"[audit_anchor] anchor thread error: {e}", flush=True)
            time.sleep(interval * 3600)

    t = threading.Thread(target=_loop, daemon=True, name="audit-anchor")
    t.start()
    return t


def start_verify_thread(interval_hours=None):
    interval = float(interval_hours if interval_hours is not None else _verify_interval_hours())

    def _loop():
        while True:
            try:
                verify_job()
            except Exception as e:
                print(f"[audit_anchor] verify thread error: {e}", flush=True)
            time.sleep(interval * 3600)

    t = threading.Thread(target=_loop, daemon=True, name="audit-anchor-verify")
    t.start()
    return t


# ---------------------------------------------------------------------------
# STATUS / LISTING -- for the API + UI (app.py, static/index.html).
# ---------------------------------------------------------------------------
def anchor_status():
    """Summary for the SECURITY AUDIT LOG panel: the most recent successful anchor, how many
    batches are queued/retrying, and which TSA endpoint(s) are configured. Never raises."""
    try:
        with _conn() as c:
            latest = c.execute(
                "SELECT * FROM anchors WHERE status='anchored' ORDER BY anchor_id DESC LIMIT 1"
            ).fetchone()
            pending_count = c.execute("SELECT COUNT(*) AS n FROM anchors WHERE status='pending'").fetchone()["n"]
            total_anchored = c.execute("SELECT COUNT(*) AS n FROM anchors WHERE status='anchored'").fetchone()["n"]
            oldest_pending = c.execute(
                "SELECT last_error, attempts, local_built_at FROM anchors WHERE status='pending' "
                "ORDER BY anchor_id ASC LIMIT 1"
            ).fetchone()
        latest_out = None
        if latest:
            latest = dict(latest)
            latest_out = {k: latest[k] for k in (
                "anchor_id", "batch_start_seq", "batch_end_seq", "entry_count", "is_head_reaffirm",
                "merkle_root", "tsa_url_used", "tsa_time", "local_anchored_at", "revocation_status",
                "root_trust_basis")}
        return {
            "latest_anchor": latest_out,
            "pending_count": pending_count,
            "total_anchored": total_anchored,
            "oldest_pending_error": dict(oldest_pending) if oldest_pending else None,
            "tsa_urls_configured": _tsa_urls(),
            "anchor_interval_hours": _anchor_interval_hours(),
            "verify_interval_hours": _verify_interval_hours(),
        }
    except Exception as e:
        return {"error": str(e)}


def list_anchors(limit=50):
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT anchor_id, status, batch_start_seq, batch_end_seq, entry_count, "
                "is_head_reaffirm, merkle_root, tsa_url_used, tsa_time, local_anchored_at, "
                "revocation_status, root_trust_basis, attempts, last_error FROM anchors "
                "ORDER BY anchor_id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[audit_anchor] list_anchors failed: {e}", flush=True)
        return []


if __name__ == "__main__":
    # Smoke test: `python3 audit_anchor.py` -- exercises the Merkle builder + full anchor/verify
    # pipeline against a temp DB seeded with real audit.py events. Runs entirely OFFLINE by
    # default (no TSA network call) so it works in any sandboxed dev environment; set
    # AUDIT_ANCHOR_LIVE_TEST=1 to also attempt a REAL TSA round-trip against TSA_PRIMARY_URL
    # (default https://freetsa.org/tsr) -- that part needs a network path this module's own dev
    # sandbox may not have (e.g. a restricted CI/agent environment), so run it somewhere with real
    # internet access to actually confirm the live path end-to-end.
    import tempfile
    d = tempfile.mkdtemp()
    audit.AUDIT_DB = os.path.join(d, "audit.db")
    audit.AUDIT_JSONL = os.path.join(d, "audit.jsonl")
    audit.AUDIT_HMAC_KEY_FILE = os.path.join(d, "audit_hmac.key")
    audit._HMAC_KEY = audit._load_or_create_hmac_key()
    AUDIT_DB = audit.AUDIT_DB
    audit.init_db()
    init_db()

    for i in range(7):
        audit.emit("auth.login.success" if i % 2 == 0 else "config.changed",
                   actor="u1", actor_name="jason", src_ip="203.0.113.9",
                   resource=f"/api/test/{i}", detail={"i": i})

    rows, is_reaffirm = _select_batch_rows()
    print(f"batch selected: {len(rows)} entries, is_head_reaffirm={is_reaffirm}")
    leaves = [leaf_hash(r) for r in rows]
    root, levels = build_merkle_tree(leaves)
    print(f"merkle root: {root}")
    all_ok = True
    for i in range(len(leaves)):
        proof = inclusion_proof(levels, i)
        ok = verify_inclusion(leaves[i], proof, root)
        all_ok = all_ok and ok
        print(f"  leaf {i}: proof_len={len(proof)} verify={ok}")
    print(f"all inclusion proofs verify offline: {all_ok}")

    anchor_id = _create_pending_batch(rows, is_reaffirm)
    print(f"pending anchor created: #{anchor_id}")

    if os.environ.get("AUDIT_ANCHOR_LIVE_TEST") == "1":
        print("attempting a REAL TSA round-trip against", _tsa_urls(), "...")
        with _conn() as c:
            row = dict(c.execute("SELECT * FROM anchors WHERE anchor_id=?", (anchor_id,)).fetchone())
        ok = _attempt_anchor(row)
        print(f"live anchor attempt result: {ok}")
        if ok:
            print("anchor_status():", json.dumps(anchor_status(), indent=2, default=str))
            print("verify_entry(first seq):", json.dumps(verify_entry(rows[0]["seq"]), indent=2, default=str))
            print("verify_job():", json.dumps(verify_job(), indent=2, default=str))
    else:
        print("AUDIT_ANCHOR_LIVE_TEST not set to 1 -- skipping the real TSA network round-trip. "
              "The Merkle build/inclusion-proof logic above ran fully offline and needed no "
              "network. Set AUDIT_ANCHOR_LIVE_TEST=1 (with real internet access) to also exercise "
              "the RFC 3161 request/response/verify path against an actual TSA.")
