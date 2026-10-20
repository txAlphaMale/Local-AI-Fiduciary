# Fiduciary — ISO/IEC 27001:2022 Annex A Statement of Applicability

**Scope:** the `portfolio-api` application and its data volume, self-hosted, single-household deployment. Authored as a portfolio/demo GRC artifact, not a certified third-party audit — every "Applied"/"Partial" status names real, checkable evidence in this codebase; every "N/A" is justified by this deployment's actual scale (no employees, no facility, single tenant), not used to hide a real gap.

**Summary:** 32/93 Applied · 23/93 Partial · 38/93 Not Applicable.

---

## Organizational controls (37)

| Control | Title | Status | Rationale / Evidence |
|---|---|---|---|
| A.5.1 | Policies for information security | **Partial** | No single top-level ISMS policy document, but security rationale is documented inline throughout the codebase (threat-model comments) and formalized here in RESUME-TRACKER.md / this Statement of Applicability. _(`RESUME-TRACKER.md, this document`)_ |
| A.5.2 | Information security roles and responsibilities | **N/A** | Single-operator household app -- no organizational roles to assign beyond the app's own admin/viewer RBAC (see 5.15/5.18/8.2). |
| A.5.3 | Segregation of duties | **N/A** | One operator. RBAC (5.15) separates admin vs. viewer capability within the app, but there is no multi-person operational team to segregate duties across. |
| A.5.4 | Management responsibilities | **N/A** | No management hierarchy -- single household operator. |
| A.5.5 | Contact with authorities | **N/A** | No regulatory relationship; not a regulated financial institution. |
| A.5.6 | Contact with special interest groups | **N/A** | No formal ISAC/threat-sharing membership for a personal project. |
| A.5.7 | Threat intelligence | **Partial** | No subscribed feed, but dependency/CVE awareness is informal via GitHub's own advisories; SEC-7b (CI dependency scanning) formalizes this. _(`compliance/RISK-REGISTER.md`)_ |
| A.5.8 | Information security in project management | **Applied** | Every feature build in this engagement is annotated with its control mapping before/while being written (see RESUME-TRACKER.md's SEC-1..SEC-8 backlog) -- security is designed in, not bolted on after. _(`RESUME-TRACKER.md`)_ |
| A.5.9 | Inventory of information and other associated assets | **Applied** | The app's own data model IS the asset inventory -- every table/JSON file in store.py and SECRET_SPECS/CONFIG_SPECS is a documented, named data asset. _(`portfolio-api/store.py, secrets_crypto.py`)_ |
| A.5.10 | Acceptable use of information and other associated assets | **N/A** | Single-user/household context -- no separate user population needing an AUP. |
| A.5.11 | Return of assets | **N/A** | No employees/contractors with issued assets to return. |
| A.5.12 | Classification of information | **Partial** | Data is informally classified by sensitivity throughout code comments (e.g. "real financial figures" vs. generic) and by gitignore boundaries (compliance/ public, docs/ internal, data/ runtime-sensitive), but no formal classification schema/labels exist. _(`.gitignore`)_ |
| A.5.13 | Labelling of information | **N/A** | No formal labelling scheme -- see 5.12. |
| A.5.14 | Information transfer | **Partial** | All data transfer is either same-host (no network hop) or over TLS to the browser (see 8.24); outbound calls to market-data/LLM providers carry no PII by design in most cases, but this hasn't been formally reviewed provider-by-provider. _(`tls.py`)_ |
| A.5.15 | Access control | **Applied** | Two-role RBAC (admin / viewer) enforced centrally in the request gate; every write path is denied unless explicitly allowlisted for a viewer session (deny-by-default). _(`portfolio-api/app.py _security_and_auth_gate, _VIEWER_ALLOWED_WRITE_PATHS`)_ |
| A.5.16 | Identity management | **Applied** | Each account is a distinct row (uid, username, role) with its own credentials and MFA state; exactly one admin identity, viewers created only through an explicit admin action. _(`portfolio-api/auth.py`)_ |
| A.5.17 | Authentication information | **Applied** | Argon2 password hashing, TOTP MFA (RFC 6238) with single-use hashed backup codes, and a separate host-CLI-recoverable recovery-key mechanism -- no plaintext credential storage anywhere. _(`portfolio-api/auth.py`)_ |
| A.5.18 | Access rights | **Applied** | Role is always server-assigned at account-creation time, never client-supplied -- structurally impossible for a session to self-escalate from viewer to admin. _(`portfolio-api/app.py POST /api/auth/users`)_ |
| A.5.19 | Information security in supplier relationships | **Partial** | External data providers (market-data APIs, LLM providers, SnapTrade) are used read-only with scoped credentials, but no formal supplier register/risk-tiering exists yet. _(`compliance/RISK-REGISTER.md`)_ |
| A.5.20 | Addressing information security within supplier agreements | **N/A** | Consumer-tier API terms-of-service, not negotiated contracts -- not applicable at this scale. |
| A.5.21 | Managing information security in the ICT supply chain | **Partial** | Dependencies are pinned in requirements files; SEC-7b adds automated CVE scanning in CI. _(`.github/workflows`)_ |
| A.5.22 | Monitoring, review and change management of supplier services | **N/A** | No SLA-bound supplier relationships to formally review. |
| A.5.23 | Information security for use of cloud services | **Partial** | Currently self-hosted (no cloud dependency for core data); a documented hybrid-LLM path exists for a future AWS deployment but isn't live. _(`AWS-HOSTING-GUIDE.md (internal)`)_ |
| A.5.24 | Information security incident management planning and preparation | **Applied** | A written, followable IR runbook exists mapping detect->triage->contain->eradicate->recover->post-incident to real app features. _(`compliance/IR-RUNBOOK.md`)_ |
| A.5.25 | Assessment and decision on information security events | **Applied** | The detections engine classifies findings (failed-login burst, first-seen IP, MFA-disable-then-change) and the IR runbook's triage section defines severity bands. _(`portfolio-api/detections.py, compliance/IR-RUNBOOK.md`)_ |
| A.5.26 | Response to information security incidents | **Applied** | Concrete containment actions are documented and directly executable from the app's own UI (password/MFA/recovery-key rotation, key rotation, viewer removal). _(`compliance/IR-RUNBOOK.md`)_ |
| A.5.27 | Learning from information security incidents | **Applied** | The runbook's post-incident section requires recording what happened and turning any real gap into a new, scoped detection rule rather than a vague action item. _(`compliance/IR-RUNBOOK.md`)_ |
| A.5.28 | Collection of evidence | **Applied** | The audit log is HMAC hash-chained (tamper-evident) specifically so it can serve as trustworthy evidence, not just a record. _(`portfolio-api/audit.py verify_chain()`)_ |
| A.5.29 | Information security during disruption | **Partial** | No formal continuity plan yet; SEC-7a (backup/restore) is the first concrete piece. _(`compliance/RISK-REGISTER.md`)_ |
| A.5.30 | ICT readiness for business continuity | **Partial** | SEC-7a: encrypted backup/restore scripts (evidenced restore test, byte-for-byte diff-verified, documented RPO/RTO) now have an actual scheduler wired up -- a systemd timer installer for local hosting, an EventBridge Scheduler rule for the AWS demo -- built 2026-07-27. Still Partial, not Applied: the installers/schedules themselves have not yet been run for real and confirmed live in either environment (see RESUME-TRACKER.md's 2026-07-27 backup-automation entry) -- flip to Applied once Jason confirms both. |
| A.5.31 | Legal, statutory, regulatory and contractual requirements | **N/A** | Personal household app, not a regulated entity offering services to others. |
| A.5.32 | Intellectual property rights | **N/A** | No third-party IP licensing concerns beyond standard open-source dependency licenses. |
| A.5.33 | Protection of records | **Applied** | Financial/PII data and the audit log are both encrypted at rest (Fernet) and access-gated; the audit log additionally can't be silently altered (hash chain). _(`portfolio-api/secrets_crypto.py, audit.py`)_ |
| A.5.34 | Privacy and protection of PII | **Partial** | Strong technical protection (encryption, access control) exists; the *programmatic* privacy controls (notice, ROPA, DSAR, DPIA) are SEC-8, still queued. _(`SEC-8 (queued)`)_ |
| A.5.35 | Independent review of information security | **N/A** | No external audit engaged for a personal project; this GRC pass itself is the closest analog, performed with an explicit instruction to correct rather than flatter. |
| A.5.36 | Compliance with policies, rules and standards for information security | **Partial** | This Statement of Applicability + the IR runbook + the risk register together are the compliance baseline being checked against, but no periodic formal compliance-review cadence exists yet. _(`compliance/`)_ |
| A.5.37 | Documented operating procedures | **Applied** | Deploy/verify steps are documented per feature (see RESUME-TRACKER.md's "Jason deploy + verify" sections) and the IR runbook is itself a documented operating procedure. _(`RESUME-TRACKER.md, compliance/IR-RUNBOOK.md`)_ |

## People controls (8)

| Control | Title | Status | Rationale / Evidence |
|---|---|---|---|
| A.6.1 | Screening | **N/A** | No employees to screen -- single household operator. |
| A.6.2 | Terms and conditions of employment | **N/A** | No employment relationship. |
| A.6.3 | Information security awareness, education and training | **N/A** | No workforce to train; the operator's own professional security background (healthcare tech SecOps leadership) is the relevant qualification here, not a formal program. |
| A.6.4 | Disciplinary process | **N/A** | No employees. |
| A.6.5 | Responsibilities after termination or change of employment | **N/A** | No employees. |
| A.6.6 | Confidentiality or non-disclosure agreements | **N/A** | No third parties with access requiring an NDA. |
| A.6.7 | Remote working | **N/A** | Not applicable in an employment sense; the app itself is designed to be safely reachable remotely (TLS, MFA, session gate) which is covered under 8.20/5.17 instead. |
| A.6.8 | Information security event reporting | **Applied** | Any household member (viewer or admin) can observe and report a suspicious event; the audit log + detections engine mean most events don't depend on a human noticing first. _(`compliance/IR-RUNBOOK.md section 1`)_ |

## Physical controls (14)

| Control | Title | Status | Rationale / Evidence |
|---|---|---|---|
| A.7.1 | Physical security perimeters | **N/A** | Self-hosted on personal hardware in a private residence -- standard home physical security applies, not a data-center perimeter model. |
| A.7.2 | Physical entry | **N/A** | Same as 7.1. |
| A.7.3 | Securing offices, rooms and facilities | **N/A** | Same as 7.1. |
| A.7.4 | Physical security monitoring | **N/A** | Same as 7.1. |
| A.7.5 | Protecting against physical and environmental threats | **N/A** | Home-server context; no data-center-grade environmental controls, and none claimed. |
| A.7.6 | Working in secure areas | **N/A** | Not applicable at this scale. |
| A.7.7 | Clear desk and clear screen | **N/A** | Not applicable at this scale. |
| A.7.8 | Equipment siting and protection | **Partial** | Host hardware (i7-6700K / 32GB / dual GTX 1080 Ti) is on a private, access-controlled home network, not otherwise hardened to enterprise physical standards. |
| A.7.9 | Security of assets off-premises | **N/A** | No mobile/off-site assets. |
| A.7.10 | Storage media | **Partial** | Data-at-rest is encrypted regardless of the physical disk (see 8.24), which mitigates most of this control's intent even without formal media-handling procedures. _(`portfolio-api/secrets_crypto.py`)_ |
| A.7.11 | Supporting utilities | **N/A** | Home infrastructure, not a facility. |
| A.7.12 | Cabling security | **N/A** | Not applicable at this scale. |
| A.7.13 | Equipment maintenance | **N/A** | Not applicable at this scale. |
| A.7.14 | Secure disposal or re-use of equipment | **Partial** | No formal disk-wipe procedure documented yet for eventual hardware retirement; mitigated somewhat by encryption-at-rest (7.10) making raw disk contents unreadable without the key. |

## Technological controls (34)

| Control | Title | Status | Rationale / Evidence |
|---|---|---|---|
| A.8.1 | User endpoint devices | **N/A** | No managed device fleet -- household members use their own personal devices as ordinary web-app clients; nothing installed locally. |
| A.8.2 | Privileged access rights | **Applied** | Exactly one admin account (hardcoded server-side, never client-elevated); privileged actions (account management, secret rotation, TLS config) are denied to viewer sessions. _(`portfolio-api/app.py _require_admin`)_ |
| A.8.3 | Information access restriction | **Applied** | Whole-app deny-by-default session gate; every route requires an authenticated session except an explicit small public allowlist (login, first-run, health check). _(`portfolio-api/app.py _AUTH_PUBLIC_PATHS`)_ |
| A.8.4 | Access to source code | **N/A** | Single-developer-assisted project; no separate source-control access-tiering beyond normal git repository permissions. |
| A.8.5 | Secure authentication | **Applied** | Argon2 + TOTP MFA + rate-limited login attempts + single-use backup codes. _(`portfolio-api/auth.py`)_ |
| A.8.6 | Capacity management | **Partial** | Job-trigger rate limiting exists (prevents unbounded GPU/network-heavy background jobs per session), but no formal capacity-planning process for the host hardware. _(`portfolio-api/app.py JOB_RATE_LIMIT_PER_MIN`)_ |
| A.8.7 | Protection against malware | **Partial** | Standard host-OS malware protection assumed; no application-layer file-upload scanning beyond type/size validation on the knowledge-RAG ingestion path. _(`portfolio-api/knowledge.py`)_ |
| A.8.8 | Management of technical vulnerabilities | **Applied** | SEC-7b: automated CVE/dependency scanning is live in CI (pip-audit + Trivy image scan), blocking on CRITICAL/HIGH findings that have a vendor fix per a documented 7/30-day patch SLA (unfixed upstream findings are reported on every run and reviewed weekly, not hidden), with a formal .trivyignore risk-acceptance mechanism (exp: dates) for accepted exceptions. _(`.github/workflows/dependency-scan.yml, .trivyignore`)_ |
| A.8.9 | Configuration management | **Applied** | Every security-relevant config change (policy, retirement config, TLS state, etc.) is diffed old->new and logged as a config.changed audit event. _(`portfolio-api/app.py _emit_config_changed`)_ |
| A.8.10 | Information deletion | **Applied** | The audit log has an automated retention/disposal job that deletes events past the retention window and logs the disposal itself as evidence. _(`portfolio-api/audit.py purge_expired`)_ |
| A.8.11 | Data masking | **Applied** | The audit-log viewer masks source IPs and never surfaces secret VALUES (only key names) even to an authenticated admin session. _(`portfolio-api/audit.py read_recent(redact_ip=)`)_ |
| A.8.12 | Data leakage prevention | **Partial** | Encryption-at-rest and RBAC reduce exposure surface; no dedicated DLP/egress-monitoring tooling (disproportionate at this scale). |
| A.8.13 | Information backup | **Partial** | SEC-7a: encrypted backup/restore scripts (evidenced, byte-for-byte-verified restore test, documented RPO/RTO) plus a systemd-timer installer (local) and an EventBridge Scheduler rule (AWS demo) to actually run them automatically -- built 2026-07-27, not yet Applied pending Jason confirming each is live (see RESUME-TRACKER.md's 2026-07-27 backup-automation entry). _(`scripts/backup.sh, scripts/restore.sh, scripts/install-local-backup.sh, scripts/aws-demo/schedule-backup.sh`)_ |
| A.8.14 | Redundancy of information processing facilities | **N/A** | Single-host household deployment -- no redundant facility is proportionate here. |
| A.8.15 | Logging | **Applied** | Append-only, encrypted, structured security event log covering auth, config, secrets, TLS, RBAC, and detections -- one row per event with actor/IP/outcome/resource. _(`portfolio-api/audit.py`)_ |
| A.8.16 | Monitoring activities | **Applied** | A rules engine sweeps the log every 5 minutes (and on demand) for failed-login bursts, first-seen IPs, and MFA-disable-then-change sequences. _(`portfolio-api/detections.py`)_ |
| A.8.17 | Clock synchronization | **Applied** | All audit timestamps are UTC, generated server-side from a single process clock (no client-supplied timestamps trusted anywhere in the logging path). _(`portfolio-api/audit.py _now_iso equivalent`)_ |
| A.8.18 | Use of privileged utility programs | **N/A** | No separate privileged utility/OS-admin tooling exposed through the app. |
| A.8.19 | Installation of software on operational systems | **Partial** | Container-based deployment (Docker Compose) constrains what runs in the app's runtime environment; no formal change-approval gate for image updates yet. _(`docker-compose.yml`)_ |
| A.8.20 | Networks security | **Applied** | In-app TLS provisioning (self-signed, CSR/import, or ACME), HSTS emitted when active, and an explicit fix ensures a downgrade from HTTPS doesn't strand a browser on cached HSTS. _(`portfolio-api/tls.py, app.py /api/tls/clear`)_ |
| A.8.21 | Security of network services | **Partial** | External API calls (market data, LLM, SnapTrade) are outbound-only over TLS; no formal per-service security review has been documented yet. |
| A.8.22 | Segregation of networks | **N/A** | Single-host household deployment; container-level isolation exists (separate services in docker-compose) but a full network-segmentation model isn't proportionate here. _(`docker-compose.yml`)_ |
| A.8.23 | Web filtering | **N/A** | No outbound web-filtering proxy is in scope for a personal application. |
| A.8.24 | Use of cryptography | **Applied** | Fernet (AES-128-CBC + HMAC) at rest across essentially all sensitive tables/files; TLS in transit; Argon2 for passwords; now key-versioned (SEC-3) so rotation doesn't require a full re-encrypt outage. _(`portfolio-api/secrets_crypto.py`)_ |
| A.8.25 | Secure development life cycle | **Partial** | Every feature in this engagement is built with an explicit control mapping and tested via TestClient against the real app before being considered done, but there's no formal SDLC policy document beyond that practice. _(`RESUME-TRACKER.md`)_ |
| A.8.26 | Application security requirements | **Applied** | Pydantic request models give automatic input validation with structured 422 errors; security requirements (encryption, RBAC, audit logging) are stated inline in code comments before/while building each feature. _(`portfolio-api/app.py _validation_error_handler`)_ |
| A.8.27 | Secure system architecture and engineering principles | **Applied** | Deny-by-default gate, envelope encryption, append-only+hash-chained audit log, least-privilege RBAC -- consistent defense-in-depth design across the app, not single-point controls. _(`portfolio-api/app.py`)_ |
| A.8.28 | Secure coding | **Partial** | Consistent patterns (parameterized SQL via sqlite3's placeholder syntax, no raw string interpolation into queries observed in the audited modules) but no formal static-analysis gate yet. |
| A.8.29 | Security testing in development and acceptance | **Applied** | Every SEC-1..SEC-5 control was verified end-to-end via Starlette TestClient against the real FastAPI app object (not mocks) before being marked done -- login/logout, RBAC denial, chain-integrity, dedup, and encryption round-trips were all actually executed. _(`RESUME-TRACKER.md verification sections`)_ |
| A.8.30 | Outsourced development | **N/A** | No outsourced development -- built directly by the operator with AI-assisted engineering. |
| A.8.31 | Separation of development, test and production environments | **Partial** | Tests run against isolated env-var-scoped databases (see this SEC-4 test run) rather than the live data volume, but there's no separate staging deployment distinct from production. |
| A.8.32 | Change management | **Applied** | Every config/security-relevant change is captured old->new in the audit log; TLS and RBAC state changes have their own dedicated event types. _(`portfolio-api/audit.py EVENT_TYPES`)_ |
| A.8.33 | Test information | **Applied** | Tests use synthetic seeded data (scratch SQLite DBs, made-up usernames/IPs) rather than real financial data -- confirmed for every SEC-item's TestClient run this engagement. |
| A.8.34 | Protection of information systems during audit testing | **Applied** | All verification testing in this engagement runs against isolated, disposable databases (tempfile-based, env-var overridden) -- never against the live data volume. |

---
*Generated from `generate_soa.py` — the single source of truth also used to produce `controls.json` for the demo site's Security Posture page. Edit the data there, not this file directly, so the two never drift apart.*
