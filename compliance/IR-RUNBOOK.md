# Incident Response Runbook

**Scope:** this document covers incident response for a single-tenant, self-hosted deployment of
Fiduciary (one household's data, one host). It is written to be genuinely followable against the
app as it exists today — every step below points at a real endpoint, script, or UI panel, not an
aspirational one. Where the app's scale changes the "textbook" answer (e.g. no SOC to page, no
multi-tenant blast-radius question), that's stated plainly rather than glossed over.

**Control mapping:** ISO/IEC 27001:2022 A.5.24 (incident management planning), A.5.25 (assess and
decide on security events), A.5.26 (response to incidents), A.5.27 (learning from incidents),
A.5.28 (evidence collection) · NIST SP 800-53 Rev5 IR-4 (incident handling), IR-5 (monitoring),
IR-6 (reporting), IR-8 (incident response plan) · SOC 2 (2017 TSC) CC7.3 (evaluates security
events), CC7.4 (responds to incidents), CC7.5 (identifies/develops/implements recovery activities).

## 1. Detect

Two sources feed detection, both already running:

- **The rules engine** (`detections.py`), which sweeps the audit log every 5 minutes (or on demand
  via SETTINGS → SECURITY → SECURITY AUDIT LOG → "RUN DETECTIONS NOW", or `POST
  /api/detections/run`) and fires `detection.alert` events for: a failed-login burst (5+ failed
  logins for one username within 10 minutes), a first-seen source IP for an account with prior
  login history, or MFA being disabled followed by a sensitive change (secret/config/account
  action) from the same actor within 30 minutes — the classic account-takeover sequence. The
  current rule catalog is always visible at `GET /api/detections/rules`.
