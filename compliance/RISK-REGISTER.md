# Fiduciary — Risk Register

**This static file is no longer the live document (2026-07-27).** The Risk Register is now
generated fresh from the running app's own writable data store on every read — see the
"RISK-REGISTER" section of `portfolio-api/app.py` (`_read_risk_register` /
`_render_risk_register_markdown`) for why: `compliance/` is read-only bind-mounted locally and
baked into the image at build time for the AWS demo, so there is no writable path back into this
directory from a running container. Editing THIS file has no effect on what the app actually
serves or on what chat tools/the Security Posture page display.

**To read the current register:** open the running app → SETTINGS → SECURITY → SECURITY POSTURE →
GRC DOCUMENTS → "Risk Register", or ask the Fiduciary chat persona directly (it has a
`get_compliance_doc` tool for exactly this). **To edit it:** use the "RISK REGISTER (live)" panel on
that same Security Posture page (admin-only) — see `RISK-REGISTER-PROCESS.md` for the rules that
editor enforces (rows are never deleted, closures are permanent and require a reason, R-numbers are
never reused).

This file is kept in git only so the repository isn't missing a `RISK-REGISTER.md` entirely for
anyone browsing the source directly — it intentionally does not attempt to mirror the live content,
since keeping a static copy in sync by hand is exactly the staleness problem the live-rendering
design was built to eliminate (see RESUME-TRACKER.md's 2026-07-27 entries: R-13 and R-14 were both
already caught stale within days of being written by hand).

---
*Companion documents: `compliance/RISK-REGISTER-PROCESS.md` (the maintenance process),
`compliance/STATEMENT-OF-APPLICABILITY.md`, `compliance/IR-RUNBOOK.md`, `RESUME-TRACKER.md`.*
