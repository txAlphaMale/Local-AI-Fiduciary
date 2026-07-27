"""
audit.py -- SEC-1/SEC-2: the app's security audit / event log (append-only except for one
documented retention exception, encrypted-at-rest, HMAC-hash-chained), plus a plaintext JSONL
mirror intended as a SIEM-ingestible log source.

WHY (RESUME-TRACKER.md "SECURITY-PORTFOLIO CONTROL BACKLOG", SEC-1; SECURITY-CONTROLS-GAP-ANALYSIS.md
C-1): before this module, the app captured `request.client.host` only to feed the in-memory rate
limiter (auth.rate_limited) -- nothing was ever persisted, so there was no record of who logged in,
from where, when, or what security-relevant change they made. That is the single control most
heavily weighted by SOC 2 / ISO 27001 / NIST audits, and it was entirely absent.

CONTROL MAPPING:
  - ISO/IEC 27001:2022  A.8.15 (Logging), A.8.16 (Monitoring activities)
  - NIST SP 800-53 Rev5 AU-2 (event selection), AU-3 (content of records), AU-12 (generation),
                        AU-9 (protection of audit info -- HMAC-keyed chain below), AU-11 (retention
                        -- purge_expired() below)
  - SOC 2 (2017 TSC)    CC7.2 (detection/monitoring of anomalies)
  - ISO/IEC 27701:2019  privacy event logging (extends the above to PII-affecting events)

DESIGN:
  * Own SQLite file (AUDIT_DB), 0600, separate from auth.db/store.db/chat.db -- keeping the security
    event trail physically separate from application data is itself a minor hardening property and
    mirrors auth.py's rationale for a dedicated credentials DB.
  * APPEND-ONLY (with one documented exception): this module never exposes a way to edit or delete
    an event's own content. purge_expired() (SEC-2, below) is the sole exception -- it deletes whole
    rows past the retention window and flips a chain_anchor flag on exactly one surviving row; it
    never edits an event's ts/actor/detail/etc.
  * ENCRYPTED AT REST: the free-form `detail` (which may hold old->new config values, resource
    identifiers, usernames) is JSON-serialized and encrypted with the app's shared Fernet key via
    secrets_crypto -- same primitive/key already protecting every other sensitive column. The
    indexable/queryable core columns (ts, event_type, outcome, actor id) stay plaintext for the same
    reason the rest of the app leaves lookup keys plaintext: Fernet ciphertext is non-deterministic
    and can't be filtered/sorted on. src_ip is kept plaintext deliberately -- it is operational
    security data a detection rule must filter on; redaction happens at the VIEWER layer (SEC-2),
    not at rest.
  * HASH-CHAINED (SEC-2, 2026-07-25 upgrade -- AU-9 proper): each row stores prev_hash + entry_hash
    where entry_hash = HMAC-SHA256(AUDIT_HMAC_KEY, prev_hash + canonical_core + sha256(detail_json)).
    The HMAC key lives in its OWN file (AUDIT_HMAC_KEY_FILE), generated independently from and never
    derived from secrets_crypto's Fernet key -- an attacker who obtains only the audit DB (e.g. a
    stolen backup, or a bug that leaks DB contents but not the filesystem) cannot recompute a valid
    forward chain without also obtaining this separate key file. This is a genuine improvement over
    a plain hash chain (which anyone with DB write access alone could recompute), though it does NOT
    protect against an attacker with full host/root access who can read every key file the app can
    read -- no software-only control fully can. That boundary is intentionally stated plainly rather
    than oversold. Rows written before this upgrade keep their original plain-SHA256 `scheme` value
    (`scheme='sha256'`) so verify_chain() can validate old and new rows correctly in the same pass --
    a hard cutover would have made every pre-upgrade row look "tampered" the instant this shipped,
    which would be worse than the plain-hash chain it replaces.
  * RETENTION / DISPOSAL (SEC-2): purge_expired() deletes the oldest contiguous prefix of events
    past AUDIT_RETENTION_DAYS and re-anchors the chain at the new oldest surviving row (chain_anchor
    flag) so verify_chain() doesn't report a false break at the purge boundary -- see purge_expired()
    and verify_chain() docstrings. The purge itself is logged as its own audit event (count + oldest/
    newest timestamps purged + the new anchor's seq/hash), so disposal is part of the evidence trail,
    not a silent DELETE. This is the one documented exception to "append-only": the anchor flag on
    exactly one row is flipped by a purge, and only a purge -- no event's own content is ever edited.
  * SIEM MIRROR: every event is also appended as ONE JSON object per line to AUDIT_JSONL. The mirror
    carries only the operational core fields a SIEM/detection needs (ts, actor, src_ip, event_type,
    outcome, resource, corr_id, entry_hash) and deliberately OMITS the encrypted `detail` blob, so
    the most sensitive payload stays only in the encrypted DB while the log source stays clean and
    parseable. This is the "log source -> normalize -> detect" entry point for SEC-4.

SAFETY CONTRACT: emit() MUST NEVER raise into a request path. A failure to write an audit record
must not break the user action being audited (that would turn logging into a denial-of-service on
the app itself). All failures are swallowed and reported to stderr only.
"""
import os
import sqlite3
import json
import time
import hashlib
import hmac as _hmac_mod
import threading
import datetime

