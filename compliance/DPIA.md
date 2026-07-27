# Fiduciary — Data Protection Impact Assessment (DPIA)

**Purpose:** ISO/IEC 27701 and SOC 2 Privacy (P1–P8) both expect a documented assessment of
privacy risk before/while processing sensitive personal data at meaningful scale. This is a
lightweight DPIA scaled to what this actually is — a self-hosted, single-household application —
not a template written for a multi-tenant SaaS product. Where a section would otherwise be
boilerplate for this deployment's real scale, that's stated plainly rather than padded out.

## 1. Description of the processing

Fiduciary processes household financial data (holdings, income, retirement assumptions, insurance/
estate documents), account credentials for its own login system, and conversational data with an
in-app AI assistant, in order to provide financial planning, analysis, and life-planning guidance.
It optionally calls external market-data, brokerage-aggregation, and LLM providers using
credentials the household supplies. Full detail: `compliance/ROPA.md`.

## 2. Necessity and proportionality

- Every data category processed is necessary for the feature it supports — there is no category
  collected "just in case" or for a purpose unrelated to financial planning (no advertising,
  analytics-for-sale, or third-party data brokering exists anywhere in this app).
- External calls are opt-in per feature (SETTINGS → SECRETS) — a household that never configures
  SnapTrade/Monarch/Tavily/a cloud LLM simply never sends data to those providers; a fully
  local-only configuration (CSV import + Ollama) is possible and processes nothing outside the host.
- Chat/knowledge data sent to an LLM/embeddings provider is limited to what the household's own
  query/documents require — the app does not proactively push the full financial dataset into
  every prompt (tool-calling patterns fetch specific data on demand).

## 3. Risks to data subjects (the household members using this deployment)

| Risk | Likelihood | Impact | Assessment |
|---|---|---|---|
| Unauthorized access to financial/PII data via a compromised session or stolen backup | Low | High | Mitigated by encryption-at-rest, MFA, RBAC, and (as of SEC-7a) encrypted backups — see `compliance/RISK-REGISTER.md` R-1/R-2 |
| Sensitive financial context sent to an external LLM provider retained/logged beyond this app's control | Low-Medium | Medium | Mitigated by the household's ability to choose a local-only model; NOT eliminated for any household that opts into a cloud LLM provider — this is an accepted, disclosed trade-off of using that feature, stated in `compliance/PRIVACY-NOTICE.md` | 
| A viewer-role household member seeing more than intended | Low | Low | Mitigated — viewers get read access to shared dashboards by design (that's the intended use case: a spouse reviewing the household's finances), not a data-minimization failure; the risk that matters is a viewer gaining WRITE/admin capability, which is denied by RBAC (SEC-5) |
| Chat transcripts persisting indefinitely with no automatic expiry | Medium | Medium | **Real, disclosed gap** — see `compliance/DATA-RETENTION-SCHEDULE.md`'s flagged-gaps section; not yet remediated |
| Host compromise exposing everything at once | Low | High | Explicitly out of scope for application-layer controls to fully prevent — disclosed in `compliance/RISK-REGISTER.md` R-7 and `compliance/IR-RUNBOOK.md`'s chain-integrity caveat, not hidden behind a false sense of completeness |

## 4. Measures to address the risks

Already in place: encryption at rest (Fernet, key-versioned since SEC-3), MFA + Argon2 password
hashing, RBAC with logged denials, a tamper-evident audit log with an active detection engine,
encrypted backups with an evidenced restore test, and a written IR runbook.

Queued, not yet built: automatic expiry for chat transcripts (would close the one row above marked
"real, disclosed gap"); a formal per-provider data-retention review for external LLM/market-data
services (currently assessed at the feature-configuration level, not per-provider-contract level).

## 5. Residual risk and conclusion

After the controls above, residual risk is assessed as **Low-to-Medium** for a household that uses
only local/CSV-based features, and **Medium** for a household that opts into cloud LLM/brokerage-
aggregation features — the increase in the latter case is an inherent, disclosed trade-off of
using those specific features, not an application defect. This is judged proportionate to the
processing's purpose (the household's own financial planning, at the household's own informed
choice of which optional features to enable).

**Sign-off:** Jason Bentley, as the operator/data controller of this deployment, 2026-07-25.
This DPIA should be revisited if: a new category of sensitive data is added, a new external
provider integration is introduced, or this app is ever adapted for multi-tenant/public use beyond
a single household (which would also require revisiting the DSAR scope decision in
`compliance/ROPA.md` and `GET /api/privacy/export`).

---
*Companion documents: `compliance/PRIVACY-NOTICE.md`, `compliance/ROPA.md`,
`compliance/RISK-REGISTER.md`, `compliance/IR-RUNBOOK.md`.*
