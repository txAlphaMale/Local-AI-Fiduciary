# Fiduciary — fresh install (shareable subset)

This repo is a full homelab, but the **Fiduciary** needs only the pieces below. A fresh
install runs the slim `docker-compose.fiduciary.yml` and ignores everything else.

## What's in the Fiduciary subset

**Services** (`docker-compose.fiduciary.yml`): `ollama`, `open-webui`, `mcpo`, `portfolio-api`, `n8n`.

**Files that matter:**
- `portfolio-api/` — the brain (dashboard, REST API, every agent, the data providers)
- `owui/tools/portfolio_terminal_tool.py`, `owui/tools/paper_trading_tool.py` — the two OWUI tools
- `owui/knowledge/Fiduciary/` — reference PDFs for the knowledge collection
- `mcpo/config.json.example` — time + tavily (+ monarch only if you use it)
- `portfolio-*.n8n.json` — the scheduled agent workflows
- `.env.example`, `holdings.csv.example`, `snaptrade_register.py`, `setup.py`
- `docs/DEPENDENCIES.md`, `docs/OPEN-WEBUI-SETUP.md`

**Ignore** (not part of the Fiduciary): `Chef*`, `chef-api/`, `OW-*.py`, and the
`home_assistant` / `icloud` / `pfsense` / `google_workspace` tools + their `*-openapi.json`.

## Install (in order)

```bash
# 1. Prereqs: Docker + NVIDIA Container Toolkit; Ollama installed on the host.

# 2. Clone + configure
git clone https://github.com/txAlphaMale/local-ai-fiduciary.git
cd local-ai-fiduciary
cp .env.example .env                        # fill FINNHUB, TAVILY, SEC_USER_AGENT,
                                            # WEBUI_SECRET_KEY, MCPO_API_KEY (+ provider vars, step 4)
cp mcpo/config.json.example mcpo/config.json
#   ^ if you're NOT using Monarch, delete the "monarch" block from mcpo/config.json

# 3. Pull the models
ollama pull qwen3.5:27b     # analysis + Strategist
ollama pull bge-m3          # embeddings for the knowledge RAG
```

**4. Pick your data source** — set `PORTFOLIO_PROVIDER` in `.env`:

| Provider | Steps |
|----------|-------|
| `csv` *(no account)* | Fill a `holdings.csv` from `holdings.csv.example`; put it where `HOLDINGS_CSV` points (default `/data/holdings.csv`, or uncomment the bind-mount in the compose). |
| `snaptrade` *(live, many brokers incl. Robinhood)* | Free dev signup at snaptrade.com → set `SNAPTRADE_CLIENT_ID`/`CONSUMER_KEY`; run `python3 snaptrade_register.py` to link brokerage(s) and get `USER_ID`/`USER_SECRET`. |
| `monarch` | Set `MONARCH_EMAIL`/`PASSWORD`/`MFA_SECRET`. |

```bash
# 5. Start the stack
docker compose -f docker-compose.fiduciary.yml up -d --build

# 6. Automate what can be automated (imports n8n workflows, prints the OWUI checklist)
python3 setup.py
```

**7. Open WebUI** (http://localhost:3000) — follow **`docs/OPEN-WEBUI-SETUP.md`**:
import the two tools, create the `Fiduciary` + `financial-strategist` models (base
`qwen3.5:27b`, **Function Calling = Native**, **num_ctx 32768**), paste the system
prompts, and attach the `Fiduciary Reference` knowledge collection.

## Verify

```bash
curl -s http://localhost:8097/health          # -> {"ok":true,...}
```
- Dashboard at http://localhost:8097 shows your positions (from your chosen provider).
- In Open WebUI, ask the Fiduciary "what's my net worth?" — it should call a tool and answer.
