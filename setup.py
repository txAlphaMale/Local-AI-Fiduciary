#!/usr/bin/env python3
"""
setup.py — one-shot setup helper for the Fiduciary stack.

Does what can be safely automated and prints a checklist for the rest. Safe to re-run.
The n8n API calls are BEST-EFFORT (the API shape varies by version); if one fails, the
script prints the manual step, which always works.

Usage:
    python3 setup.py                 # run all steps
    python3 setup.py --skip-model    # skip the large ollama pull

Optional environment (steps skip gracefully if unset):
    OLLAMA_MODEL   default qwen3.6:27b
    N8N_URL        default http://localhost:5678   (n8n is OPTIONAL — your own or the
                   bundled --profile bundled-n8n; skip entirely if you don't schedule)
    N8N_API_KEY    n8n -> Settings -> n8n API -> create key   (to auto-import workflows)
"""
import os, sys, json, glob, shutil, subprocess, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OK, WARN, ERR, DOT = "[ok]", "[!]", "[x]", " - "


def log(sym, msg): print(f"  {sym} {msg}")
def head(msg): print(f"\n=== {msg} ===")


def http(method, url, headers=None, data=None, timeout=60):
    body = json.dumps(data).encode() if isinstance(data, (dict, list)) else data
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))
    if isinstance(data, (dict, list)):
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            try:
                return r.status, json.loads(raw)
            except ValueError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except Exception as e:
        return None, str(e)


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


def step_n8n():
    head("3. n8n workflows")
    url = os.environ.get("N8N_URL", "http://localhost:5678").rstrip("/")
    key = os.environ.get("N8N_API_KEY")
    files = sorted(glob.glob(str(ROOT / "n8n" / "portfolio-*.n8n.json")))
    if not files:
        log(WARN, "no n8n/portfolio-*.n8n.json workflows found")
        return
    if not key:
        log(WARN, "N8N_API_KEY not set -> import these manually (n8n -> Workflows -> Import from File):")
        for f in files:
            log(DOT, os.path.basename(f))
        log(DOT, "Then EITHER leave ANALYSIS_TOKEN blank (endpoints run unguarded) OR create a Header")
        log(DOT, "Auth credential 'Portfolio Analysis Token' (header X-Analysis-Token) and bind it.")
        return
    hdr = {"X-N8N-API-KEY": key, "Accept": "application/json"}
    for f in files:
        try:
            wf = json.load(open(f, encoding="utf-8"))
        except Exception as e:
            log(ERR, f"{os.path.basename(f)}: unreadable ({e})")
            continue
        payload = {k: wf[k] for k in ("name", "nodes", "connections", "settings") if k in wf}
        payload.setdefault("settings", {})
        status, resp = http("POST", f"{url}/api/v1/workflows", hdr, payload)
        if status in (200, 201):
            log(OK, f"imported {os.path.basename(f)}")
        else:
            log(ERR, f"{os.path.basename(f)}: import failed ({status}) {str(resp)[:100]}")
    log(WARN, "After import: set up the 'Portfolio Analysis Token' credential (or blank ANALYSIS_TOKEN), then Publish each workflow.")


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
    step_n8n()
    print("\nNext:  docker compose up -d --build")
    print("Then:  open the dashboard at http://localhost:8097 (chat is the CHAT tab)")


if __name__ == "__main__":
    main()
