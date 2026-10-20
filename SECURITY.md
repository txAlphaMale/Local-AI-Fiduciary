# Security policy

Fiduciary is a personal, self-hosted project published as a portfolio piece. It is read-only by design
(it never moves money) and is provided as-is under the AGPL-3.0-or-later license; there is no
commercial support or service-level commitment.

## Reporting a vulnerability
Please report suspected vulnerabilities **privately** through GitHub's "Report a vulnerability" button
(Security tab → Advisories) rather than a public issue. Include the affected version/commit, steps to
reproduce, and the impact you believe it has. I will acknowledge reports on a best-effort basis.

## Scope
In scope: the application code in `portfolio-api/`, its container/compose configuration, and the
infrastructure code in `infra/` and `scripts/`. Out of scope: third-party services the app can connect
to (Plaid, Monarch, SnapTrade, market-data and LLM providers) and vulnerabilities that require an already
fully compromised host.

## Design and controls
The threat model, risk register, incident-response runbook and control mapping live in
[`compliance/`](compliance/) (start with `compliance/RISK-REGISTER-PROCESS.md` and
`compliance/IR-RUNBOOK.md`). Known residual risks are listed there rather than hidden.

## Contributions
This project is not accepting outside contributions or pull requests (see the README license section).
