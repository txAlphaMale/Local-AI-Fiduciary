# Fiduciary — Risk Register

**Scope:** the `portfolio-api` application and its data volume, self-hosted, single-household
deployment. Written as a portfolio/demo GRC artifact — likelihood/impact ratings reflect this
app's actual scale (one household, one host, no external tenants), not generic enterprise
assumptions. Owner is "Jason" throughout since there is one operator; the column is kept for
format-completeness and to make clear risk acceptance is a deliberate, named act, not a default.

**Rating scale:** Likelihood and Impact are each Low / Medium / High. Inherent risk = the
combination before any treatment; Residual risk = after the mitigations actually in place today
(not after planned/queued ones — a queued mitigation doesn't reduce risk until it's built).

| # | Asset | Threat | Likelihood | Impact | Inherent | Existing treatment | Residual | Owner |
|---|---|---|---|---|---|---|---|---|
| R-1 | Financial/PII data (holdings, income, estate docs, chat transcripts) | Unauthorized read via a compromised session or stolen backup | Low | High | Medium-High | Fernet encryption at rest across all sensitive tables/files (`secrets_crypto.py`); Argon2+MFA-gated sessions; deny-by-default access gate | Low-Medium | Jason |
| R-2 | Master encryption key(s) (Fernet, HMAC audit key) | Key file loss or theft (host compromise, bad backup hygiene) | Low | High | Medium-High | Key-versioning (SEC-3) limits blast radius of a *future* rotation, but does not protect the *current* key file itself; documented "back this file up separately" guidance | Medium (residual risk is real — a stolen current-version key file still decrypts current data) | Jason |
| R-3 | Security audit log integrity | An attacker (or operator mistake) alters or deletes historical events to hide activity | Low | Medium | Low-Medium | HMAC hash chain (separate key from Fernet) detects any alteration; retention/disposal is itself logged | Low | Jason |
| R-4 | Account credentials (password, MFA, recovery key) | Credential-stuffing / brute-force login attempts | Medium | Medium | Medium | Argon2 hashing, TOTP MFA, sliding-window login rate limiting, single-use hashed backup codes | Low | Jason |
| R-5 | Household viewer account | A viewer session used to attempt privileged actions (curiosity or compromise) | Low | Low | Low | Server-side role assignment (never client-supplied); deny-by-default write gate; every denial logged (`authz.denied`) | Low | Jason |
| R-6 | TLS/transport | Downgrade to plaintext HTTP exposing credentials/session cookies in transit | Low | High | Medium | In-app TLS provisioning + HSTS when active; explicit `max-age=0` fix so a genuine downgrade doesn't leave a browser stuck refusing HTTP either | Low | Jason |
| R-7 | Host OS / container runtime | Compromise of the underlying host (root access) | Low | High | Medium-High | Standard OS hardening assumed, containerized service boundaries (docker-compose); explicitly documented that the audit hash chain does NOT protect against a full root/host compromise (an attacker with both key files could rebuild a consistent chain) | Medium (real, named, un-mitigated by the app layer — a host-level control gap by design) | Jason |
| R-8 | Backup / disaster recovery | No tested restore path today — a corrupted DB or lost host means real data loss | Medium | High | Medium-High | Manual "back up this key file" guidance only; no automated encrypted backup or evidenced restore test yet | **High** — SEC-7 (queued) is the direct treatment for this row | Jason |
| R-9 | Third-party API credentials (market data, LLM providers, SnapTrade) | Leaked API key used for unauthorized calls (cost/abuse, not data breach — these are outbound-only, read-scoped) | Low | Low-Medium | Low | Stored via the same encrypted secrets store as everything else; scoped, read-only credentials where the provider supports it | Low | Jason |
| R-10 | Dependency supply chain (Python/JS packages) | A vulnerable or malicious transitive dependency | Medium | Medium | Medium | Dependencies pinned; no automated CVE scanning yet | Medium — SEC-7 (CI dependency scanning) is the direct treatment | Jason |
| R-11 | LLM/chat data path | Sensitive financial context sent to an external LLM provider is logged/retained by that provider beyond Fiduciary's control | Low-Medium | Medium | Medium | Documented hybrid-LLM path exists (local-first where feasible); no per-provider data-retention audit done yet | Medium | Jason |
| R-12 | Detection engine coverage | A real attack pattern falls outside the 3 current detection rules and goes unnoticed | Medium | Medium | Medium | 3 rules cover the highest-value, most checkable scenarios today (failed-login burst, first-seen IP, MFA-disable-then-change); explicitly documented as v1, not exhaustive | Medium (residual gap is honest, not hidden — new incidents are meant to grow this rule set, per the IR runbook's post-incident step) | Jason |
| R-13 | Privacy/PII program maturity | No formal DSAR/export-delete workflow, privacy notice, or DPIA yet for data that is, in substance, sensitive financial PII | Medium | Medium | Medium | Strong technical controls (encryption, RBAC) exist; the *programmatic* privacy layer is SEC-8, still queued | Medium — treatment queued, not yet reducing residual risk | Jason |
| R-14 | Demo/portfolio instance itself | Publishing a public demo instance widens the attack surface beyond the private household deployment | Medium | Low-Medium | Low-Medium | Planned: synthetic pseudo-data only (`DEMO_MODE`), isolated/throwaway auth, scheduled resets — none of this is built yet, so this row's rating will drop once the demo harness (tasks #29-31) lands | Medium until `DEMO_MODE` isolation is built | Jason |

---
*Companion documents: `compliance/STATEMENT-OF-APPLICABILITY.md` (control-by-control ISO 27001
Annex A status), `compliance/IR-RUNBOOK.md` (response procedure), `RESUME-TRACKER.md` (build
history and the SEC-1..SEC-8 backlog that treats several of the rows above).*
