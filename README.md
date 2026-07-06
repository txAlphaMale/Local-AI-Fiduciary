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

Three containers via Docker Compose, plus Ollama on the host:

| Service           | Role |
|-------------------|------|
| **portfolio-api** | The brain: normalizes your holdings, computes everything, serves the dashboard, the REST API, and the built-in chat. |
| **mcpo**          | Bridges MCP servers (Monarch, Tavily, time) to the app over HTTP. |
| **n8n** *(optional)* | Schedules the recurring agent runs. Treated as an **external dependency** — bring your own n8n, or enable the bundled one with `--profile bundled-n8n`. See *Scheduling* below. |
| **Ollama** *(host)* | Runs the local LLM that does the analysis & Strategist reasoning. Not a container here — see prerequisites. |

So a plain `docker compose up -d` runs just **two** containers (`portfolio-api` + `mcpo`);
Ollama and n8n are dependencies you provide (n8n only if you want scheduling).

The **dashboard** (`http://localhost:8097`) has tabs for Positions, Research (B/S/H
briefs), Insights (drift), Plan (FIRE + Monte Carlo + Tax/ACA/Social-Security editor),
Ideas (opportunity scan), Strategist (paper trading), Audits (Tax/TLH/liquidity), and
Chat.

> **Why Ollama is on the host, not in the stack:** the analysis model is multiple GB and
> GPU pinning is host-specific. Keeping Ollama on the host avoids duplicating the model
> and lets you manage GPUs your own way. The app reaches it at `host.docker.internal:11434`.

---

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
| **Portfolio source** | Your holdings (see below) | Monarch / SnapTrade, or a local CSV (no account needed) |

---

## Quick start

```bash
# 1. Copy the templates and fill in YOUR values (never commit the real ones)
cp .env.example .env                          # then edit .env
cp mcpo/config.json.example mcpo/config.json  # REQUIRED before first run

# 2. Pull the models (on the host)
ollama pull qwen3.6:27b
ollama pull bge-m3

# 3. Start the stack
docker compose up -d --build

# 4. Open the dashboard
#    http://localhost:8097
```

> Running this **alongside another Compose stack on the same host?** The containers are
> named `fiduciary-*` and mcpo isn't published, so nothing collides — except the two
> published host ports. If `8097` or `5678` are taken, set `DASH_PORT` / `FID_N8N_PORT`
> in `.env` (e.g. `FID_N8N_PORT=5679`). Note: use `FID_N8N_PORT`, not `N8N_PORT` — the
> latter is read by n8n as its internal port and collides with its task-runner broker on 5679.

Then run **`python setup.py`** to import the n8n workflows and print any remaining steps.

---

## Portfolio data source (`PORTFOLIO_PROVIDER`)

The Fiduciary needs your holdings. Set `PORTFOLIO_PROVIDER` in `.env`:

- **`csv`** *(most portable — no brokerage account required)*: maintain a simple
  `holdings.csv` (see **`holdings.csv.example`**: ticker, quantity, cost_basis, account,
  institution, type). Export from your broker or fill it by hand. Put "IRA/Roth/401k" in
  the account name so the tax audit classifies it. Path via `HOLDINGS_CSV`
  (default `/data/holdings.csv`); prices are fetched live from Finnhub.
- **`snaptrade`** *(live, many brokerages incl. Robinhood)*: one connection aggregates
  holdings across brokerages via [SnapTrade](https://snaptrade.com/) (free dev tier). Set
  the `SNAPTRADE_*` vars, then run **`python snaptrade_register.py`** once to register a
  user, link your brokerage(s), and get your `USER_ID`/`USER_SECRET`.
- **`monarch`**: pulls live from [Monarch Money](https://www.monarchmoney.com/) via mcpo
  (set the `MONARCH_*` vars). Monarch aggregates most brokerages.

> All three plug into the same `build()` interface, so you can switch providers with one
> env var without touching anything else.

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

## Scheduling (n8n) — optional, bring-your-own

The recurring agents (daily research, weekly Strategist, drift, discovery, etc.) are
driven by n8n workflows in `n8n/`. n8n is treated like Ollama: **an external dependency
you may already run.** Pick one:

- **You already run n8n** *(recommended if so)* — leave the bundled one off
  (`docker compose up -d` doesn't start it) and **import `n8n/*.n8n.json` into your
  existing n8n**. The workflows call the dashboard at `http://host.docker.internal:8097`,
  so any n8n on the same host can drive this stack.
- **You don't run n8n** — start the bundled one:
  ```bash
  docker compose --profile bundled-n8n up -d
  ```
  It publishes on `FID_N8N_PORT` (default 5678).
- **You don't want scheduling at all** — skip n8n entirely; every agent can still be
  run on demand from the dashboard / REST API.

Two things the workflows need regardless of which n8n runs them: the dashboard must be
reachable at the URL in the HTTP nodes (default `http://host.docker.internal:8097` —
edit the nodes if you changed `DASH_PORT` or run n8n off-host), and the `X-Analysis-Token`
header must match this stack's `ANALYSIS_TOKEN`.

## Automated setup

`setup.py` will validate your `.env` and templates, import the `n8n/portfolio-*.n8n.json`
workflows via n8n's REST API, and pull the required Ollama model. See `INSTALL.md` for
the full walkthrough.

---

## Security & sharing

- **Read `SHARING-CHECKLIST.md` before pushing anywhere.** Your `.env`,
  `mcpo/config.json`, any `*-sample.json`, `holdings.csv`, and the runtime `data/` are
  git-ignored — but verify.
- Use a **private repository** for anything holding your configuration.
- Your portfolio data and API keys never leave your machine.

## Not financial advice

This project is for education and personal research. It is not investment, tax, or legal
advice. Verify everything with a qualified professional before acting.
