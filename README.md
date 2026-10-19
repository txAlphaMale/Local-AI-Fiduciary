# Fiduciary

A **self-hosted, local-first financial analysis assistant**. It reads your portfolio,
researches your holdings against SEC fundamentals + analyst consensus + market news,
and runs proactive (read-only) agents — rebalancing/drift, retirement/FIRE with Monte
Carlo, an opportunity scanner, tax-loss-harvesting & asset-location audits, a liquidity
audit — plus a sandboxed **"Strategist" paper-trading agent** that makes hypothetical
buy/sell decisions with fake money so you can watch how it thinks. A built-in **chat**
UI (Fiduciary + Strategist personas, read-only tools) lives right in the dashboard.

> **Not financial advice.** Everything here is informational and educational. The
> Strategist trades a *hypothetical* ledger — no real orders are ever placed. All your
> data stays on your machine; nothing is sent anywhere except the read-only public/free
> APIs you configure (SEC EDGAR, Finnhub, Tavily) and your own portfolio source.

---

## What's in the stack

Two containers via Docker Compose, plus Ollama on the host:

| Service           | Role |
|-------------------|------|
| **portfolio-api** | The brain: normalizes your holdings, computes everything, serves the dashboard, the REST API, the built-in chat, **and the built-in job scheduler** (the recurring agent runs — see *Scheduling* below). |
| **mcpo**          | Bridges MCP servers (Tavily, time, and optionally the unofficial Monarch integration) to the app over HTTP. |
| **Ollama** *(host)* | Runs the local LLM that does the analysis & Strategist reasoning. Not a container here — see prerequisites. |

So `docker compose up -d` runs just **two** containers (`portfolio-api` + `mcpo`); Ollama
is the only host dependency. Scheduling is internal — there is no separate scheduler
container to run. *(Earlier versions used an external **n8n** container for cron; that has
been retired in favor of a built-in scheduler.)*

The **dashboard** (`http://localhost:8097`) has tabs for Positions, Research (B/S/H
briefs), Insights (drift), Plan (FIRE + Monte Carlo + Tax/ACA/Social-Security editor),
Ideas (opportunity scan), Strategist (paper trading), Audits (Tax/TLH/liquidity),
Calculators, and Chat.

**Calculators tab:** WHAT-IF · MY DEBTS (extra/one-time payment against your real liabilities —
payoff time and interest saved), true-cost MORTGAGE, LOAN, LAND LOAN, DRIP (anchored on annual
dividend per share, with keyless ticker lookup, tax, contributions and a value-over-time chart),
a TVM solver (leave one of N / I/Y / PV / PMT / FV blank), RENT vs BUY, AFFORDABILITY, POINTS,
YIELDS, INFLATION, LUMP vs ANNUITY and NPV/IRR. All arithmetic lives in
`portfolio-api/calculators.py` (pure functions, `python3 -m unittest test_calculators`), served at
`POST /api/calc` and shared with the chat's `run_calculator` tool. Read-only roles may use it
(it stores nothing). The DRIP ticker lookup (`GET /api/calc/dividend-lookup`) uses the keyless
Yahoo chart API with Finnhub as a fallback.

> **Why Ollama is on the host, not in the stack:** the analysis model is multiple GB and
> GPU pinning is host-specific. Keeping Ollama on the host avoids duplicating the model
> and lets you manage GPUs your own way. The app reaches it at `host.docker.internal:11434`.

> **Optional: hosted Claude instead of local Ollama (2026-07-24).** Chat/analysis can
> alternatively route through Anthropic's Claude API — swappable from SETTINGS → AI / LLM
> PROVIDER, no `.env` editing required. Local Ollama stays the default and needs nothing
> changed to keep working exactly as before; Knowledge/RAG embeddings always stay local
> regardless (Anthropic has no embeddings API). See `NEW-USER-SETUP-GUIDE.md`.

---

### Document vault, emergency access and the Emergency Kit

