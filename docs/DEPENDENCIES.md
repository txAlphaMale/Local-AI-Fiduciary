# Dependencies

Everything the **Fiduciary stack** needs.

## Runtime

| Requirement | Notes |
|-------------|-------|
| **Docker** + Compose | Docker Desktop w/ WSL2 (Windows) or Docker Engine (Linux). |
| **Ollama** | Installed on the host, reachable at `host.docker.internal:11434`. |
| **NVIDIA GPU** | Strongly recommended. The 27B model needs ~16 GB VRAM to run comfortably; CPU works but responses take minutes. |
| **RAM** | ~32 GB recommended. |

## Local LLM (Ollama)

```bash
ollama pull qwen3.6:27b     # analysis + Strategist reasoning
ollama pull bge-m3          # embeddings for the Fiduciary knowledge RAG
```

- `qwen3.6:27b` is the main model — it powers the daily research job, the Strategist
  (server-side weekly loop + the in-app chat persona), and the Fiduciary chat persona. It
  must support **tool/function calling** (qwen3.6 does).
- `bge-m3` is a small embedding model for the Fiduciary's knowledge RAG — required if you
  use the knowledge collection.
- A lighter alternative (faster, less capable) can be set via `ANALYSIS_MODEL` in
  `.env`, e.g. `qwen3:14b` — pull it too if you use it.
- No uncensored/abliterated models are needed for the Fiduciary.

## External APIs (all have free tiers)

| Service | Env var(s) | Used by | Sign up |
|---------|-----------|---------|---------|
| **Finnhub** | `FINNHUB_API_KEY` | quotes, valuation multiples, trading volume (liquidity) | https://finnhub.io/ |
| **Tavily** | `TAVILY_API_KEY` | news-discovery agent | https://tavily.com/ |
| **SEC EDGAR** | `SEC_USER_AGENT` (your email, no signup) | GAAP fundamentals | — |
| **Monarch Money** *(optional)* | `MONARCH_EMAIL`, `MONARCH_PASSWORD`, `MONARCH_MFA_SECRET` | live portfolio (only if `PORTFOLIO_PROVIDER=monarch`) | https://www.monarchmoney.com/ |
| **SnapTrade** *(optional)* | `SNAPTRADE_CLIENT_ID`, `SNAPTRADE_CONSUMER_KEY` (+ `SNAPTRADE_USER_ID`, `SNAPTRADE_USER_SECRET` for commercial keys only) | live multi-brokerage portfolio, incl. Robinhood (only if `PORTFOLIO_PROVIDER=snaptrade`; link brokerages in SETTINGS → ACCOUNTS → SnapTrade, or run `snaptrade_register.py`). Installs `snaptrade-python-sdk`. | https://snaptrade.com/ |

> Finnhub's **free tier** does not return analyst price targets or mutual-fund
> profiles — the app degrades gracefully (nulls, not errors) when they're missing.

## Internal auth tokens (you generate these — not from any service)

`MCPO_API_KEY`, `ANALYSIS_TOKEN` — set to any long random string,
e.g. `openssl rand -hex 24`. `ANALYSIS_TOKEN` is **optional**: leave it blank and the
`/api/run-*` endpoints run unguarded (fine for a single-user local box).

## Python / container deps

Handled automatically by Docker — `portfolio-api/requirements.txt` (FastAPI, uvicorn,
requests) is installed at image build. Nothing to install on the host beyond Docker + Ollama.
