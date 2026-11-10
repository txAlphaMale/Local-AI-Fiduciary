# Design decisions

Decisions the owner has made on purpose, with the reason, so a later change (or a later session) does not undo them by accident. Newest first.
An entry is a decision, not a to-do: if circumstances change, add a new dated entry that supersedes it.

## 2026-11-10: The app stays reachable from every device on the home network
**Decision (Jason):** the app's ports (8097 plain HTTP redirect, 8443 HTTPS) stay published on all interfaces of the host so any phone, laptop or tablet on the home network can open it. It is not restricted to localhost.
**Why:** it is a household tool used from several devices; a localhost-only binding would make it unusable from a phone.
**What keeps this acceptable (do not remove without replacing):** the app requires an account login (Argon2 hashes, optional TOTP MFA, per-IP login rate limit); HTTPS is on (self-signed certificate; the browser warning is expected); the container runs unprivileged (non-root, read-only filesystem, no Linux capabilities); the audit log records logins and denials; the router must NOT port-forward 8097/8443 to the internet.
**If it ever needs to be reachable from outside the home:** use a VPN back into the home network (or a reverse proxy with its own authentication) rather than publishing these ports.
Risk register: R-7 (exposure on all interfaces is an accepted, owner-decided risk with the controls above).

## 2026-11-10: Interface preferences are stored with the account, not only in the browser
**Decision (Jason):** layout and customisation (dashboard panel order, graph switches, tab and sub-tab order, theme, calculator inputs) are saved on the server per signed-in account and applied in every browser, so clearing cookies or switching devices never loses them.
**How:** `prefs.py` / `GET,POST /api/prefs`; the browser keeps a local copy and syncs a moment after each change; the server copy wins at sign-in, local-only values are uploaded once. Encrypted at rest. Not used on the public demo (one shared login).

## 2026-11-10: Cash is not an investment in the dashboard widgets
**Decision (Jason):** the allocation and mix widgets do not count cash as an investment holding, but keep one slice for free cash sitting in brokerage accounts.
**Definition:** "brokerage cash" = a Cash line in an account that also holds securities. A Cash-only account (savings, checking, emergency fund) is bank cash and is left out of the widgets. The INVESTED tile excludes all cash. The planning and risk engines still see cash (policy drift needs the cash sleeve); only the display changed.

## 2026-11-09: Tax figures change by deliberate, tested steps, never from a web search
**Decision:** next-year figures are researched ahead of time and shown (AUDITS > GROUNDING) with how sure each one is; the engines only use a new year's figures when they are present in the year-keyed tables and confirmed. The app never rewrites its own tax tables from a web lookup, because a misread number would silently drive every plan.

## 2026-11-09: The chat can read memory but never write it
**Decision:** saved notes and past chats are readable by the Fiduciary through read-only tools; only the signed-in admin saves, edits or deletes a note. The Fiduciary has no write tool of any kind.
