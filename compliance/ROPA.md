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
| P-12 | Document vault | uploaded files the household chooses to keep (wills, trusts, POAs, insurance, IDs, medical, titles); encrypted titles/filenames; sha256; expiry/review dates; per-item visibility (private/household/emergency) | Keep estate and identity documents in one place, available to the right person if the admin cannot act | `/data/vault/blobs` (ciphertext) + `vault_items`/`vault_folders` in `store.db` | AES-256-GCM per file; per-file key wrapped by the versioned Fernet keyring; included in encrypted backups | Until the household deletes the item (delete crypto-shreds the key) | None — never sent off-host; never indexed into RAG/chat |
| P-13 | Emergency access state | designee, wait period, grant type, request history (who/when/decision) | Time-delayed break-glass access to `emergency` documents or admin takeover | `emergency_config` / `emergency_requests` in `store.db`; events in the audit log | `store.db` 0600; audit log encrypted + hash-chained | Request rows indefinite (audit value); events follow the audit retention | Admin notified via push/SMTP (shared address) |
| P-14 | Emergency Kit | passphrase-encrypted archive of `emergency` documents + binder export (contacts, executor, account map) | Survive total loss of the server | Temporary 0600 file under `/data/tmp` until downloaded (single use, 10 min) — then it lives wherever the admin saves it | scrypt + AES-256-GCM; passphrase never stored; metadata only (time, count, item-set hash) kept | Server copy deleted at download/expiry/restart; admin-held copy is the admin's to manage | None |
| P-15 | Plaid connection (optional) | Plaid client ID/secret; per-institution access tokens; balances, investment holdings and liabilities (APR, minimum payment) returned by Plaid | Use real bank/brokerage/card/loan accounts as the app's data source instead of a CSV | Secrets in the encrypted secrets store; tokens + item list in `plaid_items.json` inside the encrypted store blob | Fernet at rest; tokens never returned to the browser, logged or audited; admin-only endpoints; REMOVE calls `/item/remove` first | Until the institution is removed (removal stops Plaid billing and deletes the token) | Plaid, Inc. (processor; receives institution logins on its hosted page, returns account data; no transactions requested) |

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
