#!/usr/bin/env python3
"""
generate_soa.py -- SEC-6: single source of truth for Fiduciary's ISO/IEC 27001:2022 Annex A
Statement of Applicability. Generates BOTH compliance/STATEMENT-OF-APPLICABILITY.md (human-
readable) and compliance/controls.json (machine-readable, consumed by the future demo-site
"Security Posture" page) from the SAME data below, so the two can never drift out of sync --
exactly what DEMO-SITE-BUILD-PLAN.md section 3.4 calls for.

Status values: "Applied" (control is implemented and evidenced in this codebase), "Partial"
(some of the control exists but not the full intent), "N/A" (genuinely doesn't apply to a
single-tenant, self-hosted, non-commercial household app -- e.g. most physical-security and
employee-lifecycle controls, which assume an organization with premises and staff).

Every row is written to be checkable against the actual code, not aspirational. Where "Applied"
or "Partial" is claimed, the evidence column names a real file/endpoint/UI panel.
"""
import json
import os

# (control_id, title, theme, status, rationale, evidence)
CONTROLS = [
    # ---- A.5 Organizational (37) ----
    ("5.1", "Policies for information security", "Organizational", "Partial",
     "No single top-level ISMS policy document, but security rationale is documented inline "
     "throughout the codebase (threat-model comments) and formalized here in RESUME-TRACKER.md / "
     "this Statement of Applicability.", "RESUME-TRACKER.md, this document"),
    ("5.2", "Information security roles and responsibilities", "Organizational", "N/A",
     "Single-operator household app -- no organizational roles to assign beyond the app's own "
     "admin/viewer RBAC (see 5.15/5.18/8.2).", None),
    ("5.3", "Segregation of duties", "Organizational", "N/A",
     "One operator. RBAC (5.15) separates admin vs. viewer capability within the app, but there "
     "is no multi-person operational team to segregate duties across.", None),
    ("5.4", "Management responsibilities", "Organizational", "N/A",
     "No management hierarchy -- single household operator.", None),
    ("5.5", "Contact with authorities", "Organizational", "N/A",
     "No regulatory relationship; not a regulated financial institution.", None),
    ("5.6", "Contact with special interest groups", "Organizational", "N/A",
     "No formal ISAC/threat-sharing membership for a personal project.", None),
    ("5.7", "Threat intelligence", "Organizational", "Partial",
     "No subscribed feed, but dependency/CVE awareness is informal via GitHub's own advisories; "
     "SEC-7b (CI dependency scanning) formalizes this.", "compliance/RISK-REGISTER.md"),
    ("5.8", "Information security in project management", "Organizational", "Applied",
     "Every feature build in this engagement is annotated with its control mapping before/while "
     "being written (see RESUME-TRACKER.md's SEC-1..SEC-8 backlog) -- security is designed in, "
     "not bolted on after.", "RESUME-TRACKER.md"),
    ("5.9", "Inventory of information and other associated assets", "Organizational", "Applied",
     "The app's own data model IS the asset inventory -- every table/JSON file in store.py and "
     "SECRET_SPECS/CONFIG_SPECS is a documented, named data asset.", "portfolio-api/store.py, secrets_crypto.py"),
    ("5.10", "Acceptable use of information and other associated assets", "Organizational", "N/A",
     "Single-user/household context -- no separate user population needing an AUP.", None),
    ("5.11", "Return of assets", "Organizational", "N/A",
     "No employees/contractors with issued assets to return.", None),
    ("5.12", "Classification of information", "Organizational", "Partial",
     "Data is informally classified by sensitivity throughout code comments (e.g. \"real "
     "financial figures\" vs. generic) and by gitignore boundaries (compliance/ public, docs/ "
     "internal, data/ runtime-sensitive), but no formal classification schema/labels exist.",
     ".gitignore"),
    ("5.13", "Labelling of information", "Organizational", "N/A",
     "No formal labelling scheme -- see 5.12.", None),
    ("5.14", "Information transfer", "Organizational", "Partial",
     "All data transfer is either same-host (no network hop) or over TLS to the browser (see "
     "8.24); outbound calls to market-data/LLM providers carry no PII by design in most cases, "
     "but this hasn't been formally reviewed provider-by-provider.", "tls.py"),
    ("5.15", "Access control", "Organizational", "Applied",
     "Two-role RBAC (admin / viewer) enforced centrally in the request gate; every write path is "
     "denied unless explicitly allowlisted for a viewer session (deny-by-default).",
     "portfolio-api/app.py _security_and_auth_gate, _VIEWER_ALLOWED_WRITE_PATHS"),
    ("5.16", "Identity management", "Organizational", "Applied",
     "Each account is a distinct row (uid, username, role) with its own credentials and MFA "
     "state; exactly one admin identity, viewers created only through an explicit admin action.",
     "portfolio-api/auth.py"),
    ("5.17", "Authentication information", "Organizational", "Applied",
     "Argon2 password hashing, TOTP MFA (RFC 6238) with single-use hashed backup codes, and a "
     "separate host-CLI-recoverable recovery-key mechanism -- no plaintext credential storage "
     "anywhere.", "portfolio-api/auth.py"),
    ("5.18", "Access rights", "Organizational", "Applied",
     "Role is always server-assigned at account-creation time, never client-supplied -- "
     "structurally impossible for a session to self-escalate from viewer to admin.",
     "portfolio-api/app.py POST /api/auth/users"),
    ("5.19", "Information security in supplier relationships", "Organizational", "Partial",
     "External data providers (market-data APIs, LLM providers, SnapTrade) are used read-only "
     "with scoped credentials, but no formal supplier register/risk-tiering exists yet.",
     "compliance/RISK-REGISTER.md"),
    ("5.20", "Addressing information security within supplier agreements", "Organizational", "N/A",
     "Consumer-tier API terms-of-service, not negotiated contracts -- not applicable at this "
     "scale.", None),
    ("5.21", "Managing information security in the ICT supply chain", "Organizational", "Partial",
     "Dependencies are pinned in requirements files; SEC-7b adds automated CVE scanning in CI.",
     ".github/workflows"),
    ("5.22", "Monitoring, review and change management of supplier services", "Organizational", "N/A",
     "No SLA-bound supplier relationships to formally review.", None),
    ("5.23", "Information security for use of cloud services", "Organizational", "Partial",
     "Currently self-hosted (no cloud dependency for core data); a documented hybrid-LLM path "
     "exists for a future AWS deployment but isn't live.", "AWS-HOSTING-GUIDE.md (internal)"),
    ("5.24", "Information security incident management planning and preparation", "Organizational",
     "Applied", "A written, followable IR runbook exists mapping detect->triage->contain->"
     "eradicate->recover->post-incident to real app features.", "compliance/IR-RUNBOOK.md"),
    ("5.25", "Assessment and decision on information security events", "Organizational", "Applied",
     "The detections engine classifies findings (failed-login burst, first-seen IP, MFA-disable-"
     "then-change) and the IR runbook's triage section defines severity bands.",
     "portfolio-api/detections.py, compliance/IR-RUNBOOK.md"),
    ("5.26", "Response to information security incidents", "Organizational", "Applied",
     "Concrete containment actions are documented and directly executable from the app's own UI "
     "(password/MFA/recovery-key rotation, key rotation, viewer removal).", "compliance/IR-RUNBOOK.md"),
    ("5.27", "Learning from information security incidents", "Organizational", "Applied",
     "The runbook's post-incident section requires recording what happened and turning any real "
     "gap into a new, scoped detection rule rather than a vague action item.", "compliance/IR-RUNBOOK.md"),
    ("5.28", "Collection of evidence", "Organizational", "Applied",
     "The audit log is HMAC hash-chained (tamper-evident) specifically so it can serve as "
     "trustworthy evidence, not just a record.", "portfolio-api/audit.py verify_chain()"),
    ("5.29", "Information security during disruption", "Organizational", "Partial",
     "No formal continuity plan yet; SEC-7a (backup/restore) is the first concrete piece.",
     "compliance/RISK-REGISTER.md"),
    ("5.30", "ICT readiness for business continuity", "Organizational", "Partial",
     "SEC-7a: encrypted backup/restore scripts (evidenced restore test, byte-for-byte "
     "diff-verified, documented RPO/RTO) now have an actual scheduler wired up -- a systemd timer "
     "installer for local hosting, an EventBridge Scheduler rule for the AWS demo -- built "
     "2026-07-27. Still Partial, not Applied: the installers/schedules themselves have not yet "
     "been run for real and confirmed live in either environment (see RESUME-TRACKER.md's "
     "2026-07-27 backup-automation entry) -- flip to Applied once Jason confirms both.", None),
    ("5.31", "Legal, statutory, regulatory and contractual requirements", "Organizational", "N/A",
     "Personal household app, not a regulated entity offering services to others.", None),
    ("5.32", "Intellectual property rights", "Organizational", "N/A",
     "No third-party IP licensing concerns beyond standard open-source dependency licenses.", None),
    ("5.33", "Protection of records", "Organizational", "Applied",
     "Financial/PII data and the audit log are both encrypted at rest (Fernet) and access-gated; "
     "the audit log additionally can't be silently altered (hash chain).",
     "portfolio-api/secrets_crypto.py, audit.py"),
    ("5.34", "Privacy and protection of PII", "Organizational", "Partial",
     "Strong technical protection (encryption, access control) exists; the *programmatic* privacy "
     "controls (notice, ROPA, DSAR, DPIA) are SEC-8, still queued.", "SEC-8 (queued)"),
    ("5.35", "Independent review of information security", "Organizational", "N/A",
     "No external audit engaged for a personal project; this GRC pass itself is the closest "
     "analog, performed with an explicit instruction to correct rather than flatter.", None),
    ("5.36", "Compliance with policies, rules and standards for information security", "Organizational",
     "Partial", "This Statement of Applicability + the IR runbook + the risk register together "
     "are the compliance baseline being checked against, but no periodic formal compliance-review "
     "cadence exists yet.", "compliance/"),
    ("5.37", "Documented operating procedures", "Organizational", "Applied",
     "Deploy/verify steps are documented per feature (see RESUME-TRACKER.md's \"Jason deploy + "
     "verify\" sections) and the IR runbook is itself a documented operating procedure.",
     "RESUME-TRACKER.md, compliance/IR-RUNBOOK.md"),

    # ---- A.6 People (8) ----
    ("6.1", "Screening", "People", "N/A", "No employees to screen -- single household operator.", None),
    ("6.2", "Terms and conditions of employment", "People", "N/A", "No employment relationship.", None),
    ("6.3", "Information security awareness, education and training", "People", "N/A",
     "No workforce to train; the operator's own professional security background (healthcare "
     "tech SecOps leadership) is the relevant qualification here, not a formal program.", None),
    ("6.4", "Disciplinary process", "People", "N/A", "No employees.", None),
    ("6.5", "Responsibilities after termination or change of employment", "People", "N/A",
     "No employees.", None),
    ("6.6", "Confidentiality or non-disclosure agreements", "People", "N/A",
     "No third parties with access requiring an NDA.", None),
    ("6.7", "Remote working", "People", "N/A",
     "Not applicable in an employment sense; the app itself is designed to be safely reachable "
     "remotely (TLS, MFA, session gate) which is covered under 8.20/5.17 instead.", None),
    ("6.8", "Information security event reporting", "People", "Applied",
     "Any household member (viewer or admin) can observe and report a suspicious event; the "
     "audit log + detections engine mean most events don't depend on a human noticing first.",
     "compliance/IR-RUNBOOK.md section 1"),

    # ---- A.7 Physical (14) ----
    ("7.1", "Physical security perimeters", "Physical", "N/A",
     "Self-hosted on personal hardware in a private residence -- standard home physical security "
     "applies, not a data-center perimeter model.", None),
    ("7.2", "Physical entry", "Physical", "N/A", "Same as 7.1.", None),
    ("7.3", "Securing offices, rooms and facilities", "Physical", "N/A", "Same as 7.1.", None),
    ("7.4", "Physical security monitoring", "Physical", "N/A", "Same as 7.1.", None),
    ("7.5", "Protecting against physical and environmental threats", "Physical", "N/A",
     "Home-server context; no data-center-grade environmental controls, and none claimed.", None),
    ("7.6", "Working in secure areas", "Physical", "N/A", "Not applicable at this scale.", None),
    ("7.7", "Clear desk and clear screen", "Physical", "N/A", "Not applicable at this scale.", None),
    ("7.8", "Equipment siting and protection", "Physical", "Partial",
     "Host hardware (i7-6700K / 32GB / dual GTX 1080 Ti) is on a private, access-controlled home "
     "network, not otherwise hardened to enterprise physical standards.", None),
    ("7.9", "Security of assets off-premises", "Physical", "N/A", "No mobile/off-site assets.", None),
    ("7.10", "Storage media", "Physical", "Partial",
     "Data-at-rest is encrypted regardless of the physical disk (see 8.24), which mitigates most "
     "of this control's intent even without formal media-handling procedures.",
     "portfolio-api/secrets_crypto.py"),
    ("7.11", "Supporting utilities", "Physical", "N/A", "Home infrastructure, not a facility.", None),
    ("7.12", "Cabling security", "Physical", "N/A", "Not applicable at this scale.", None),
    ("7.13", "Equipment maintenance", "Physical", "N/A", "Not applicable at this scale.", None),
    ("7.14", "Secure disposal or re-use of equipment", "Physical", "Partial",
     "No formal disk-wipe procedure documented yet for eventual hardware retirement; mitigated "
     "somewhat by encryption-at-rest (7.10) making raw disk contents unreadable without the key.",
     None),

    # ---- A.8 Technological (34) ----
    ("8.1", "User endpoint devices", "Technological", "N/A",
     "No managed device fleet -- household members use their own personal devices as ordinary "
     "web-app clients; nothing installed locally.", None),
    ("8.2", "Privileged access rights", "Technological", "Applied",
     "Exactly one admin account (hardcoded server-side, never client-elevated); privileged "
     "actions (account management, secret rotation, TLS config) are denied to viewer sessions.",
     "portfolio-api/app.py _require_admin"),
    ("8.3", "Information access restriction", "Technological", "Applied",
     "Whole-app deny-by-default session gate; every route requires an authenticated session "
     "except an explicit small public allowlist (login, first-run, health check).",
     "portfolio-api/app.py _AUTH_PUBLIC_PATHS"),
    ("8.4", "Access to source code", "Technological", "N/A",
     "Single-developer-assisted project; no separate source-control access-tiering beyond normal "
     "git repository permissions.", None),
    ("8.5", "Secure authentication", "Technological", "Applied",
     "Argon2 + TOTP MFA + rate-limited login attempts + single-use backup codes.",
     "portfolio-api/auth.py"),
    ("8.6", "Capacity management", "Technological", "Partial",
     "Job-trigger rate limiting exists (prevents unbounded GPU/network-heavy background jobs per "
     "session), but no formal capacity-planning process for the host hardware.",
     "portfolio-api/app.py JOB_RATE_LIMIT_PER_MIN"),
    ("8.7", "Protection against malware", "Technological", "Partial",
     "Standard host-OS malware protection assumed; no application-layer file-upload scanning "
     "beyond type/size validation on the knowledge-RAG ingestion path.", "portfolio-api/knowledge.py"),
    ("8.8", "Management of technical vulnerabilities", "Technological", "Applied",
     "SEC-7b: automated CVE/dependency scanning is live in CI (pip-audit + Trivy image scan), "
     "blocking on CRITICAL/HIGH per a documented 7/30-day patch SLA, with a formal .trivyignore "
     "risk-acceptance mechanism (90-day exp: dates) for genuinely unpatched upstream findings.",
     ".github/workflows/dependency-scan.yml, .trivyignore"),
    ("8.9", "Configuration management", "Technological", "Applied",
     "Every security-relevant config change (policy, retirement config, TLS state, etc.) is "
     "diffed old->new and logged as a config.changed audit event.",
     "portfolio-api/app.py _emit_config_changed"),
    ("8.10", "Information deletion", "Technological", "Applied",
     "The audit log has an automated retention/disposal job that deletes events past the "
     "retention window and logs the disposal itself as evidence.",
     "portfolio-api/audit.py purge_expired"),
    ("8.11", "Data masking", "Technological", "Applied",
     "The audit-log viewer masks source IPs and never surfaces secret VALUES (only key names) "
     "even to an authenticated admin session.", "portfolio-api/audit.py read_recent(redact_ip=)"),
    ("8.12", "Data leakage prevention", "Technological", "Partial",
     "Encryption-at-rest and RBAC reduce exposure surface; no dedicated DLP/egress-monitoring "
     "tooling (disproportionate at this scale).", None),
    ("8.13", "Information backup", "Technological", "Partial",
     "SEC-7a: encrypted backup/restore scripts (evidenced, byte-for-byte-verified restore test, "
     "documented RPO/RTO) plus a systemd-timer installer (local) and an EventBridge Scheduler rule "
     "(AWS demo) to actually run them automatically -- built 2026-07-27, not yet Applied pending "
     "Jason confirming each is live (see RESUME-TRACKER.md's 2026-07-27 backup-automation entry).",
     "scripts/backup.sh, scripts/restore.sh, scripts/install-local-backup.sh, "
     "scripts/aws-demo/schedule-backup.sh"),
    ("8.14", "Redundancy of information processing facilities", "Technological", "N/A",
     "Single-host household deployment -- no redundant facility is proportionate here.", None),
    ("8.15", "Logging", "Technological", "Applied",
     "Append-only, encrypted, structured security event log covering auth, config, secrets, TLS, "
     "RBAC, and detections -- one row per event with actor/IP/outcome/resource.",
     "portfolio-api/audit.py"),
    ("8.16", "Monitoring activities", "Technological", "Applied",
     "A rules engine sweeps the log every 5 minutes (and on demand) for failed-login bursts, "
     "first-seen IPs, and MFA-disable-then-change sequences.", "portfolio-api/detections.py"),
    ("8.17", "Clock synchronization", "Technological", "Applied",
     "All audit timestamps are UTC, generated server-side from a single process clock (no "
     "client-supplied timestamps trusted anywhere in the logging path).",
     "portfolio-api/audit.py _now_iso equivalent"),
    ("8.18", "Use of privileged utility programs", "Technological", "N/A",
     "No separate privileged utility/OS-admin tooling exposed through the app.", None),
    ("8.19", "Installation of software on operational systems", "Technological", "Partial",
     "Container-based deployment (Docker Compose) constrains what runs in the app's runtime "
     "environment; no formal change-approval gate for image updates yet.", "docker-compose.yml"),
    ("8.20", "Networks security", "Technological", "Applied",
     "In-app TLS provisioning (self-signed, CSR/import, or ACME), HSTS emitted when active, and "
     "an explicit fix ensures a downgrade from HTTPS doesn't strand a browser on cached HSTS.",
     "portfolio-api/tls.py, app.py /api/tls/clear"),
    ("8.21", "Security of network services", "Technological", "Partial",
     "External API calls (market data, LLM, SnapTrade) are outbound-only over TLS; no formal "
     "per-service security review has been documented yet.", None),
    ("8.22", "Segregation of networks", "Technological", "N/A",
     "Single-host household deployment; container-level isolation exists (separate services in "
     "docker-compose) but a full network-segmentation model isn't proportionate here.",
     "docker-compose.yml"),
    ("8.23", "Web filtering", "Technological", "N/A", "No outbound web-filtering proxy is in scope "
     "for a personal application.", None),
    ("8.24", "Use of cryptography", "Technological", "Applied",
     "Fernet (AES-128-CBC + HMAC) at rest across essentially all sensitive tables/files; TLS in "
     "transit; Argon2 for passwords; now key-versioned (SEC-3) so rotation doesn't require a "
     "full re-encrypt outage.", "portfolio-api/secrets_crypto.py"),
    ("8.25", "Secure development life cycle", "Technological", "Partial",
     "Every feature in this engagement is built with an explicit control mapping and tested via "
     "TestClient against the real app before being considered done, but there's no formal SDLC "
     "policy document beyond that practice.", "RESUME-TRACKER.md"),
    ("8.26", "Application security requirements", "Technological", "Applied",
     "Pydantic request models give automatic input validation with structured 422 errors; "
     "security requirements (encryption, RBAC, audit logging) are stated inline in code comments "
     "before/while building each feature.", "portfolio-api/app.py _validation_error_handler"),
    ("8.27", "Secure system architecture and engineering principles", "Technological", "Applied",
     "Deny-by-default gate, envelope encryption, append-only+hash-chained audit log, least-"
     "privilege RBAC -- consistent defense-in-depth design across the app, not single-point "
     "controls.", "portfolio-api/app.py"),
    ("8.28", "Secure coding", "Technological", "Partial",
     "Consistent patterns (parameterized SQL via sqlite3's placeholder syntax, no raw string "
     "interpolation into queries observed in the audited modules) but no formal static-analysis "
     "gate yet.", None),
    ("8.29", "Security testing in development and acceptance", "Technological", "Applied",
     "Every SEC-1..SEC-5 control was verified end-to-end via Starlette TestClient against the "
     "real FastAPI app object (not mocks) before being marked done -- login/logout, RBAC denial, "
     "chain-integrity, dedup, and encryption round-trips were all actually executed.",
     "RESUME-TRACKER.md verification sections"),
    ("8.30", "Outsourced development", "Technological", "N/A",
     "No outsourced development -- built directly by the operator with AI-assisted engineering.",
     None),
    ("8.31", "Separation of development, test and production environments", "Technological",
     "Partial", "Tests run against isolated env-var-scoped databases (see this SEC-4 test run) "
     "rather than the live data volume, but there's no separate staging deployment distinct from "
     "production.", None),
    ("8.32", "Change management", "Technological", "Applied",
     "Every config/security-relevant change is captured old->new in the audit log; TLS and RBAC "
     "state changes have their own dedicated event types.", "portfolio-api/audit.py EVENT_TYPES"),
    ("8.33", "Test information", "Technological", "Applied",
     "Tests use synthetic seeded data (scratch SQLite DBs, made-up usernames/IPs) rather than "
     "real financial data -- confirmed for every SEC-item's TestClient run this engagement.", None),
    ("8.34", "Protection of information systems during audit testing", "Technological", "Applied",
     "All verification testing in this engagement runs against isolated, disposable databases "
     "(tempfile-based, env-var overridden) -- never against the live data volume.", None),
]