AUDIT_DB = os.environ.get("AUDIT_DB", "/data/audit.db")
AUDIT_JSONL = os.environ.get("AUDIT_JSONL", "/data/audit.jsonl")
# SEC-2: separate key file from secrets_crypto's Fernet key -- see module docstring "HASH-CHAINED".
AUDIT_HMAC_KEY_FILE = os.environ.get("AUDIT_HMAC_KEY_FILE", "/data/audit_hmac.key")
# SEC-2: retention window for purge_expired() (ISO A.8.10, NIST AU-11, SOC 2 CC6.5/Privacy P4).
AUDIT_RETENTION_DAYS = int(os.environ.get("AUDIT_RETENTION_DAYS", "365"))

# 2026-07-26 -- found live on the AWS demo: `request.client.host` is the load balancer's own ENI
# address once this app sits behind an ALB (or any reverse proxy), never the real visitor -- every
# event logged the SAME constant IP for every visitor, which also silently neutered the SEC-4
# first-seen-IP detection rule (an IP that never changes is "already seen" forever after the first
# hit) and meant the login/recovery rate limiters were bucketing ALL visitors together under one
# key. TRUST_PROXY_HEADERS=1 opts into resolving the real client from X-Forwarded-For instead --
# but ONLY when explicitly set, because blindly trusting XFF is itself a spoofing vector: any
# caller that can reach this process directly could hand-craft that header to frame another IP in
# the audit log. This must be set ONLY where the deployment's own network guarantees the sole path
# in is through exactly one trusted hop (e.g. the AWS demo's ECS security group, which only accepts
# inbound traffic from the ALB's security group -- see AWS-DEMO-HOSTING-GUIDE.md). Left OFF (0) by
# default for the real household instance and the local demo project, where there is no proxy at
# all and `request.client.host` is already correct -- turning this on there would let anyone on the
# LAN spoof their logged IP for free.
#
# Read LAZILY (a function, not a module-level constant read once at import time) -- deliberately.
# app.py imports this module (line ~62) BEFORE it runs its settings.json -> os.environ boot-merge
# (line ~114, the mechanism the SETTINGS -> ADVANCED KNOBS "Trust reverse-proxy headers" toggle
# relies on, added 2026-07-27). A module-level constant here would have permanently frozen in
# whatever os.environ held at that early import moment, silently ignoring a UI-saved value for the
# rest of the process's life. Re-checking os.environ on every call costs nothing measurable and
# sidesteps the ordering hazard entirely -- it's still only ever read once per request, and still
# only ever changes on a restart either way (same "restart to apply" contract as every other knob
# in CONFIG_SPECS).
def _trust_proxy_headers():
    return os.environ.get("TRUST_PROXY_HEADERS", "0") == "1"


