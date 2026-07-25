# Fiduciary — Data Retention Schedule

**Scope:** every distinct data class stored by `portfolio-api`, verified against the actual code
(`audit.py`, `auth.py`, `store.py`, `app.py`, `knowledge.py`) rather than assumed. Where retention
is genuinely open-ended today, that is stated plainly as a gap, not glossed over — a household app
holding financial PII should still have a deliberate answer for "how long does this live and why,"
even where the honest current answer is "until the operator deletes it."

Controls: ISO/IEC 27001:2022 **A.8.10** (Information deletion); NIST SP 800-53 **SI-12**
(information management and retention), **AU-11** (audit record retention); SOC 2 (2017 TSC)
**CC6.5**, Privacy **P4**.

| Data class | Current retention | Disposal mechanism | Rationale |
|---|---|---|---|
| Security audit log events (`audit.py`) | 365 days (`AUDIT_RETENTION_DAYS`, configurable) | Automated daily background job (`purge_expired`/`start_retention_thread`) deletes the oldest contiguous prefix past the window and re-anchors the hash chain; the purge itself is logged as its own `audit.retention.purged` event | Long enough to investigate a slow-burn incident, short enough to bound storage and reduce the value of the log as a target; disposal is itself evidenced, not a silent DELETE |
| Session cookies | 30 days (`SESSION_MAX_AGE_DAYS`, configurable) | Cookie expiry + server-side session-store lifetime | Balances re-login friction against the exposure window of a stolen session |
| Encryption keys (Fernet keyring, audit HMAC key) | Indefinite — every key version ever issued is retained forever by design | None (deliberate) | Envelope encryption requires old key versions to remain available to decrypt data that was never re-encrypted under a newer key (SEC-3); deleting an old key version would make its ciphertext permanently unrecoverable |
| Recovery keys (account recovery, per-account) | The raw key is never stored at all — shown once at creation/regeneration, only its Argon2 hash persists | N/A — nothing to dispose of; regenerating one immediately invalidates the prior hash | Matches how the password itself is handled; the "secret" is never at rest in a form an attacker with DB access could use directly |
| Financial config & snapshots (policy, retirement config, accounts, insurance, scanner, etc. — `store.py` Stage A/B) | Indefinite — this is the app's working data, meant to persist for the life of the account | Manual only (edited or cleared by the user through the app's own UI) | This is the actual product data the household relies on; there is no "expiry" concept that would make sense here, unlike a security log |
| Chat transcripts (`chat.db`) | Indefinite per session until the user deletes that session | Manual only (per-session delete in the CHAT tab) | May contain sensitive financial context discussed with the Fiduciary/Strategist personas; **flagged gap** — no automatic expiry exists yet, unlike the audit log |
| Value/price history, Strategist run log (`store.py` typed tables) | Indefinite (2026-07-24 migration moved these off an in-memory trim-on-write scheme into real tables); `STRAT_RUN_LOG_MAX`/`limit` params now only cap what's *returned* by an API call, not what's stored | Manual only | Historical performance/decision data is valuable to keep for the app's own trend analysis; no PII-specific reason to expire it, but see the "flagged gaps" note below |
| Verifier log (`VERIFIER_LOG_MAX`) | Capped by line COUNT (1000 lines), not by time | Oldest lines silently dropped once the cap is exceeded | Operational debug log, not a compliance-relevant record — count-based capping is appropriate here |
| On-demand research cache | Capped by ticker COUNT (last 20), not by time | Oldest ticker's cache entry dropped once the cap is exceeded | Pure performance cache, no retention-sensitive content |
| Knowledge/RAG documents (`knowledge/` folder + vector index) | Indefinite until the user removes the source file | Manual only — incremental reindex drops a document's chunks/vectors once its source file is deleted | User-curated reference material (e.g. an IPS); the user IS the retention decision-maker for their own uploaded documents |
| TLS certificates & private keys | Indefinite until explicitly rotated or cleared | Manual (`/api/tls/clear`, cert regeneration/import, ACME renewal) | Standard operational lifecycle, not a data-privacy retention question |

## Flagged gaps (honest, not yet remediated)

1. **Chat transcripts have no automatic expiry.** Given they can contain sensitive financial
   context, a time-based or count-based automatic disposal (mirroring the audit log's pattern)
   would close this gap. Not yet built.
2. **No formal per-data-class retention *policy document* existed before this schedule** — the
   behaviors above were each independently reasonable engineering decisions made at different
   times, not derived from a single stated retention policy. This document is the first pass at
   making that policy explicit and consistent; SEC-8 (privacy program) will extend it with a
   DSAR-driven delete workflow that can act across every row above from one place.

---
*Companion documents: `compliance/RISK-REGISTER.md`, `compliance/STATEMENT-OF-APPLICABILITY.md`
(see A.8.10), `compliance/IR-RUNBOOK.md`.*
