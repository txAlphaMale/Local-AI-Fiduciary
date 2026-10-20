# Fiduciary — Privacy Notice

**Who this is for:** anyone with a login to this deployment of Fiduciary — the household admin
account and any additional view-only accounts the admin has created for other household members.

**Scope:** this notice describes how THIS self-hosted instance of Fiduciary handles data. It is
not a SaaS privacy policy for a company processing your data on shared infrastructure — there is
no company here, no data sale, no advertising, and no other tenant who can see your data. The
admin who runs this deployment is also the data controller for it in the ordinary sense of that
word: they operate the server, hold the encryption keys, and are responsible for its security.

## What is collected and why

| Category | Examples | Why |
|---|---|---|
| Account identity | username, password (hashed, never stored in plain text), MFA status | So you can log in and so the app can tell admin vs. viewer accounts apart |
| Security/session history | login success/failure, source IP (masked in the viewer), MFA/recovery events, config changes you made | So the household can see what happened to its own accounts and detect suspicious activity (see the SECURITY AUDIT LOG panel) |
| Household financial data | holdings, account balances, retirement assumptions, insurance/estate documents, budget/scenario config | This is the core purpose of the app — financial planning and analysis for the household |
| Chat transcripts | conversations with the in-app AI assistant | So you can review past advice and so the assistant has conversational context |
| Knowledge/reference documents | anything you drop into the Knowledge folder (e.g. your own Investment Policy Statement) | So the AI assistant can answer questions grounded in documents you chose to provide |
| Vault documents & emergency access | files you upload to the document vault (stored encrypted), your choice of who may see each one, and — if you turn on emergency access — who the designated contact is and the history of any request | So important papers live in one place and the right person can reach them if the account owner cannot act. Nothing here is ever sent off this server or given to the AI assistant |

Full technical detail, including exactly which files/tables hold what and for how long, is in
`compliance/ROPA.md` (Records of Processing Activities) and `compliance/DATA-RETENTION-SCHEDULE.md`.

## How it's protected

- Passwords are Argon2-hashed; MFA (TOTP) is supported and recommended.
- Nearly all sensitive data is encrypted at rest (Fernet/AES), including the financial config,
  chat transcripts, the knowledge index, and the security audit log itself.
- The audit log is tamper-evident (HMAC hash chain) — it can prove whether it's been altered.
- Access is role-based: an admin can manage the deployment and household accounts; a viewer can
  see the dashboards but cannot change security-relevant settings.
- Full detail: `compliance/STATEMENT-OF-APPLICABILITY.md`.

## Third parties involved

Certain features call external services on your behalf, using credentials you provide (SETTINGS
→ SECRETS): market-data providers (e.g. Finnhub, SEC EDGAR, FINRA, RentCast), a brokerage-linking
service (SnapTrade, only if configured), a bank/brokerage/loan account-linking service
(Plaid, only if configured — it receives your institution logins on its own hosted page, never on this server, and
returns balances, holdings and liabilities, not transactions), an account-aggregation service (Monarch, only if
configured), a news/search provider (Tavily, only if configured), and an AI/LLM provider (which
may be a local model with no external call at all, or a cloud provider like Anthropic/OpenAI if
configured). Each of these only receives what a normal use of that feature requires (e.g. a stock
ticker to a market-data API, not your whole account list) — see `compliance/ROPA.md` for a
per-category breakdown. None of them are paid to advertise to you, and this app does not sell or
share your data with anyone for marketing purposes — there is no marketing function in this app at all.

## Your rights over your own data

- **Access / export:** SETTINGS → SECURITY → "PRIVACY & YOUR DATA" → "EXPORT MY DATA" downloads a
  JSON file of your account identity and your own security/login history. (This does not include
  the shared household financial ledger — see the scope note in that panel and in `compliance/ROPA.md`
  for why that's a jointly-used household asset, not one person's individually-identifiable data.)
- **Deletion:** any account holder can remove their own account (SETTINGS → SECURITY → "RESET
  ACCOUNT"); an admin can remove any viewer account (SETTINGS → SECURITY → "HOUSEHOLD ACCOUNTS").
  Deleting the shared financial data itself is done the same way you'd clear any of the app's own
  config — there's no separate "erase everything" button because the app doesn't distinguish
  "your" financial data from the household's the way it does for login identity.
- **Correction:** every piece of financial data you can view, you can also edit directly in the
  app — there's no separate correction request process needed.

## Changes to this notice

This notice is a versioned file in the app's own repository (`compliance/PRIVACY-NOTICE.md`) —
changes are visible in its version-control history like any other part of the app.

---
*Companion documents: `compliance/ROPA.md`, `compliance/DATA-RETENTION-SCHEDULE.md`,
`compliance/DPIA.md`, `compliance/STATEMENT-OF-APPLICABILITY.md`.*