def _resolve_client_ip(src_ip, xff):
    """Best-known real client IP for THIS deployment's trust model. When TRUST_PROXY_HEADERS is on
    and an X-Forwarded-For value is present, the LAST comma-separated entry is trusted -- that's
    the one the single trusted hop (e.g. the ALB, or a self-hosted tester's own reverse proxy)
    itself appended; anything to its LEFT could have been supplied by the original client and is
    deliberately never trusted. Falls back to the raw TCP peer (src_ip) otherwise, which is exactly
    the old (pre-2026-07-26) behavior."""
    if not _trust_proxy_headers() or not xff:
        return src_ip
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    return parts[-1] if parts else src_ip

CURRENT_SCHEME = "hmac-sha256"

# Serializes the read-prev-hash -> insert sequence so the chain stays consistent under concurrent
# requests. Cheap: audit writes are rare relative to app work and each insert is tiny.
_lock = threading.Lock()

# The minimum event set SEC-1 commits to emitting (documented so SEC-4 detections and any reviewer
# know the vocabulary). Free-form event_type strings are still allowed; this is the canonical core.
EVENT_TYPES = (
    "auth.login.success", "auth.login.failure",
    "auth.logout",
    "auth.mfa.enrolled", "auth.mfa.disabled", "auth.mfa.failure",
    "auth.recovery.used", "auth.recovery.reset",
    "auth.password.changed", "auth.username.changed",
    "auth.recovery_key.regenerated",
    "auth.account.reset",
    "authz.denied",
    "session.created", "session.expired",
    "secret.read", "secret.changed", "secret.rotated",
    "config.changed",
    "data.exported", "data.shared",
    # TLS/certificate security-state transitions (2026-07-25 broadening) -- distinct from generic
    # config.changed because these change the app's TRANSPORT security posture (what the Strict-
    # Transport-Security/Secure-cookie logic in app.py's _https_active() reacts to), not an
    # arbitrary user preference. Worth their own vocabulary for SEC-4 detections later (e.g. "cert
    # cleared outside a maintenance window").
    "tls.cert.self_signed_generated", "tls.cert.csr_generated", "tls.cert.imported",
    "tls.acme.enrolled", "tls.acme.renewed", "tls.auto_renew.changed", "tls.cert.cleared",
    # SEC-2 (2026-07-25): the retention/disposal job logs its own action under this type.
    "audit.retention.purged",
    # SEC-5 (2026-07-25): admin-managed household accounts (RBAC) + the viewer-role write denial.
    "auth.user.created", "auth.user.deleted",
    # SEC-5 UX gap fix (2026-07-26): in-place admin<->viewer role swap (notifications.py module
    # docstring / RESUME-TRACKER.md "no in-place role-change" gap).
    "auth.user.role_changed",
    # SEC-4 (2026-07-25): the detection engine (detections.py) logs its own findings as events of
    # this type, reusing this module's storage/chain/viewer rather than a parallel alert store —
    # an alert IS a security event.
    "detection.alert",
    # Demo harness (2026-07-25): demo_seed.py logs its own seed/reset actions here too, same
    # "an X is a security event" reasoning as detection.alert above — a demo instance being wiped
    # and reseeded is itself a security-relevant state transition worth a durable record.
    "demo.seeded", "demo.reset",
)


