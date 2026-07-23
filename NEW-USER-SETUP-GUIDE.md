# New User Setup Guide

You've got the containers running (`INSTALL.md`) and Monarch/holdings connected — the
dashboard loads and shows real positions. This doc is the next step: a handful of forms,
scattered across a few panels, that unlock the rest of the app.

**Nothing below is required.** Skip anything you don't want to enter — that module just
stays a placeholder instead of a real number, and nothing else breaks. Do them in any order;
the ranking below is by "biggest unlock for the least typing," not a hard dependency chain.

Every field lives in one of two small JSON files (`retirement.json`, `insurance.json`) edited
through the UI — you never hand-edit JSON. Everything is local; nothing you enter here is
sent anywhere.

---

## Tier 0 — already working, zero setup

Skip straight to Tier 1 if you just want the checklist. For context: these already produce
real numbers off your connected holdings alone — Positions, Research briefs, Risk/Exposure
(VaR, CVaR, factors, concentration, drift-vs-Monarch's-own-asset-types), Fees/Liquidity,
Tax-Loss Harvesting, and the Dashboard donuts/movers/net-worth. You get a real, useful tool
even if you stop reading here.

---

## Tier 1 — do these first (PLAN → ASSUMPTIONS)

One panel, biggest unlock. Every field below is optional, but each one turns a placeholder
into a real number somewhere.

| Field | Unlocks | Notes |
|---|---|---|
| **State of residence** | Estate (community property / TOD deeds), ACA (Medicaid-gap vs. likely-Medicaid), Tax (state estimate), Cash·Debt (mortgage-interest / Treasury notes) | **New.** Leave blank and the app tries to auto-detect from a tracked real-estate account's address; if it can't (you rent, or your home isn't tracked), it silently assumes Texas and now flags that in ⚠ ATTENTION until you set it. Don't skip this one if you're not in Texas. |
| Filing status | Tax, Roth-conversion headroom, gain-harvest budget | mfj / single / hoh |
| Current age (+ spouse age if married) | Retirement Monte Carlo, funded ratio, life-insurance need horizon | |
| Target (retirement) age | FI number, years-to-FI, life-insurance need horizon | |
| Annual expenses | FI number, tax bracket estimate, ACA bridge-year MAGI default | Leave blank to use Monarch's own cashflow read where available |
| Annual savings | FI number, years-to-FI | |
| Household size | ACA subsidy/FPL calculation | Defaults to 2 |
| Birth year (+ spouse) | Social Security estimate, IRMAA modeling in the Roth ladder | |
| SS estimated benefit at FRA ($/mo) | Social Security estimate, retirement income floor | Best source: your own ssa.gov/myaccount statement |
| Estate donees (# of people) | Annual-exclusion gifting-capacity runway | |
| Estate home cost basis | Step-up ledger — quantifies the gain a step-up would wipe on your primary residence | Purchase price + capital improvements |

---

## Tier 2 — real dollars if they apply to you (PLAN → INSURANCE)

| Field | Unlocks | Notes |
|---|---|---|
| Your income (+ spouse) | Life-insurance need (capital-needs/DIME/HLV), disability-income gap | Nothing in PLAN → INSURANCE computes without at least one income entered — the panel tells you this (`setup_needed`) |
| Children (#) | Life-insurance need (education line) | |
| Policies in force (life, disability, umbrella, home, auto, LTC) | NEED vs. IN-FORCE vs. GAP on every line | One row per policy — kind, owner, coverage, premium, expiration. Pull from each policy's declarations page |
| Home value override | P&C dwelling-coverage check | Only needed if your home isn't tracked via Monarch/Zillow |

## Tier 2 — cash & debt (PLAN → CASH·DEBT)

Same household-register file as Insurance, different panel.

| Field | Unlocks | Notes |
|---|---|---|
| Mortgage rate + monthly payment | Payoff-vs-invest verdict | |
| Refi candidate rate + closing costs | Refinance break-even months | |
| Deposit yield (checking/savings APY) | Idle-cash drag $/yr | |
| Per-debt APR (inline on each avalanche row) | Debt-payoff ranking | Only debts missing a rate show as `set-rate` |
| Mortgage? checkbox (per debt row) | Correctly excludes the real mortgage from the avalanche table | Only needed if the account name doesn't obviously say "mortgage" (e.g. a Rocket-branded loan) |

## Tier 2 — benefits (PLAN → BENEFITS)

Same household-register file again.

| Field | Unlocks | Notes |
|---|---|---|
| 401(k) pre-tax / after-tax % + match formula (tier 1/2 % and rate) | Match-capture check, contribution-room ladder | |
| Bi-weekly 401(k) total (you + match) | Implied-salary cross-check | |
| Plan offers after-tax contributions? + in-service rollover status | Mega-backdoor Roth capacity | If ISR status is "unknown," ask your plan administrator — it's the one fact that unlocks or blocks this lane |
| HSA coverage tier + your contribution + invested? | HSA headroom | |

---

## Tier 3 — everyone eventually, not everyone immediately

| Field | Where | Unlocks | Notes |
|---|---|---|---|
| ACA benchmark Silver premium (SLCSP $/yr) | PLAN → ASSUMPTIONS | A real dollar premium-tax-credit estimate, not just the %-of-income figure | Only relevant pre-Medicare (before 65) |
| IPS policy targets (by asset type + by sleeve) | INSIGHTS → POLICY · DRIFT (edit via the policy form) | Drift-band alerts | Ships with a reasonable default (70/10/15/5 US-equity/Intl/Bond/Cash) — only edit if your actual target allocation differs |
| Position caps per ticker | Same panel | Per-name concentration alerts | |
| API keys (Finnhub, Tavily) | `.env` (see `INSTALL.md`) | Research briefs, news alerts, opportunity scanning | Infra-level, not a UI form — mentioned here only because it gates a lot of Research/Ideas |

---

## After you fill in anything

Most panels update live or on the next background refresh. If a number still looks stale,
`↻ REFRESH` in the header forces a full recompute. Check ⚠ ATTENTION (the header badge) after
a first pass — it'll flag anything still obviously incomplete (state of residence unset,
missing income for the insurance module, drift bands, etc.) rather than making you hunt for it.

## What's NOT here

This guide is about **data you type in**. It doesn't cover connecting Monarch/holdings in
the first place (`INSTALL.md`) or what each number MEANS once it's computed — for that,
open the in-app **WIKI** tab (search it, or click any "?" next to a panel header) or the
**GLOSSARY** category inside it for term definitions (options, tax/retirement acronyms, risk
metrics, real estate/business terms, estate/insurance/benefits terms).
