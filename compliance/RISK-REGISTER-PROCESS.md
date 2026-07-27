# Risk Register — Maintenance Process

**Scope:** this document explains how `compliance/RISK-REGISTER.md` actually gets maintained —
adding a new risk, updating one as treatments are built, monitoring it over time, and closing one
out. It exists because a risk register that's accurate on the day it's written but never touched
again is worse than no register at all — it creates false confidence. Written for whoever operates
this app day-to-day, not assuming prior GRC background.

**Control mapping:** ISO/IEC 27001:2022 Clause 6.1.2 (information security risk assessment
process), Clause 8.2 (information security risk assessment — performed at planned intervals or on
significant change) · NIST SP 800-53 Rev5 RA-3 (risk assessment), RA-7 (risk response) · SOC 2
(2017 TSC) CC3.1-CC3.4 (risk identification, analysis, and response).

## 1. When to add a new row

Add a row when you identify a genuinely new asset-threat pair — not every finding needs one. A
single CVE inside an already-tracked row (like R-10, "dependency supply chain") is a *data point*
for that row, not a new row. Add a new row when the threat is categorically different from
anything already tracked: a new integration, a new class of attacker, a new asset the app now
handles that it didn't before.

Each new row needs, at minimum: the asset, the threat scenario in plain language (who does what to
cause harm — not just "vulnerability"), a Likelihood and Impact rating (Low/Medium/High, see §4),
and an honest "Existing treatment" — what's actually built today, not what's planned. If nothing is
built yet, say so; Inherent = Residual until a real treatment lands. Assign the next sequential
`R-` number; never reuse a retired number (see §3).

## 2. Monitoring — this is the step that's easiest to skip

A register only stays true if something forces a look at it periodically, and if concrete build
work updates it *as it happens* rather than in a separate "GRC cleanup" pass that never comes.
Two mechanisms do this here, deliberately, so it doesn't depend on memory or discipline alone:

- **Tied to real automation, where possible.** R-10 is the model: SEC-7's CI dependency scan runs
  on every push touching dependency files, weekly regardless (Monday 09:00 UTC, `.github/workflows/
  dependency-scan.yml`), and any CVE risk-accepted via `.trivyignore` carries a 90-day `exp:` date
  that forces the scan to fail again automatically once it lapses — which is your cue to come back
  and re-evaluate that specific row, not a calendar reminder you have to set yourself.
- **Tied to build work.** Whenever a tracked treatment ships (a SEC-# item, a fix, a new control),
  update that row's "Existing treatment" and re-rate "Residual" in the same session the work is
  done — not queued as a follow-up. A stale register (like R-10 sitting on "no automated CVE
  scanning yet" for two days after the scanning was built and had already gone through four rounds
  of fixes) is exactly the failure mode this process exists to prevent.

Where no automation exists to force a review (most rows today — R-1 through R-9, R-11 through
R-14), the honest answer is: review happens when you're touching that area of the app for other
reasons, or during an incident (the IR runbook's post-incident step explicitly calls for updating
the detection rule set and, by extension, the register), not on a fixed calendar. If you want a
fixed-cadence review independent of any single row's automation, that would need to be built (e.g.
a scheduled task prompting a quarterly pass over every row) — not present today.

## 3. Closing or removing a row

**Never delete a row outright.** Mark it closed in place, with a reason and date, so the register
stays an honest audit trail of what was assessed and when — not just a snapshot of current-day
concerns. In practice: change Residual to state the closure reason (e.g. "Closed 2026-07-27 —
won't-do, see RESUME-TRACKER.md"), or add a trailing note to the row. If a row is retired, its `R-`
number is retired with it — the next new row gets the next unused number, it does not reclaim a
retired one. This mirrors how two backlog items were closed in `RESUME-TRACKER.md` on 2026-07-27
(narrative-generation-slowness, private AWS migration) — closed with the actual reasoning recorded,
not silently dropped.

A row should only be removed entirely (not just closed) if the asset itself stops existing — e.g.
a feature is deleted from the app, so the risk it posed no longer applies to anything. Even then,
prefer marking it "Retired — asset removed \<date\>" over deleting the line, unless the register is
getting unwieldy and a separate historical log is introduced to hold retired rows.

## 4. Rating consistently

Likelihood and Impact are each Low/Medium/High, rated against this app's actual scale — a single
self-hosted household deployment, not a generic enterprise assumption (stated in the register's own
scope line). Inherent = the combination before any treatment; Residual = after what's actually
built *today*. A treatment that's designed but not yet shipped does not lower Residual — see R-8
and R-13 in the current register for honest examples of this (both rated worse on Residual than a
generous read might suggest, specifically because the real mitigation is still queued).

When in doubt about a rating, err toward the less comfortable one and say why in the "Existing
treatment" column — the register is more useful as an honest gap list than as a document optimized
to look clean.

## 5. Where this shows up

The register itself, and this process document, are both served read-only through the Security
Posture page (SETTINGS → SECURITY → SECURITY POSTURE → GRC DOCUMENTS), via the same
`_COMPLIANCE_DOCS` allowlist in `app.py` that serves the Statement of Applicability, IR Runbook, and
the rest — so anyone with access to that page, not just whoever is reading the repo directly, can
see both the current risk picture and the process behind it.

---
*Companion documents: `compliance/RISK-REGISTER.md` (the register itself),
`compliance/STATEMENT-OF-APPLICABILITY.md`, `compliance/IR-RUNBOOK.md` (its post-incident step is
the other trigger for a register update), `RESUME-TRACKER.md` (build history, including the R-10
update and SEC-7 rollout this document references as its worked example).*