def _load_or_create_hmac_key():
    """Bootstrap AUDIT_HMAC_KEY_FILE (0600), independent of secrets_crypto's Fernet key -- see
    module docstring. Mirrors secrets_crypto._load_or_create_key()'s own persist-if-missing
    pattern. RISK, stated plainly (same category as secrets_crypto's own key-loss warning): if
    this file can't be persisted, an in-memory-only key is used for THIS RUN ONLY -- the chain
    becomes unverifiable across a restart (a fresh random key won't match past HMACs). Back this
    file up alongside the data volume and secrets.key; losing it doesn't lose any DATA (events
    stay readable/decryptable via secrets_crypto as always) but does permanently forfeit the
    ability to prove the pre-loss chain wasn't tampered with after the fact."""
    try:
        with open(AUDIT_HMAC_KEY_FILE, "rb") as f:
            k = f.read()
            if k:
                return k
    except FileNotFoundError:
        pass
    key = os.urandom(32)
    try:
        os.makedirs(os.path.dirname(AUDIT_HMAC_KEY_FILE) or ".", exist_ok=True)
        with open(AUDIT_HMAC_KEY_FILE, "wb") as f:
            f.write(key)
        os.chmod(AUDIT_HMAC_KEY_FILE, 0o600)
    except Exception as e:
        print(f"[audit] could not persist HMAC key -- using an in-memory-only key for this run "
              f"(chain will not verify across a restart): {e}", flush=True)
    return key


_HMAC_KEY = _load_or_create_hmac_key()


def _entry_hash(scheme, prev_hash, core, detail_hash_src):
    """Scheme-aware entry-hash computation so verify_chain() can validate both pre- and
    post-SEC-2 rows in the same pass (see module docstring)."""
    payload = (prev_hash + "\n" + core + "\n" + _sha(detail_hash_src)).encode("utf-8")
    if scheme == "hmac-sha256":
        return _hmac_mod.new(_HMAC_KEY, payload, hashlib.sha256).hexdigest()
    return hashlib.sha256(payload).hexdigest()  # legacy pre-2026-07-25 scheme


