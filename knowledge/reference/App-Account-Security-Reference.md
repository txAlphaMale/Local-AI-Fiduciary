# Using This App — Login, MFA, Backup Codes & Account Recovery (Reference)

_Corpus doc for the Fiduciary/Strategist chat's `search_knowledge` tool. This is NOT financial or
tax guidance — it documents how THIS APPLICATION's own login, multi-factor authentication, and
account-recovery features work, so the chat can answer "how do I..." questions about the app
itself instead of having no grounding for them. Written 2026-07-24, matching the in-app WIKI
("Getting started" section — SETTINGS panel, or the guided tour) as of that date; if the UI ever
changes, treat the live WIKI panel inside the app as the source of truth over this file._

## Logging in

This deployment has exactly one user account (a single household, not a multi-user system). The
first time the app runs, it shows a "CREATE YOUR ACCOUNT" screen instead of the dashboard — pick a
username and password there. After that, every visit requires logging in with that username and
password (session cookie, not a shared token). If multi-factor authentication (MFA) is enabled
(see below), login also asks for a 6-digit authenticator code or a backup code.

## Multi-factor authentication (MFA) and backup codes

MFA is a rotating 6-digit code from any authenticator app (Google Authenticator, Authy, 1Password,
Bitwarden, etc.), required at login on top of the password. Because this app manages a household's
full financial picture, MFA is treated as a CRITICAL setup step, not an optional extra — it shows a
`!` on the in-app setup checklist and is a stop on the guided tour.

**Setting it up:** either right after creating the account (the first-run screen offers to walk
through it immediately, checked by default), or any time later via SETTINGS → IAM → "SET UP
MFA": scan the QR code with an authenticator app, enter the 6-digit code it shows, and you're given
**10 single-use backup codes** — save them somewhere safe, they're shown exactly once and never
again. SETTINGS → IAM also shows how many backup codes remain; once down to the last couple,
regenerate a fresh set of 10 by disabling and re-enabling MFA.

**Lost phone/authenticator, but backup codes are still on hand:** log in using one of the saved
backup codes in place of the 6-digit code — the login screen's code field explicitly accepts
either, so this is a normal login, not a special recovery flow. Once logged in, go to SETTINGS →
IAM, disable MFA (requires the current password), then set it up again from scratch with the
new device.

**Lost backup codes too, or MFA was never set up:** MFA alone can't help — see Account Recovery
below.

## Account recovery — two independent tiers

There are two separate recovery paths for two different situations. Do not confuse them:

**Tier 1 — forgot the password, but MFA still works** (authenticator app or a saved backup code
still available). On the sign-in screen, click "Forgot password? (requires MFA)", enter the
username and a current authenticator code (or an unused backup code), then set a new password.
This reuses the exact same code check the normal login screen uses, so it is exactly as secure as
a regular MFA login. No host/server access is needed for Tier 1 — it's a normal in-browser flow.

**Tier 2 — MFA is also gone** (lost the authenticator AND the backup codes), or MFA was never set
up in the first place, and the password is forgotten. This needs host/container access to the
machine running this deployment — it cannot be done from the browser alone. Run, from the machine
hosting the app:

```
docker compose exec portfolio-api python auth.py --reset-credentials
```

This asks for the username, the account's **recovery key**, and a new password. The recovery key
is a one-time secret shown ONCE — either right after the account was first created, or whenever it
was last (re)generated in SETTINGS → IAM → "HOST RECOVERY KEY". Like MFA, having a recovery
key on file is a CRITICAL checklist item. **There is no way to recover a lost recovery key — only
to generate a brand-new one while account access still exists.** If one hasn't been generated yet,
do it now, before it's needed.

**No recovery key on file at all:** `--reset-credentials` cannot help without one. The only
remaining option is `python auth.py --wipe-all` (same command form) — this is genuinely
destructive: it deletes the login, ALL financial config/history/paper-trading scenarios, chat
history, and the knowledge index cache, returning the app to a blank first-run state. This is
intentional, not a bug: host/container access alone is deliberately NOT enough to quietly reset
just the login and take over existing data — it either gets the real owner back into their OWN
account (via the recovery key) or wipes everything and starts fresh, with nothing in between. This
is what makes MFA/password protection meaningful even against someone who has server access.

**Bottom line:** generate a recovery key today, in SETTINGS → IAM, and store it like a
password — it's the only thing standing between "forgot my password" and "lost everything."

## Other account actions in SETTINGS → IAM

_(2026-07-26: account/access management was split out of SETTINGS → SECURITY into its own
SETTINGS → IAM subtab, to keep the security-operations panel — Certificate/Audit Log/Privacy —
from getting crowded. Everything in this section lives in IAM now, not SECURITY.)_

- **Rename account** (change username): password-gated — enter the current password to confirm,
  then set a new username.
- **Household accounts** (admin only): create view-only accounts for other household members, or
  remove them. A viewer can read the whole dashboard but is denied any write action except their
  own password/MFA/logout.
- **⚠ Reset account (advanced):** deletes ONLY the login/MFA record and returns the app to the
  first-run "create your account" screen — for re-testing onboarding, or starting over with a
  fresh username. Requires BOTH the current password AND typing the exact username back as two
  independent confirmations, since it can't be undone from the UI. This does NOT touch financial
  config, chat history, or the knowledge index — those all survive a reset-account and are still
  there once a new account is created. This is a different, much narrower action than Tier 2's
  `--wipe-all` above (which deletes everything, not just the login).

**Certificate/HTTPS did NOT move** — SETTINGS → SECURITY → Certificate still sets up encrypted
HTTPS (self-signed, Let's Encrypt/ACME, or a manual CSR for an internal Certificate Authority);
it's unrelated to login/account management, which is why it stayed in SECURITY alongside the
audit log and privacy/data-export panels.

## Where this lives in the app

Account/access items above are live under SETTINGS → IAM; Certificate/audit log/privacy stay under
SETTINGS → SECURITY. Both are also covered as "Getting started" entries in the in-app WIKI (search
for "MFA" or "Account recovery" there, or follow the guided tour) — that live copy is the source of
truth if this document ever falls behind a UI change.
