# Fiduciary — Records of Processing Activities (ROPA)

**Purpose:** ISO/IEC 27701:2019 §7.2/§8.2 and common DSAR practice both expect a data inventory —
what categories of data are processed, why, where they live, who/what receives them, and how long
they're kept. This is that inventory, derived directly from `SECRET_SPECS` (app.py), `store.py`'s
schema, and `audit.py`'s event model — not written in the abstract.

**Controller context:** this is a self-hosted, single-tenant household application. There is one
"controller" in the classical privacy sense — the admin who operates this deployment — and no
separate data-processing company. Where a "legal basis" column would appear in a GDPR-style ROPA,
the honest answer for every row below is **consent/contract-of-use**: the household chose to run
this app for its own financial planning, and every category exists because the household itself
entered it or asked a connected feature to fetch it.

| # | Category | Description | Purpose | Storage location | Protection | Retention | Recipients / processors |
|---|---|---|---|---|---|---|---|
| P-1 | Account identity | username, Argon2 password hash, MFA (TOTP secret + backup-code hashes), role | Authentication, session management, RBAC | `auth.py` (`AUTH_DB`, SQLite) | Password/backup-codes irreversibly hashed; TOTP secret encrypted (reversible, needed to verify codes) | Account lifetime | None external — local only |
| P-2 | Recovery key | one-time-shown recovery secret (only its Argon2 hash retained) | Host-CLI account recovery if MFA + password are both lost | `auth.py` (`AUTH_DB`) | Hashed, never stored raw | Account lifetime (hash only) | None external |
| P-3 | Security/session history | login events, MFA/recovery use, config/secret/TLS changes, RBAC denials, detection alerts | Security monitoring, incident response, DSAR export source | `audit.py` (`AUDIT_DB`, separate from app data) | Fernet-encrypted detail, HMAC hash-chained (tamper-evident), IP masked in the viewer | 365 days (`AUDIT_RETENTION_DAYS`), automated purge | None external |
| P-4 | Household financial data | holdings, account balances, retirement/tax assumptions, scanner/opportunity config | Core financial planning/analysis function of the app | `store.py` Stage A/B tables (various JSON blobs + typed tables) | Fernet-encrypted at rest | Indefinite — working data, user-managed | None external by default |
| P-5 | Insurance / estate documents | policy details, named contacts (attorney, executor, etc.), estate-planning notes | Protection-gap analysis, estate continuity planning | `store.py` (insurance.json) | Fernet-encrypted at rest | Indefinite — working data, user-managed | None external |
| P-6 | Chat transcripts | conversations with the Fiduciary/Strategist AI personas | Conversational context, review of past advice | `chat.db` (SQLite) | Fernet-encrypted | Indefinite until user deletes a session — **flagged gap, no auto-expiry** (see `compliance/DATA-RETENTION-SCHEDULE.md`) | The configured LLM provider receives PROMPT CONTENT at inference time (see P-9) |
| P-7 | Knowledge/RAG documents | user-uploaded reference docs (e.g. an IPS) + their embedding vectors | Grounding the AI assistant's answers in the household's own reference material | `knowledge/` folder + `knowledge_index.json` | Vectors/index Fernet-encrypted; source files are host filesystem, user-controlled | Indefinite until the user removes the source file | The configured embeddings provider receives document TEXT at index time (may be a local model — see P-9) |
| P-8 | Third-party API credentials | market-data/brokerage/aggregation/LLM provider keys (`SECRET_SPECS`) | Enable the corresponding optional feature | `secrets.json` / `mcpo-secrets/mcpo.env` | Fernet-encrypted (app-scope) or file-permission-restricted (mcpo-file scope) | Until changed/removed by the user | The respective provider only (scoped to what that feature calls) |
| P-9 | Outbound calls to market-data / LLM / brokerage providers | tickers, portfolio queries, chat prompts, brokerage account linkage tokens (SnapTrade/Monarch) | Fulfilling the specific feature the household is using | N/A (in-transit only, over TLS) | TLS in transit; provider selection lets the household choose local-only (Ollama) to avoid any external call for chat/embeddings | N/A (not stored by this app beyond the request) | Finnhub, SEC EDGAR, FINRA, RentCast, SnapTrade, Monarch (via mcpo), Tavily, and/or the configured LLM/embeddings provider — only whichever the household has actually configured |
| P-10 | TLS certificates & keys | self-signed/CSR/ACME certificate + private key | Encrypt traffic between the household's browser and this server | `tls.py` (`/data`) | Private key file-permission-restricted (0600) | Until rotated/cleared | ACME path only: the configured Certificate Authority (e.g. Let's Encrypt) for issuance/renewal |
| P-11 | Encryption key material | Fernet keyring (versioned), audit HMAC key | Protects everything else in this table | `/data/secrets_keyring.json`, `/data/audit_hmac.key` | File-permission-restricted; never leaves the host | Indefinite — every version retained (SEC-3 design) | None external |

## Notes on completeness

This ROPA covers every data class currently identified in the codebase's `SECRET_SPECS` list and
`store.py`'s known file set (cross-checked against `compliance/DATA-RETENTION-SCHEDULE.md`, which
was built independently from the same source). If a new persisted data class is added to the app,
it should get a row here in the same pass — the retention schedule and this ROPA are meant to stay
current together, the same discipline already applied to `compliance/controls.json` and its
generator script.

---
*Companion documents: `compliance/PRIVACY-NOTICE.md`, `compliance/DATA-RETENTION-SCHEDULE.md`,
`compliance/DPIA.md`.*