PLAN → VAULT keeps estate and identity documents encrypted on this server (AES-256-GCM per file; the per-file key is wrapped by the app's versioned key; deleting a document destroys its key). **Honest threat model:** this is server-side encryption at rest — it protects stolen disks and backups, not a compromised host (root plus the key file can decrypt). Each document is `private` (admin only), `household` (admin + view-only accounts) or `emergency`. Documents are never given to the AI assistant.

**Emergency access (off by default):** SETTINGS → IAM → EMERGENCY ACCESS lets you name one view-only household member as the emergency contact. They can *request* access; you are notified immediately on every channel (always-on, cannot be muted) and can deny it during the wait (minimum 24 hours, default 7 days). If you do nothing, they get read access to `emergency` documents for a limited time (or, if you choose TAKEOVER and they have MFA, they become the admin). Settings that widen access wait 24 hours before taking effect. See `compliance/IR-RUNBOOK.md` §7.

**Emergency Kit:** PLAN → VAULT → GENERATE EMERGENCY KIT produces a passphrase-encrypted file with all `emergency` documents plus your contacts and instructions, readable without this server (`kit_decrypt.py` needs only Python and `cryptography`). The passphrase is shown once; store it apart from the file.

*Testing only:* `EMERGENCY_ALLOW_SHORT_WAIT=1` (see `.env.example`) bypasses the wait floor, cool-down and notification gate — never set it on a real deployment.

## Prerequisites

- **Docker** with Compose v2 (Docker Desktop + WSL2 on Windows, or Docker Engine on Linux).
- **[Ollama](https://ollama.com/)** running on the host.
- **NVIDIA GPU strongly recommended** — the 27B model is slow on CPU. ~16 GB VRAM lets it
  run; more headroom is better. It works on CPU but expect multi-minute responses.
- ~32 GB system RAM recommended.

## Models to pull

```bash
ollama pull qwen3.6:27b        # analysis + Strategist reasoning (core)
ollama pull bge-m3             # embeddings for the Fiduciary knowledge RAG
# (optional lighter/faster alternative, set via ANALYSIS_MODEL)
# ollama pull qwen3:14b
```

## API keys you'll need (all free tiers)

| Service | Used for | Get a key |
|---------|----------|-----------|
| **Finnhub** | Quotes, valuation multiples, trading volume | https://finnhub.io/ |
| **Tavily**  | News-discovery agent | https://tavily.com/ |
| **SEC EDGAR** | GAAP fundamentals — needs a descriptive User-Agent **with your email** (no signup) | just set `SEC_USER_AGENT` |
| **Portfolio source** | Your holdings (see below) | A local CSV (no account needed) or SnapTrade; Monarch is optional/unofficial |

---

## Quick start

```bash
# 1. Copy the templates and fill in YOUR values (never commit the real ones)
cp .env.example .env                                    # then edit .env
cp mcpo/config.json.example mcpo/config.json            # REQUIRED before first run
cp mcpo-secrets/mcpo.env.example mcpo-secrets/mcpo.env  # REQUIRED before first run

# 2. Pull the models (on the host)
ollama pull qwen3.6:27b
ollama pull bge-m3

# 3. Start the stack
docker compose up -d --build

# 4. Open the dashboard
#    http://localhost:8097
```

> Running this **alongside another Compose stack on the same host?** The containers are
> named `fiduciary-*` and mcpo isn't published, so nothing collides — except the one
> published host port. If `8097` is taken, set `DASH_PORT` in `.env`.

Then run **`python setup.py`** to validate your config and print any remaining steps.

---

## Portfolio data source (`PORTFOLIO_PROVIDER`)

The Fiduciary needs your holdings. Set `PORTFOLIO_PROVIDER` in `.env` (default: `csv`).

### Data sources — supported paths

- **`csv`** *(most portable — no brokerage account required)*: maintain a simple
  `holdings.csv` (see **`holdings.csv.example`**: ticker, quantity, cost_basis, account,
  institution, type) — or just use the in-app **HOLDINGS** tab, which edits the same file
  through an editable table/CSV-upload with no host file access needed. Put "IRA/Roth/401k"
  in the account name so the tax audit classifies it. Path via `HOLDINGS_CSV`
  (default `/data/holdings.csv`); prices are fetched live from Finnhub.
- **`snaptrade`** *(live, many brokerages incl. Robinhood)*: one connection aggregates
  holdings across brokerages via [SnapTrade](https://snaptrade.com/) (free dev tier). Set
  the `SNAPTRADE_*` vars, then run **`python snaptrade_register.py`** once to register a
  user, link your brokerage(s), and get your `USER_ID`/`USER_SECRET`.

### Unofficial / optional — **Monarch (unofficial, at your own risk)**

- **`monarch`** *(off by default)*: Monarch Money has **no public API**. This integration
  talks to its private, undocumented API through mcpo, and Monarch's terms restrict
  automated access. It may break without notice, and your Monarch account could be
  restricted or closed. It is not a supported integration. To enable it, an admin must tick the
  risk acknowledgment under **SETTINGS → ACCOUNTS → Monarch (unofficial, at your own risk)**
  (the acknowledgment is audit-logged as `integration.monarch.enabled`); selecting `monarch`
  in `.env` alone does nothing until that is done. Then set the `MONARCH_*` secrets. If Monarch
  fails, the app shows a clear error and backs off for 5 minutes rather than retrying.

> All providers plug into the same `build()` interface, so you can switch with one setting
> without touching anything else. The app runs fully without Monarch.

**Liabilities (loans, credit cards, etc.) — 2026-07-30:** `csv`/`snaptrade` never had a way
to represent debt (every account was hardcoded as an asset, so net worth for those two
providers was assets-only). A separate **LIABILITIES** table, right below the HOLDINGS
editor, fixes this for any provider — including `monarch`, for a debt Monarch itself
doesn't sync. Enter name/institution/type/balance owed (rate % and minimum payment are
optional and write straight into the same debt config PLAN → CASH·DEBT already uses), and
it counts against net worth and feeds the debt-payoff desk exactly like a synced account.
Backed by `GET`/`POST /api/manual-liabilities` — see the in-app WIKI → Getting started →
"No brokerage account? The HOLDINGS tab" entry for full field-by-field detail.

---

## Configuration — the ⚙ Settings gear (in the dashboard)

Most tuning lives in the dashboard now, not in files. Click the **⚙ gear** (top-right) for:

- **Advanced knobs** — Monte-Carlo paths, fees/liquidity thresholds, tax-audit cutoffs (apply
  immediately), and model config (analysis model tag, LLM timeout / keep-alive — apply on restart).
- **Secrets & keys** — a write-only, masked editor for the app's own keys (Finnhub, SEC user-agent,
  SnapTrade, analysis token). Values are never shown back and are saved to a gitignored, `0600`
  `secrets.json`; they apply on the next restart.

Precedence is always **environment variable > saved setting > default** — anything you set in `.env`
wins and is shown read-only in the UI. Note: the **Monarch / Tavily / mcpo** keys are used by the
*mcpo container*, so they can't be applied from here — set those in `.env` and restart. Per-account
DRIP/fractional flags live under **AUDITS → ACCOUNTS**, and the plan/policy/scanner assumptions in
their own panels.

## Scheduling — built in

The recurring agents run on a **scheduler inside `portfolio-api`** — no extra container,
no HTTP round-trip. Each scheduled job is enqueued into the same **serial job queue** that
all heavy work funnels through, so GPU work never overlaps and you get one place to see
what's queued/running (`GET /api/jobs`, and the badge in the dashboard header).

Default schedule (container-local time — set `TZ` in `.env`, defaults to `America/Chicago`):

| Job | Default time |
|-----|-------------|
| Daily analysis (B/S/H briefs) | every day 18:00 |
| News discovery & alerts | every day 07:00 |
| Opportunity scan | Saturday 18:00 |
| Rebalance / drift refresh | Sunday 19:00 |
| Retirement projection | 1st of month 08:00 |
| Strategist review | Friday 17:00 |

Change any of these in the dashboard under **⚙ Settings → Task Schedule** (24-hour times,
each with an overridable default). The whole scheduler can be turned off with
`INTERNAL_SCHEDULER=0` in `.env`. Every job can also be triggered on demand from the
dashboard / REST API at any time — the queue dedupes a duplicate that's already queued or
running rather than double-firing.

## Automated setup

`setup.py` will validate your `.env` and templates and pull the required Ollama model.
See `INSTALL.md` for the full walkthrough.

**Once it's running:** `INSTALL.md` gets the containers up and Monarch/holdings connected —
that alone gives you a real Positions/Research/Risk view. A handful of other panels
(Estate, ACA, Social Security, Insurance, Cash·Debt, Benefits) need you to type in some
facts about yourself first, or they stay placeholders. See `NEW-USER-SETUP-GUIDE.md` for
what to fill in and where, ranked by how much it unlocks.

---

## Security & sharing

- **Read `SHARING-CHECKLIST.md` before pushing anywhere.** Your `.env`,
  `mcpo/config.json`, any `*-sample.json`, `holdings.csv`, and the runtime `data/` are
  git-ignored — but verify.
- Use a **private repository** for anything holding your configuration.
- Your portfolio data and API keys never leave your machine.

## License

Copyright (C) 2026 Jason Bentley. Licensed under the
[GNU Affero General Public License v3.0 or later](LICENSE) (`AGPL-3.0-or-later`).

This project is developed for personal use and is **not accepting outside contributions
or pull requests.** Third-party components keep their own licenses; see [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). Optional integrations
(for example Monarch) are unofficial, off by default, and subject to the provider's own
terms of service; you are responsible for complying with them.

## Disclaimer

This project is an educational tool for personal research. It is **not** financial,
investment, tax, or legal advice, and nothing it outputs (including AI-generated analysis)
should be acted on without verifying with a qualified professional. You run it on your own
hardware and are solely responsible for complying with the terms of every third-party
provider you connect (brokerage aggregators, market-data and news APIs, Monarch, etc.).
It is provided as-is, without warranty.
