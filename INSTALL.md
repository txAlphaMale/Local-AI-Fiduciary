# Fiduciary — fresh install

A self-contained stack: `portfolio-api` + `mcpo` (and an optional `n8n`). Ollama runs on
the host. See `README.md` for what each piece does; this is the step-by-step.

## Files that matter

- `portfolio-api/` — the brain (dashboard, REST API, every agent, the data providers, the built-in chat)
- `mcpo/config.json.example` — time + tavily (+ monarch only if you use it)
- `n8n/portfolio-*.n8n.json` — the scheduled agent workflows (optional; see step 6)
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
# 5. Start the stack (portfolio-api + mcpo; n8n is opt-in, step 6)
docker compose up -d --build

# 6. Automate what can be automated (validates config, pulls models, imports n8n workflows)
python3 setup.py
```

**6. Scheduling (optional).** The recurring agents run as n8n workflows in `n8n/`. n8n is
an external dependency — use your own, or start the bundled one:

```bash
docker compose --profile bundled-n8n up -d          # only if you want the bundled n8n
```

Import `n8n/portfolio-*.n8n.json` into whichever n8n you run (or let `setup.py` do it with
`N8N_API_KEY` set). The workflows call `http://host.docker.internal:8097`; set each one's
`X-Analysis-Token` header to match this stack's `ANALYSIS_TOKEN`.

## Verify

```bash
curl -s http://localhost:8097/health          # -> {"ok":true,...}
```
- Dashboard at http://localhost:8097 shows your positions (from your chosen provider).
- Open the **CHAT** tab and ask the Fiduciary "what's my net worth?" — it should call a
  tool and answer.
