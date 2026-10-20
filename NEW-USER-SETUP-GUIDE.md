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
| Manually-entered liabilities (HOLDINGS tab → LIABILITIES, new 2026-07-30) | Any loan/credit card, for CSV/SnapTrade users (or a Monarch household tracking a debt Monarch itself doesn't sync) | Rate/min-payment can be set right on the liability row or here — same saved value either way |

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
| IPS policy targets (by asset type + by sleeve) | RISK · EXPOSURE → POLICY · DRIFT (edit via the policy form) | Drift-band alerts | Ships with a reasonable default (70/10/15/5 US-equity/Intl/Bond/Cash) — only edit if your actual target allocation differs |
| Position caps per ticker | Same panel | Per-name concentration alerts | |
| API keys (Finnhub, Tavily) | SETTINGS → SECRETS (2026-07-24 — was `.env`-only) | Research briefs, news alerts, opportunity scanning | `.env` still works as a fallback/seed value, but the UI is the preferred path now — see the in-app WIKI → "Secrets encryption & the secrets.key backup" entry. |
| AI provider (Claude, optional) | SETTINGS → AI / LLM PROVIDER + SECRETS | Swaps chat/analysis from your local Ollama models to Anthropic's hosted Claude API | Purely optional — local Ollama (the default) already runs every feature at zero marginal cost. See below before turning this on. |
| No brokerage account? Portfolio data source (CSV) | HOLDINGS tab + SETTINGS → ACCOUNTS | A full portfolio with no Monarch/SnapTrade account needed at all | See "No-brokerage-account holdings (CSV)" below — new 2026-07-24, this used to require hand-placing a file on the host. |
| Manual liabilities (loans, credit cards) | HOLDINGS tab → LIABILITIES (below the positions table) | Debt shows up in net worth and PLAN → CASH·DEBT even with no Monarch/SnapTrade sync | New 2026-07-30 — see "No-brokerage-account holdings (CSV)" below; previously `csv`/`snaptrade` accounts could never represent debt at all. |
| Notifications (push and/or email) | SETTINGS → NOTIFICATIONS (+ SETTINGS → SECRETS & KEYS for email) | Security alerts and portfolio-outage warnings pushed/emailed to you, instead of only showing up in the audit log | New 2026-07-26 — see "Notifications" below. |

---

## AI provider — local Ollama vs. hosted Claude (optional)

Everything AI-driven in this app — chat, the weekly Strategist run, cash-deployment advice, the
Dashboard narrative, and every research brief's synthesis — defaults to your own local Ollama
models, run on your own GPUs at zero marginal cost. **You don't need to read this section for the
app to work.** It documents an *optional* alternative: pointing chat/analysis at Anthropic's
Claude API instead, added 2026-07-24.

**Before enabling this:** a claude.ai Pro or Max chat subscription does **not** include API
access. The key this needs comes from a separate [console.anthropic.com](https://console.anthropic.com)
account with its own pay-per-token billing — check [platform.claude.com/docs/pricing](https://platform.claude.com/docs/pricing)
for current rates before turning this on, especially for the Strategist tier below, which can
burn meaningfully more tokens per run than everyday chat.

**What moves, what doesn't:**
- Chat, the Dashboard narrative, and research-brief JSON synthesis can run on an "everyday"
  Claude model.
- The autonomous weekly Strategist run and the on-demand cash-deployment advice planner — both
  longer, tool-heavier ReAct loops — can independently use a stronger "Strategist" Claude model,
  so a quick chat question doesn't pay that tier's cost every turn.
- **Knowledge/RAG embeddings cannot move to Claude at all** — Anthropic has no embeddings API.
  They stay on local Ollama regardless of what the chat provider is set to (unless you separately
  point the embeddings provider at an OpenAI-compatible embeddings host). This doesn't affect
  retrieval quality either way — Claude only ever sees the already-retrieved passages as plain
  text, exactly like Ollama does today.

**Where to set it up:** SETTINGS → AI / LLM PROVIDER for the provider/host/model fields, SETTINGS
→ SECRETS for the API key(s) (masked, write-only — never shown again once saved). Like the other
Model config fields, changes apply the next time portfolio-api's container is **recreated**
(`docker compose up -d` — a plain `restart` reuses the existing container's environment and will
NOT pick this up), not live. Full detail — including exactly which app features fall into which
tier — is in the in-app **WIKI → Getting started → "AI provider (SETTINGS → AI / LLM PROVIDER)"**
entry.

---

## No-brokerage-account holdings (CSV) — the HOLDINGS tab, plus manual liabilities

Don't use Monarch Money and don't have a SnapTrade-supported brokerage? You don't need either.
The **HOLDINGS** tab (top-level, always visible) is a full portfolio data source with zero
account linking — you enter positions directly.

**Before 2026-07-24** this required hand-placing a `holdings.csv` file on the host filesystem
before the app was usable at all — a real gap for anyone without Monarch/SnapTrade. It's now a
normal in-app panel:

- **Edit the table directly** — add/remove rows, type values in. Only ticker and quantity are
  required (quantity is a dollar amount for `Cash`-type rows); name, cost basis, account, and
  institution are optional and default sensibly.
- **Upload a CSV** — click ⬆ UPLOAD CSV to replace the whole table at once from a spreadsheet
  export. Click ⬇ DOWNLOAD TEMPLATE first if you want the right column headers to start from.
- **Saving is all-or-nothing** — if any row fails validation (blank ticker, a zero/negative or
  non-numeric quantity, a non-numeric cost basis), nothing is written and every problem row is
  listed together, rather than silently dropping one position with no explanation.

**One more step to make it live:** filling in the table doesn't automatically switch the active
data source. Check the banner at the top of the HOLDINGS tab — if it says these holdings aren't
active, go to SETTINGS → ACCOUNTS → "Portfolio data source" and set it to `csv` (applies immediately). This field used to be
`.env`-only (`PORTFOLIO_PROVIDER=csv`); either path works, but the UI dropdown is the easier one
now.

This is also the fastest way back onto a working dashboard if Monarch/mcpo is ever down —
switch to CSV temporarily instead of staring at a portfolio-load error. Full detail in the in-app
**WIKI → Getting started → "No brokerage account? The HOLDINGS tab (CSV positions + manual
liabilities)"** entry.

### Manual liabilities (loans, credit cards) — new 2026-07-30

**Before this,** `csv` and `snaptrade` accounts were always treated as pure assets — there was no
way to represent a loan or credit card at all, so net worth for anyone on those two providers was
assets-only, and PLAN → CASH·DEBT had nothing to show. This is now fixed with a small, separate
**LIABILITIES** table directly below the HOLDINGS positions table (same tab, same page — nothing
new to navigate to):

- **Not part of the CSV/positions table above** — that table requires a ticker and a positive
  share quantity, which a liability has neither of. Liabilities get their own table instead, with
  fields that actually fit a debt: name (required, must be unique), institution, type (Credit
  Card/Auto Loan/Student Loan/Personal Loan/HELOC/Mortgage/Other Loan), and balance owed (required,
  a positive number).
- **Rate % and minimum payment are optional, right on the same row** — and they write into the
  exact same saved fields SETTINGS → PLAN CASH·DEBT already edits for any debt by name. Fill them
  in on either screen; there's no second copy to keep in sync.
- **Marking a row "Mortgage"** automatically excludes it from the debt avalanche and routes it into
  mortgage payoff-vs-invest instead — the same effect as the "Mortgage?" checkbox on a
  Monarch-synced account.
- **Works no matter which provider is active** — `csv`, `snaptrade`, and even `monarch` (for a
  debt Monarch itself never syncs, like a private family loan). A saved liability counts against
  net worth and shows up in PLAN → CASH·DEBT and the CONTINUITY binder's liabilities list
  immediately, exactly like a synced account.
- There's no CSV-upload or hand-edited file equivalent for this table (unlike the positions editor
  above) — it's in-app only, backed by `GET`/`POST /api/manual-liabilities`.

---

## Notifications (push and/or email) — optional but recommended

By default, the only way to learn about a security event (a login from a new location, MFA
disabled, an admin action) or a portfolio-data-source outage is to open SETTINGS → SECURITY →
SECURITY AUDIT LOG yourself. SETTINGS → NOTIFICATIONS (new 2026-07-26) closes that gap — self-
hosted, no third-party push service and no paid cloud email API required.

**Push (this device only):**
- Open SETTINGS → NOTIFICATIONS and click **🔔 SUBSCRIBE THIS DEVICE**, then allow the browser's
  permission prompt.
- **iPhone/iPad:** Apple only allows push notifications for an installed web app, never an
  ordinary open Safari tab. First tap the Share icon → **"Add to Home Screen,"** then reopen the
  app from that Home Screen icon before subscribing. Requires iOS 16.4+ (released March 2023 —
  nearly every device in use qualifies).
- **Android / desktop Chrome, Firefox, Edge:** subscribe directly from an ordinary browser tab, no
  install step needed.
- Each device/browser subscribes independently — subscribe your phone AND your laptop if you want
  both to get alerts.

**Email (one shared address for the whole household, admin-only):**
- Under SETTINGS → SECRETS & KEYS → **"Notifications (Email)"**, enter your SMTP server details.
  For Gmail: host `smtp.gmail.com`, port `587`, your full email address as the username, and an
  **App Password** — not your normal Gmail password. Google requires 2-Step Verification turned on
  first before it will generate one (search "Google App Passwords" if you've never made one). Any
  other email provider's SMTP details work the same way.
- Then flip the email checkbox on in SETTINGS → NOTIFICATIONS.

**Either way:** pick which categories notify you (security alerts, portfolio outages) under
SETTINGS → NOTIFICATIONS, then click **✉ SEND TEST NOTIFICATION** to confirm delivery actually
works before relying on it. "Portfolio outage" alerts only ever fire for whichever data source
you've actually configured — if you're on the CSV/HOLDINGS tab or have never touched SnapTrade,
you will never see a SnapTrade alert. Full detail in the in-app **WIKI → Getting started → "Push &
email notifications"** entry.

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
