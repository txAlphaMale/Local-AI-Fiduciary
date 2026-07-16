#!/usr/bin/env python3
"""
setup.py — one-shot setup helper for the Fiduciary stack.

Does what can be safely automated and prints a checklist for the rest. Safe to re-run.

Scheduling is built into portfolio-api (no external scheduler to configure) — see the
"Task Schedule" note printed at the end and README.md.

Usage:
    python3 setup.py                 # run all steps
    python3 setup.py --skip-model    # skip the large ollama pull

Optional environment (steps skip gracefully if unset):
    OLLAMA_MODEL   default qwen3.6:27b
"""
import os, sys, shutil, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OK, WARN, ERR, DOT = "[ok]", "[!]", "[x]", " - "


def log(sym, msg): print(f"  {sym} {msg}")
def head(msg): print(f"\n=== {msg} ===")


def step_env():
    head("1. Environment & config templates")
    env = ROOT / ".env"
    if env.exists():
        log(OK, ".env present")
    elif (ROOT / ".env.example").exists():
        shutil.copy(ROOT / ".env.example", env)
        log(WARN, "created .env from .env.example -> EDIT IT and fill in your keys before starting the stack")
    else:
        log(ERR, "no .env or .env.example found")
    cfg, ex = ROOT / "mcpo" / "config.json", ROOT / "mcpo" / "config.json.example"
    if cfg.exists():
        log(OK, "mcpo/config.json present")
    elif ex.exists():
        shutil.copy(ex, cfg)
        log(WARN, "created mcpo/config.json from example")
    else:
        log(WARN, "mcpo/config.json missing (and no example)")


def step_model(skip=False):
    head("2. Ollama models")
    models = [os.environ.get("OLLAMA_MODEL", "qwen3.6:27b"), "bge-m3"]   # LLM + RAG embeddings
    if skip:
        log(WARN, "skipped (--skip-model). Pull manually: " + "; ".join(f"ollama pull {m}" for m in models))
        return
    if shutil.which("ollama") is None:
        log(ERR, "ollama not on PATH. Install Ollama, then pull: " + ", ".join(models))
        return
    for m in models:
        log(DOT, f"pulling {m} (be patient on the large one)...")
        try:
            subprocess.run(["ollama", "pull", m], check=True)
            log(OK, f"{m} ready")
        except subprocess.CalledProcessError as e:
            log(ERR, f"ollama pull {m} failed: {e}")


def step_schedule():
    head("3. Scheduling (built in)")
    log(OK, "The recurring agents run on a scheduler inside portfolio-api — nothing to install.")
    log(DOT, "Defaults (container-local time; set TZ in .env, default America/Chicago):")
    log(" ", "  analysis 18:00 daily · discovery 07:00 daily · opportunity scan Sat 18:00")
    log(" ", "  rebalance Sun 19:00 · retirement 1st 08:00 · strategist Fri 17:00")
    log(DOT, "Change the times in the dashboard: Settings (gear) -> Task Schedule.")
    log(DOT, "Disable the scheduler entirely with INTERNAL_SCHEDULER=0 in .env.")
    log(DOT, "Every job can also be run on demand from the dashboard / REST API.")


def step_provider():
    head("Portfolio data source (set PORTFOLIO_PROVIDER in .env)")
    log(DOT, "monarch  : set MONARCH_EMAIL / MONARCH_PASSWORD / MONARCH_MFA_SECRET (live via Monarch Money)")
    log(DOT, "csv      : fill holdings.csv (template: holdings.csv.example) at $HOLDINGS_CSV (default /data/holdings.csv)")
    log(" ", "           columns ticker,quantity,cost_basis,account,institution,type; prices fetch live from Finnhub")
    log(" ", "           tip: put IRA/Roth/401k in the account name so the tax audit classifies it")
    log(DOT, "snaptrade: free dev signup at https://snaptrade.com -> set SNAPTRADE_CLIENT_ID + SNAPTRADE_CONSUMER_KEY, then")
    log(" ", "           run  python3 snaptrade_register.py  to link brokerage(s) and get SNAPTRADE_USER_ID/USER_SECRET")


def main():
    skip = "--skip-model" in sys.argv
    print("Fiduciary setup helper\n(re-runnable; automated steps fall back to manual instructions)")
    step_env()
    step_provider()
    step_model(skip)
    step_schedule()
    print("\nNext:  docker compose up -d --build")
    print("Then:  open the dashboard at http://localhost:8097 (chat is the CHAT tab)")


if __name__ == "__main__":
    main()