def _conn():
    os.makedirs(os.path.dirname(AUDIT_DB) or ".", exist_ok=True)
    c = sqlite3.connect(AUDIT_DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    """Create the append-only events table. Called once at startup (app.py), alongside
    store.init_db()/auth.init_db()."""
    try:
        with _conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS events(
                seq         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,          -- UTC ISO-8601 (Z)
                actor       TEXT,                   -- uid, or NULL/'-' when unauthenticated
                actor_name  TEXT,                   -- username at time of event (plaintext; low-sensitivity, aids review)
                src_ip      TEXT,                   -- resolved real client (see _resolve_client_ip; plaintext, detections filter on it)
                xff         TEXT,                   -- raw X-Forwarded-For header, kept verbatim for forensic double-checking
                event_type  TEXT NOT NULL,
                resource    TEXT,                   -- endpoint/path or logical target
                outcome     TEXT NOT NULL,          -- success | failure | denied | error
                corr_id     TEXT,                   -- request correlation id
                detail_enc  TEXT,                   -- Fernet-encrypted JSON (old->new values, etc.)
                prev_hash   TEXT,
                entry_hash  TEXT NOT NULL
            )""")
            # SEC-2 migration (2026-07-25): CREATE TABLE IF NOT EXISTS doesn't add columns to an
            # already-existing table -- a deployment with SEC-1 already live needs these ALTERed in.
            # Mirrors auth.py's own PRAGMA table_info migration pattern for recovery_key_hash.
            cols = {row["name"] for row in c.execute("PRAGMA table_info(events)").fetchall()}
            if "scheme" not in cols:
                # Existing rows predate the HMAC upgrade -- they were written with the plain-SHA256
                # scheme, so backfill them as 'sha256' (NOT the new default) rather than silently
                # relabeling history. New rows explicitly pass CURRENT_SCHEME at insert time below.
                c.execute("ALTER TABLE events ADD COLUMN scheme TEXT NOT NULL DEFAULT 'sha256'")
            if "chain_anchor" not in cols:
                c.execute("ALTER TABLE events ADD COLUMN chain_anchor INTEGER NOT NULL DEFAULT 0")
            c.execute("CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_events_type ON events(event_type)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_events_actor ON events(actor)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_events_outcome ON events(outcome)")
        try:
            os.chmod(AUDIT_DB, 0o600)
        except Exception:
            pass
    except Exception as e:
        print(f"[audit] init_db failed (audit logging disabled this run): {e}", flush=True)


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def request_source(request):
    """Extract (src_ip, xff) from a Starlette/FastAPI request, with src_ip already RESOLVED to the
    best-known real client (see _resolve_client_ip above) -- this is what gets persisted to the
    events table's src_ip column and is what every consumer (the audit viewer, SEC-4 detections,
    rate limiting) should treat as "the client." The raw XFF header string is returned alongside it
    and stored too, for forensic double-checking, but src_ip is the one to actually use. Never
    raises."""
    raw_ip, xff = None, None
    try:
        raw_ip = request.client.host if request and request.client else None
        if request is not None:
            xff = request.headers.get("x-forwarded-for")
    except Exception:
        pass
    return _resolve_client_ip(raw_ip, xff), xff


def client_ip(request):
    """Public one-call helper for non-audit consumers (app.py's login/recovery/job-trigger rate
    limiters) that need the same resolved real-client IP audit logging uses, without needing to
    know about xff/_resolve_client_ip themselves. Same trust rules as request_source() above."""
    return request_source(request)[0]


def emit(event_type, outcome="success", *, actor=None, actor_name=None, src_ip=None, xff=None,
         resource=None, detail=None, corr_id=None, request=None):
    """Record one security event. NEVER raises (see module SAFETY CONTRACT).

    Pass either explicit src_ip/xff, or a `request` to have them extracted. `detail` is any
    JSON-serializable dict; it is encrypted at rest and OMITTED from the SIEM mirror.
    """
    try:
        if request is not None and src_ip is None and xff is None:
            src_ip, xff = request_source(request)
        ts = _now_iso()
        detail_json = ""
        if detail:
            try:
                detail_json = json.dumps(detail, default=str, sort_keys=True, separators=(",", ":"))
            except Exception:
                detail_json = json.dumps({"_unserializable": str(detail)})
        try:
            from secrets_crypto import encrypt as _encrypt
            detail_enc = _encrypt(detail_json) if detail_json else None
        except Exception:
            # If encryption is unavailable for any reason, fail CLOSED on the sensitive blob rather
            # than writing it in plaintext -- the event still records (type/outcome/actor/ip), just
            # without its detail payload.
            detail_enc = None

        # Bind the chain hash to what is RECOVERABLE at verify time (the stored, encrypted detail).
        # If the detail could not be encrypted/stored (detail_enc is None), it contributes an empty
        # string to the hash on both sides, keeping verify_chain() symmetric with emit().
        detail_hash_src = detail_json if detail_enc else ""
        core = "|".join([
            ts, str(actor or "-"), str(src_ip or "-"), str(event_type),
            str(resource or "-"), str(outcome), str(corr_id or "-"),
        ])
        with _lock:
            with _conn() as c:
                row = c.execute("SELECT entry_hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
                prev_hash = row["entry_hash"] if row else ("GENESIS-" + _sha(AUDIT_DB))
                entry_hash = _entry_hash(CURRENT_SCHEME, prev_hash, core, detail_hash_src)
                c.execute(
                    """INSERT INTO events
                       (ts, actor, actor_name, src_ip, xff, event_type, resource, outcome,
                        corr_id, detail_enc, prev_hash, entry_hash, scheme, chain_anchor)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                    (ts, actor, actor_name, src_ip, xff, event_type, resource, outcome,
                     corr_id, detail_enc, prev_hash, entry_hash, CURRENT_SCHEME),
                )
        _mirror_jsonl({
            "ts": ts, "actor": actor, "actor_name": actor_name, "src_ip": src_ip,
            "event_type": event_type, "resource": resource, "outcome": outcome,
            "corr_id": corr_id, "entry_hash": entry_hash,
        })
        # Notification hook (2026-07-26) -- the "detect" stage (this function) now also feeds the
        # "notify" stage: notifications.py decides whether this event is push/email-worthy and, if
        # so, dispatches on its own background thread. Lazy-imported (rather than a top-of-file
        # import) so a broken notifications.py can NEVER prevent audit.py itself from loading --
        # this module's own SAFETY CONTRACT (never raise, never block the write above) extends to
        # this call: best-effort only, wrapped separately from the try/except that guards the
        # actual DB write so a notify failure is never confused with an audit-write failure.
        try:
            import notifications
            notifications.notify_from_event(event_type, outcome, actor_name, resource, detail, ts)
        except Exception as e:
            print(f"[audit] notify hook failed for {event_type}: {e}", flush=True)
        return True
    except Exception as e:
        print(f"[audit] emit failed for {event_type}/{outcome}: {e}", flush=True)
        return False