- **Manual observation** — anyone with access to the household (owner or a viewer account) may
  notice something off (an unrecognized login banner, a security-settings change they didn't make,
  a device they don't recognize).

Either path lands in the same place: the **SECURITY AUDIT LOG** (SETTINGS → SECURITY), which is
append-only, HMAC hash-chained (tamper-evident — `GET /api/audit/verify` reports whether the chain
is intact), and Fernet-encrypted at rest. An alert IS an event in this log (`event_type:
"detection.alert"`), not a separate system to check — there is no outbound paging/email/Slack
integration in this app (documented scope decision in `detections.py`; wiring one in is a
follow-up, not a gap that was missed).

## 2. Triage

For any alert or suspicious observation:

1. Open SETTINGS → SECURITY → SECURITY AUDIT LOG and read the surrounding events for the affected
   account — actor, source IP, event type, outcome, and (where present) the detail payload.
2. Confirm chain integrity first (`GET /api/audit/verify` / the panel's status line). If the chain
   reports broken, treat this as evidence tampering, not just an intrusion — see §4a below; the
   chain protects against a compromised *client*, not a compromised *host/root* (documented limit
   of the design — a root-level attacker who can edit the SQLite file and both the HMAC key and
   Fernet key on disk can rebuild a consistent chain; the control raises the bar, it doesn't claim
   to be unbeatable).
3. Classify severity:
   - **Low** — a single failed login, a first-seen IP that the household recognizes (e.g. a new
     phone or a trip). Note it, no action needed.
   - **Medium** — a failed-login burst against a real account, or a first-seen IP nobody
     recognizes. Proceed to containment for that account.
   - **High** — MFA disabled followed by a sensitive change neither household member made, or a
     broken hash chain. Proceed to containment immediately and assume compromise.

## 3. Contain

Available containment actions, in the order they should typically be reached for:

1. **Force a logout / rotate the affected account's credentials.** Change the account's password
   (SETTINGS → SECURITY → change password) — this invalidates the session cookie the attacker may
   be using (sessions are tied to server-side state, not just a bearer token).
2. **Rotate MFA.** If MFA was disabled by the attacker, re-enable it and regenerate backup codes;
   if TOTP itself may be compromised (rare — the shared secret never leaves the server except at
   setup), disable and re-set-up from scratch.
3. **Regenerate the recovery key.** SETTINGS → SECURITY → "REGENERATE RECOVERY KEY" invalidates
   the old one immediately (only its Argon2 hash is ever stored, so the old key becomes useless the
   moment a new one is issued).
4. **Remove a compromised viewer account.** An admin can delete any viewer account from HOUSEHOLD
   ACCOUNTS (SETTINGS → SECURITY) — instant, and the account can be recreated cleanly afterward.
   The sole admin account cannot be self-deleted while a viewer account still exists (a lockout
   guard added under SEC-5), so there is always a way back in during this process.
5. **Rotate the encryption key.** If secret material itself (not just credentials) is suspected
   exposed, SETTINGS → SECURITY → ENCRYPTION KEY → "ROTATE KEY NOW" issues a new Fernet key
   immediately for all *new* writes (existing ciphertext keeps decrypting under its original,
   retained key — ✱rotation narrows exposure going forward; it does not itself re-encrypt history,
   see §4b).
6. **Take the host offline**, if the host itself (not just the app) is suspected compromised — this
   is a manual, out-of-app step (stop the container / pull the network cable), appropriate only for
   the "High" severity + broken-chain case.

## 4. Eradicate

- **4a — broken hash chain:** this means either a real attacker altered stored events, or (far more
  likely in practice) a legitimate operational mistake — a restore from an older backup, a manual
  edit to the SQLite file, or a bug. Either way: stop, do not run retention/disposal until resolved,
  and compare against the most recent known-good backup (see §5) to identify exactly which rows
  diverge before deciding whether to restore or to accept the divergence with a documented note.
- **4b — key rotation is not remediation by itself.** Rotating the encryption key stops an attacker
  who has the *old* key from reading *new* secrets, but any secret encrypted under the old key
  before rotation is still only as safe as that old key was — if a key file itself was exfiltrated,
  the actual remediation is changing every real credential the exposed secrets protected (bank/
  brokerage passwords, API tokens, etc.), not just rotating Fiduciary's own key.
- **Re-verify RBAC.** Confirm HOUSEHOLD ACCOUNTS lists only accounts that should exist, and that
  the sole 'admin' row is the household's own — role is always server-assigned (never
  client-supplied), so this should be structurally impossible to have drifted, but verify anyway.

## 5. Recover

- Confirm `GET /api/audit/verify` reports `ok: true` before resuming normal operation.
- If a restore from backup was needed, confirm the app boots clean, the sole-admin account exists
  and can log in with MFA, and TLS/HTTPS state (if configured) is as expected — a fresh SQLite file
  replacing a corrupted one is the recovery path; there is no in-app "undo" for the audit log
  itself by design (append-only means there is nothing to selectively revert).
- Resume the automatic retention job (`start_retention_thread`) and detections sweep
  (`start_detections_thread`) if either was paused during investigation — both start automatically
  on app boot, so a simple restart is sufficient.

## 6. Post-incident

- Record what happened, in this file's revision history or a dated addendum: what was detected,
  how, what containment steps were taken, and what (if anything) should change as a result (a new
  detection rule, a tighter RBAC boundary, a process change).
- If the incident revealed a real gap (e.g. a scenario none of the three current rules would have
  caught), that becomes a new, scoped detections.py rule rather than a vague "improve monitoring"
  note — the existing three rules were each chosen because they're genuinely checkable against this
  app's own event vocabulary; new ones should meet the same bar.
- This is a household-scale app: there is no SOC, no external reporting obligation triggered by an
  incident here, and no other tenant whose blast radius could be affected. The runbook is scoped to
  match that reality rather than importing enterprise assumptions (a 24/7 SOC handoff, a legal/PR
  workstream, a multi-tenant containment matrix) that don't apply.

---
*Companion controls: SEC-1/SEC-2 (audit log + tamper-evident chain), SEC-3 (key rotation), SEC-4
(detections engine, this runbook's detect stage), SEC-5 (RBAC). See `RESUME-TRACKER.md` for build
history and `SECURITY-CONTROLS-GAP-ANALYSIS.md` for the full control-mapping rationale.*
