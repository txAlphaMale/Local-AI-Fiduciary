"""
audit.py -- SEC-1: the app's security audit / event log (append-only, encrypted-at-rest,
hash-chained), plus a plaintext JSONL mirror intended as a SIEM-ingestible log source.

WHY (RESUME-TRACKER.md "SECURITY-PORTFOLIO CONTROL BACKLOG", SEC-1; SECURITY-CONTROLS-GAP-ANALYSIS.md
C-1): before this module, the app captured `request.client.host` only to feed the in-memory rate
limiter (auth.rate_limited) -- nothing was ever persisted, so there was no record of who logged in,
from where, when, or what security-relevant change they made. That is the single control most
heavily weighted by SOC 2 / ISO 27001 / NIST audits, and it was entirely absent.

CONTROL MAPPING:
  - ISO/IEC 27001:2022  A.8.15 (Logging), A.8.16 (Monitoring activities)
  - NIST SP 800-53 Rev5 AU-2 (event selection), AU-3 (content of records), AU-12 (generation),
                        AU-9 (protection of audit info -- partial; full HMAC-keyed tamper-evidence
                        + retention + redacted viewer is SEC-2)
  - SOC 2 (2017 TSC)    CC7.2 (detection/monitoring of anomalies)
  - ISO/IEC 27701:2019  privacy event logging (extends the above to PII-affecting events)

DESIGN:
  * Own SQLite file (AUDIT_DB), 0600, separate from auth.db/store.db/chat.db -- keeping the security
    event trail physically separate from application data is itself a minor hardening property and
    mirrors auth.py's rationale for a dedicated credentials DB.
  * APPEND-ONLY: this module never exposes UPDATE or DELETE. Retention-based disposal is SEC-2.
  * ENCRYPTED AT REST: the free-form `detail` (which may hold old->new config values, resource
    identifiers, usernames) is JSON-serialized and encrypted with the app's shared Fernet key via
    secrets_crypto -- same primitive/key already protecting every other sensitive column. The
    indexable/queryable core columns (ts, event_type, outcome, actor id) stay plaintext for the same
    reason the rest of the app leaves lookup keys plaintext: Fernet ciphertext is non-deterministic
    and can't be filtered/sorted on. src_ip is kept plaintext deliberately -- it is operational
    security data a detection rule must filter on; redaction happens at the VIEWER layer (SEC-2),
    not at rest.
  * HASH-CHAINED: each row stores prev_hash + entry_hash where
        entry_hash = sha256(prev_hash + canonical_core + sha256(detail_json)).
    This makes silent edits/deletes of historical rows detectable (verify_chain()). NOTE: a plain
    SHA-256 chain detects casual tampering but an attacker with write access to the DB could
    recompute the chain forward; upgrading to an HMAC keyed with separately-held key material is
    SEC-2 (AU-9 proper). Called out here so the boundary is honest.
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
import threading
import datetime

AUDIT_DB = os.environ.get("AUDIT_DB", "/data/audit.db")
AUDIT_JSONL = os.environ.get("AUDIT_JSONL", "/data/audit.jsonl")

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
)


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
                src_ip      TEXT,                   -- request.client.host (plaintext: detections filter on it)
                xff         TEXT,                   -- X-Forwarded-For chain when behind the planned reverse proxy
                event_type  TEXT NOT NULL,
                resource    TEXT,                   -- endpoint/path or logical target
                outcome     TEXT NOT NULL,          -- success | failure | denied | error
                corr_id     TEXT,                   -- request correlation id
                detail_enc  TEXT,                   -- Fernet-encrypted JSON (old->new values, etc.)
                prev_hash   TEXT,
                entry_hash  TEXT NOT NULL
            )""")
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
    """Extract (src_ip, xff) from a Starlette/FastAPI request. XFF matters once the app sits behind
    the reverse proxy the demo/AWS plans put in front of it -- request.client.host would otherwise be
    the proxy, not the real client. Never raises."""
    src_ip, xff = None, None
    try:
        src_ip = request.client.host if request and request.client else None
        if request is not None:
            xff = request.headers.get("x-forwarded-for")
    except Exception:
        pass
    return src_ip, xff


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
                entry_hash = _sha(prev_hash + "\n" + core + "\n" + _sha(detail_hash_src))
                c.execute(
                    """INSERT INTO events
                       (ts, actor, actor_name, src_ip, xff, event_type, resource, outcome,
                        corr_id, detail_enc, prev_hash, entry_hash)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (ts, actor, actor_name, src_ip, xff, event_type, resource, outcome,
                     corr_id, detail_enc, prev_hash, entry_hash),
                )
        _mirror_jsonl({
            "ts": ts, "actor": actor, "actor_name": actor_name, "src_ip": src_ip,
            "event_type": event_type, "resource": resource, "outcome": outcome,
            "corr_id": corr_id, "entry_hash": entry_hash,
        })
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


def verify_chain():
    """Recompute the hash chain across every row and confirm it matches what was stored. Returns
    {"ok": bool, "checked": n, "broken_at_seq": seq|None}. This is the control that makes the log
    trustworthy as audit evidence (detects an edited/deleted historical row)."""
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT seq, ts, actor, src_ip, event_type, resource, outcome, corr_id, "
                "detail_enc, prev_hash, entry_hash FROM events ORDER BY seq ASC"
            ).fetchall()
        prev = None
        for r in rows:
            expected_prev = prev if prev is not None else ("GENESIS-" + _sha(AUDIT_DB))
            if r["prev_hash"] != expected_prev:
                return {"ok": False, "checked": r["seq"], "broken_at_seq": r["seq"]}
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
            recomputed = _sha(r["prev_hash"] + "\n" + core + "\n" + _sha(detail_json))
            if recomputed != r["entry_hash"]:
                return {"ok": False, "checked": r["seq"], "broken_at_seq": r["seq"]}
            prev = r["entry_hash"]
        return {"ok": True, "checked": len(rows), "broken_at_seq": None}
    except Exception as e:
        print(f"[audit] verify_chain failed: {e}", flush=True)
        return {"ok": False, "checked": 0, "broken_at_seq": None, "error": str(e)}


if __name__ == "__main__":
    # Smoke test: `python audit.py` -- emits a couple of events and verifies the chain.
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