def _mirror_jsonl(record):
    """Best-effort append to the SIEM log source. Failure here never fails the DB write above."""
    try:
        os.makedirs(os.path.dirname(AUDIT_JSONL) or ".", exist_ok=True)
        line = json.dumps(record, default=str, separators=(",", ":"))
        with open(AUDIT_JSONL, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        try:
            os.chmod(AUDIT_JSONL, 0o600)
        except Exception:
            pass
    except Exception as e:
        print(f"[audit] jsonl mirror failed: {e}", flush=True)


# ---------------------------------------------------------------------------
# Read + integrity helpers. The read-only, IP-REDACTED VIEWER ENDPOINT + UI is SEC-2; these are the
# primitives it will build on, and verify_chain() is what makes the log trustworthy as evidence.
# ---------------------------------------------------------------------------
def read_recent(limit=200, redact_ip=False):
    """Most-recent-first events with the detail payload decrypted. `redact_ip` masks the last octet
    (IPv4) for the SEC-2 viewer's privacy-preserving default. Returns a list of dicts."""
    out = []
    try:
        with _conn() as c:
            rows = c.execute("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (int(limit),)).fetchall()
        for r in rows:
            detail = None
            if r["detail_enc"]:
                try:
                    from secrets_crypto import decrypt_or_legacy as _dec
                    raw = _dec(r["detail_enc"])
                    detail = json.loads(raw) if raw else None
                except Exception:
                    detail = {"_undecryptable": True}
            ip = r["src_ip"]
            if redact_ip and ip and ip.count(".") == 3:
                ip = ".".join(ip.split(".")[:3] + ["x"])
            out.append({
                "seq": r["seq"], "ts": r["ts"], "actor": r["actor"], "actor_name": r["actor_name"],
                "src_ip": ip, "event_type": r["event_type"], "resource": r["resource"],
                "outcome": r["outcome"], "corr_id": r["corr_id"], "detail": detail,
            })
    except Exception as e:
        print(f"[audit] read_recent failed: {e}", flush=True)
    return out


def query_core(event_type=None, resource_prefix=None, since_ts=None, limit=2000):
    """SEC-4 (2026-07-25) -- read-only helper for the detection engine (detections.py) and any
    future consumer that needs to scan the CORE plaintext columns (ts/actor/actor_name/src_ip/
    event_type/resource/outcome) WITHOUT decrypting `detail` -- detection rules match on patterns
    over these columns, not on the encrypted payload, so this is both simpler and faster than
    routing every rule through read_recent()'s decrypt step. Oldest-first (rules reason about
    sequences of events over time). Never raises -- returns [] on any failure, same fail-safe
    contract as the rest of this module (a broken detection query must not break request paths
    that happen to call into it)."""
    try:
        sql = "SELECT seq, ts, actor, actor_name, src_ip, event_type, resource, outcome FROM events WHERE 1=1"
        args = []
        if event_type:
            sql += " AND event_type=?"
            args.append(event_type)
        if resource_prefix:
            sql += " AND resource LIKE ?"
            args.append(resource_prefix + "%")
        if since_ts:
            sql += " AND ts >= ?"
            args.append(since_ts)
        sql += " ORDER BY seq ASC LIMIT ?"
        args.append(int(limit))
        with _conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[audit] query_core failed: {e}", flush=True)
        return []


def verify_chain():
    """Recompute the hash chain across every row and confirm it matches what was stored. Returns
    {"ok": bool, "checked": n, "broken_at_seq": seq|None, "reason": str|None}. This is the control
    that makes the log trustworthy as audit evidence (detects an edited/deleted historical row).

    Handles two boundary cases so a healthy, untampered log always verifies ok=True:
      (a) mixed schemes -- a row written before the SEC-2 HMAC upgrade recomputes with plain
          SHA-256 (its own `scheme` column says so); a row written after recomputes with HMAC.
          The prev_hash -> entry_hash CHAIN LINK between an old-scheme row and a new-scheme row is
          still checked exactly as normal (linkage doesn't care which formula produced either
          hash, only that they match).
      (b) a prior retention purge -- the row flagged chain_anchor=1 (the oldest survivor of a
          purge_expired() call) is exempt from the "prev_hash must equal the previous row's
          entry_hash" check, since that previous row was deliberately, auditably deleted. Its OWN
          entry_hash is still fully verified (tamper-detection on that row's own content is not
          weakened) -- only the link to a now-absent predecessor is treated as trusted-by-purge-
          record rather than recomputed. The true first-ever row (index 0, chain_anchor=0, never
          purged) still gets the strict GENESIS check -- only a genuine post-purge anchor skips it.
    """
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT seq, ts, actor, src_ip, event_type, resource, outcome, corr_id, "
                "detail_enc, prev_hash, entry_hash, scheme, chain_anchor "
                "FROM events ORDER BY seq ASC"
            ).fetchall()
        prev = None
        for i, r in enumerate(rows):
            is_anchor = bool(r["chain_anchor"])
            if i == 0 and not is_anchor:
                # True, never-purged genesis: prev_hash must match the computed seed exactly.
                if r["prev_hash"] != ("GENESIS-" + _sha(AUDIT_DB)):
                    return {"ok": False, "checked": r["seq"], "broken_at_seq": r["seq"],
                            "reason": "bad_genesis"}
            elif not is_anchor:
                if r["prev_hash"] != prev:
                    return {"ok": False, "checked": r["seq"], "broken_at_seq": r["seq"],
                            "reason": "prev_hash_mismatch"}
            # else: post-purge anchor row -- prev-link check intentionally skipped (see docstring).
            detail_json = ""
            if r["detail_enc"]:
                try:
                    from secrets_crypto import decrypt_or_legacy as _dec
                    detail_json = _dec(r["detail_enc"]) or ""
                except Exception:
                    detail_json = ""
            core = "|".join([
                r["ts"], str(r["actor"] or "-"), str(r["src_ip"] or "-"), str(r["event_type"]),
                str(r["resource"] or "-"), str(r["outcome"]), str(r["corr_id"] or "-"),
            ])
            recomputed = _entry_hash(r["scheme"] or "sha256", r["prev_hash"], core, detail_json)
            if recomputed != r["entry_hash"]:
                return {"ok": False, "checked": r["seq"], "broken_at_seq": r["seq"],
                        "reason": "entry_hash_mismatch"}
            prev = r["entry_hash"]
        return {"ok": True, "checked": len(rows), "broken_at_seq": None, "reason": None}
    except Exception as e:
        print(f"[audit] verify_chain failed: {e}", flush=True)
        return {"ok": False, "checked": 0, "broken_at_seq": None, "error": str(e)}


def purge_expired(retention_days=None, actor_name="system", request=None):
    """SEC-2 retention/disposal (ISO A.8.10, NIST AU-11, SOC 2 CC6.5/Privacy P4): delete the
    oldest contiguous prefix of events whose ts is older than the retention window, then re-anchor
    the chain at the new oldest surviving row (chain_anchor=1) so verify_chain() doesn't report a
    false break at the purge boundary. The purge itself is logged as its own 'audit.retention.purged'
    event -- count purged, oldest/newest ts purged, retention_days used, and the new anchor row's
    seq -- so disposal is part of the evidence trail, not a silent DELETE (see module docstring).

    Safe to call repeatedly / on a schedule (e.g. a daily job): a no-op (purged=0) when nothing is
    past the retention window yet. Never raises. Returns {"purged": n, "anchor_seq": seq|None}."""
    try:
        days = int(retention_days if retention_days is not None else AUDIT_RETENTION_DAYS)
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        with _lock:
            with _conn() as c:
                to_purge = c.execute(
                    "SELECT seq, ts FROM events WHERE ts < ? ORDER BY seq ASC", (cutoff,)
                ).fetchall()
                if not to_purge:
                    return {"purged": 0, "anchor_seq": None}
                oldest_purged_ts, newest_purged_ts = to_purge[0]["ts"], to_purge[-1]["ts"]
                max_purge_seq = to_purge[-1]["seq"]
                purged_count = len(to_purge)
                c.execute("DELETE FROM events WHERE seq <= ?", (max_purge_seq,))
                anchor = c.execute(
                    "SELECT seq FROM events ORDER BY seq ASC LIMIT 1"
                ).fetchone()
                anchor_seq = None
                if anchor:
                    c.execute("UPDATE events SET chain_anchor=1 WHERE seq=?", (anchor["seq"],))
                    anchor_seq = anchor["seq"]
        emit("audit.retention.purged", actor_name=actor_name, request=request,
             resource="audit.retention",
             detail={"purged_count": purged_count, "oldest_purged_ts": oldest_purged_ts,
                     "newest_purged_ts": newest_purged_ts, "retention_days": days,
                     "new_anchor_seq": anchor_seq})
        return {"purged": purged_count, "anchor_seq": anchor_seq}
    except Exception as e:
        print(f"[audit] purge_expired failed: {e}", flush=True)
        return {"purged": 0, "anchor_seq": None, "error": str(e)}


def start_retention_thread(check_interval_s=None):
    """Background daemon thread: calls purge_expired() once per AUDIT_RETENTION_CHECK_INTERVAL_S
    (default 24h). Mirrors tls.py's start_renewal_thread() pattern -- a periodic security-hygiene
    check that runs automatically without needing a slot in the user-facing TASK SCHEDULE UI
    (that UI is for user-configurable analysis jobs; this is internal housekeeping, same category
    as certificate auto-renewal). Called once at app startup (app.py, alongside audit.init_db()).
    Safe to call in a sandbox/test process too -- daemon=True means it never blocks process exit."""
    interval = int(check_interval_s or os.environ.get("AUDIT_RETENTION_CHECK_INTERVAL_S", "86400"))

    def _loop():
        while True:
            try:
                purge_expired()
            except Exception as e:
                print(f"[audit] retention thread error: {e}", flush=True)
            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True, name="audit-retention")
    t.start()
    return t


