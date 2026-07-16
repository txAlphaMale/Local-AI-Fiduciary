# Fiduciary — fresh install

A self-contained two-container stack: `portfolio-api` + `mcpo`. Ollama runs on the host,
and scheduling is built into `portfolio-api` (no separate scheduler container). See
`README.md` for what each piece does; this is the step-by-step.

## Files that matter

- `portfolio-api/` — the brain (dashboard, REST API, every agent, the data providers, the built-in chat, the job scheduler)
- `mcpo/config.json.example` — time + tavily (+ monarch only if you use it)
- `docker-compose.yml`, `.env.example`, `holdings.csv.example`, `snaptrade_register.py`, `setup.py`
- `docs/DEPENDENCIES.md`

## Install (in order)

```bash
# 1. Prereqs: Docker + Compose v2; NVIDIA Container Toolkit for GPU; Ollama on the host.

# 2. Clone + configure
git clone https://github.com/txAlphaMale/Local-AI-Fiduciary.git
cd Local-AI-Fiduciary
cp .env.example .env                          # fill FINNHUB, TAVILY, SEC_USER_AGENT,
                                              # MCPO_API_KEY, ANALYSIS_TOKEN (+ provider vars, step 4)
cp mcpo/config.json.example mcpo/config.json  # REQUIRED before first up
#   ^ if you're NOT using Monarch, delete the "monarch" block from mcpo/config.json

# 3. Pull the models (on the host)
ollama pull qwen3.6:27b     # analysis + Strategist
ollama pull bge-m3          # embeddings for the knowledge RAG
```

**4. Pick your data source** — set `PORTFOLIO_PROVIDER` in `.env`:

| Provider | Steps |
|----------|-------|
| `csv` *(no account)* | Fill a `holdings.csv` from `holdings.csv.example`; put it where `HOLDINGS_CSV` points (default `/data/holdings.csv`, or bind-mount it in the compose). |
| `snaptrade` *(live, many brokers incl. Robinhood)* | Free dev signup at snaptrade.com → set `SNAPTRADE_CLIENT_ID`/`CONSUMER_KEY`; run `python3 snaptrade_register.py` to link brokerage(s) and get `USER_ID`/`USER_SECRET`. |
| `monarch` | Set `MONARCH_EMAIL`/`PASSWORD`/`MFA_SECRET`. |

```bash
# 5. Start the stack (portfolio-api + mcpo)
docker compose up -d --build

# 6. Automate what can be automated (validates config, pulls models)
python3 setup.py
```

**6. Scheduling — built in, nothing to install.** The recurring agents run on a scheduler
inside `portfolio-api`, enqueued into the same serial job queue as all heavy work. Default
times (Central by default — set `TZ` in `.env`): analysis daily 18:00, discovery daily
07:00, opportunity scan Sat 18:00, rebalance Sun 19:00, retirement 1st 08:00, strategist
Fri 17:00. Change them in the dashboard under **⚙ Settings → Task Schedule**, or disable the
scheduler entirely with `INTERNAL_SCHEDULER=0`. Every job can also be run on demand from the
dashboard / REST API.

## Verify

```bash
curl -s http://localhost:8097/health          # -> {"ok":true,...}
```
- Dashboard at http://localhost:8097 shows your positions (from your chosen provider).
- Open the **CHAT** tab and ask the Fiduciary "what's my net worth?" — it should call a
  tool and answer.

> After the first run you can manage most config from the **⚙ Settings gear** (top-right):
> advanced knobs, model config, and a masked write-only editor for the app's own keys
> (Finnhub / SEC user-agent / SnapTrade). Anything already set in `.env` wins and shows
> read-only. Monarch / Tavily keys stay in `.env` (they're used by the mcpo container).