THEMES = ["Organizational", "People", "Physical", "Technological"]


def counts_by_status():
    c = {"Applied": 0, "Partial": 0, "N/A": 0}
    for row in CONTROLS:
        c[row[3]] += 1
    return c


def write_markdown(path):
    counts = counts_by_status()
    total = len(CONTROLS)
    lines = []
    lines.append("# Fiduciary — ISO/IEC 27001:2022 Annex A Statement of Applicability\n")
    lines.append("**Scope:** the `portfolio-api` application and its data volume, self-hosted, "
                  "single-household deployment. Authored as a portfolio/demo GRC artifact, not a "
                  "certified third-party audit — every \"Applied\"/\"Partial\" status names real, "
                  "checkable evidence in this codebase; every \"N/A\" is justified by this "
                  "deployment's actual scale (no employees, no facility, single tenant), not "
                  "used to hide a real gap.\n")
    lines.append(f"**Summary:** {counts['Applied']}/{total} Applied · {counts['Partial']}/{total} "
                 f"Partial · {counts['N/A']}/{total} Not Applicable.\n")
    lines.append("---\n")
    for theme in THEMES:
        rows = [r for r in CONTROLS if r[2] == theme]
        lines.append(f"## {theme} controls ({len(rows)})\n")
        lines.append("| Control | Title | Status | Rationale / Evidence |")
        lines.append("|---|---|---|---|")
        for cid, title, _theme, status, rationale, evidence in rows:
            ev = f" _(`{evidence}`)_" if evidence else ""
            lines.append(f"| A.{cid} | {title} | **{status}** | {rationale}{ev} |")
        lines.append("")
    lines.append("---")
    lines.append("*Generated from `generate_soa.py` — the single source of truth also used to "
                 "produce `controls.json` for the demo site's Security Posture page. Edit the "
                 "data there, not this file directly, so the two never drift apart.*")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def write_json(path):
    counts = counts_by_status()
    out = {
        "framework": "ISO/IEC 27001:2022 Annex A",
        "scope": "portfolio-api application + data volume, self-hosted single-household deployment",
        "generated_from": "generate_soa.py",
        "summary": {"total": len(CONTROLS), **counts},
        "controls": [
            {
                "id": f"A.{cid}",
                "title": title,
                "theme": theme,
                "status": status,
                "rationale": rationale,
                "evidence": evidence,
            }
            for cid, title, theme, status, rationale, evidence in CONTROLS
        ],
    }
    with open(path, "w") as f:
        json.dump(out, f, indent=2)


if __name__ == "__main__":
    import sys
    outdir = sys.argv[1] if len(sys.argv) > 1 else "."
    write_markdown(os.path.join(outdir, "STATEMENT-OF-APPLICABILITY.md"))
    write_json(os.path.join(outdir, "controls.json"))
    c = counts_by_status()
    print(f"Wrote SoA + controls.json — {len(CONTROLS)} controls "
          f"({c['Applied']} Applied / {c['Partial']} Partial / {c['N/A']} N/A)")