if __name__ == "__main__":
    # Smoke test: `python audit.py` -- emits a couple of events, verifies the chain, and exercises
    # purge_expired()'s re-anchoring. NOTE: AUDIT_HMAC_KEY_FILE is loaded at IMPORT time (module
    # load), so reassigning AUDIT_DB/AUDIT_JSONL here (as below) does NOT also relocate the HMAC
    # key file -- run with AUDIT_HMAC_KEY_FILE set in the environment beforehand for a fully
    # isolated run; otherwise this falls back to an in-memory-only key (printed warning), which is
    # still functionally correct for a one-off smoke test, just not what a real deployment does.
    import tempfile
    d = tempfile.mkdtemp()
    AUDIT_DB = os.path.join(d, "audit.db")
    AUDIT_JSONL = os.path.join(d, "audit.jsonl")
    init_db()
    emit("auth.login.success", actor="u1", actor_name="jason", src_ip="203.0.113.9",
         resource="/api/auth/login")
    emit("config.changed", actor="u1", actor_name="jason", resource="/api/policy",
         detail={"field": "target_allocation", "old": "60/40", "new": "70/30"})
    print("recent:", json.dumps(read_recent(redact_ip=True), indent=2))
    print("verify:", verify_chain())
    print("purge (0-day retention -- purges everything but re-anchors):", purge_expired(retention_days=0))
    print("verify after purge:", verify_chain())
