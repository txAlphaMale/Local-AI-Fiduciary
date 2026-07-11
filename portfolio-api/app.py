"""
portfolio-api — a self-hosted portfolio terminal for the Fiduciary.

Reads the Monarch Money data SERVER-SIDE through the local mcpo proxy (so the
MCPO_API_KEY and your financial data never touch the browser), trims + rolls up
the holdings across every investment account, and serves a dark "Bloomberg-style"
dashboard. Read-only: it only calls Monarch GET-style ops via mcpo.

Endpoints:
  GET /                -> the dashboard (static/index.html)
  GET /api/portfolio   -> computed rollup JSON (?refresh=1 to bypass cache)
  GET /health
"""

import os
import json
import time
import math
import random
import re
import datetime
import threading

import requests
from fastapi import FastAPI, Request, Header
from fastapi.responses import JSONResponse, FileResponse

import edgar   # SEC EDGAR / GAAP fundamentals
import tax     # bracket-aware federal tax engine (G5) — replaces the 3 flat tax knobs
import aca     # ACA MAGI ledger + subsidy-cliff estimator (G1) — pairs with tax.py for the bridge
import ss      # Social Security benefit + taxation engine (G2) — real-terms MC income floor
import knowledge  # local user-populated RAG over a knowledge folder (N12) — search_knowledge tool
import grounding  # dated-constant registry + staleness watchdog (G13a)
import estate     # estate & generational-wealth engine (G2) — step-up, gift-vs-hold, titling
import risk        # tail & co-movement suite (G4) — VaR/CVaR, correlation/effective bets, factors
import sizing      # position sizing & concentration action program (G7) — CVaR loss budget, staged sale
import roth        # unified marginal-rate curve + Roth conversion ladder (G8) — brackets+LTCG+NIIT+ACA+IRMAA
import leverage    # asset-backed borrowing, ruin-model-first (G6) — margin-call sim + borrow-vs-sell
import avenues     # cross-avenue after-tax comparison sheet (G12) — where the marginal dollar goes
import thesis       # per-holding thesis & exit-trigger registry (G14) — exit-discipline-as-code
import realestate   # rental real-estate deal underwriter (G9) — NOI/cap/DSCR/CoC/after-tax IRR
import bizacq       # small-business acquisition underwriter (G11) — SDE mult/DSCR/CoC/QSBS/after-tax IRR
import mcblock      # block-bootstrap MC draw mode (Part-4) — real historical return sequences
import insider      # SEC Form 4 insider-transaction signal (G15) — context-only net open-market buy/sell

# ---- Secrets bootstrap (N6 Phase 2) --------------------------------------------
# Merge the gitignored secrets.json into the process env BEFORE the config constants
# below read os.environ, so UI-managed secrets take effect on (re)start. REAL env vars
# (from .env / compose) always WIN — we only fill gaps, never override. Only portfolio-api's
# OWN secrets are managed here; cross-container secrets (Monarch/Tavily used by the mcpo
# container) can't be injected from here and stay in .env.
SECRETS_FILE = os.environ.get("SECRETS_FILE", "/data/secrets.json")
_SETTINGS_FILE_BOOT = os.environ.get("SETTINGS_FILE", "/data/settings.json")
# Non-secret config (read as module constants below) that the SETTINGS tab can manage;
# applied on RESTART via this boot-merge. Numeric analysis knobs are handled separately (live).
_CONFIG_ENV_KEYS = ("ANALYSIS_MODEL", "LLM_TIMEOUT", "LLM_KEEP_ALIVE")
# Keys present in the REAL environment at boot (from .env / compose). Used so the UI can
# honestly mark those "env-locked" — env wins and the UI must not pretend to override it.
_ENV_SECRETS_AT_BOOT = {_k for _k, _v in os.environ.items() if _v not in (None, "")}


def _boot_merge(_path, _only=None):
    """Fill env GAPS from a JSON store BEFORE the config constants read os.environ. Real env
    always wins (we never overwrite a set var). _only restricts to a key allow-list."""
    try:
        with open(_path, encoding="utf-8") as _bf:
            for _k, _v in (json.load(_bf) or {}).items():
                if _only is not None and _k not in _only:
                    continue
                if _v not in (None, "") and not os.environ.get(_k):
                    os.environ[_k] = str(_v)
    except Exception:
        pass


_boot_merge(SECRETS_FILE)                          # secrets (all keys)
_boot_merge(_SETTINGS_FILE_BOOT, _CONFIG_ENV_KEYS)  # restart-class non-secret config only

MCPO = os.environ.get("MCPO_BASE_URL", "http://mcpo:8000").rstrip("/")
ROUTE = os.environ.get("MONARCH_ROUTE", "monarch")
KEY = os.environ.get("MCPO_API_KEY", "")
TTL = int(os.environ.get("CACHE_TTL", "600"))
TIMEOUT = int(os.environ.get("MCPO_TIMEOUT", "90"))
ANALYSIS_FILE = os.environ.get("ANALYSIS_FILE", "/data/analysis.json")
POLICY_FILE = os.environ.get("POLICY_FILE", "/data/policy.json")
INSIGHTS_FILE = os.environ.get("INSIGHTS_FILE", "/data/insights.json")
DEFAULT_POLICY = {"targets_by_type": {}, "max_position_pct": 10.0, "drift_threshold_pct": 5.0,
                  "position_caps": {},   # per-ticker overrides, e.g. {"PLTR": 40, "RKLB": 40}
                  # IPS §5 asset-CLASS sleeves (the policy the drift monitor should measure, not Monarch
                  # "asset type"). Shipped defaults = the IPS draft; edit via POST /api/policy.
                  "targets_by_sleeve": {"US Equity": 70, "Intl Equity": 10, "Bond": 15, "Cash": 5}}
RETIRE_FILE = os.environ.get("RETIRE_FILE", "/data/retirement.json")
DEFAULT_RETIRE = {"withdrawal_rate_pct": 4.0, "expected_real_return_pct": 5.0,
                  "annual_expenses_override": None, "annual_savings_override": None,
                  "current_age": None, "target_age": 60, "exclude_account_types": ["Real Estate"],
                  "income_target_yield_pct": 3.5, "optimistic_real_return_pct": 8.0,
                  "volatility_pct": None,
                  # Funded ratio (G1): share of spending that is essential ("needs") vs
                  # flexible ("wants") — needs are discounted risk-free, wants at your
                  # expected real return.
                  "needs_pct_of_expenses": 65,
                  # Estate (G2): heirs/donees for the annual-exclusion gifting runway.
                  "estate_donees": None,
                  # Tax engine (G5): federal-only (TX has no state income tax).
                  "filing_status": "mfj", "spouse_age": None,
                  # Ordinary bracket to "fill" with Roth conversions in low years.
                  "convert_fill_to_rate": 0.12,
                  # ACA / MAGI (G1): pre-Medicare marketplace subsidy planning.
                  "household_size": 2,          # people on the tax return (drives FPL)
                  "aca_slcsp_annual": None,     # your local benchmark Silver premium ($/yr), for a $ PTC estimate
                  # Social Security (G2): real-terms benefit floor fed into the Monte Carlo.
                  # Most reliable input is your SSA statement's "at full retirement age" estimate.
                  "birth_year": None, "ss_claim_age": None,          # claim age (62-70); None -> FRA
                  "ss_estimated_fra_benefit": None,                  # $/mo at FRA from ssa.gov/myaccount (best)
                  "ss_pia_monthly": None, "ss_aime": None,           # alternative PIA inputs
                  "spouse_birth_year": None, "spouse_ss_claim_age": None,
                  "spouse_ss_estimated_fra_benefit": None, "spouse_ss_pia_monthly": None}
MC_RUNS = int(os.environ.get("MC_RUNS", "3000"))                        # Monte Carlo simulation paths
MC_VOLATILITY_PCT = float(os.environ.get("MC_VOLATILITY_PCT", "16.0"))  # annual real-return std dev (equity-heavy)
MC_END_AGE = int(os.environ.get("MC_END_AGE", "95"))                    # money must last to this age
MC_RETIREMENT_YEARS = int(os.environ.get("MC_RETIREMENT_YEARS", "30"))  # fallback decumulation if ages unknown
SCANNER_FILE = os.environ.get("SCANNER_FILE", "/data/scanner.json")
OPPORTUNITIES_FILE = os.environ.get("OPPORTUNITIES_FILE", "/data/opportunities.json")
OPP_TTL = int(os.environ.get("OPP_TTL", "21600"))   # serve cached scan up to 6h; refresh in background
OPP_MIN_FIT = int(os.environ.get("OPP_MIN_FIT", "3"))       # only surface ideas scoring >= this (of 4)
OPP_MAX_IDEAS = int(os.environ.get("OPP_MAX_IDEAS", "10"))  # cap the opportunity + discovery lists
DISC_BRIEF_LIMIT = int(os.environ.get("DISC_BRIEF_LIMIT", "8"))  # full research briefs for the top N discovered
FL_FILE = os.environ.get("FL_FILE", "/data/fees_liquidity.json")
FL_TTL = int(os.environ.get("FL_TTL", "43200"))                 # cache fees/liquidity audit ~12h
LIQ_MIN_DDV = float(os.environ.get("LIQ_MIN_DDV", "2000000"))   # avg daily $ volume below this = thin
MICROCAP_MUSD = float(os.environ.get("MICROCAP_MUSD", "300"))   # market cap (millions) below this = micro-cap
LIQ_PARTICIPATION = float(os.environ.get("LIQ_PARTICIPATION", "0.10"))  # assume you trade <=10% of daily volume
FEE_HIGH_PCT = float(os.environ.get("FEE_HIGH_PCT", "0.40"))    # expense ratio (%) at/above this = "high fee"
# ---- Runtime settings store (N6): advanced knobs editable from the SETTINGS tab.
# Precedence: ENV var (if set) > settings.json > default; read at REQUEST time so changes
# apply WITHOUT a restart. Secrets/provider creds are NOT here — they stay in .env (Phase 2).
SETTINGS_FILE = os.environ.get("SETTINGS_FILE", "/data/settings.json")
SETTING_SPECS = {
    "mc_runs":       {"env": "MC_RUNS",       "default": 3000,    "cast": int,   "label": "Monte Carlo paths", "group": "Monte Carlo"},
    "liq_min_ddv":   {"env": "LIQ_MIN_DDV",   "default": 2000000, "cast": float, "label": "Thin: daily $ volume below ($)", "group": "Fees / Liquidity"},
    "microcap_musd": {"env": "MICROCAP_MUSD", "default": 300,     "cast": float, "label": "Micro-cap: market cap below ($M)", "group": "Fees / Liquidity"},
    "fee_high_pct":  {"env": "FEE_HIGH_PCT",  "default": 0.40,    "cast": float, "label": "High expense ratio at/above (%)", "group": "Fees / Liquidity"},
    "tlh_min_loss":  {"env": "TLH_MIN_LOSS",  "default": 50,      "cast": float, "label": "Ignore TLH losses below ($)", "group": "Tax audit"},
    "loc_min_yield": {"env": "LOC_MIN_YIELD", "default": 3.0,     "cast": float, "label": "Tax-inefficient yield at/above (%)", "group": "Tax audit"},
    "strategist_search_budget": {"env": "STRATEGIST_SEARCH_BUDGET", "default": 20, "cast": int, "label": "Strategist web-search calls per autonomous run", "group": "Strategist"},
    "tips_fallback_pct": {"env": "TIPS_FALLBACK_PCT", "default": 2.0, "cast": float, "label": "TIPS real yield fallback (%) when FRED is unreachable", "group": "Funded ratio"},
    "rb_lower_band":  {"env": "RB_LOWER_BAND",  "default": 70,  "cast": float, "label": "Risk-based guardrails: cut spending when success < (%)", "group": "Monte Carlo"},
    "rb_upper_band":  {"env": "RB_UPPER_BAND",  "default": 99,  "cast": float, "label": "Risk-based guardrails: raise spending when success > (%)", "group": "Monte Carlo"},
    "rb_adjust_pct":  {"env": "RB_ADJUST_PCT",  "default": 10,  "cast": float, "label": "Risk-based guardrails: spending adjustment step (%)", "group": "Monte Carlo"},
    "rb_inner_runs":  {"env": "RB_INNER_RUNS",  "default": 400, "cast": int,   "label": "Risk-based guardrails: paths per success-grid cell", "group": "Monte Carlo"},
    "mc_block_years": {"env": "MC_BLOCK_YEARS", "default": 5,   "cast": int,   "label": "Block-bootstrap MC: block length (years of contiguous history)", "group": "Monte Carlo"},
    "estate_heir_ltcg_pct": {"env": "ESTATE_HEIR_LTCG_PCT", "default": 15.0, "cast": float, "label": "Gift-vs-hold: assumed heir LTCG rate (%)", "group": "Estate"},
    "risk_hist_years":   {"env": "RISK_HIST_YEARS",   "default": 2,   "cast": int,   "label": "Risk suite: years of daily history to reconstruct (risk ramp)", "group": "Risk suite"},
    "risk_hist_max_age": {"env": "RISK_HIST_MAX_AGE", "default": 3,   "cast": int,   "label": "Risk suite: refetch a ticker's daily history after (days)", "group": "Risk suite"},
    "risk_min_days":     {"env": "RISK_SUITE_MIN_DAYS","default": 60, "cast": int,   "label": "Risk suite: minimum common trading days to compute", "group": "Risk suite"},
    "sizing_loss_budget_pct": {"env": "SIZING_LOSS_BUDGET_PCT", "default": 5.0, "cast": float, "label": "Sizing: max % of net worth at risk from ONE name (CVaR)", "group": "Position sizing"},
    "sizing_horizon_days":    {"env": "SIZING_HORIZON_DAYS",    "default": 20,  "cast": int,   "label": "Sizing: CVaR loss-budget horizon (days)", "group": "Position sizing"},
    "sizing_stage_years":     {"env": "SIZING_STAGE_YEARS",     "default": 5,   "cast": int,   "label": "Sizing: staged-sale years (spread the trim)", "group": "Position sizing"},
    "sizing_ltcg_rate_pct":   {"env": "SIZING_LTCG_RATE_PCT",   "default": 15.0,"cast": float, "label": "Sizing: residual LTCG rate for the staged sale (%)", "group": "Position sizing"},
    "roth_target_ceiling_pct":{"env": "ROTH_TARGET_CEILING_PCT","default": 24.0,"cast": float, "label": "Roth ladder: fill conversions up to this marginal rate (%)", "group": "Roth / marginal curve"},
    "roth_sweep_max":         {"env": "ROTH_SWEEP_MAX",         "default": 300000,"cast": int, "label": "Marginal curve: max next-dollar income to sweep ($)", "group": "Roth / marginal curve"},
    "roth_sweep_step":        {"env": "ROTH_SWEEP_STEP",        "default": 2500,"cast": int,   "label": "Marginal curve: sweep step ($)", "group": "Roth / marginal curve"},
    "roth_rmd_age":           {"env": "ROTH_RMD_AGE",           "default": 73,  "cast": int,   "label": "Roth ladder: RMD age (end of the conversion window)", "group": "Roth / marginal curve"},
    "roth_conversion_growth_pct": {"env": "ROTH_CONVERSION_GROWTH_PCT", "default": 5.0, "cast": float, "label": "Roth ladder: assumed traditional-balance growth (%)", "group": "Roth / marginal curve"},
    "roth_irmaa_from_age":    {"env": "ROTH_IRMAA_FROM_AGE",    "default": 63,  "cast": int,   "label": "Marginal curve: model IRMAA from this age (2-yr lookback)", "group": "Roth / marginal curve"},
    "leverage_loan_rate_pct":     {"env": "LEVERAGE_LOAN_RATE_PCT",     "default": 7.0,  "cast": float, "label": "Leverage: SBLOC/margin APR (variable, SOFR-based) (%)", "group": "Leverage"},
    "leverage_maintenance_pct":   {"env": "LEVERAGE_MAINTENANCE_PCT",   "default": 35.0, "cast": float, "label": "Leverage: broker maintenance-margin requirement (%)", "group": "Leverage"},
    "leverage_ruin_tolerance_pct":{"env": "LEVERAGE_RUIN_TOLERANCE_PCT","default": 5.0,  "cast": float, "label": "Leverage: max acceptable P(margin call) for 'prudent' LTV (%)", "group": "Leverage"},
    "leverage_horizon_years":     {"env": "LEVERAGE_HORIZON_YEARS",     "default": 2,    "cast": int,   "label": "Leverage: ruin-model horizon (years)", "group": "Leverage"},
    "leverage_paths":             {"env": "LEVERAGE_PATHS",             "default": 3000, "cast": int,   "label": "Leverage: ruin-model bootstrap paths", "group": "Leverage"},
    "leverage_expected_return_pct":{"env": "LEVERAGE_EXPECTED_RETURN_PCT","default": 7.0, "cast": float, "label": "Leverage: assumed asset expected return (borrow-vs-sell) (%)", "group": "Leverage"},
    "leverage_ltcg_rate_pct":     {"env": "LEVERAGE_LTCG_RATE_PCT",     "default": 15.0, "cast": float, "label": "Leverage: LTCG rate for the sell alternative (%)", "group": "Leverage"},
    "avenue_market_return_pct":   {"env": "AVENUE_MARKET_RETURN_PCT",   "default": 7.0,  "cast": float, "label": "Cross-avenue: broad-index expected return (%)", "group": "Cross-avenue"},
    "avenue_div_yield_pct":       {"env": "AVENUE_DIV_YIELD_PCT",       "default": 1.3,  "cast": float, "label": "Cross-avenue: index dividend yield (taxed yearly) (%)", "group": "Cross-avenue"},
    "avenue_concentrated_return_pct":{"env": "AVENUE_CONCENTRATED_RETURN_PCT","default": 7.0,"cast": float, "label": "Cross-avenue: concentrated-add expected return (%)", "group": "Cross-avenue"},
    "avenue_treasury_yield_pct":  {"env": "AVENUE_TREASURY_YIELD_PCT",  "default": 4.55, "cast": float, "label": "Cross-avenue: Treasury/bond yield (%)", "group": "Cross-avenue"},
    "avenue_debt_apr_pct":        {"env": "AVENUE_DEBT_APR_PCT",        "default": 6.57, "cast": float, "label": "Cross-avenue: your debt APR (pay-down return) (%)", "group": "Cross-avenue"},
    "avenue_debt_deductible":     {"env": "AVENUE_DEBT_DEDUCTIBLE",     "default": 0.0,  "cast": float, "label": "Cross-avenue: debt interest deductible? (1=yes,0=no)", "group": "Cross-avenue"},
    "avenue_re_cap_rate_pct":     {"env": "AVENUE_RE_CAP_RATE_PCT",     "default": 6.0,  "cast": float, "label": "Cross-avenue: real-estate cap rate (%)", "group": "Cross-avenue"},
    "avenue_re_appreciation_pct": {"env": "AVENUE_RE_APPRECIATION_PCT", "default": 2.0,  "cast": float, "label": "Cross-avenue: real-estate appreciation (%)", "group": "Cross-avenue"},
    "avenue_re_depr_shield_frac": {"env": "AVENUE_RE_DEPR_SHIELD_FRAC", "default": 0.6,  "cast": float, "label": "Cross-avenue: RE income fraction shielded by depreciation", "group": "Cross-avenue"},
    "avenue_roth_future_rate_pct":{"env": "AVENUE_ROTH_FUTURE_RATE_PCT","default": 24.0, "cast": float, "label": "Cross-avenue: expected future (RMD) marginal rate for Roth arb (%)", "group": "Cross-avenue"},
    "avenue_match_rate_pct":      {"env": "AVENUE_MATCH_RATE_PCT",      "default": 0.0,  "cast": float, "label": "Cross-avenue: employer/HSA match rate (%, 0 if none)", "group": "Cross-avenue"},
    "avenue_match_cap_dollars":   {"env": "AVENUE_MATCH_CAP_DOLLARS",   "default": 0.0,  "cast": float, "label": "Cross-avenue: annual match-eligible contribution ($)", "group": "Cross-avenue"},
    "biz_loan_rate_pct":          {"env": "BIZ_LOAN_RATE_PCT",          "default": 10.0, "cast": float, "label": "Business acq: SBA 7(a)/seller loan rate (variable, prime-based) (%)", "group": "Business acquisition"},
    "insider_lookback_days":      {"env": "INSIDER_LOOKBACK_DAYS",      "default": 120,  "cast": int,   "label": "Insider signal: Form 4 lookback window (days)", "group": "Insider signal"},
    "insider_max_filings":        {"env": "INSIDER_MAX_FILINGS",        "default": 20,   "cast": int,   "label": "Insider signal: max Form 4 filings fetched per ticker", "group": "Insider signal"},
}
# Non-secret model/infra config keyed by ENV name (stored in settings.json under that name;
# applied on RESTART via _boot_merge). NOT read live — they set module constants at import.
CONFIG_SPECS = [
    {"key": "ANALYSIS_MODEL", "label": "Analysis / Strategist model (Ollama tag)", "group": "Model", "default": "qwen3.6:27b"},
    {"key": "LLM_TIMEOUT",    "label": "LLM per-call timeout (seconds)",           "group": "Model", "default": "1200"},
    {"key": "LLM_KEEP_ALIVE", "label": "Keep model resident (e.g. 30m)",           "group": "Model", "default": "30m"},
]


def _read_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _knob(key):
    """Resolve an advanced knob: ENV override > settings.json > default."""
    spec = SETTING_SPECS[key]
    ev = os.environ.get(spec["env"])
    if ev not in (None, ""):
        try:
            return spec["cast"](ev)
        except (TypeError, ValueError):
            pass
    v = _read_settings().get(key)
    if v is not None:
        try:
            return spec["cast"](v)
        except (TypeError, ValueError):
            pass
    return spec["default"]


DISCOVERY_FILE = os.environ.get("DISCOVERY_FILE", "/data/discovery.json")
PAPER_FILE = os.environ.get("PAPER_FILE", "/data/paper.json")
DEFAULT_PAPER_CASH = float(os.environ.get("PAPER_STARTING_CASH", "2000"))
DEFAULT_SCANNER = {"watchlist": [], "include_holding_peers": False,
                   "criteria": {"min_rev_growth_pct": 15.0, "min_fcf_margin_pct": 0.0,
                                "max_pe_forward": 60.0, "min_analyst_buy_pct": 60.0,
                                "min_piotroski": 0,   # 0 = off; when >0 it becomes a 5th fit criterion
                                "max_share_price": None, "min_share_price": None},
                   "strategy_description": "Quality growth: durable revenue growth, positive free cash flow, "
                                           "analyst support, and a valuation that isn't extreme.",
                   "discovery_queries": ["promising growth stocks to watch",
                                         "notable analyst upgrades and stock movers this week",
                                         "emerging AI, space, and defense technology stocks"],
                   "alert_holdings_limit": 15}
# Per-account (per-brokerage) settings the analysis factors in. Keyed by account NAME
# (the stable id used across the portfolio payload). DRIP = dividends auto-reinvested
# into the SAME security; fractional_ok = broker supports fractional-share buys.
ACCOUNTS_FILE = os.environ.get("ACCOUNTS_FILE", "/data/accounts.json")
DEFAULT_ACCOUNT_FLAGS = {"drip": False, "fractional_ok": True}
ANALYSIS_TOKEN = os.environ.get("ANALYSIS_TOKEN", "")
FINNHUB = os.environ.get("FINNHUB_API_KEY", "")
OLLAMA = os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434").rstrip("/")
ANALYSIS_MODEL = os.environ.get("ANALYSIS_MODEL", "qwen3.6:27b")
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "1200"))     # big models on Pascal are slow
LLM_KEEP_ALIVE = os.environ.get("LLM_KEEP_ALIVE", "30m")     # keep model resident between briefs
ANALYSIS_NUM_CTX = int(os.environ.get("ANALYSIS_NUM_CTX", "16384"))   # fit full fundamentals+valuation+news without truncation
ANALYSIS_THINK = os.environ.get("ANALYSIS_THINK", "0").lower() in ("1", "true", "yes", "on")  # reasoning ON = deeper briefs, slower

_run_lock = threading.Lock()
_run_state = {"status": "idle", "analyzed": 0, "total": 0, "started": 0}

app = FastAPI(title="portfolio-terminal", version="0.1.0")
_cache = {"ts": 0.0, "data": None}


# ---------------- mcpo / Monarch ----------------
def _post(op, payload):
    url = f"{MCPO}/{ROUTE}/{op}"
    headers = {"Content-Type": "application/json"}
    if KEY:
        headers["Authorization"] = f"Bearer {KEY}"
    r = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _accounts(data):
    if isinstance(data, dict):
        return data.get("accounts") or data.get("data") or []
    return data if isinstance(data, list) else []


def _holds(data):
    # raw: {"holdings": {"portfolio": {"aggregateHoldings": {"edges": [{"node": {...}}]}}}}
    node = data
    for k in ("holdings", "portfolio", "aggregateHoldings"):
        if isinstance(node, dict) and k in node:
            node = node[k]
    if isinstance(node, dict) and "edges" in node:
        return [e.get("node", e) for e in node["edges"] if isinstance(e, dict)]
    return node if isinstance(node, list) else []


def _ref_price(h):
    """Best available per-share price for a holding (implied current, then security/holding price)."""
    s = h.get("security") or {}
    qty, val = h.get("quantity"), h.get("totalValue")
    if qty and val:                       # implied current price/share = the most reliable anchor
        return val / qty
    return s.get("currentPrice") or s.get("closingPrice") \
        or ((h.get("holdings") or [{}])[0] or {}).get("closingPrice")


def _basis_convention(raw):
    """Monarch's `basis` is per-share for some providers (Wells Fargo) and a TOTAL for
    others (Vanguard, E*TRADE). Detect which per account by majority vote: for each
    holding compare basis-as-per-share vs basis-as-total against the known price/share,
    and pick whichever interpretation implies a saner purchase price."""
    votes_total = votes_ps = 0
    for h in raw:
        qty, basis = h.get("quantity"), h.get("basis")
        ref = _ref_price(h)
        if not qty or not basis or basis <= 0 or not ref or ref <= 0:
            continue
        d_ps = abs(math.log(basis / ref))            # basis interpreted as per-share cost
        d_tot = abs(math.log((basis / qty) / ref))   # basis interpreted as a total cost
        if d_tot < d_ps:
            votes_total += 1
        else:
            votes_ps += 1
    return "total" if votes_total > votes_ps else "per_share"


def _hold(h, conv="per_share"):
    s = h.get("security") or {}
    qty = h.get("quantity")
    basis = h.get("basis")              # per-share OR total, depending on the account's provider
    val = h.get("totalValue")
    if qty is not None and basis is not None and basis > 0:
        cost_total = round(basis if conv == "total" else qty * basis, 2)
    else:
        cost_total = None               # no basis reported (e.g. cash, Principal 401k)
    # Backstop guard: if a row is still absurd vs current value (genuinely corrupt data),
    # treat cost/gain as unknown rather than show garbage.
    val_f = val if val is not None else 0
    suspect = cost_total is not None and (cost_total <= 0 or (val_f > 0 and cost_total > val_f * 50))
    if suspect:
        cost_total = None
    gain = round(val - cost_total, 2) if (val is not None and cost_total is not None) else None
    gain_pct = round(gain / cost_total * 100, 2) if (gain is not None and cost_total) else None
    return {
        "ticker": s.get("ticker") or (s.get("name") or "?"),
        "name": s.get("name"),
        "type": s.get("typeDisplay") or s.get("type") or "Other",
        "quantity": qty,
        "price": s.get("currentPrice"),
        "value": round(val, 2) if val is not None else 0,
        "cost_total": cost_total,
        "gain": gain,
        "gain_pct": gain_pct,
        "day_pct": s.get("oneDayChangePercent"),
        "suspect": suspect,
    }


def _derive(accounts, positions, by_account, inv, net_worth, total_assets, total_liabilities):
    """Source-agnostic derivation: given normalized accounts + positions (from ANY provider),
    compute the full dashboard payload (consolidation, weights, aggregations, movers)."""
    invested = round(sum(p["value"] or 0 for p in positions), 2)
    total_gain = round(sum(p["gain"] or 0 for p in positions if p["gain"] is not None), 2)
    total_cost = round(sum(p["cost_total"] or 0 for p in positions if p["cost_total"] is not None), 2)

    # consolidate the same security held across multiple accounts into one row
    cons = {}
    for p in positions:
        key = p["ticker"] or p["name"] or "?"
        c = cons.setdefault(key, {"ticker": p["ticker"], "name": p["name"], "type": p["type"],
                                  "quantity": 0.0, "value": 0.0, "cost_total": 0.0,
                                  "price": p["price"], "day_pct": p["day_pct"],
                                  "accounts": [], "suspect": False})
        c["quantity"] = round(c["quantity"] + (p["quantity"] or 0), 4)
        c["value"] = round(c["value"] + (p["value"] or 0), 2)
        if p.get("suspect") or p["cost_total"] is None:
            c["suspect"] = True   # any unreliable leg -> the merged cost is incomplete
        else:
            c["cost_total"] = round(c["cost_total"] + p["cost_total"], 2)
        c["accounts"].append(p["account"])
    consolidated = []
    for c in cons.values():
        c["accounts"] = sorted(set(a for a in c["accounts"] if a))
        c["account_count"] = len(c["accounts"])
        if c["suspect"] or not c["cost_total"]:
            c["cost_total"] = c["gain"] = c["gain_pct"] = None
        else:
            c["gain"] = round(c["value"] - c["cost_total"], 2)
            c["gain_pct"] = round(c["gain"] / c["cost_total"] * 100, 2) if c["cost_total"] else None
        c["weight_pct"] = round(c["value"] / invested * 100, 2) if invested else None
        consolidated.append(c)
    consolidated.sort(key=lambda x: -(x["value"] or 0))

    def agg(keyfn):
        d = {}
        for p in positions:
            d[keyfn(p)] = round(d.get(keyfn(p), 0) + (p["value"] or 0), 2)
        return [{"label": k, "value": v} for k, v in sorted(d.items(), key=lambda x: -x[1])]

    # gainers / losers / movers consolidated by security (one row per ticker)
    have_gain = [c for c in consolidated if c["gain"] is not None]
    have_day = [c for c in consolidated if c["day_pct"] is not None]

    return {
        "generated_at": int(time.time()),
        "net_worth": round(net_worth, 2),
        "total_assets": round(total_assets, 2),
        "total_liabilities": round(total_liabilities, 2),
        "invested_value": invested,
        "total_cost": total_cost,
        "total_unrealized_gain": total_gain,
        "total_gain_pct": round(total_gain / total_cost * 100, 2) if total_cost else None,
        "account_count": len(accounts),
        "investment_account_count": len(inv),
        "position_count": len(positions),
        "unique_securities": len(consolidated),
        "consolidated": consolidated,
        "by_institution": agg(lambda p: p["institution"] or "?"),
        "by_type": agg(lambda p: p["type"] or "Other"),
        "accounts": sorted(accounts, key=lambda a: -(a["balance"] or 0)),
        "by_account": sorted(by_account, key=lambda a: -(a.get("value") or 0)),
        "gainers": sorted(have_gain, key=lambda p: -p["gain"])[:8],
        "losers": sorted(have_gain, key=lambda p: p["gain"])[:8],
        "day_movers": sorted(have_day, key=lambda p: -abs(p["day_pct"]))[:10],
        "positions": sorted(positions, key=lambda p: -(p["value"] or 0)),
    }


def _build_monarch():
    accts_raw = _accounts(_post("get_accounts", {}))
    accounts, net_worth, total_assets, total_liabilities = [], 0.0, 0.0, 0.0
    for a in accts_raw:
        bal = a.get("displayBalance") or 0
        if a.get("includeBalanceInNetWorth"):
            # Monarch reports liabilities (isAsset=False) with a POSITIVE balance = debt owed,
            # so they must be SUBTRACTED from net worth, not added (the old bug double-counted them).
            if a.get("isAsset") is False:
                net_worth -= bal
                total_liabilities += bal
            else:
                net_worth += bal
                total_assets += bal
        accounts.append({
            "id": a.get("id"),
            "name": a.get("displayName"),
            "institution": (a.get("institution") or {}).get("name"),
            "type": (a.get("type") or {}).get("display"),
            "balance": round(bal, 2),
            "holdings_count": a.get("holdingsCount") or 0,
            "is_asset": a.get("isAsset"),
        })

    inv = [a for a in accounts if a["holdings_count"] > 0]
    positions, by_account = [], []
    for a in inv:
        try:
            raw = _holds(_post("get_account_holdings", {"account_id": a["id"]}))
            conv = _basis_convention(raw)          # per-account: per-share vs total cost basis
            comp = [_hold(h, conv) for h in raw]
            comp = [c for c in comp if (c.get("quantity") or 0) > 0]   # exclude sold-out (0-share) positions
        except Exception as e:  # one bad account shouldn't sink the whole dashboard
            by_account.append({"account": a["name"], "institution": a["institution"],
                               "value": a["balance"], "error": str(e), "holdings": []})
            continue
        tv = round(sum((c["value"] or 0) for c in comp), 2)
        by_account.append({"account": a["name"], "institution": a["institution"],
                           "value": tv, "balance": a["balance"], "holdings": comp})
        for c in comp:
            c2 = dict(c)
            c2["account"] = a["name"]
            c2["institution"] = a["institution"]
            positions.append(c2)

    return _derive(accounts, positions, by_account, inv, net_worth, total_assets, total_liabilities)


def _build_csv():
    """Portfolio from a local holdings.csv (set PORTFOLIO_PROVIDER=csv) — no brokerage account
    required, works for any broker. Columns: ticker, quantity, cost_basis, account, institution, type.
    Prices are fetched live from Finnhub. Put IRA/Roth/401k in the account name so the tax audit
    classifies it correctly. Path via HOLDINGS_CSV (default /data/holdings.csv)."""
    import csv as _csv
    path = os.environ.get("HOLDINGS_CSV", "/data/holdings.csv")
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(_csv.DictReader(f))
    except FileNotFoundError:
        return _derive([], [], [], [], 0.0, 0.0, 0.0)
    accounts_map, positions = {}, []
    for r in rows:
        tkr = (r.get("ticker") or "").strip().upper()
        try:
            qty = float(r.get("quantity") or 0)
        except ValueError:
            qty = 0
        if not tkr or qty <= 0:
            continue
        try:
            cost = float(r["cost_basis"]) if (r.get("cost_basis") or "").strip() else None
        except (ValueError, KeyError):
            cost = None
        acct = (r.get("account") or "Brokerage").strip() or "Brokerage"
        inst = (r.get("institution") or "").strip() or None
        typ = (r.get("type") or "Stock").strip() or "Stock"
        q = {"c": 1.0, "dp": 0.0} if typ.lower() == "cash" else (_finnhub("quote", tkr) or {})
        price = (q.get("c") if isinstance(q, dict) else None) or None
        dp = q.get("dp") if isinstance(q, dict) else None
        value = round(qty * price, 2) if price else None
        gain = round(value - cost, 2) if (value is not None and cost is not None) else None
        positions.append({
            "ticker": tkr, "name": (r.get("name") or "").strip() or tkr, "type": typ,
            "quantity": round(qty, 4), "value": value, "cost_total": cost, "price": price,
            "day_pct": dp, "gain": gain,
            "gain_pct": round(gain / cost * 100, 2) if (gain is not None and cost) else None,
            "suspect": cost is None, "account": acct, "institution": inst,
        })
        a = accounts_map.setdefault(acct, {"id": acct, "name": acct, "institution": inst,
                                           "type": "Investments", "balance": 0.0,
                                           "holdings_count": 0, "is_asset": True})
        a["balance"] = round(a["balance"] + (value or 0), 2)
        a["holdings_count"] += 1
    accounts = list(accounts_map.values())
    by_account = [{"account": a["name"], "institution": a["institution"],
                   "value": round(sum(p["value"] or 0 for p in positions if p["account"] == a["name"]), 2),
                   "balance": a["balance"],
                   "holdings": [p for p in positions if p["account"] == a["name"]]}
                  for a in accounts]
    nw = round(sum(p["value"] or 0 for p in positions), 2)
    return _derive(accounts, positions, by_account, accounts, nw, nw, 0.0)


def _dig(obj, *keys):
    """Safely walk nested dict keys; return None if any level is missing/not a dict."""
    for k in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _build_snaptrade():
    """Portfolio via SnapTrade (PORTFOLIO_PROVIDER=snaptrade) — one connection aggregates holdings
    across many brokerages (Robinhood, Fidelity, Schwab, E*TRADE, Webull, ...). Requires a dev
    signup (SNAPTRADE_CLIENT_ID / SNAPTRADE_CONSUMER_KEY) and a registered, brokerage-linked user
    (SNAPTRADE_USER_ID / SNAPTRADE_USER_SECRET — see snaptrade_register.py). Field access is
    defensive; verify the mapping against one live response the first time (see the diag prints)."""
    cid = os.environ.get("SNAPTRADE_CLIENT_ID")
    ckey = os.environ.get("SNAPTRADE_CONSUMER_KEY")
    uid = os.environ.get("SNAPTRADE_USER_ID")
    usec = os.environ.get("SNAPTRADE_USER_SECRET")
    if not all([cid, ckey, uid, usec]):
        raise RuntimeError("SnapTrade not configured: set SNAPTRADE_CLIENT_ID, SNAPTRADE_CONSUMER_KEY, "
                           "SNAPTRADE_USER_ID, SNAPTRADE_USER_SECRET (run snaptrade_register.py first).")
    from snaptrade_client import SnapTrade   # pip: snaptrade-python-sdk
    st = SnapTrade(consumer_key=ckey, client_id=cid)

    def _body(resp):
        return getattr(resp, "body", resp) or []

    accts_raw = _body(st.account_information.list_user_accounts(user_id=uid, user_secret=usec))
    accounts, positions, by_account = [], [], []
    for ar in accts_raw:
        aid = _dig(ar, "id")
        aname = _dig(ar, "name") or _dig(ar, "number") or "Account"
        inst = _dig(ar, "institution_name")
        bal = _num(_dig(ar, "balance", "total", "amount")) or 0.0
        accounts.append({"id": aid, "name": aname, "institution": inst, "type": "Investments",
                         "balance": round(bal, 2), "holdings_count": 0, "is_asset": True})
        try:
            plist = _body(st.account_information.get_user_account_positions(
                user_id=uid, user_secret=usec, account_id=aid))
        except Exception as e:
            by_account.append({"account": aname, "institution": inst, "value": round(bal, 2),
                               "error": str(e), "holdings": []})
            continue
        holds = []
        for p in plist:
            tkr = _dig(p, "symbol", "symbol", "symbol") or _dig(p, "symbol", "symbol") or _dig(p, "symbol")
            tkr = str(tkr).upper().strip() if isinstance(tkr, str) else ""
            qty = _num(_dig(p, "units")) or 0
            if not tkr or qty == 0:
                continue
            price = _num(_dig(p, "price"))
            avg = _num(_dig(p, "average_purchase_price"))
            cost = round(qty * avg, 2) if avg is not None else None
            value = round(qty * price, 2) if price is not None else None
            gain = round(value - cost, 2) if (value is not None and cost is not None) else None
            rec = {"ticker": tkr, "name": _dig(p, "symbol", "symbol", "description") or tkr,
                   "type": _dig(p, "symbol", "symbol", "type", "description") or "Stock",
                   "quantity": round(qty, 4), "value": value, "cost_total": cost, "price": price,
                   "day_pct": None, "gain": gain,
                   "gain_pct": round(gain / cost * 100, 2) if (gain is not None and cost) else None,
                   "suspect": cost is None}
            holds.append(rec)
            rec2 = dict(rec)
            rec2["account"], rec2["institution"] = aname, inst
            positions.append(rec2)
        accounts[-1]["holdings_count"] = len(holds)
        by_account.append({"account": aname, "institution": inst,
                           "value": round(sum(h["value"] or 0 for h in holds), 2),
                           "balance": round(bal, 2), "holdings": holds})
    nw = round(sum(p["value"] or 0 for p in positions), 2)
    return _derive(accounts, positions, by_account, accounts, nw, nw, 0.0)


def build():
    """Dispatch to the configured portfolio provider (PORTFOLIO_PROVIDER=monarch|csv|snaptrade)."""
    p = os.environ.get("PORTFOLIO_PROVIDER", "monarch").lower()
    if p == "csv":
        d = _build_csv()
    elif p == "snaptrade":
        d = _build_snaptrade()
    else:
        d = _build_monarch()
    _snapshot_value(d)          # record today's value for the accruing risk-metrics history
    return d


@app.get("/api/portfolio")
def portfolio(refresh: int = 0):
    now = time.time()
    if refresh or _cache["data"] is None or (now - _cache["ts"]) > TTL:
        try:
            _cache["data"] = build()
            _cache["ts"] = now
        except Exception as e:
            if _cache["data"] is None:
                return JSONResponse({"error": f"Could not load portfolio: {e}"}, status_code=502)
    return {"cached_age_s": int(now - _cache["ts"]), **_cache["data"]}


def _data():
    now = time.time()
    if _cache["data"] is None or (now - _cache["ts"]) > TTL:
        _cache["data"] = build()
        _cache["ts"] = now
    return _cache["data"]


# ---------------- Daily value snapshot -> realized risk metrics (accrues over time) ----------------
VALUE_HISTORY_FILE = os.environ.get("VALUE_HISTORY_FILE", "/data/value_history.json")
RISK_MIN_DAYS = int(os.environ.get("RISK_MIN_DAYS", "20"))


def _read_value_history():
    try:
        with open(VALUE_HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f) or []
    except Exception:
        return []


def _snapshot_value(d):
    """Append one {date, net_worth, invested_value} row per calendar day so a real return history
    accrues for realized volatility / max-drawdown / Sharpe. Idempotent per day (updates today's row)."""
    if not d:
        return
    try:
        today = datetime.date.today().isoformat()
        hist = _read_value_history()
        # benchmark close (SPY), fetched at most once/day (reuse today's if already stored)
        spy = None
        if hist and hist[-1].get("date") == today and hist[-1].get("spy"):
            spy = hist[-1].get("spy")
        else:
            try:
                spy = _price(os.environ.get("BENCHMARK_TICKER", "SPY"))
            except Exception:
                spy = None
        row = {"date": today, "net_worth": d.get("net_worth"),
               "invested_value": d.get("invested_value"), "spy": spy}
        if hist and hist[-1].get("date") == today:
            hist[-1] = row
        else:
            hist.append(row)
        hist = hist[-2500:]
        os.makedirs(os.path.dirname(VALUE_HISTORY_FILE) or ".", exist_ok=True)
        with open(VALUE_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(hist, f)
    except Exception:
        pass


def _risk_metrics():
    """Realized risk from the accrued daily value history: annualized volatility, max drawdown,
    best/worst day, and a rough Sharpe. Returns a 'building' status until enough days are recorded.
    NOTE: computed on portfolio VALUE, so deposits/withdrawals add some noise to returns; max
    drawdown (peak-to-trough) is robust to that, volatility/Sharpe are approximate."""
    hist = _read_value_history()
    pts = [(h.get("date"), h.get("invested_value") or h.get("net_worth"))
           for h in hist if (h.get("invested_value") or h.get("net_worth"))]
    n = len(pts)
    if n < RISK_MIN_DAYS:
        return {"status": "building", "days_recorded": n, "days_needed": RISK_MIN_DAYS,
                "since": (pts[0][0] if pts else None),
                "note": f"Recording daily portfolio value ({n}/{RISK_MIN_DAYS} days). Realized "
                        "volatility, max drawdown and Sharpe appear once enough days accrue."}
    vals = [v for _, v in pts]
    rets = [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals)) if vals[i - 1]]
    mean = sum(rets) / len(rets) if rets else 0.0
    var = sum((x - mean) ** 2 for x in rets) / len(rets) if rets else 0.0
    sd = var ** 0.5
    ann_vol = sd * (252 ** 0.5)
    ann_ret = mean * 252
    peak, maxdd, dd_date = vals[0], 0.0, None
    for (dt, _), v in zip(pts, vals):
        peak = max(peak, v)
        dd = v / peak - 1 if peak else 0
        if dd < maxdd:
            maxdd, dd_date = dd, dt
    worst = min(rets) if rets else None
    best = max(rets) if rets else None
    rf = float(os.environ.get("RISK_FREE_PCT", "4.0")) / 100.0
    sharpe = round((ann_ret - rf) / ann_vol, 2) if ann_vol else None
    # conventional target-semideviation: sum squared shortfalls below MAR(=0) over ALL periods, not just down days
    dsd = ((sum(min(0.0, r) ** 2 for r in rets) / len(rets)) ** 0.5 * (252 ** 0.5)) if rets else 0.0
    sortino = round((ann_ret - rf) / dsd, 2) if dsd else None
    # ---- benchmark-relative (portfolio vs SPY), over days where both series exist ----
    bench = None
    bpts = [(h.get("invested_value") or h.get("net_worth"), h.get("spy"))
            for h in hist if (h.get("invested_value") or h.get("net_worth")) and h.get("spy")]
    if len(bpts) >= RISK_MIN_DAYS:
        pv = [p for p, _ in bpts]
        bv = [b for _, b in bpts]
        pr = [pv[i] / pv[i - 1] - 1 for i in range(1, len(pv)) if pv[i - 1]]
        br = [bv[i] / bv[i - 1] - 1 for i in range(1, len(bv)) if bv[i - 1]]
        m = min(len(pr), len(br))
        pr, br = pr[-m:], br[-m:]
        if m >= 2:
            mp, mb = sum(pr) / m, sum(br) / m
            cov = sum((pr[i] - mp) * (br[i] - mb) for i in range(m)) / m
            vb = sum((x - mb) ** 2 for x in br) / m
            beta_b = (cov / vb) if vb else None
            alpha_ann = ((mp - (beta_b or 0) * mb) * 252) if beta_b is not None else None
            active = [pr[i] - br[i] for i in range(m)]
            ma = sum(active) / m
            sda = (sum((x - ma) ** 2 for x in active) / m) ** 0.5
            pc, bc = 1.0, 1.0
            for x in pr:
                pc *= (1 + x)
            for x in br:
                bc *= (1 + x)
            bench = {"benchmark": os.environ.get("BENCHMARK_TICKER", "SPY"), "days": m + 1,
                     "portfolio_return_pct": round((pc - 1) * 100, 1),
                     "benchmark_return_pct": round((bc - 1) * 100, 1),
                     "excess_return_pct": round((pc - bc) * 100, 1),
                     "beta_vs_benchmark": round(beta_b, 2) if beta_b is not None else None,
                     "alpha_annualized_pct": round(alpha_ann * 100, 1) if alpha_ann is not None else None,
                     "information_ratio": round(ma / sda * (252 ** 0.5), 2) if sda else None,
                     "note": "Portfolio vs benchmark over the recorded window (both from daily closes; flow "
                             "noise applies). Excess = your cumulative return minus the benchmark's."}
    # Calmar (return per unit of max-drawdown pain) + Treynor (return per unit of market risk).
    calmar = risk.calmar(ann_ret * 100, maxdd * 100)
    treynor = risk.treynor(ann_ret * 100, rf * 100, (bench or {}).get("beta_vs_benchmark")) if bench else None
    return {"status": "ready", "days_recorded": n, "since": pts[0][0], "benchmark": bench,
            "annualized_volatility_pct": round(ann_vol * 100, 1),
            "annualized_return_pct": round(ann_ret * 100, 1),
            "max_drawdown_pct": round(maxdd * 100, 1), "max_drawdown_date": dd_date,
            "best_day_pct": round(best * 100, 2) if best is not None else None,
            "worst_day_pct": round(worst * 100, 2) if worst is not None else None,
            "sharpe": sharpe, "sortino": sortino, "calmar": calmar, "treynor": treynor,
            "note": "Realized from your recorded daily portfolio value. Deposits/withdrawals add some "
                    "noise to volatility & Sharpe (risk-free "
                    + os.environ.get("RISK_FREE_PCT", "4.0") + "%); max drawdown is robust. Informational."}


@app.get("/api/risk-metrics")
def get_risk_metrics():
    return _risk_metrics()


@app.get("/api/value-history")
def get_value_history():
    return {"history": _read_value_history()}


# ============ G4: tail & co-movement risk suite (VaR/CVaR, correlation, factors) ============
# The "risk ramp": rather than wait ~20 days of live snapshots to accrue, reconstruct 2-3 yr
# of daily portfolio returns from CURRENT holdings x their historical dailies (assumes today's
# weights held throughout — stated clearly in the output). Data: Stooq free daily CSV per
# ticker (cached); Fama-French 5+momentum daily from Ken French's free library (cached). All
# heavy math is in risk.py. Defensive everywhere — unreachable sources degrade gracefully.
PRICE_HIST_DIR = os.environ.get("PRICE_HIST_DIR", "/data/price_hist")
FF_FACTORS_FILE = os.environ.get("FF_FACTORS_FILE", "/data/ff_factors.json")
FF_MAX_AGE = int(os.environ.get("FF_MAX_AGE_DAYS", "7"))            # refetch factor library weekly
RISK_SUITE_FILE = os.environ.get("RISK_SUITE_FILE", "/data/risk_suite.json")
RISK_SUITE_TTL = int(os.environ.get("RISK_SUITE_TTL", "86400"))    # serve cached suite up to ~1 day
_risk_lock = threading.Lock()
_FF_NAMES = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "Mom"]


def _stooq_daily(sym):
    """Daily close history for a US-listed ticker from Stooq's free CSV endpoint, cached per
    ticker to PRICE_HIST_DIR and refetched only after risk_hist_max_age days. Returns
    [[date, close], ...] ascending by date, trimmed to ~risk_hist_years, or [] on any
    failure (defensive — deploy-iterates against the live source on the host)."""
    if not sym:
        return []
    safe = re.sub(r"[^A-Za-z0-9.\-]", "", sym).upper()
    path = os.path.join(PRICE_HIST_DIR, safe + ".json")
    max_age = _knob("risk_hist_max_age") * 86400
    try:
        with open(path, encoding="utf-8") as f:
            cached = json.load(f)
        if time.time() - (cached.get("fetched") or 0) < max_age and cached.get("series"):
            return cached["series"]
    except Exception:
        cached = None
    series = []
    try:
        url = "https://stooq.com/q/d/l/"
        r = requests.get(url, params={"s": safe.lower() + ".us", "i": "d"}, timeout=25)
        if r.status_code < 400 and r.text and "Date,Open" in r.text[:64]:
            for line in r.text.strip().splitlines()[1:]:
                parts = line.split(",")
                if len(parts) >= 5:
                    dt, close = parts[0], parts[4]
                    try:
                        series.append([dt, float(close)])
                    except ValueError:
                        continue
    except Exception:
        series = []
    if series:
        series.sort(key=lambda x: x[0])
        keep = _knob("risk_hist_years") * 252 + 10
        series = series[-keep:]
        try:
            os.makedirs(PRICE_HIST_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"fetched": int(time.time()), "series": series}, f)
        except Exception:
            pass
        return series
    # fetch failed — fall back to any stale cache we still have rather than nothing
    return (cached or {}).get("series", []) if cached else []


def _parse_ff_csv(text, value_names):
    """Parse a Ken French daily factor CSV (already unzipped). Rows are 'YYYYMMDD, v1, v2, ...'
    in PERCENT; the daily table ends at the first blank/non-date line. value_names maps the
    file's data columns (in order) to our factor keys. Returns {YYYYMMDD: {key: decimal}}."""
    out = {}
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if not parts or not re.fullmatch(r"\d{8}", parts[0] or ""):
            if out:                      # date block finished (footer/annual table follows)
                if len(parts[0] or "") == 6:   # monthly/annual section starts (YYYYMM) — stop
                    break
                continue
            continue
        vals = parts[1:]
        row = {}
        ok = True
        for i, key in enumerate(value_names):
            try:
                row[key] = float(vals[i]) / 100.0
            except (IndexError, ValueError):
                ok = False
                break
        if ok:
            out[parts[0]] = row
    return out


def _fetch_ken_french():
    """Download FF5 + momentum DAILY factor libraries (free Ken French zips), merge on date,
    cache to FF_FACTORS_FILE. Best-effort; returns the merged {YYYYMMDD:{...}} or {}."""
    import io
    import zipfile
    base = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    merged = {}
    try:
        r = requests.get(base + "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip", timeout=40)
        if r.status_code < 400:
            z = zipfile.ZipFile(io.BytesIO(r.content))
            txt = z.read(z.namelist()[0]).decode("latin-1")
            five = _parse_ff_csv(txt, ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"])
            merged = {d: dict(v) for d, v in five.items()}
    except Exception:
        pass
    try:
        r = requests.get(base + "F-F_Momentum_Factor_daily_CSV.zip", timeout=40)
        if r.status_code < 400:
            z = zipfile.ZipFile(io.BytesIO(r.content))
            txt = z.read(z.namelist()[0]).decode("latin-1")
            mom = _parse_ff_csv(txt, ["Mom"])
            for d, v in mom.items():
                if d in merged:
                    merged[d]["Mom"] = v["Mom"]
    except Exception:
        pass
    if merged:
        try:
            with open(FF_FACTORS_FILE, "w", encoding="utf-8") as f:
                json.dump({"fetched": int(time.time()), "factors": merged}, f)
        except Exception:
            pass
    return merged


def _read_ff():
    """Merged FF factor map, from cache when fresh else refetched. {} if never obtained."""
    try:
        with open(FF_FACTORS_FILE, encoding="utf-8") as f:
            c = json.load(f)
        if time.time() - (c.get("fetched") or 0) < FF_MAX_AGE * 86400 and c.get("factors"):
            return c["factors"]
    except Exception:
        c = None
    fresh = _fetch_ken_french()
    if fresh:
        return fresh
    return (c or {}).get("factors", {}) if c else {}


def _reconstructed_stats(port):
    """Annualized return/vol, max drawdown, Sharpe, Calmar from the reconstructed daily series
    (compounded — flow-free, since it's rebuilt from prices, unlike the live-snapshot panel)."""
    n = len(port)
    if n < 2:
        return {}
    cum, peak, maxdd = 1.0, 1.0, 0.0
    for r in port:
        cum *= (1 + r)
        peak = max(peak, cum)
        dd = cum / peak - 1
        if dd < maxdd:
            maxdd = dd
    ann_ret = cum ** (252.0 / n) - 1
    m = sum(port) / n
    sd = (sum((x - m) ** 2 for x in port) / n) ** 0.5
    ann_vol = sd * (252 ** 0.5)
    rf = float(os.environ.get("RISK_FREE_PCT", "4.0")) / 100.0
    sharpe = round((ann_ret - rf) / ann_vol, 2) if ann_vol else None
    return {"days": n, "annualized_return_pct": round(ann_ret * 100, 1),
            "annualized_vol_pct": round(ann_vol * 100, 1),
            "max_drawdown_pct": round(maxdd * 100, 1), "sharpe": sharpe,
            "calmar": risk.calmar(ann_ret * 100, maxdd * 100)}


def _risk_factor_exposure(ret_dates, port):
    """Regress the reconstructed portfolio's daily EXCESS return on FF5+momentum. ret_dates
    are 'YYYY-MM-DD' aligned to port. None if the factor library is unavailable or no overlap."""
    ff = _read_ff()
    if not ff:
        return None
    cols = [[] for _ in _FF_NAMES]
    excess = []
    for i, dt in enumerate(ret_dates):
        row = ff.get(dt.replace("-", ""))
        if not row or any(k not in row for k in _FF_NAMES) or "RF" not in row:
            continue
        excess.append(port[i] - row["RF"])
        for j, nm in enumerate(_FF_NAMES):
            cols[j].append(row[nm])
    if len(excess) < 30:
        return None
    return risk.factor_exposure(excess, cols, _FF_NAMES)


def _risk_suite():
    """Assemble the G4 tail/co-movement suite from reconstructed daily history. Status:
    'unavailable' (no holdings), 'building' (not enough resolvable history yet), or 'ready'."""
    d = _data()
    cons = [c for c in d.get("consolidated", [])
            if c.get("ticker") and (c.get("type") or "") != "Cash"]
    weights = {}
    for c in cons:
        weights[c["ticker"]] = weights.get(c["ticker"], 0.0) + (c.get("value") or 0.0)
    invested = round(sum(weights.values()), 2)
    if not weights or invested <= 0:
        return {"status": "unavailable", "generated_at": int(time.time()),
                "note": "No non-cash holdings to analyze."}
    series = {}
    for t in weights:
        s = _stooq_daily(t)
        if len(s) >= 30:
            series[t] = s
    resolved_val = round(sum(weights[t] for t in series), 2)
    coverage = round(resolved_val / invested * 100, 1) if invested else 0.0
    dates, rets = risk.align_returns(series)
    order = [t for t in series if rets.get(t)]
    min_days = _knob("risk_min_days")
    if len(dates) - 1 < min_days or not order:
        return {"status": "building", "generated_at": int(time.time()),
                "common_days": max(len(dates) - 1, 0), "days_needed": min_days,
                "coverage_pct": coverage, "resolved": sorted(order),
                "unresolved": sorted(t for t in weights if t not in series),
                "note": f"Reconstructing daily history from holdings ({len(dates)-1}/{min_days} "
                        "common trading days across resolvable tickers). Tickers without free "
                        "Stooq daily data (many funds / foreign listings) are excluded; the "
                        "suite reflects the covered share of the book."}
    port = risk.portfolio_returns(weights, rets)
    ret_dates = dates[1:]
    cmat = risk.corr_matrix(rets, order)
    cov = risk.cov_matrix(rets, order)
    return {
        "status": "ready", "generated_at": int(time.time()),
        "coverage_pct": coverage, "invested_noncash": invested,
        "resolved": sorted(order),
        "unresolved": sorted(t for t in weights if t not in series),
        "window": {"from": dates[0], "to": dates[-1], "common_days": len(dates)},
        "tail_risk": risk.var_table(port),
        "reconstructed_stats": _reconstructed_stats(port),
        "diversification": risk.effective_bets(cov, weights, order),
        "top_correlations": risk.top_correlations(cmat, order),
        "correlation_order": order,
        "correlation_matrix": [[None if v is None else round(v, 2) for v in row] for row in cmat],
        "factor_exposure": _risk_factor_exposure(ret_dates, port),
        "note": "RISK RAMP: daily portfolio history RECONSTRUCTED from your CURRENT holdings x "
                "their historical daily closes (Stooq), i.e. it assumes today's weights held "
                "throughout the window — not your actual past path. VaR/CVaR are historical "
                "simulation (empirical tail, captures skew/fat-tails); effective bets & the "
                "correlation matrix expose hidden single-factor risk; factor betas come from the "
                "Fama-French daily library. Informational, not advice.",
    }


def _read_risk_suite():
    try:
        with open(RISK_SUITE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _do_risk_suite():
    if not _risk_lock.acquire(blocking=False):
        return
    try:
        res = _risk_suite()
        os.makedirs(os.path.dirname(RISK_SUITE_FILE) or ".", exist_ok=True)
        with open(RISK_SUITE_FILE, "w", encoding="utf-8") as f:
            json.dump(res, f)
    except Exception:
        pass
    finally:
        _risk_lock.release()


@app.get("/api/risk-suite")
def get_risk_suite():
    """G4 tail/co-movement suite, CACHED (fetches daily history per holding + the factor
    library, so the first build runs in the background). Refreshes when stale."""
    cached = _read_risk_suite()
    fresh = bool(cached) and (time.time() - (cached.get("generated_at") or 0) < RISK_SUITE_TTL)
    if not fresh and not _risk_lock.locked():
        threading.Thread(target=_do_risk_suite, daemon=True).start()
    if cached:
        return {**cached, "cached": True, "refreshing": (not fresh)}
    return {"status": "computing", "note": "First risk-suite build is running (fetching daily "
            "history per holding) — refresh in a moment.", "computing": True}


@app.post("/api/run-risk-suite")
def run_risk_suite(x_analysis_token: str = Header(default="")):
    if ANALYSIS_TOKEN and x_analysis_token != ANALYSIS_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    threading.Thread(target=_do_risk_suite, daemon=True).start()
    return {"status": "started"}


# ============ G5: performance attribution — per-holding contribution + money-weighted IRR ============
# Reuses the G4 reconstructed history (per-ticker Stooq dailies, cached) for a chain-linked
# per-holding contribution-to-return ("did concentration pay?"), and the paper book's KNOWN
# dated flows for a real money-weighted IRR. Cached + background-built like the risk suite.
ATTRIB_FILE = os.environ.get("ATTRIB_FILE", "/data/attribution.json")
ATTRIB_TTL = int(os.environ.get("ATTRIB_TTL", "86400"))
_attrib_lock = threading.Lock()


def _paper_irr(p, marked):
    """Money-weighted (dollar-weighted) IRR of the hypothetical paper book from its KNOWN
    external cash flows: starting cash + seed cost at inception, each recurring/manual deposit
    at its timestamp (money IN = negative), and today's marked value (money OUT = positive).
    Buys/sells are INTERNAL and excluded. Simple return (total gain / capital) shown alongside."""
    created = p.get("created")
    flows = []
    base_in = (marked.get("starting_cash") or 0) + (marked.get("seed_basis") or 0)
    if created and base_in:
        flows.append((int(created), -float(base_in)))
    for tx in (p.get("transactions") or []):
        if tx.get("action") == "deposit" and tx.get("ts"):
            flows.append((int(tx["ts"]), -float(tx.get("value") or 0)))
    now = int(time.time())
    if marked.get("total_value") is not None:
        flows.append((now, float(marked["total_value"])))
    r = risk.xirr(flows)
    years = round((now - created) / (365.0 * 86400.0), 2) if created else None
    return {"money_weighted_irr_pct": round(r * 100, 2) if r is not None else None,
            "simple_return_pct": marked.get("return_pct"),
            "invested_capital": marked.get("invested_capital"),
            "current_value": marked.get("total_value"),
            "since": created, "years": years, "n_flows": len(flows),
            "short_window": bool(years is not None and years < 0.25),
            "note": "Money-weighted IRR annualizes the paper book's return accounting for WHEN "
                    "capital went in (starting cash + seed cost + deposits); buys/sells are "
                    "internal. Simple return = total gain / capital in (not annualized). Over a "
                    "very short window the annualized IRR is unstable."}


def _attribution():
    """Per-holding contribution-to-return (real holdings, reconstructed window) + paper-book IRR."""
    d = _data()
    cons = [c for c in d.get("consolidated", [])
            if c.get("ticker") and (c.get("type") or "") != "Cash"]
    weights = {}
    for c in cons:
        weights[c["ticker"]] = weights.get(c["ticker"], 0.0) + (c.get("value") or 0.0)
    invested = round(sum(weights.values()), 2)
    contribution = None
    if weights:
        series = {}
        for t in weights:
            s = _stooq_daily(t)
            if len(s) >= 30:
                series[t] = s
        dates, rets = risk.align_returns(series)
        order = [t for t in series if rets.get(t)]
        if len(dates) - 1 >= _knob("risk_min_days") and order:
            contribution = risk.contribution_to_return(weights, rets, order)
            resolved = round(sum(weights[t] for t in order), 2)
            contribution["coverage_pct"] = round(resolved / invested * 100, 1) if invested else 0.0
            contribution["unresolved"] = sorted(t for t in weights if t not in series)
            contribution["window"] = {"from": dates[0], "to": dates[-1], "common_days": len(dates)}
            hold = contribution["holdings"]
            by_w = sorted(hold, key=lambda h: -(h["weight_pct"] or 0))
            top_w, top_c = (by_w[0] if by_w else None), (hold[0] if hold else None)
            drag = min(hold, key=lambda h: (h["contribution_pct"] or 0)) if hold else None
            verdict = None
            if top_w and top_c:
                if top_w["ticker"] == top_c["ticker"]:
                    verdict = (f"Your largest position ({top_w['ticker']}, {top_w['weight_pct']}%) was also "
                               f"your top return driver ({top_w['share_of_return_pct']}% of the return) — "
                               "concentration PAID over this window.")
                elif (top_w["contribution_pct"] or 0) < 0:
                    verdict = (f"Your largest position ({top_w['ticker']}, {top_w['weight_pct']}%) DETRACTED "
                               f"({top_w['contribution_pct']}pt) — concentration HURT over this window.")
                else:
                    verdict = (f"Your top driver was {top_c['ticker']} ({top_c['share_of_return_pct']}% of the "
                               f"return) while your largest weight is {top_w['ticker']} ({top_w['weight_pct']}%) "
                               "— sizing and contribution diverge.")
            contribution["concentration_check"] = {
                "top_contributor": top_c["ticker"] if top_c else None,
                "top_contributor_share_pct": top_c["share_of_return_pct"] if top_c else None,
                "largest_weight": top_w["ticker"] if top_w else None,
                "largest_weight_pct": top_w["weight_pct"] if top_w else None,
                "largest_weight_share_pct": top_w["share_of_return_pct"] if top_w else None,
                "biggest_drag": (drag["ticker"] if (drag and (drag["contribution_pct"] or 0) < 0) else None),
                "biggest_drag_pct": (drag["contribution_pct"] if (drag and (drag["contribution_pct"] or 0) < 0) else None),
                "verdict": verdict}
    paper = _paper_marked(_read_paper())
    paper_perf = _paper_irr(_read_paper(), paper)
    return {"generated_at": int(time.time()), "invested_noncash": invested,
            "contribution": contribution, "paper_performance": paper_perf,
            "note": "Attribution. Per-holding contribution is chain-linked (parts sum to the total) "
                    "over the reconstructed window and assumes CURRENT weights (risk ramp), so it "
                    "shows what today's positioning would have driven, not the literal past path. "
                    "Money-weighted IRR is on the hypothetical paper book, where cash-flow dates are "
                    "known. Informational, not advice."}


def _read_attrib():
    try:
        with open(ATTRIB_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _do_attrib():
    if not _attrib_lock.acquire(blocking=False):
        return
    try:
        res = _attribution()
        os.makedirs(os.path.dirname(ATTRIB_FILE) or ".", exist_ok=True)
        with open(ATTRIB_FILE, "w", encoding="utf-8") as f:
            json.dump(res, f)
    except Exception:
        pass
    finally:
        _attrib_lock.release()


@app.get("/api/attribution")
def get_attribution():
    """G5 performance attribution, CACHED (reuses the cached per-ticker daily history; first
    build runs in the background if that cache is cold). Refreshes when stale."""
    cached = _read_attrib()
    fresh = bool(cached) and (time.time() - (cached.get("generated_at") or 0) < ATTRIB_TTL)
    if not fresh and not _attrib_lock.locked():
        threading.Thread(target=_do_attrib, daemon=True).start()
    if cached:
        return {**cached, "cached": True, "refreshing": (not fresh)}
    return {"status": "computing", "computing": True,
            "note": "First attribution build is running — refresh in a moment."}


@app.post("/api/run-attribution")
def run_attribution(x_analysis_token: str = Header(default="")):
    if ANALYSIS_TOKEN and x_analysis_token != ANALYSIS_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    threading.Thread(target=_do_attrib, daemon=True).start()
    return {"status": "started"}


# ============ G7: position sizing & concentration action program ============
# Prices the concentration CHOICES: a CVaR-based max single-name loss budget (the primary,
# measurable sizing gate), inverse-vol target weights, and a tax-aware staged-sale schedule
# that spends the annual 0%-LTCG budget first. Reuses the G4 per-ticker daily history (cached)
# for vol/CVaR and tax.py for the harvest budget / stacked LTCG tax. Read-only — never trades.
SIZING_FILE = os.environ.get("SIZING_FILE", "/data/sizing.json")
SIZING_TTL = int(os.environ.get("SIZING_TTL", "86400"))
_sizing_lock = threading.Lock()


def _ticker_stats(sym, horizon_days):
    """Annualized vol, trailing mean, and horizon-scaled 95% CVaR for one ticker from its cached
    daily history (shared with the risk suite). None if too little data."""
    s = _stooq_daily(sym)
    if len(s) < 40:
        return None
    rets = risk.pct_returns([c for _, c in s])
    if len(rets) < 30:
        return None
    m = sum(rets) / len(rets)
    sd = (sum((x - m) ** 2 for x in rets) / len(rets)) ** 0.5
    cv = risk.hist_var_cvar(rets, 95)
    cvar_frac = (cv["cvar_frac"] * (horizon_days ** 0.5)) if cv else None
    return {"vol_annual": round(sd * (252 ** 0.5), 4), "mu_annual": round(m * 252, 4),
            "cvar_frac": cvar_frac}


def _sizing():
    """Assemble the G7 sizing/action program from reconstructed per-ticker risk + embedded gains."""
    d = _data()
    nw = d.get("net_worth") or d.get("invested_value") or 0.0
    cons = [c for c in d.get("consolidated", [])
            if c.get("ticker") and (c.get("type") or "") != "Cash"]
    invested = round(sum(c.get("value") or 0 for c in cons), 2)
    horizon = _knob("sizing_horizon_days")
    positions = []
    for c in cons:
        st = _ticker_stats(c["ticker"], horizon)
        if not st:
            continue
        gain, val = c.get("gain"), (c.get("value") or 0.0)
        gf = (gain / val) if (gain is not None and val) else None
        positions.append({"ticker": c["ticker"], "value": val, "weight_pct": c.get("weight_pct"),
                          "gain": gain, "gain_fraction_pct": round(gf * 100, 1) if gf is not None else None,
                          "vol_annual": st["vol_annual"], "mu_annual": st["mu_annual"],
                          "cvar_frac": st["cvar_frac"]})
    if not positions:
        return {"status": "building", "generated_at": int(time.time()),
                "note": "Position sizing needs reconstructed daily history (shared with the risk "
                        "suite). Build the RISK suite first, or retry in a moment."}
    lb_pct = _knob("sizing_loss_budget_pct")
    lb = sizing.loss_budget_analysis(positions, nw, lb_pct, horizon)
    targets = sizing.inverse_vol_targets(positions)
    tmap = {p["ticker"]: p for p in positions}
    for r in lb["positions"]:
        p = tmap.get(r["ticker"]) or {}
        r["current_weight_pct"] = p.get("weight_pct")
        r["target_weight_pct"] = targets.get(r["ticker"])
        r["gain_fraction_pct"] = p.get("gain_fraction_pct")
        r["half_kelly_ceiling_pct"] = sizing.half_kelly_ceiling(p.get("mu_annual"), p.get("vol_annual"))
        r["exchange_fund_note"] = sizing.exchange_fund_note(p.get("weight_pct"), p.get("gain_fraction_pct"))
    # staged sale over over-budget names that carry a GAIN (losses don't add tax cost)
    over = [r for r in lb["positions"] if r["over_budget"]]
    total_trim = round(sum(r["suggested_trim"] or 0 for r in over), 2)
    total_gain = 0.0
    for r in over:
        gf = (tmap.get(r["ticker"]) or {}).get("gain_fraction_pct")
        if gf and gf > 0 and r["suggested_trim"]:
            total_gain += r["suggested_trim"] * (gf / 100.0)
    total_gain = round(total_gain, 2)
    budget0, now_tax = 0.0, None
    try:
        cfg = _read_retire()
        ctx = _tax_ctx(cfg)
        gross_ord = float(cfg.get("annual_expenses_override") or 0) or 0.0
        ded = tax.standard_deduction(ctx["status"], ctx["age"], ctx["spouse_age"])
        ord_tax = max(0.0, gross_ord - ded)
        budget0 = tax.gains_harvest_budget(gross_ord, ctx["status"], ded,
                                           ctx["age"], ctx["spouse_age"])["gains_at_0pct_budget"]
        now_tax = tax.ltcg_tax(ord_tax, total_gain, ctx["status"])[0]
    except Exception:
        budget0, now_tax = 0.0, None
    staged = (sizing.staged_diversification_plan(total_trim, total_gain, budget0,
              _knob("sizing_ltcg_rate_pct"), now_tax, max_years=_knob("sizing_stage_years"))
              if total_trim > 0 else None)
    return {"status": "ready", "generated_at": int(time.time()), "net_worth": round(nw, 2),
            "invested_noncash": invested, "resolved": sorted(tmap.keys()),
            "unresolved": sorted(c["ticker"] for c in cons if c["ticker"] not in tmap),
            "loss_budget": lb, "inverse_vol_targets": targets, "staged_sale": staged,
            "note": "Position sizing & concentration action program. The CVaR loss budget is the "
                    "PRIMARY gate (measurable); inverse-vol targets equalize standalone risk; the "
                    "staged sale spends the 0%-LTCG budget first. Kelly is a caveated reference "
                    "only (single-name edge is unmeasurable). Read-only — never places a trade; "
                    "confirm any sale + its tax with a CPA."}


def _read_sizing():
    try:
        with open(SIZING_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _do_sizing():
    if not _sizing_lock.acquire(blocking=False):
        return
    try:
        res = _sizing()
        os.makedirs(os.path.dirname(SIZING_FILE) or ".", exist_ok=True)
        with open(SIZING_FILE, "w", encoding="utf-8") as f:
            json.dump(res, f)
    except Exception:
        pass
    finally:
        _sizing_lock.release()


@app.get("/api/sizing")
def get_sizing():
    """G7 position sizing / concentration action program, CACHED (reuses the cached per-ticker
    daily history; first build runs in the background if that cache is cold)."""
    cached = _read_sizing()
    fresh = bool(cached) and (time.time() - (cached.get("generated_at") or 0) < SIZING_TTL)
    if not fresh and not _sizing_lock.locked():
        threading.Thread(target=_do_sizing, daemon=True).start()
    if cached:
        return {**cached, "cached": True, "refreshing": (not fresh)}
    return {"status": "computing", "computing": True,
            "note": "First sizing build is running — refresh in a moment."}


@app.post("/api/run-sizing")
def run_sizing(x_analysis_token: str = Header(default="")):
    if ANALYSIS_TOKEN and x_analysis_token != ANALYSIS_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    threading.Thread(target=_do_sizing, daemon=True).start()
    return {"status": "started"}


# ============ G8: unified marginal-rate curve + multi-year Roth conversion ladder ============
# Composes EVERY marginal cost of the next dollar of ordinary income into one curve: federal
# ordinary tax + the LTCG "bump zone" + NIIT (all from tax.effective_rate), the ACA premium-
# subsidy phase-out + 400%-FPL cliff (aca.py, pre-65), and IRMAA Medicare steps (roth.py,
# age 63+). Then a multi-year Roth ladder fills conversions up to a target ceiling over the
# pre-RMD valley. Pure CPU (no network) — computed on request. SS deprioritized (torpedo omitted).


def _ptc_dollars(magi_value, actx):
    """Annual PTC (subsidy) $ at a MAGI, or None if no SLCSP configured (can't dollarize)."""
    if not actx.get("slcsp_annual"):
        return None
    est = aca.ptc_estimate(magi_value, actx["household_size"], actx["slcsp_annual"])
    if not est.get("eligible"):
        return 0.0                      # coverage gap or over the cliff → no subsidy
    return est.get("estimated_annual_ptc", 0.0)


def _marginal_curve():
    """Sweep the next dollar of ordinary income (a Roth conversion) and compose the total
    marginal cost at each point. Returns the point series (+ per-point tax/aca/irmaa split),
    the recommended fill-to, the ACA cliff, and IRMAA step markers."""
    cfg = _read_retire()
    ctx = _tax_ctx(cfg)
    actx = _aca_ctx(cfg)
    status = ctx["status"]
    age = cfg.get("current_age")
    base_ord = float(cfg.get("roth_base_ordinary") or 0)
    ltcg = float(cfg.get("roth_base_ltcg") or 0)
    magi_base = base_ord + ltcg
    step, mx = _knob("roth_sweep_step"), _knob("roth_sweep_max")
    irmaa_on = (age is None) or (age >= _knob("roth_irmaa_from_age"))
    people_med = 2 if status == "mfj" else 1
    aca_pre65 = (age is None) or (age < 65)
    er0 = tax.effective_rate(base_ord, ltcg, status, age=ctx["age"], spouse_age=ctx["spouse_age"],
                             include_niit=True, magi=magi_base)
    tax0 = er0["total_tax"]
    ptc0 = _ptc_dollars(magi_base, actx) if aca_pre65 else None
    irmaa0 = roth.irmaa_annual_part_b(magi_base, status, people_med) if irmaa_on else 0.0
    deltas, costs, comps = [], [], []
    d = 0
    while d <= mx:
        er = tax.effective_rate(base_ord + d, ltcg, status, age=ctx["age"],
                                spouse_age=ctx["spouse_age"], include_niit=True, magi=magi_base + d)
        tax_cost = er["total_tax"] - tax0
        aca_cost = 0.0
        if aca_pre65 and ptc0 is not None:
            ptc = _ptc_dollars(magi_base + d, actx)
            aca_cost = max(0.0, (ptc0 or 0) - (ptc or 0))
        irmaa_cost = (roth.irmaa_annual_part_b(magi_base + d, status, people_med) - irmaa0) if irmaa_on else 0.0
        deltas.append(d)
        costs.append(tax_cost + aca_cost + irmaa_cost)
        comps.append({"tax": round(tax_cost, 0), "aca": round(aca_cost, 0), "irmaa": round(irmaa_cost, 0)})
        d += step
    points = roth.marginal_and_avg(deltas, costs)
    for i, p in enumerate(points):
        p["components"] = comps[i]
    cliff_delta, cliff_info = None, None
    if aca_pre65 and actx.get("household_size"):
        cl = aca.cliff(magi_base, actx["household_size"])
        hr = cl.get("headroom_to_cliff")
        if hr is not None and hr > 0:
            cliff_delta, cliff_info = hr, cl
    ceiling = _knob("roth_target_ceiling_pct")
    fill = roth.recommend_fill(points, ceiling, cliff_delta)
    irmaa_markers = ([round(e - magi_base, 0) for e in roth.irmaa_tier_edges(status)
                      if magi_base < e <= magi_base + mx] if irmaa_on else [])
    return {"points": points, "recommended_fill": fill, "ceiling_pct": ceiling,
            "aca_cliff_delta": cliff_delta, "aca_cliff": cliff_info,
            "irmaa_markers": irmaa_markers, "irmaa_modeled": irmaa_on,
            "assumptions": {"base_ordinary": base_ord, "ltcg": ltcg, "current_age": age,
                            "filing_status": status, "aca_pre65": aca_pre65,
                            "slcsp_annual": actx.get("slcsp_annual"),
                            "household_size": actx.get("household_size"),
                            "people_on_medicare": people_med, "magi_base": magi_base},
            "note": "Effective MARGINAL rate on the next dollar of ordinary income (a Roth "
                    "conversion), composing federal ordinary tax + the LTCG bump zone + NIIT + the "
                    "ACA subsidy phase-out/cliff (pre-65) + IRMAA steps (63+). Texas adds no state "
                    "tax, so this IS the whole marginal cost. Educational — size with a CPA."}


def _roth_plan():
    """Marginal curve + a multi-year conversion ladder over the pre-RMD valley."""
    curve = _marginal_curve()
    cfg = _read_retire()
    age = cfg.get("current_age")
    rmd = _knob("roth_rmd_age")
    n_years = min(40, max(0, rmd - age)) if age else 10
    trad = _balances_by_tax_status().get("deferred", 0.0)
    fill = curve["recommended_fill"]["fill_to"]
    ld = roth.ladder(fill, n_years, age, datetime.date.today().year, trad,
                     growth_pct=_knob("roth_conversion_growth_pct"),
                     aca_cliff_fill=curve.get("aca_cliff_delta"), aca_until_age=65)
    return {"generated_at": int(time.time()), "curve": curve, "ladder": ld,
            "trad_balance": round(trad, 2), "rmd_age": rmd, "annual_fill": fill,
            "note": "The ladder converts up to the recommended fill each year (or the tighter "
                    "ACA-cliff cap while under 65) until RMD age, moving pre-tax dollars to Roth "
                    "in the low-rate years. Each conversion has its own 5-year clock. Read-only "
                    "decision-support — not tax advice; confirm conversions with a CPA."}


@app.get("/api/roth")
def get_roth():
    """G8 unified marginal-rate curve + multi-year Roth conversion ladder (pure CPU, live)."""
    try:
        return _roth_plan()
    except Exception as e:
        return JSONResponse({"error": f"roth/marginal-curve engine failed: {e}"}, status_code=500)


# ============ G6: asset-backed borrowing — RUIN-MODEL-FIRST ============
# Leads with the risk: a bootstrap margin-call ("ruin") probability vs LTV drawn from the book's
# OWN fat-tailed reconstructed returns (G4) -> a conservative max prudent LTV; then an after-tax
# borrow-vs-sell comparison and the buy-borrow-die risks. Only TAXABLE/brokerage assets are
# pledgeable. Cached + background-built (the bootstrap is a couple seconds). MODELS borrowing —
# never borrows, never places an order.
LEVERAGE_FILE = os.environ.get("LEVERAGE_FILE", "/data/leverage.json")
LEVERAGE_TTL = int(os.environ.get("LEVERAGE_TTL", "86400"))
_leverage_lock = threading.Lock()


def _leverage():
    """Ruin curve + borrow-vs-sell on the pledgeable (taxable) book, using reconstructed returns."""
    d = _data()
    bal = _balances_by_tax_status()
    collateral = bal.get("taxable", 0.0)      # only taxable/brokerage assets can be pledged
    cons = [c for c in d.get("consolidated", [])
            if c.get("ticker") and (c.get("type") or "") != "Cash"]
    weights, gain_num, gain_den = {}, 0.0, 0.0
    for c in cons:
        v = c.get("value") or 0.0
        weights[c["ticker"]] = weights.get(c["ticker"], 0.0) + v
        g = c.get("gain")
        if g is not None and v:
            gain_num += g
            gain_den += v
    gain_fraction = (gain_num / gain_den) if gain_den else 0.0
    series = {}
    for t in weights:
        s = _stooq_daily(t)
        if len(s) >= 30:
            series[t] = s
    dates, rets = risk.align_returns(series)
    order = [t for t in series if rets.get(t)]
    port = (risk.portfolio_returns(weights, rets)
            if (len(dates) - 1 >= _knob("risk_min_days") and order) else [])
    if not port:
        return {"status": "building", "generated_at": int(time.time()),
                "note": "Leverage ruin model needs reconstructed daily history (shared with the "
                        "risk suite). Build the RISK suite first, or retry shortly."}
    maint = _knob("leverage_maintenance_pct") / 100.0
    horizon_days = int(_knob("leverage_horizon_years") * 252)
    grid = [i / 100.0 for i in range(5, 61, 5)]
    ruin = leverage.ruin_curve(port, grid, maint, horizon_days,
                               n_paths=_knob("leverage_paths"),
                               tolerance_pct=_knob("leverage_ruin_tolerance_pct"))
    prudent_ltv = (ruin or {}).get("max_prudent_ltv_pct", 0.0) / 100.0
    draw = round(collateral * prudent_ltv, 2)
    bvs = leverage.borrow_vs_sell(draw, gain_fraction, _knob("leverage_ltcg_rate_pct"),
                                  _knob("leverage_loan_rate_pct"),
                                  _knob("leverage_expected_return_pct"),
                                  _knob("leverage_horizon_years"))
    return {"status": "ready", "generated_at": int(time.time()),
            "collateral_value": round(collateral, 2),
            "gain_fraction_pct": round(gain_fraction * 100, 1),
            "loan_rate_pct": _knob("leverage_loan_rate_pct"),
            "expected_return_pct": _knob("leverage_expected_return_pct"),
            "ruin": ruin, "prudent_draw": draw, "borrow_vs_sell": bvs,
            "risks": leverage.BUY_BORROW_DIE_RISKS,
            "note": "Only taxable/brokerage assets are pledgeable (retirement accounts can't be). "
                    "The ruin curve is the binding constraint — the max prudent LTV keeps the "
                    "margin-call probability under tolerance using your own fat-tailed returns. "
                    "Loan rate is variable (SOFR-based). MODELS borrowing only — never borrows. "
                    "Not advice; a margin call can force a sale at the worst time."}


def _read_leverage():
    try:
        with open(LEVERAGE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _do_leverage():
    if not _leverage_lock.acquire(blocking=False):
        return
    try:
        res = _leverage()
        os.makedirs(os.path.dirname(LEVERAGE_FILE) or ".", exist_ok=True)
        with open(LEVERAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(res, f)
    except Exception:
        pass
    finally:
        _leverage_lock.release()


@app.get("/api/leverage")
def get_leverage():
    """G6 asset-backed borrowing ruin model + borrow-vs-sell, CACHED (bootstrap runs in the
    background; reuses the cached per-ticker daily history)."""
    cached = _read_leverage()
    fresh = bool(cached) and (time.time() - (cached.get("generated_at") or 0) < LEVERAGE_TTL)
    if not fresh and not _leverage_lock.locked():
        threading.Thread(target=_do_leverage, daemon=True).start()
    if cached:
        return {**cached, "cached": True, "refreshing": (not fresh)}
    return {"status": "computing", "computing": True,
            "note": "First leverage build is running (margin-call bootstrap) — refresh shortly."}


@app.post("/api/run-leverage")
def run_leverage(x_analysis_token: str = Header(default="")):
    if ANALYSIS_TOKEN and x_analysis_token != ANALYSIS_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    threading.Thread(target=_do_leverage, daemon=True).start()
    return {"status": "started"}


# ============ G12: cross-avenue after-tax comparison sheet ============
# Where should the marginal dollar go? Reduces every avenue to a comparable AFTER-TAX return with
# a risk/liquidity tag. Pulls the live marginal rate + 0%-harvest budget from tax.py, the Roth
# fill from the G8 curve, and the loan rate from G6; everything else is a SETTINGS assumption.
# Pure CPU, live. Read-only decision-support.


def _avenues():
    cfg = _read_retire()
    ctx = _tax_ctx(cfg)
    status = ctx["status"]
    gross_ord = 0.0
    try:
        gross_ord = float(cfg.get("annual_expenses_override") or cfg.get("roth_base_ordinary") or 0) or 0.0
    except Exception:
        gross_ord = 0.0
    ded = tax.standard_deduction(status, ctx["age"], ctx["spouse_age"])
    taxable_ord = max(0.0, gross_ord - ded)
    marginal_rate = tax.marginal_ordinary_rate(taxable_ord, status) * 100.0
    try:
        harvest_budget = tax.gains_harvest_budget(gross_ord, status, ded,
                                                  ctx["age"], ctx["spouse_age"])["gains_at_0pct_budget"]
    except Exception:
        harvest_budget = 0.0
    # Roth fill + the marginal rate at that fill (the "convert now at" rate), from the G8 curve.
    roth_fill, roth_now = 0.0, marginal_rate
    try:
        cur = _marginal_curve()
        roth_fill = (cur.get("recommended_fill") or {}).get("fill_to") or 0.0
        at = (cur.get("recommended_fill") or {}).get("at_point") or {}
        if at.get("avg_rate_pct") is not None:
            roth_now = at["avg_rate_pct"]
    except Exception:
        pass
    inp = {
        "marginal_ordinary_rate_pct": marginal_rate,
        "ltcg_rate_pct": _knob("sizing_ltcg_rate_pct"),
        "debt_apr_pct": _knob("avenue_debt_apr_pct"),
        "debt_deductible": _knob("avenue_debt_deductible") > 0,
        "treasury_yield_pct": _knob("avenue_treasury_yield_pct"),
        "market_return_pct": _knob("avenue_market_return_pct"),
        "div_yield_pct": _knob("avenue_div_yield_pct"),
        "concentrated_return_pct": _knob("avenue_concentrated_return_pct"),
        "re_cap_rate_pct": _knob("avenue_re_cap_rate_pct"),
        "re_appreciation_pct": _knob("avenue_re_appreciation_pct"),
        "re_depr_shield_frac": _knob("avenue_re_depr_shield_frac"),
        "loan_rate_pct": _knob("leverage_loan_rate_pct"),
        "harvest_budget": harvest_budget,
        "roth_fill": roth_fill,
        "roth_now_rate_pct": roth_now,
        "roth_future_rate_pct": _knob("avenue_roth_future_rate_pct"),
        "match_rate_pct": _knob("avenue_match_rate_pct"),
        "match_cap_dollars": _knob("avenue_match_cap_dollars"),
    }
    sheet = avenues.build_sheet(inp)
    sheet["generated_at"] = int(time.time())
    sheet["assumptions"] = {"marginal_ordinary_rate_pct": round(marginal_rate, 1),
                            "ltcg_rate_pct": _knob("sizing_ltcg_rate_pct"),
                            "harvest_budget": round(harvest_budget, 0),
                            "roth_fill": round(roth_fill, 0), "roth_now_rate_pct": round(roth_now, 1),
                            "baseline_ordinary_income": round(gross_ord, 0), "filing_status": status}
    return sheet


@app.get("/api/avenues")
def get_avenues():
    """G12 cross-avenue after-tax comparison sheet (pure CPU, live)."""
    try:
        return _avenues()
    except Exception as e:
        return JSONResponse({"error": f"avenues engine failed: {e}"}, status_code=500)


# ============ G14: thesis & exit-trigger registry — exit-discipline-as-code ============
# Per holding: WHY you own it + the fundamentals AT ENTRY + pre-registered falsifiable exit
# triggers (FCF-margin decay, reverse-DCF implied-growth ceiling, Piotroski floor, drawdown,
# custom). Each run checks current fundamentals against them; a tripped trigger is a pre-committed
# review/exit signal that forces a brief citing the broken condition. Read-only; MODELS the sell.
THESES_FILE = os.environ.get("THESES_FILE", "/data/theses.json")
THESES_EVAL_FILE = os.environ.get("THESES_EVAL_FILE", "/data/theses_eval.json")
THESES_TTL = int(os.environ.get("THESES_TTL", "43200"))       # cache the evaluation ~12h
_theses_lock = threading.Lock()


def _read_theses():
    try:
        with open(THESES_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _write_theses(reg):
    os.makedirs(os.path.dirname(THESES_FILE) or ".", exist_ok=True)
    with open(THESES_FILE, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)


def _thesis_metrics(sym):
    """Current fundamentals for trigger evaluation — same edgar/valuation/reverse-DCF stack the
    scanner uses. Returns the metric keys thesis.py triggers reference."""
    fund = edgar.fundamentals(sym) or {}
    val = _valuation(sym) or {}
    m = (fund.get("metrics") or {}) if fund.get("available") else {}
    r = (fund.get("ratios") or {}) if fund.get("available") else {}
    rd = _reverse_dcf(val, fund) or {}
    pio = ((fund.get("scores") or {}).get("piotroski_f") or {}) if fund.get("available") else {}
    return {"price": _price(sym), "fcf_margin_pct": r.get("fcf_margin_pct"),
            "rev_growth_pct": m.get("revenue_growth_pct"), "net_margin_pct": r.get("net_margin_pct"),
            "piotroski": pio.get("score"), "pe_forward": val.get("pe_forward") or val.get("pe_ttm"),
            "implied_growth_pct": rd.get("implied_growth_pct"),
            "accruals_ratio_pct": _accruals_ratio(fund),
            "valuation_percentile": (_valuation_percentile(sym) or {}).get("cheapness_percentile"),
            "insider_net_buy_pct": (_insider_signal(sym) or {}).get("net_buy_pct"),
            "as_of": int(time.time())}


def _accruals_ratio(fund):
    """Balance-sheet accruals ratio = (net income − operating cash flow) / total assets, in %.
    A high positive value (earnings running well ahead of cash) is a classic earnings-QUALITY red
    flag (Sloan 1996) — the robust, low-data-requirement subset of a Beneish-style screen. A FLAG
    among many (noisy for high-growth names), never a veto. None if the inputs aren't available."""
    if not fund or not fund.get("available"):
        return None
    ttm = fund.get("ttm") or {}
    ni = ttm.get("net_income") if ttm.get("net_income") is not None else fund.get("net_income")
    ocf = ttm.get("operating_cash_flow") if ttm.get("operating_cash_flow") is not None else fund.get("operating_cash_flow")
    ta = fund.get("total_assets")
    if ni is None or ocf is None or not ta:
        return None
    return round((ni - ocf) / ta * 100.0, 1)


def _evaluate_theses():
    reg = _read_theses()
    out = []
    for tk, spec in reg.items():
        try:
            mtr = _thesis_metrics(tk)
        except Exception:
            mtr = {}
        ev = thesis.evaluate_thesis(spec, mtr)
        ev["metrics"] = mtr
        out.append(ev)
    out.sort(key=lambda e: (-e["tripped_count"], e["ticker"] or ""))
    return {"generated_at": int(time.time()), "evaluations": out,
            "tripped_total": sum(1 for e in out if e["tripped_count"] > 0),
            "registered": len(reg)}


def _read_theses_eval():
    try:
        with open(THESES_EVAL_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _do_theses_eval():
    if not _theses_lock.acquire(blocking=False):
        return
    try:
        res = _evaluate_theses()
        os.makedirs(os.path.dirname(THESES_EVAL_FILE) or ".", exist_ok=True)
        with open(THESES_EVAL_FILE, "w", encoding="utf-8") as f:
            json.dump(res, f)
    except Exception:
        pass
    finally:
        _theses_lock.release()


@app.get("/api/theses")
def get_theses():
    """The thesis registry + the cached exit-trigger evaluation + holdings that still lack a
    thesis (to prompt seeding). Evaluation runs in the background (an edgar fetch per name)."""
    reg = _read_theses()
    cached = _read_theses_eval()
    fresh = bool(cached) and (time.time() - (cached.get("generated_at") or 0) < THESES_TTL)
    if reg and not fresh and not _theses_lock.locked():
        threading.Thread(target=_do_theses_eval, daemon=True).start()
    held = []
    try:
        held = [c.get("ticker") for c in _data().get("consolidated", [])
                if c.get("ticker") and (c.get("type") or "") not in ("Cash", "Mutual Fund", "ETF")]
    except Exception:
        pass
    unregistered = sorted(t for t in held if t not in reg)
    return {"registry": reg, "evaluation": (cached or {"evaluations": [], "note": "run pending"}),
            "unregistered_holdings": unregistered, "refreshing": bool(reg and not fresh),
            "trigger_types": list(thesis.METRIC_LABELS.keys()),
            "note": "Pre-registered, falsifiable exit triggers per holding. A tripped trigger is a "
                    "pre-committed review/exit signal — exit discipline decided in advance, not in a "
                    "drawdown. Educational, not advice."}


@app.post("/api/theses")
async def post_theses(req: Request):
    """Upsert a holding's thesis. Body: {ticker, thesis, triggers?[...], entry?{...}}. If entry is
    omitted, a snapshot of current fundamentals is taken as the entry baseline; if triggers are
    omitted, a sensible default set is used."""
    body = await req.json()
    tk = str(body.get("ticker") or "").upper().strip()
    if not tk:
        return JSONResponse({"error": "ticker required"}, status_code=400)
    reg = _read_theses()
    cur = dict(reg.get(tk) or {})
    cur["ticker"] = tk
    if "thesis" in body:
        cur["thesis"] = str(body.get("thesis") or "")
    if isinstance(body.get("triggers"), list):
        cur["triggers"] = body["triggers"]
    elif not cur.get("triggers"):
        cur["triggers"] = thesis.default_triggers()
    if isinstance(body.get("entry"), dict):
        cur["entry"] = body["entry"]
    elif not cur.get("entry"):
        snap = {}
        try:
            snap = _thesis_metrics(tk)
        except Exception:
            snap = {}
        snap["date"] = datetime.date.today().isoformat()
        cur["entry"] = snap
    now = int(time.time())
    cur.setdefault("created", now)
    cur["updated"] = now
    reg[tk] = cur
    _write_theses(reg)
    threading.Thread(target=_do_theses_eval, daemon=True).start()
    return {"ok": True, "ticker": tk, "thesis": cur}


@app.post("/api/theses/delete")
async def delete_theses(req: Request):
    body = await req.json()
    tk = str(body.get("ticker") or "").upper().strip()
    reg = _read_theses()
    if tk in reg:
        reg.pop(tk, None)
        _write_theses(reg)
        threading.Thread(target=_do_theses_eval, daemon=True).start()
        return {"ok": True, "deleted": tk}
    return JSONResponse({"error": "not found"}, status_code=404)


@app.post("/api/run-theses")
def run_theses(x_analysis_token: str = Header(default="")):
    if ANALYSIS_TOKEN and x_analysis_token != ANALYSIS_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    threading.Thread(target=_do_theses_eval, daemon=True).start()
    return {"status": "started"}


# ============ G9: rental real-estate deal underwriter ============
# Turns the AVENUES real-estate assumption into an actual deal: NOI/cap/DSCR/CoC + after-tax
# levered IRR over the hold (with depreciation recapture + LTCG at sale). Also surfaces the
# owner's EXISTING real-estate accounts (normally exclude_account_types'd out of analysis).
# The app supplies deal inputs; county comps stay manual. Pure CPU, read-only.
REALESTATE_FILE = os.environ.get("REALESTATE_FILE", "/data/realestate.json")


def _re_defaults():
    return {"price": 400000, "down_pct": 25, "closing_pct": 3,
            "loan_rate_pct": _knob("avenue_debt_apr_pct"), "term_years": 30,
            "gross_annual_rent": 32000, "vacancy_pct": 5, "opex_pct": 35,
            "annual_appreciation_pct": _knob("avenue_re_appreciation_pct"), "rent_growth_pct": 2,
            "hold_years": 10, "sale_cost_pct": 7, "land_frac": 0.2, "depr_years": 27.5,
            "bonus_depr_pct": 0, "marginal_tax_pct": 24, "ltcg_pct": _knob("sizing_ltcg_rate_pct")}


def _realestate_holdings():
    """Existing real-estate account values from the portfolio feed (they're excluded from the
    securities analysis, so surface them here). Defensive across balance field names."""
    out, total = [], 0.0
    try:
        for a in (_data().get("accounts") or []):
            typ = str(a.get("type") or "")
            if "real estate" in typ.lower() or typ.lower() in ("real_estate", "realestate"):
                bal = a.get("balance")
                if bal is None:
                    bal = a.get("displayBalance") if a.get("displayBalance") is not None else a.get("value")
                bal = float(bal or 0)
                total += bal
                out.append({"name": a.get("name"), "type": typ, "value": round(bal, 2)})
    except Exception:
        pass
    return out, round(total, 2)


@app.get("/api/realestate")
def get_realestate():
    """Last underwrite (if any) + existing real-estate holdings + grounded default inputs."""
    last = None
    try:
        with open(REALESTATE_FILE, encoding="utf-8") as f:
            last = json.load(f)
    except Exception:
        last = None
    holdings, total = _realestate_holdings()
    return {"last": last, "existing_holdings": holdings, "existing_total": total,
            "defaults": _re_defaults(),
            "note": "Underwrite a rental deal (income → financing → tax → after-tax IRR). Existing "
                    "real-estate accounts are surfaced here since they're excluded from the "
                    "securities analysis. Comps/rents are your inputs; confirm tax treatment with a CPA."}


@app.post("/api/realestate/underwrite")
async def post_realestate(req: Request):
    """Underwrite a deal. Body = the input keys (see realestate.underwrite); missing keys default.
    Persists the last run. Not a money action."""
    body = await req.json()
    inp = {**_re_defaults(), **{k: v for k, v in (body or {}).items() if v is not None}}
    try:
        result = realestate.underwrite(inp)
    except Exception as e:
        return JSONResponse({"error": f"underwrite failed: {e}"}, status_code=400)
    rec = {"inputs": inp, "result": result, "generated_at": int(time.time())}
    try:
        os.makedirs(os.path.dirname(REALESTATE_FILE) or ".", exist_ok=True)
        with open(REALESTATE_FILE, "w", encoding="utf-8") as f:
            json.dump(rec, f)
    except Exception:
        pass
    return rec


# ============ G11: small-business acquisition underwriter ============
# The business-ownership leg of the all-avenue directive — underwrites buying a cash-flowing
# small business (SDE multiple / DSCR / cash-on-cash / payback / after-tax levered IRR + QSBS).
# Pure CPU, read-only. Rates as knobs (SBA 7(a) is variable on prime).
BUSINESS_FILE = os.environ.get("BUSINESS_FILE", "/data/business.json")


def _biz_defaults():
    return {"price": 1000000, "sde": 300000, "revenue": 1500000, "down_pct": 15,
            "seller_note_pct": 0, "loan_rate_pct": _knob("biz_loan_rate_pct"), "loan_term_years": 10,
            "working_capital": 50000, "owner_salary": 0, "sde_growth_pct": 3, "hold_years": 5,
            "exit_multiple": 0, "marginal_tax_pct": 24, "ltcg_pct": _knob("sizing_ltcg_rate_pct"), "qsbs": 0}


@app.get("/api/business")
def get_business():
    last = None
    try:
        with open(BUSINESS_FILE, encoding="utf-8") as f:
            last = json.load(f)
    except Exception:
        last = None
    return {"last": last, "defaults": _biz_defaults(),
            "note": "Underwrite a small-business acquisition (SDE → SBA/seller debt → after-tax IRR + "
                    "QSBS). SDE add-backs need a Quality-of-Earnings review; SBA 7(a) rates are variable "
                    "(prime-based). Educational, not advice — confirm structure/tax with counsel + CPA."}


@app.post("/api/business/underwrite")
async def post_business(req: Request):
    """Underwrite a business acquisition. Body = bizacq.underwrite input keys; missing keys default.
    Persists the last run. Not a money action."""
    body = await req.json()
    inp = {**_biz_defaults(), **{k: v for k, v in (body or {}).items() if v is not None}}
    try:
        result = bizacq.underwrite(inp)
    except Exception as e:
        return JSONResponse({"error": f"underwrite failed: {e}"}, status_code=400)
    rec = {"inputs": inp, "result": result, "generated_at": int(time.time())}
    try:
        os.makedirs(os.path.dirname(BUSINESS_FILE) or ".", exist_ok=True)
        with open(BUSINESS_FILE, "w", encoding="utf-8") as f:
            json.dump(rec, f)
    except Exception:
        pass
    return rec


# ============ Attention digest — materiality-filtered "what needs attention" ============
# The read-side of the alerting layer: aggregates signals the other modules ALREADY compute
# (exit-trigger trips, sizing loss-budget breaches, concentration/stress flags, grounding
# staleness) into one prioritized list. Reads cached outputs — cheap, read-only, no new data.


def _alerts_digest():
    alerts = []

    def add(level, cat, msg, where):
        alerts.append({"level": level, "category": cat, "message": msg, "where": where})

    # 1) Exit discipline — tripped thesis triggers (G14)
    for e in ((_read_theses_eval() or {}).get("evaluations") or []):
        if e.get("tripped_count", 0) > 0:
            add("action", "Exit discipline",
                f"{e.get('ticker')}: {e.get('action')} — {', '.join(e.get('tripped_labels') or [])}",
                "RESEARCH → THESES")
    # 2) Position sizing — single-name loss-budget breaches (G7)
    for r in (((_read_sizing() or {}).get("loss_budget") or {}).get("positions") or []):
        if r.get("over_budget"):
            add("warn", "Position sizing",
                f"{r.get('ticker')} exceeds the single-name CVaR loss budget "
                f"(at-risk {r.get('at_risk_pct_of_nw')}% of net worth; suggested trim "
                f"${(r.get('suggested_trim') or 0):,.0f})", "INSIGHTS → SIZING")
    # 3) Concentration + macro stress (G4 / exposure)
    ex = _read_exp() or {}
    for f in ((ex.get("concentration") or {}).get("flags") or []):
        add("warn", "Concentration", f, "INSIGHTS → EXPOSURE")
    ms = ex.get("macro_stress") or {}
    scen = ms.get("scenarios") or []
    if scen:
        worst = min(scen, key=lambda s: (s.get("pct_impact") or 0))
        if (worst.get("pct_impact") or 0) <= -20:
            add("info", "Stress", f"Worst modeled macro scenario '{worst.get('name')}': "
                f"{worst.get('pct_impact')}% ({usd_neg(worst.get('dollar_impact'))})",
                "INSIGHTS → EXPOSURE")
    # 4) Data grounding staleness (G13)
    try:
        for a in grounding.alerts():
            if a.get("level") in ("stale", "action"):
                add("action", "Data grounding", a.get("msg"), "AUDITS → GROUNDING")
    except Exception:
        pass

    order = {"action": 0, "warn": 1, "info": 2}
    alerts.sort(key=lambda a: order.get(a["level"], 9))
    return {"generated_at": int(time.time()), "alerts": alerts,
            "counts": {lvl: sum(1 for a in alerts if a["level"] == lvl) for lvl in ("action", "warn", "info")},
            "note": "Materiality-filtered digest of what needs attention, aggregated from the exit-trigger "
                    "registry, the sizing loss budget, concentration/stress, and the grounding watchdog. "
                    "Reads cached results (build the RISK/SIZING/THESES panels to populate). Read-only, not advice."}


def usd_neg(n):
    try:
        return ("-$" + format(abs(round(n)), ",")) if n is not None else "—"
    except Exception:
        return "—"


@app.get("/api/alerts")
def get_alerts():
    """Attention digest — prioritized action/warn/info items aggregated across the modules."""
    return _alerts_digest()


# ---------------- AI scorekeeping: track how the model's Buy/Hold/Sell calls actually perform ----------------
RATING_LOG_FILE = os.environ.get("RATING_LOG_FILE", "/data/rating_log.json")


def _log_rating(ticker, llm):
    """Append a dated snapshot of the model's rating + confidence + price so we can later score its
    calls. One entry per ticker per day (same-day re-runs overwrite). Best-effort, never raises."""
    rating = (llm or {}).get("rating")
    if not ticker or rating not in ("Buy", "Hold", "Sell"):
        return
    try:
        px = _price(ticker)
        if not px:
            return
        today = datetime.date.today().isoformat()
        try:
            with open(RATING_LOG_FILE, encoding="utf-8") as f:
                log = json.load(f) or []
        except Exception:
            log = []
        log = [e for e in log if not (e.get("ticker") == ticker and e.get("date") == today)]
        log.append({"date": today, "ts": int(time.time()), "ticker": ticker, "rating": rating,
                    "confidence": (llm or {}).get("confidence"), "price": round(px, 4)})
        log = log[-5000:]
        os.makedirs(os.path.dirname(RATING_LOG_FILE) or ".", exist_ok=True)
        with open(RATING_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(log, f)
    except Exception:
        pass


def _scorecard(min_age_days=None):
    """Score the model's historical calls: forward return from each dated rating's price to the
    current price, aggregated by rating and by confidence. Read-only, educational."""
    min_age = int(min_age_days or os.environ.get("SCORECARD_MIN_AGE_DAYS", "14"))
    try:
        with open(RATING_LOG_FILE, encoding="utf-8") as f:
            log = json.load(f) or []
    except Exception:
        log = []
    if not log:
        return {"status": "empty", "note": "No rating history yet — the scorecard accrues as the daily "
                "analysis runs and enough time passes to measure forward returns."}
    today = datetime.date.today()
    px_now = {}
    for t in sorted({e.get("ticker") for e in log if e.get("ticker")}):
        try:
            px_now[t] = _price(t)
        except Exception:
            px_now[t] = None
    scored = []
    for e in log:
        p0, pn = e.get("price"), px_now.get(e.get("ticker"))
        try:
            age = (today - datetime.date.fromisoformat(e.get("date"))).days
        except Exception:
            age = None
        if not p0 or not pn or age is None or age < min_age:
            continue
        scored.append({**e, "age_days": age, "fwd_return_pct": round((pn / p0 - 1) * 100, 2)})

    def agg(items):
        if not items:
            return None
        rets = [x["fwd_return_pct"] for x in items]
        return {"n": len(items), "avg_fwd_return_pct": round(sum(rets) / len(rets), 2)}

    by_rating = {r: agg([x for x in scored if x["rating"] == r]) for r in ("Buy", "Hold", "Sell")}
    by_conf = {c: agg([x for x in scored if x.get("confidence") == c]) for c in ("high", "med", "low")}
    directional = [x for x in scored if x["rating"] in ("Buy", "Sell")]
    hits = sum(1 for x in directional
               if (x["rating"] == "Buy" and x["fwd_return_pct"] > 0)
               or (x["rating"] == "Sell" and x["fwd_return_pct"] < 0))
    edge = None
    if by_rating["Buy"] and by_rating["Hold"]:
        edge = round(by_rating["Buy"]["avg_fwd_return_pct"] - by_rating["Hold"]["avg_fwd_return_pct"], 2)
    # ---- G13c: calibration — does stated confidence carry real information? ----
    # Map each confidence tier to an implied P(directional call is right) and score it
    # against the realized hit (Buy→rose, Sell→fell). Brier = mean (p − outcome)²:
    # 0.25 is a coin flip; lower is better. The per-tier gap (actual − implied) is the
    # honest read: a 'high' tier that hits like the 'low' tier is decoration, not signal.
    CONF_P = {"high": 0.75, "med": 0.60, "low": 0.50}
    calibration = None
    cal_calls = [x for x in directional if x.get("confidence") in CONF_P]
    if cal_calls:
        briers = []
        tiers = {}
        for x in cal_calls:
            p = CONF_P[x["confidence"]]
            hit = 1.0 if ((x["rating"] == "Buy" and x["fwd_return_pct"] > 0)
                          or (x["rating"] == "Sell" and x["fwd_return_pct"] < 0)) else 0.0
            briers.append((p - hit) ** 2)
            t = tiers.setdefault(x["confidence"], {"n": 0, "hits": 0})
            t["n"] += 1
            t["hits"] += int(hit)
        by_tier = []
        for c in ("high", "med", "low"):
            t = tiers.get(c)
            if t:
                actual = round(t["hits"] / t["n"] * 100, 1)
                by_tier.append({"confidence": c, "n": t["n"], "implied_hit_pct": CONF_P[c] * 100,
                                "actual_hit_pct": actual,
                                "gap_pts": round(actual - CONF_P[c] * 100, 1)})
        calibration = {"brier": round(sum(briers) / len(briers), 3), "coin_flip_brier": 0.25,
                       "n": len(cal_calls), "implied_p": CONF_P, "by_tier": by_tier,
                       "note": ("Brier score of the model's stated confidence vs realized directional "
                                "hits (implied P: high 75% / med 60% / low 50% — an assumption, stated "
                                "so it can be argued with). Below 0.25 beats a coin flip; a tier whose "
                                "actual hit rate doesn't exceed the tier below it is not earning its "
                                "label. Small samples are noisy.")}
    return {"status": "ready" if scored else "building", "scored_calls": len(scored),
            "logged_calls": len(log), "min_age_days": min_age,
            "by_rating": by_rating, "by_confidence": by_conf,
            "buy_minus_hold_pct": edge, "calibration": calibration,
            "directional_hit_rate_pct": round(hits / len(directional) * 100, 1) if directional else None,
            "recent": sorted(scored, key=lambda x: -x.get("ts", 0))[:20],
            "note": "Forward return from each dated rating's price to now, by rating & confidence. "
                    "'buy_minus_hold' > 0 means the model's Buys beat its Holds; hit-rate = Buys that rose "
                    "+ Sells that fell, over directional calls. Educational — small samples are noisy, not advice."}


@app.get("/api/scorecard")
def get_scorecard():
    return _scorecard()


@app.get("/api/grounding")
def get_grounding():
    """G13 grounding watchdog: the dated-constant registry (every tax/ACA/SS figure with its
    source + verification date + staleness tripwire) and the LLM-as-judge verdict audit.
    Cheap: attribute reads + one small file — no prices, no LLM. Confidence calibration
    (Brier) lives on /api/scorecard (it needs price fetches)."""
    g = grounding.summary()
    g["verifier"] = _verifier_summary()
    return g


@app.get("/api/estate")
def get_estate():
    """G2 estate & generational view: snapshot vs the federal exemption, per-account step-up
    ledger, gift-vs-hold on the top embedded-gain taxable positions, annual-exclusion gifting
    runway, titling/beneficiary audit, trust triggers. Decision-support only — not legal advice."""
    try:
        d = _data()
        cfg = _read_retire()
        married = tax.norm_status(cfg.get("filing_status") or "mfj") == "mfj"
        growth = cfg.get("expected_real_return_pct") or 5.0
        acct_status = {a.get("name"): _account_tax_status(a.get("name"), a.get("type"))
                       for a in d.get("accounts", [])}
        accounts, taxable_positions = [], []
        for acct in d.get("by_account", []):
            name = acct.get("account")
            status = acct_status.get(name) or _account_tax_status(name, "Investments")
            if status == "na":
                continue
            hs = acct.get("holdings", [])
            val = sum(h.get("value") or 0 for h in hs)
            gains = [h.get("gain") for h in hs if h.get("gain") is not None]
            costs = [h.get("cost_total") for h in hs if h.get("cost_total") is not None]
            accounts.append({"name": name, "tax_status": status, "value": val,
                             "gain_total": sum(gains) if gains else None,
                             "basis_total": sum(costs) if costs else None})
            if status == "taxable":
                for h in hs:
                    g_ = h.get("gain")
                    if g_ and g_ > 0 and (h.get("value") or 0) > 0:
                        taxable_positions.append({"ticker": h.get("ticker"), "account": name,
                                                  "value": h["value"], "gain": g_})
        snap = estate.snapshot(d.get("net_worth"), married, growth)
        over = (snap.get("taxable_estate_if_today") or 0) > 0
        taxable_positions.sort(key=lambda x: -x["gain"])
        gvh = []
        for p in taxable_positions[:5]:
            a_ = estate.gift_vs_hold(p["value"], p["value"] - p["gain"],
                                     heir_ltcg_pct=_knob("estate_heir_ltcg_pct"),
                                     growth_pct=growth, years=20, estate_over_exemption=over)
            a_["ticker"], a_["account"] = p["ticker"], p["account"]
            gvh.append(a_)
        return {"generated_at": int(time.time()),
                "snapshot": snap,
                "stepup": estate.stepup_ledger(accounts),
                "gift_vs_hold": {"positions": gvh,
                                 "note": "Top embedded-gain TAXABLE positions, modeled gift-now vs "
                                         "hold-for-step-up over 20 years at your assumed real return."},
                "gifting": estate.gifting_capacity(cfg.get("estate_donees") or 0, married,
                                                   years=10, growth_pct=growth),
                "titling": estate.titling_audit(accounts),
                "trusts": estate.trust_triggers(d.get("net_worth"), married,
                                                cross_year=snap.get("years_until_exemption_crossed")),
                "note": "Educational decision-support, not legal/tax advice — estate documents need "
                        "a licensed attorney; confirm figures with a CPA."}
    except Exception as e:
        return JSONResponse({"error": f"estate view unavailable: {e}"}, status_code=502)


# --- Calibration memory (N13 candidate 2): feed the model its OWN scored track record into the brief
#     prompt so it self-calibrates confidence against reality — Reflexion-style self-critique via
#     CONTEXT, deliberately NOT an extra LLM generation (near-zero cost on Pascal). Cached so a nightly
#     batch computes the scorecard once, not once per brief. Best-effort: silent, empty on any failure. ---
CALIB_INJECT = os.environ.get("CALIB_INJECT", "1").lower() in ("1", "true", "yes", "on")
CALIB_TTL = int(os.environ.get("CALIB_TTL", "3600"))            # recompute the line at most this often (s)
CALIB_MIN_CALLS = int(os.environ.get("CALIB_MIN_CALLS", "10"))  # don't inject until enough scored calls to matter
_calib_cache = {"ts": 0.0, "text": ""}


def _calibration_line():
    """A compact one-liner on how the model's OWN past Buy/Hold/Sell calls actually performed, for
    injection into the brief prompt so it calibrates confidence against its real track record. Returns
    "" when disabled, empty, too small to be meaningful, or on any error. Cached for CALIB_TTL so the
    (price-fetching) scorecard runs once per batch, not per brief."""
    if not CALIB_INJECT:
        return ""
    now = time.time()
    if _calib_cache["ts"] > 0 and (now - _calib_cache["ts"]) < CALIB_TTL:
        return _calib_cache["text"]                            # fresh (even if ""): don't recompute the scorecard
    text = ""
    try:
        sc = _scorecard()
        if sc.get("status") == "ready" and (sc.get("scored_calls") or 0) >= CALIB_MIN_CALLS:
            def fmt(a):
                return f"{a['avg_fwd_return_pct']:+.1f}% (n={a['n']})" if a else "n/a"
            bc = sc.get("by_confidence") or {}
            br = sc.get("by_rating") or {}
            parts = []
            conf_bits = [f"{c}: {fmt(bc.get(c))}" for c in ("high", "med", "low") if bc.get(c)]
            if conf_bits:
                parts.append("by your stated confidence — " + ", ".join(conf_bits))
            rate_bits = [f"{r}: {fmt(br.get(r))}" for r in ("Buy", "Hold", "Sell") if br.get(r)]
            if rate_bits:
                parts.append("by rating — " + ", ".join(rate_bits))
            tail = []
            if sc.get("buy_minus_hold_pct") is not None:
                tail.append(f"Buy-minus-Hold edge {sc['buy_minus_hold_pct']:+.1f}pts")
            if sc.get("directional_hit_rate_pct") is not None:
                tail.append(f"directional hit-rate {sc['directional_hit_rate_pct']:.0f}%")
            if tail:
                parts.append("; ".join(tail))
            if parts:
                text = ("CALIBRATION — your OWN past calls, forward return measured after "
                        f">={sc.get('min_age_days')}d over {sc.get('scored_calls')} scored calls (small "
                        "samples are NOISY — treat as a weak prior, not a rule): " + " | ".join(parts)
                        + ". If a confidence tier has not actually out-earned the tier below it, be "
                        "stricter before awarding it here.\n")
    except Exception:
        text = ""
    _calib_cache.update(ts=now, text=text)
    return text


@app.get("/api/securities")
def securities():
    """Unique securities (with a ticker) for the daily analysis job to research."""
    d = _data()
    return {"generated_at": d["generated_at"],
            "securities": [{"ticker": c["ticker"], "name": c["name"], "type": c["type"],
                            "value": c["value"], "weight_pct": c["weight_pct"],
                            "gain_pct": c["gain_pct"], "accounts": c["accounts"]}
                           for c in d["consolidated"] if c["ticker"]]}


@app.get("/api/raw-holdings")
def raw_holdings(token: str = "", account_id: str = "", limit: int = 2,
                 x_analysis_token: str = Header(default="")):
    """DIAGNOSTIC (read-only): dump untrimmed Monarch aggregateHoldings node fields so
    we can verify cost-basis semantics (is `basis` per-share or a total?). Token-guarded.
    Compare each node's basis vs totalValue vs security.currentPrice * quantity."""
    tok = token or x_analysis_token
    if ANALYSIS_TOKEN and tok != ANALYSIS_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        accts = [a for a in _accounts(_post("get_accounts", {})) if (a.get("holdingsCount") or 0) > 0]
    except Exception as e:
        return JSONResponse({"error": f"upstream unavailable (mcpo/monarch not ready?): {e}"},
                            status_code=502)
    if account_id:
        accts = [a for a in accts if str(a.get("id")) == str(account_id)]
    out = []
    for a in accts:
        try:
            nodes = _holds(_post("get_account_holdings", {"account_id": a.get("id")}))
        except Exception as e:
            out.append({"account": a.get("displayName"), "id": a.get("id"), "error": str(e)})
            continue
        out.append({"account": a.get("displayName"), "id": a.get("id"),
                    "institution": (a.get("institution") or {}).get("name"),
                    "holdings_count": a.get("holdingsCount"), "sample": nodes[:limit]})
    return {"note": "raw untrimmed Monarch nodes; compare quantity/basis/totalValue vs "
                    "security.currentPrice to determine if basis is per-share or total",
            "accounts": out}


@app.get("/api/raw-accounts")
def raw_accounts(token: str = "", x_analysis_token: str = Header(default="")):
    """DIAGNOSTIC (read-only): untrimmed account fields to debug the net-worth math —
    shows includeBalanceInNetWorth, isAsset, and the displayBalance sign for liabilities."""
    tok = token or x_analysis_token
    if ANALYSIS_TOKEN and tok != ANALYSIS_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    out = []
    try:
        accts_raw = _accounts(_post("get_accounts", {}))
    except Exception as e:
        return JSONResponse({"error": f"upstream unavailable (mcpo/monarch not ready?): {e}"},
                            status_code=502)
    for a in accts_raw:
        out.append({
            "name": a.get("displayName"),
            "type": (a.get("type") or {}).get("display"),
            "institution": (a.get("institution") or {}).get("name"),
            "displayBalance": a.get("displayBalance"),
            "isAsset": a.get("isAsset"),
            "includeBalanceInNetWorth": a.get("includeBalanceInNetWorth"),
        })
    return {"note": "Monarch net worth = signed sum over includeBalanceInNetWorth accounts; "
                    "check isAsset + displayBalance sign on any liabilities",
            "count": len(out), "accounts": out}


@app.get("/api/fundamentals")
def get_fundamentals(ticker: str):
    """SEC EDGAR / GAAP fundamentals + ratios for one ticker (cached ~1 day).
    Returns {available:false, reason} for foreign listings, funds/ETFs, and non-SEC filers."""
    return edgar.fundamentals(ticker)


@app.get("/api/valuation")
def get_valuation(ticker: str):
    """Valuation multiples (P/E trailing & forward, P/B, P/S, dividend & earnings yield, beta)
    from Finnhub. Returns null if unavailable (foreign listing not covered, or no key)."""
    return _valuation(ticker) or {"ticker": ticker, "available": False, "reason": "no valuation data"}


@app.get("/api/research")
def get_research(ticker: str):
    """On-demand B/S/H research brief for ANY ticker (not just holdings): analyst consensus +
    SEC/GAAP fundamentals + valuation + recent news -> LLM rating/thesis/bull/bear/risks. SLOW
    (one model call, minutes on Pascal). Read-only, educational — NOT advice."""
    t = (ticker or "").upper().strip()
    if not t:
        return JSONResponse({"error": "ticker required"}, status_code=400)
    try:
        g = _grounded(t)
        val = _valuation(t) or {}
        fund = edgar.fundamentals(t)
        news = _news(t, t)
        llm = _llm(t, t, None, None, g, news, fund, val)
        llm = _verify_and_apply(t, t, llm, g, news, fund, val, force=True)   # on-demand → always verify
        ins = None
        try:
            ins = _insider_signal(t)   # G15 context-only insider read (cached ~6h)
        except Exception:
            ins = None
        return {"ticker": t, "generated_at": int(time.time()), "grounded": g,
                "valuation": val, "fundamentals": fund, "llm": llm, "insider": ins}
    except Exception as e:
        return JSONResponse({"error": f"research failed: {e}"}, status_code=500)


@app.get("/api/knowledge")
def get_knowledge():
    """Status of the user-populated Knowledge folder + its RAG index (no vectors/text)."""
    try:
        return knowledge.status()
    except Exception as e:
        return JSONResponse({"error": f"knowledge status failed: {e}"}, status_code=500)


@app.post("/api/knowledge/reindex")
def post_knowledge_reindex():
    """Force a rebuild of the knowledge vector index (embeds new/changed docs via Ollama bge-m3).
    Slow if you just added large PDFs. Read-only w.r.t. your data."""
    try:
        knowledge.ensure_index(force=True)
        return knowledge.status()
    except Exception as e:
        return JSONResponse({"error": f"reindex failed: {e}"}, status_code=500)


def _read_analysis():
    try:
        with open(ANALYSIS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"updated_at": None, "briefs": {}}


@app.get("/api/analysis")
def get_analysis():
    """The stored daily research briefs, filtered to currently-held (>0 share) tickers so
    sold-out positions never linger on the dashboard."""
    data = _read_analysis()
    try:
        held = {c.get("ticker") for c in _data()["consolidated"] if c.get("ticker")}
        data = {**data, "briefs": {k: v for k, v in (data.get("briefs") or {}).items() if k in held}}
    except Exception:
        pass  # if the portfolio can't be loaded, show stored briefs as-is
    return data


@app.post("/api/analysis")
async def post_analysis(req: Request, x_analysis_token: str = Header(default="")):
    """n8n writes briefs here. Body: {"ticker": "...", "grounded": {...}, "llm": {...}} (one)
    or {"briefs": {ticker: {...}}} (bulk). Merges into the stored file."""
    if ANALYSIS_TOKEN and x_analysis_token != ANALYSIS_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await req.json()
    briefs = (_read_analysis().get("briefs") or {})
    if isinstance(body.get("briefs"), dict):
        briefs.update(body["briefs"])
    elif body.get("ticker"):
        briefs[body["ticker"]] = {k: v for k, v in body.items() if k != "ticker"}
    else:
        return JSONResponse({"error": "expected {ticker,...} or {briefs:{...}}"}, status_code=400)
    out = {"updated_at": int(time.time()), "briefs": briefs}
    os.makedirs(os.path.dirname(ANALYSIS_FILE) or ".", exist_ok=True)
    with open(ANALYSIS_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return {"ok": True, "count": len(briefs)}


# ---------------- daily analysis (Buy/Sell/Hold: grounded + LLM, side by side) ----------------
def _finnhub(path, sym):
    if not FINNHUB:
        return None
    try:
        r = requests.get(f"https://finnhub.io/api/v1/{path}",
                         params={"symbol": sym, "token": FINNHUB}, timeout=20)
        return r.json() if r.status_code < 400 else None
    except Exception:
        return None


def _grounded(sym):
    rec = _finnhub("stock/recommendation", sym) or []
    latest = rec[0] if rec else {}
    pt = _finnhub("stock/price-target", sym) or {}
    buys = (latest.get("strongBuy") or 0) + (latest.get("buy") or 0)
    holds = latest.get("hold") or 0
    sells = (latest.get("sell") or 0) + (latest.get("strongSell") or 0)
    total = buys + holds + sells
    rating = None
    if total:
        rating = "Buy" if buys >= holds and buys >= sells else ("Sell" if sells > buys and sells > holds else "Hold")
    return {"rating": rating, "analyst_total": total,
            "counts": {k: latest.get(k) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")},
            "target_mean": pt.get("targetMean"), "target_high": pt.get("targetHigh"),
            "target_low": pt.get("targetLow"), "period": latest.get("period")}


def _news(sym, name):
    try:
        r = requests.post(f"{MCPO}/tavily/tavily_news_search",
                          headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
                          json={"query": f"{sym} {name} stock", "days": 7, "max_results": 5}, timeout=45)
        return r.text[:2500] if r.status_code < 400 else ""
    except Exception:
        return ""


def _valuation(sym):
    """Valuation multiples from Finnhub Basic Financials (/stock/metric). Read-only.
    Returns None if no data (e.g. foreign listing not in Finnhub, or no key)."""
    if not FINNHUB:
        return None
    try:
        r = requests.get("https://finnhub.io/api/v1/stock/metric",
                         params={"symbol": sym, "metric": "all", "token": FINNHUB}, timeout=20)
        if r.status_code >= 400:
            return None
        m = (r.json() or {}).get("metric") or {}
    except Exception:
        return None
    pe = m.get("peTTM") or m.get("peExclExtraTTM")
    out = {
        "pe_ttm": pe,
        "pe_forward": m.get("peNTM") or m.get("forwardPE"),
        "pb": m.get("pbAnnual") or m.get("pbQuarterly"),
        "ps_ttm": m.get("psTTM"),
        "dividend_yield_pct": m.get("dividendYieldIndicatedAnnual") or m.get("currentDividendYieldTTM"),
        "beta": m.get("beta"),
        "market_cap_musd": m.get("marketCapitalization"),
        "avg_volume_10d_m": m.get("10DayAverageTradingVolume"),    # millions of shares/day
        "avg_volume_3m_m": m.get("3MonthAverageTradingVolume"),    # millions of shares/day
        "eps_ttm": m.get("epsTTM"),
        "high_52w": m.get("52WeekHigh"), "low_52w": m.get("52WeekLow"),
        "earnings_yield_pct": round(100 / pe, 2) if (pe and pe != 0) else None,
    }
    return out if any(v is not None for v in out.values()) else None


def _price(sym):
    """Current share price from Finnhub /quote (free)."""
    q = _finnhub("quote", sym)
    c = q.get("c") if isinstance(q, dict) else None
    return c if c else None


_VALCTX_CACHE = {}
_INSIDER_CACHE = {}   # ticker -> (ts, signal dict)
INSIDER_TTL = int(os.environ.get("INSIDER_TTL", "21600"))   # cache the insider signal ~6h


def _valuation_percentile(sym):
    """Sector-relative valuation (Part-4): where the ticker's forward P/E sits vs its Finnhub
    peer set — a CHEAPNESS percentile (0 = cheapest of the group, 100 = richest). A relative-value
    read that own-multiple-in-a-vacuum misses; a rich reading is a candidate EXIT trigger. Cached
    ~1 day in-process. None if peers/valuation are unavailable (foreign, funds, no key)."""
    now = time.time()
    hit = _VALCTX_CACHE.get(sym)
    if hit and now - hit[0] < 86400:
        return hit[1]
    res = None
    try:
        peers = [str(p).upper() for p in (_finnhub("stock/peers", sym) or []) if p][:8]
        universe = list(dict.fromkeys([sym.upper()] + peers))
        pes = {}
        for p in universe:
            v = _valuation(p) or {}
            pe = v.get("pe_forward") or v.get("pe_ttm")
            if pe and pe > 0:
                pes[p] = pe
        mine = pes.get(sym.upper())
        if mine is not None and len(pes) >= 3:
            vals = sorted(pes.values())
            below = sum(1 for x in vals if x < mine)
            pct = round(below / len(vals) * 100)
            med = vals[len(vals) // 2]
            verdict = "cheap vs peers" if pct <= 33 else ("rich vs peers" if pct >= 67 else "in line")
            res = {"forward_pe": round(mine, 1), "peer_median_pe": round(med, 1),
                   "peer_count": len(pes), "cheapness_percentile": pct, "verdict": verdict}
    except Exception:
        res = None
    _VALCTX_CACHE[sym] = (now, res)
    return res


@app.get("/api/valuation-context")
def get_valuation_context(ticker: str):
    """Sector-relative valuation percentile for one ticker (forward P/E vs Finnhub peers)."""
    t = (ticker or "").upper().strip()
    if not t:
        return JSONResponse({"error": "ticker required"}, status_code=400)
    r = _valuation_percentile(t)
    return r or {"ticker": t, "available": False, "reason": "no peer/valuation data"}


def _insider_signal(sym):
    """G15: net OPEN-MARKET insider buy/sell signal for a ticker from its recent SEC Form 4 filings.
    Context-only. Fetches the issuer's Form 4s (EDGAR), parses each, and aggregates. Cached ~6h
    in-process (an EDGAR pull per ticker). None-ish {available:false} for foreign/funds/no filings."""
    t = (sym or "").upper().strip()
    if not t:
        return None
    now = time.time()
    hit = _INSIDER_CACHE.get(t)
    if hit and now - hit[0] < INSIDER_TTL:
        return hit[1]
    days = int(_knob("insider_lookback_days"))
    res = None
    try:
        raw = edgar.recent_form4(t, days=days, limit=int(_knob("insider_max_filings")))
        parsed = [insider.parse_form4(f.get("xml")) for f in raw]
        parsed = [p for p in parsed if p]
        if parsed:
            res = insider.aggregate(parsed, t, days)
            res["available"] = True
        else:
            res = {"ticker": t, "available": False,
                   "reason": "no Form 4 filings (foreign listing, fund/ETF, or none in window)"}
    except Exception as e:
        res = {"ticker": t, "available": False, "reason": f"insider fetch error: {e}"}
    _INSIDER_CACHE[t] = (now, res)
    return res


@app.get("/api/insider")
def get_insider(ticker: str):
    """Net open-market insider buy/sell signal for one ticker from recent SEC Form 4 filings (G15).
    CONTEXT ONLY — cluster buying is the higher-signal event; selling is noisy. Not advice.
    13F institutional ownership + short interest are data-gated (no free per-security SEC reverse
    lookup) and are deferred to a data-provider MCP, not shown here."""
    t = (ticker or "").upper().strip()
    if not t:
        return JSONResponse({"error": "ticker required"}, status_code=400)
    return _insider_signal(t) or {"ticker": t, "available": False, "reason": "no data"}


def _parse_json_obj(raw):
    """Best-effort: pull a JSON object out of a model's text response.
    Handles empty strings, ```json fences, and prose wrapped around the object."""
    if not raw:
        return None
    s = raw.strip()
    # strip markdown code fences
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        s = s[4:].strip() if s.lower().startswith("json") else s.strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    # fall back to the first balanced {...} blob
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(s[start:end + 1])
        except Exception:
            pass
    return None


def _ollama_generate(prompt):
    """Call Ollama, returning the raw text. Disables the reasoning channel so the
    JSON answer lands in `response` (gpt-oss & other reasoning models otherwise
    return an empty `response` with everything in `thinking`). Falls back gracefully
    for Ollama builds that reject the `think` field."""
    # Qwen3 family: soft-switch off the <think> channel so the JSON isn't buried in reasoning
    # (unless ANALYSIS_THINK is enabled — then let it reason for deeper, better-calibrated briefs).
    if "qwen3" in ANALYSIS_MODEL.lower() and not ANALYSIS_THINK:
        prompt = prompt + "\n/no_think"
    base = {"model": ANALYSIS_MODEL, "prompt": prompt, "format": "json", "stream": False,
            "keep_alive": LLM_KEEP_ALIVE,
            "options": {"temperature": 0.3, "num_ctx": ANALYSIS_NUM_CTX}}
    for body in ({**base, "think": ANALYSIS_THINK}, base):
        try:
            r = requests.post(f"{OLLAMA}/api/generate", json=body, timeout=LLM_TIMEOUT)
        except Exception as e:
            return None, f"request failed: {e}"
        if r.status_code >= 400:
            # a 400 about `think` -> retry without it; otherwise report
            if "think" in r.text.lower() and body.get("think") is False:
                continue
            return None, f"ollama {r.status_code}: {r.text[:300]}"
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            if "think" in str(data["error"]).lower() and body.get("think") is False:
                continue
            return None, str(data["error"])
        raw = ((data.get("response") or "").strip() or (data.get("thinking") or "").strip())
        return raw, None
    return None, "no response"


RDCF_DISCOUNT = float(os.environ.get("RDCF_DISCOUNT_PCT", "10")) / 100.0
RDCF_TERM_GROWTH = float(os.environ.get("RDCF_TERM_GROWTH_PCT", "2.5")) / 100.0
RDCF_YEARS = int(os.environ.get("RDCF_YEARS", "10"))


def _reverse_dcf(valuation, fundamentals):
    """Reverse-DCF: solve for the annual FCF growth rate the CURRENT market cap implies, given trailing
    FCF, an N-year high-growth phase, a Gordon terminal, and a discount rate. A quick 'what does the
    price assume' read — compare the implied growth to the company's actual growth. APPROXIMATION: uses
    market cap vs FCF (equity-level, not FCFF/enterprise value; ignores net debt/cash). Returns None
    when FCF<=0 or market cap/FCF is unavailable (reverse-DCF is undefined for negative FCF)."""
    if not (valuation and fundamentals and fundamentals.get("available")):
        return None
    mcap_musd = valuation.get("market_cap_musd")
    if not mcap_musd or mcap_musd <= 0:
        return None
    m = fundamentals.get("metrics") or {}
    fcf = (fundamentals.get("ttm") or {}).get("free_cash_flow")
    if fcf is None:
        fcf = m.get("free_cash_flow")
    if fcf is None or fcf <= 0:
        return None
    mcap = mcap_musd * 1e6
    r, gt, N = RDCF_DISCOUNT, RDCF_TERM_GROWTH, RDCF_YEARS

    def pv(g):
        total, f = 0.0, float(fcf)
        for i in range(1, N + 1):
            f *= (1 + g)
            total += f / (1 + r) ** i
        total += (f * (1 + gt) / (r - gt)) / (1 + r) ** N     # Gordon terminal on year-N FCF
        return total

    lo, hi = -0.5, 1.0
    if pv(lo) > mcap:      # price implies growth below the model floor
        return {"implied_growth_pct": None, "fcf_ttm_musd": round(fcf / 1e6, 1),
                "discount_pct": round(r * 100, 1), "terminal_growth_pct": round(gt * 100, 2), "years": N,
                "note": "Price implies FCF growth below the model's -50% floor (trough FCF?). Reverse-DCF."}
    if pv(hi) < mcap:      # price implies > 100%/yr — clamp
        implied = 1.0
    else:
        for _ in range(60):
            mid = (lo + hi) / 2
            if pv(mid) < mcap:
                lo = mid
            else:
                hi = mid
        implied = (lo + hi) / 2
    ig = round(implied * 100, 1)
    actual = (m.get("trends") or {}).get("revenue_cagr_pct")
    if actual is None:
        actual = m.get("revenue_growth_pct")
    verdict = None
    if actual is not None:
        if ig - actual >= 10:
            verdict = "demanding — price implies FCF growth well above recent revenue growth"
        elif actual - ig >= 10:
            verdict = "undemanding — price implies less growth than recently realized"
        else:
            verdict = "roughly in line with recent revenue growth"
    return {"implied_growth_pct": ig, "vs_actual_growth_pct": actual, "assessment": verdict,
            "fcf_ttm_musd": round(fcf / 1e6, 1), "discount_pct": round(r * 100, 1),
            "terminal_growth_pct": round(gt * 100, 2), "years": N,
            "note": "Reverse-DCF: the annual FCF growth (N yrs high-growth + terminal) the CURRENT market "
                    "cap implies vs trailing FCF. Approximation — market cap vs FCF, ignores net debt; "
                    "compare implied vs actual growth to judge how demanding the price is. Not advice."}


def _llm(sym, name, weight, gain_pct, grounded, news, fundamentals=None, valuation=None):
    fund_avail = bool(fundamentals and fundamentals.get("available"))
    fund_txt = "not available (foreign listing, fund/ETF, or not SEC-registered)"
    if fund_avail:
        fund_txt = json.dumps({"fiscal_year_end": (fundamentals.get("metrics") or {}).get("fiscal_year_end"),
                               "metrics": fundamentals.get("metrics"), "ratios": fundamentals.get("ratios"),
                               "scores": fundamentals.get("scores")})
    val_txt = json.dumps(valuation) if valuation else "not available"
    calib_block = _calibration_line()   # the model's own scored track record (may be ""); calibrates confidence
    _rdcf = _reverse_dcf(valuation, fundamentals)   # implied-growth read from price vs FCF (may be None)
    rdcf_line = ""
    if _rdcf and _rdcf.get("implied_growth_pct") is not None:
        rdcf_line = (f"REVERSE-DCF: at today's market cap the price implies roughly "
                     f"{_rdcf['implied_growth_pct']}%/yr FCF growth for {_rdcf['years']} years "
                     f"(discount {_rdcf['discount_pct']}%, terminal {_rdcf['terminal_growth_pct']}%)"
                     + (f", vs recent revenue growth {_rdcf['vs_actual_growth_pct']}%"
                        if _rdcf.get("vs_actual_growth_pct") is not None else "")
                     + f" — {_rdcf.get('assessment') or 'judge whether that is achievable'}. Weigh whether "
                       "that implied growth is realistic for this business.\n")
    # explicit coverage signal so the model rates confidence on what it WAS given, not an assumption of thinness
    n_analysts = (grounded or {}).get("analyst_total") or 0
    have_val = bool(valuation)
    have_news = bool(news and len(str(news)) > 60)
    inputs_present = sum([fund_avail, have_val, bool(n_analysts), have_news])
    coverage = (f"SEC GAAP fundamentals: {'PRESENT' if fund_avail else 'MISSING'}; "
                f"valuation multiples: {'PRESENT' if have_val else 'MISSING'}; "
                f"analyst consensus: {(str(n_analysts) + ' analysts') if n_analysts else 'NONE'}; "
                f"recent news: {'PRESENT' if have_news else 'NONE'} "
                f"({inputs_present} of 4 input sources available)")
    prompt = (
        f"Today is {datetime.date.today().isoformat()}; treat news, prices and TTM figures as of that date "
        "and do not assume an earlier year.\n"
        "You are a research analyst. Using ONLY the data provided below, return a JSON object with keys: "
        "rating (one of Buy, Hold, Sell), confidence (low, med, high), thesis, bull_case, bear_case, key_risks. "
        "This is informational only and NOT financial advice.\n"
        "CONFIDENCE RUBRIC — rate how well-supported YOUR call is by the data actually provided (see the DATA "
        "COVERAGE line), NOT how certain you are about the future:\n"
        "  - high: fundamentals, valuation, AND analyst coverage are all present and their signals broadly AGREE "
        "on direction (healthy/improving fundamentals + a defensible valuation + analyst consensus + momentum "
        "leaning the same way) — the thesis is well-corroborated.\n"
        "  - med: most inputs are present but the signals are MIXED (e.g. strong growth yet a stretched multiple, "
        "or analysts disagree with the fundamentals), or one key input is missing.\n"
        "  - low: fundamentals/valuation/analyst coverage are thin or unavailable (normal for funds/ETFs and "
        "foreign listings), or the signals CONFLICT so sharply that no call is well-supported.\n"
        "Do NOT default to low when the data is actually rich — EARN 'high' when the evidence lines up, 'med' is "
        "the normal case for a well-covered stock with mixed signals. Reserve 'low' for genuinely thin/conflicting data.\n"
        f"{calib_block}"
        "Analyze fundamentals with a GAAP lens: liquidity (current/quick), profitability (gross/operating/net "
        "margins, ROE/ROA, revenue growth and the multi-year trend), solvency (debt-to-equity/assets, interest "
        "coverage), efficiency (asset turnover), and quality of earnings (operating cash flow and free cash flow "
        "vs net income — above net income is healthier).\n"
        "When the fundamentals include a `ttm` block, prefer those trailing-twelve-month margins over a single "
        "annual 10-K print — a one-off trough or peak fiscal year misrepresents current profitability; compare "
        "the annual figure against TTM and the multi-year trend before calling a margin 'low'.\n"
        "Assess VALUATION (price vs. value): P/E trailing and forward, P/B, P/S, dividend yield, and earnings "
        "yield vs. a ~4% Treasury. Flag value traps (a low P/E alongside declining revenue/margins) and cyclical "
        "distortions (a low P/E at an earnings peak is not 'cheap'). Pristine financials at an extreme valuation "
        "still warrant caution, and the reverse. Also weigh price momentum and the multi-year growth trajectory: "
        "a durable, high-growth winner can stay richly valued, so do not mechanically downgrade on a high multiple "
        "alone — but flag clearly when the price has outrun the fundamentals.\n"
        f"{rdcf_line}"
        "When present, weigh the disciplined quant scores in `scores`: the PIOTROSKI F-SCORE (fundamental "
        "strength out of 9 — 7+ strong, <=3 weak) and the ALTMAN Z' distress score (>2.9 safe, <1.23 "
        "distress); a high-growth name with a weak F-score or distress Z is a red flag worth calling out.\n"
        "Remember fundamentals are a LAGGING, historical, accounting-dependent signal that underweights "
        "intangibles; weigh them together with the analyst consensus and recent news, not in isolation.\n"
        f"DATA COVERAGE: {coverage}.\n"
        f"Ticker: {sym} ({name}). Portfolio weight: {weight}%. Holder's unrealized total return on this position: "
        f"{gain_pct}% since purchase.\n"
        f"Analyst consensus data: {json.dumps(grounded)}.\n"
        f"SEC GAAP fundamentals (latest annual 10-K): {fund_txt}.\n"
        f"Valuation multiples: {val_txt}.\n"
        f"Recent news: {news}"
    )
    raw, err = _ollama_generate(prompt)
    if err:
        return {"error": err}
    parsed = _parse_json_obj(raw)
    if parsed is not None:
        if isinstance(parsed, dict) and _rdcf:
            parsed["reverse_dcf"] = _rdcf     # surface the implied-growth read alongside the model's call
        return parsed
    return {"error": "non-JSON model response", "raw": (raw or "")[:600]}


# --- Brief verifier (N13 candidate 1): a groundedness / LLM-as-judge pass that checks the brief's
#     claims are supported by the DATA the model was actually given (not by world knowledge), then
#     downgrades confidence + surfaces unsupported claims — it NEVER rewrites the thesis. This is one
#     EXTRA generation, so it is gated (BRIEF_VERIFY) to where a human is about to act: on-demand N9
#     research (force=True) and directional Buy/Sell nightly calls only — never the Hold batch. ---
BRIEF_VERIFY = os.environ.get("BRIEF_VERIFY", "1").lower() in ("1", "true", "yes", "on")
BRIEF_VERIFY_THRESHOLD = float(os.environ.get("BRIEF_VERIFY_THRESHOLD", "0.6"))  # below this → downgrade confidence
# G13b: keep every verifier verdict (durable JSONL) so judge quality is itself auditable —
# sample entries for human labeling, watch the downgrade rate, catch a drifting judge.
VERIFIER_LOG_FILE = os.environ.get("VERIFIER_LOG_FILE", "/data/verifier_log.jsonl")
VERIFIER_LOG_MAX = int(os.environ.get("VERIFIER_LOG_MAX", "1000"))


def _log_verifier(ticker, llm, ver):
    """Append one JSONL row per verifier verdict (capped). Best-effort, never raises."""
    try:
        row = {"ts": int(time.time()), "date": datetime.date.today().isoformat(),
               "ticker": ticker, "rating": (llm or {}).get("rating"),
               "checked": bool(ver.get("checked")),
               "grounded_score": ver.get("grounded_score"),
               "unsupported_n": len(ver.get("unsupported_claims") or []),
               "downgraded": bool((llm or {}).get("confidence_adjusted")),
               "confidence": (llm or {}).get("confidence"),
               "confidence_original": (llm or {}).get("confidence_original"),
               "reason": ver.get("reason")}
        lines = []
        try:
            with open(VERIFIER_LOG_FILE, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except Exception:
            pass
        lines.append(json.dumps(row))
        lines = lines[-VERIFIER_LOG_MAX:]
        os.makedirs(os.path.dirname(VERIFIER_LOG_FILE) or ".", exist_ok=True)
        with open(VERIFIER_LOG_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


def _verifier_summary(recent=10):
    """Aggregate judge behavior from the verdict log: volume, average groundedness, downgrade
    rate, failure rate — the 'is the judge itself sane?' dashboard (G13b)."""
    try:
        with open(VERIFIER_LOG_FILE, encoding="utf-8") as f:
            rows = [json.loads(x) for x in f.read().splitlines() if x.strip()]
    except Exception:
        rows = []
    if not rows:
        return {"status": "empty", "note": "No verifier verdicts logged yet — they accrue as "
                "Buy/Sell and on-demand briefs get the groundedness pass."}
    ok = [r for r in rows if r.get("checked")]
    scores = [r["grounded_score"] for r in ok if r.get("grounded_score") is not None]
    return {"status": "ready", "verdicts": len(rows), "checked": len(ok),
            "check_failure_pct": round((len(rows) - len(ok)) / len(rows) * 100, 1),
            "avg_grounded_score": round(sum(scores) / len(scores), 3) if scores else None,
            "below_threshold_pct": round(sum(1 for s in scores if s < BRIEF_VERIFY_THRESHOLD)
                                         / len(scores) * 100, 1) if scores else None,
            "downgrade_pct": round(sum(1 for r in ok if r.get("downgraded")) / len(ok) * 100, 1) if ok else None,
            "recent": rows[-recent:][::-1],
            "note": "Every LLM-as-judge verdict, logged for calibration: sample entries against the "
                    "underlying briefs to label the judge itself. A downgrade rate near 0% or 100% "
                    "means the threshold (or the judge) needs attention."}
_CONF_ORDER = ["low", "med", "high"]


def _downgrade_conf(c):
    try:
        i = _CONF_ORDER.index((c or "").lower())
    except ValueError:
        return c
    return _CONF_ORDER[max(0, i - 1)]


def _verify_brief(sym, name, brief, grounded, news, fundamentals=None, valuation=None):
    """LLM-as-judge groundedness check: does the brief's rating/thesis/bull/bear/risks follow from the
    SOURCE DATA provided (not from outside knowledge)? One extra model call. Returns a verification
    dict; on any failure returns {checked: False, reason} so the caller leaves the brief untouched."""
    claims = {k: brief.get(k) for k in ("rating", "thesis", "bull_case", "bear_case", "key_risks")
              if brief.get(k)}
    if not claims:
        return {"checked": False, "reason": "no claims to verify"}
    fund_txt = "not available"
    if fundamentals and fundamentals.get("available"):
        fund_txt = json.dumps({"metrics": fundamentals.get("metrics"), "ratios": fundamentals.get("ratios"),
                               "scores": fundamentals.get("scores")})
    val_txt = json.dumps(valuation) if valuation else "not available"
    prompt = (
        "You are a fiduciary compliance reviewer. Judge ONLY whether the analyst brief below is GROUNDED "
        "in the SOURCE DATA provided — i.e. each material claim is supported by, or reasonably inferable "
        "from, that data. Do NOT judge whether the claims are true in the wider world and do NOT use "
        "outside knowledge: a claim the source data neither states nor implies is UNSUPPORTED even if it "
        "sounds plausible. A rating is supported when the data broadly points that way; it is unsupported "
        "when the data is silent or points the other way.\n"
        "Return ONLY a JSON object with keys: grounded_score (a number 0.0-1.0 = the fraction of the "
        "brief's material claims that ARE supported by the source data), unsupported_claims (a list of "
        "short strings, each paraphrasing one claim NOT supported by the data — [] if all are supported), "
        "assessment (one sentence).\n"
        f"SOURCE DATA —\n"
        f"Analyst consensus: {json.dumps(grounded)}.\n"
        f"SEC GAAP fundamentals: {fund_txt}.\n"
        f"Valuation multiples: {val_txt}.\n"
        f"Recent news: {news}\n"
        f"ANALYST BRIEF to review for {sym} ({name}): {json.dumps(claims)}"
    )
    raw, err = _ollama_generate(prompt)
    if err:
        return {"checked": False, "reason": err}
    v = _parse_json_obj(raw)
    if v is None:
        return {"checked": False, "reason": "non-JSON verifier response"}
    try:
        score = float(v.get("grounded_score"))
        score = max(0.0, min(1.0, score))
    except (TypeError, ValueError):
        score = None
    uns = v.get("unsupported_claims") or []
    if not isinstance(uns, list):
        uns = [str(uns)]
    return {"checked": True, "grounded_score": score,
            "unsupported_claims": [str(x) for x in uns][:10],
            "assessment": str(v.get("assessment") or "")[:400],
            "threshold": BRIEF_VERIFY_THRESHOLD}


def _verify_and_apply(sym, name, llm, grounded, news, fundamentals=None, valuation=None, force=False):
    """Gate + apply the groundedness verifier. Runs only when BRIEF_VERIFY is on AND (this is an
    on-demand request [force] OR the rating is directional Buy/Sell) — so the nightly Hold batch never
    pays the extra generation. Attaches a `verification` block; if the brief scores below
    BRIEF_VERIFY_THRESHOLD it downgrades confidence one notch (recording the original) and NEVER
    rewrites the thesis. Best-effort: any verifier failure leaves the brief exactly as-is."""
    if not BRIEF_VERIFY or not isinstance(llm, dict) or llm.get("error"):
        return llm
    if not (force or llm.get("rating") in ("Buy", "Sell")):
        return llm
    ver = _verify_brief(sym, name, llm, grounded, news, fundamentals, valuation)
    llm["verification"] = ver
    score = ver.get("grounded_score") if ver.get("checked") else None
    if score is not None and score < BRIEF_VERIFY_THRESHOLD:
        orig = llm.get("confidence")
        new = _downgrade_conf(orig)
        if new != orig:
            llm["confidence"] = new
            llm["confidence_original"] = orig
            llm["confidence_adjusted"] = True
    _log_verifier(sym, llm, ver)   # G13b: verdicts are themselves audited
    return llm


def _do_analysis(limit):
    if not _run_lock.acquire(blocking=False):
        return
    try:
        secs = [c for c in _data()["consolidated"] if c["ticker"]]
        if limit:
            secs = secs[:limit]
        _run_state.update(status="running", analyzed=0, total=len(secs), started=int(time.time()))
        current = {c["ticker"] for c in secs}   # drop briefs for tickers no longer held (e.g. sold)
        briefs = {k: v for k, v in (_read_analysis().get("briefs") or {}).items() if k in current}
        for c in secs:
            g = _grounded(c["ticker"])
            news = _news(c["ticker"], c["name"])
            fund = edgar.fundamentals(c["ticker"])
            val = _valuation(c["ticker"])
            llm = _llm(c["ticker"], c["name"], c.get("weight_pct"), c.get("gain_pct"), g, news, fund, val)
            llm = _verify_and_apply(c["ticker"], c["name"], llm, g, news, fund, val)  # Buy/Sell only (not Holds)
            briefs[c["ticker"]] = {"name": c["name"], "grounded": g, "fundamentals": fund,
                                   "valuation": val, "llm": llm, "updated_at": int(time.time())}
            _log_rating(c["ticker"], llm)   # record rating+price for the AI scorecard
            _run_state["analyzed"] += 1
            out = {"updated_at": int(time.time()), "briefs": briefs}
            os.makedirs(os.path.dirname(ANALYSIS_FILE) or ".", exist_ok=True)
            with open(ANALYSIS_FILE, "w", encoding="utf-8") as f:  # write incrementally
                json.dump(out, f, ensure_ascii=False, indent=2)
        _run_state["status"] = "idle"
    finally:
        _run_lock.release()


@app.post("/api/run-analysis")
def run_analysis(x_analysis_token: str = Header(default=""), limit: int = 0):
    """Trigger the daily research run (background). n8n cron POSTs here once a day."""
    if ANALYSIS_TOKEN and x_analysis_token != ANALYSIS_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not FINNHUB:
        return JSONResponse({"error": "FINNHUB_API_KEY not set"}, status_code=400)
    if _run_lock.locked():
        return {"ok": True, "status": "already running", **_run_state}
    threading.Thread(target=_do_analysis, args=(limit,), daemon=True).start()
    return {"ok": True, "status": "started"}


@app.get("/api/run-analysis/status")
def run_status():
    return _run_state


# ---------------- Rebalancing / Drift Monitor (proactive, read-only, suggestions only) ----------------
def _read_policy():
    """Investment policy: target allocation by asset type, per-position cap, drift band.
    Set it via POST /api/policy. Missing keys fall back to defaults."""
    try:
        with open(POLICY_FILE, encoding="utf-8") as f:
            return {**DEFAULT_POLICY, **(json.load(f) or {})}
    except Exception:
        return dict(DEFAULT_POLICY)


@app.get("/api/policy")
def get_policy():
    return _read_policy()


@app.post("/api/policy")
async def post_policy(req: Request):
    """Set the investment policy (local config, not a money action — editable from the dashboard).
    Body may include targets_by_type (e.g. {"Stock":60,"Mutual Fund":35,"Cash":5}), max_position_pct,
    drift_threshold_pct, and position_caps (per-ticker overrides, e.g. {"PLTR":40,"RKLB":40})."""
    body = await req.json()
    pol = {**_read_policy(), **{k: v for k, v in body.items() if k in DEFAULT_POLICY}}
    os.makedirs(os.path.dirname(POLICY_FILE) or ".", exist_ok=True)
    with open(POLICY_FILE, "w", encoding="utf-8") as f:
        json.dump(pol, f, indent=2)
    return {"ok": True, "policy": pol}


_BOND_KW = ("bond", "treasury", "fixed income", "aggregate bond", "govt", "municipal", " muni",
            "tips", "income fund", "core plus", "intermediate-term")
_INTL_KW = ("international", " intl", "ex-us", "ex us", "world ex", "developed markets", "eafe",
            "emerging market", "foreign", "global equity", "europe", "pacific", "japan", "china")
_BOND_TK = {"BND", "AGG", "BNDX", "VGIT", "VCIT", "TLT", "IEF", "MUB", "SCHZ", "FXNAX", "VBTLX", "VTEB"}
_INTL_TK = {"VXUS", "VEU", "VEA", "VWO", "IXUS", "EFA", "EEM", "SCHF", "SCHE", "VT", "ACWI", "VYMI", "VSS"}


def _sleeve(c):
    """Classify a holding into an IPS asset-CLASS sleeve (US Equity / Intl Equity / Bond / Cash / Other).
    Heuristic (type + ticker map + name keywords) — a US-listed equity/fund defaults to US Equity unless
    flagged bond/international. Fund look-through could refine this later; direct-stock country is not fetched."""
    typ = c.get("type") or ""
    if typ == "Cash":
        return "Cash"
    tkr = (c.get("ticker") or "").upper()
    nm = " " + (c.get("name") or "").lower()
    if tkr in _BOND_TK or any(k in nm for k in _BOND_KW):
        return "Bond"
    if tkr in _INTL_TK or any(k in nm for k in _INTL_KW):
        return "Intl Equity"
    if typ in ("Stock", "Mutual Fund", "ETF"):
        return "US Equity"
    return "Other"


def _rebalance():
    """Compare current allocation to the target policy and surface drift + concentration with
    suggested (NOT executed) rebalancing amounts. Persists a snapshot to INSIGHTS_FILE."""
    d = _data()
    invested = d.get("invested_value") or 0
    policy = _read_policy()
    thr = policy.get("drift_threshold_pct") or 5.0
    maxw = policy.get("max_position_pct") or 10.0
    targets = policy.get("targets_by_type") or {}
    caps = policy.get("position_caps") or {}   # per-ticker cap overrides (conviction holdings)
    actual = {x["label"]: (round(x["value"] / invested * 100, 2) if invested else 0)
              for x in (d.get("by_type") or [])}

    alloc = []
    for t, tgt in targets.items():
        act = actual.get(t, 0)
        drift = round(act - tgt, 2)
        alloc.append({"type": t, "target_pct": tgt, "actual_pct": act, "drift_pct": drift,
                      "drift_usd": round(drift / 100 * invested, 2), "flag": abs(drift) >= thr})
    for t, act in actual.items():                       # held types with no explicit target
        if t not in targets:
            alloc.append({"type": t, "target_pct": None, "actual_pct": act,
                          "drift_pct": None, "drift_usd": None, "flag": False})
    alloc.sort(key=lambda x: -(x["actual_pct"] or 0))

    # ---- IPS asset-CLASS sleeve drift (the policy the IPS actually defines) ----
    sleeve_targets = policy.get("targets_by_sleeve") or {}
    # Prefer the look-through-refined asset-class split (funds split into equity/bond/cash by N-PORT
    # assetCat) from the cached look-through; fall back to the whole-fund name heuristic when absent.
    _lt = _read_lt()
    _acv = (_lt or {}).get("asset_class_value") if _lt else None
    if _acv:
        sl_val = dict(_acv)
        sleeve_source = "lookthrough"
    else:
        sl_val = {}
        for c in (d.get("consolidated") or []):
            s = _sleeve(c)
            sl_val[s] = sl_val.get(s, 0) + (c.get("value") or 0)
        sleeve_source = "heuristic"
    sl_actual = {s: (round(v / invested * 100, 2) if invested else 0) for s, v in sl_val.items()}
    ips_alloc = []
    for s, tgt in sleeve_targets.items():
        act = sl_actual.get(s, 0)
        drift = round(act - tgt, 2)
        ips_alloc.append({"sleeve": s, "target_pct": tgt, "actual_pct": act, "drift_pct": drift,
                          "drift_usd": round(drift / 100 * invested, 2), "flag": abs(drift) >= thr})
    for s, act in sl_actual.items():
        if s not in sleeve_targets:
            ips_alloc.append({"sleeve": s, "target_pct": None, "actual_pct": act,
                              "drift_pct": None, "drift_usd": None, "flag": False})
    ips_alloc.sort(key=lambda x: -(x["actual_pct"] or 0))

    conc = []
    for c in (d.get("consolidated") or []):
        w = c.get("weight_pct")
        if w is None:
            continue
        cap = caps.get(c.get("ticker"), maxw)   # the holder's own cap for this name, else the default
        if w > cap:
            conc.append({"ticker": c.get("ticker"), "name": c.get("name"), "weight_pct": w,
                         "cap_pct": cap, "excess_pct": round(w - cap, 2),
                         "trim_usd": round((w - cap) / 100 * invested, 2), "value": c.get("value"),
                         "total_return_pct": c.get("gain_pct"), "day_pct": c.get("day_pct")})
    conc.sort(key=lambda x: -x["weight_pct"])

    suggestions = []
    for a in alloc:
        if a["flag"]:
            verb = "Trim" if a["drift_pct"] > 0 else "Add to"
            suggestions.append(f"{verb} {a['type']} by ~${abs(a['drift_usd']):,.0f} "
                               f"({a['actual_pct']}% vs {a['target_pct']}% target).")
    for a in ips_alloc:
        if a["flag"]:
            verb = "Trim" if a["drift_pct"] > 0 else "Add to"
            suggestions.append(f"IPS sleeve: {verb} {a['sleeve']} by ~${abs(a['drift_usd']):,.0f} "
                               f"({a['actual_pct']}% vs {a['target_pct']}% target).")
    for c in conc:
        ret = c.get("total_return_pct")
        perf = f", up {ret:+.0f}% since cost" if ret is not None else ""
        suggestions.append(f"{c['ticker']} is {c['weight_pct']}% of the portfolio (your cap {c['cap_pct']}%{perf}) "
                           f"— trimming ~${c['trim_usd']:,.0f} brings it to your cap, but realizes gains/taxes and "
                           f"cuts a top performer, so weigh it against your conviction.")
    if not targets:
        suggestions.insert(0, "No target allocation set — define targets_by_type via POST /api/policy "
                              "to enable drift analysis (concentration checks run without it).")

    # ---- DRIP drift pressure: reinvested dividends buy MORE of the same name, growing its
    # weight over time, and are NOT available as cash to fund rebalancing. Estimate the annual
    # same-name inflow from DRIP-on accounts (per-account setting under ACCOUNTS).
    accts_cfg = _read_accounts_cfg()
    dp_briefs = (_read_analysis().get("briefs") or {})
    drip_by_ticker = {}
    for acct in (d.get("by_account") or []):
        if not _account_flags(acct.get("account"), accts_cfg)["drip"]:
            continue
        for h in acct.get("holdings", []):
            tkr, val = h.get("ticker"), h.get("value") or 0
            yld = ((dp_briefs.get(tkr) or {}).get("valuation") or {}).get("dividend_yield_pct")
            if tkr and yld and val > 0:
                drip_by_ticker[tkr] = drip_by_ticker.get(tkr, 0.0) + val * yld / 100.0
    dp_wt = {c.get("ticker"): c.get("weight_pct") for c in (d.get("consolidated") or [])}
    drip_pressure = [{"ticker": t, "est_annual_reinvestment": round(r, 2),
                      "current_weight_pct": dp_wt.get(t),
                      "annual_weight_add_pct": round(r / invested * 100, 3) if invested else None}
                     for t, r in drip_by_ticker.items() if r > 0]
    drip_pressure.sort(key=lambda x: -(x["est_annual_reinvestment"] or 0))
    drip_reinvest_total = round(sum(x["est_annual_reinvestment"] for x in drip_pressure), 2)
    if drip_pressure:
        top = drip_pressure[0]
        suggestions.append(
            f"DRIP note: ~${drip_reinvest_total:,.0f}/yr of dividends auto-reinvest into the SAME holdings "
            f"(biggest: {top['ticker']} ~${top['est_annual_reinvestment']:,.0f}/yr, +{top['annual_weight_add_pct']}% "
            f"weight/yr) — this raises concentration and that cash is NOT available to rebalance. Turn DRIP off "
            f"on a name to let its dividends pool for rebalancing instead.")

    out = {"generated_at": int(time.time()), "invested_value": invested, "policy": policy,
           "allocation": alloc, "ips_allocation": ips_alloc, "ips_sleeve_source": sleeve_source,
           "concentration": conc, "suggestions": suggestions,
           "drip_pressure": drip_pressure[:12], "drip_reinvest_annual_total": drip_reinvest_total,
           "note": "Informational rebalancing analysis — SUGGESTIONS ONLY, no orders are placed. "
                   "'ips_allocation' measures drift against your IPS asset-CLASS sleeves (US/Intl/Bond/Cash), "
                   "which is what the IPS actually specifies; 'allocation' is the older by-Monarch-type view. "
                   "'ips_sleeve_source' = 'lookthrough' when funds are split into equity/bond/cash by SEC "
                   "N-PORT assetCat (run /api/run-lookthrough to enable), else 'heuristic' (whole-fund by name). "
                   "'drip_pressure' estimates how fast DRIP-on holdings grow their own weight (reinvested "
                   "dividends aren't available to rebalance). Consider tax impact (esp. in taxable accounts) "
                   "and transaction costs before acting; consult a licensed professional for decisions."}
    try:
        os.makedirs(os.path.dirname(INSIGHTS_FILE) or ".", exist_ok=True)
        with open(INSIGHTS_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
    except Exception:
        pass
    return out


@app.get("/api/rebalance")
def get_rebalance():
    """Proactive drift & concentration report vs. your policy. Read-only, suggestions only."""
    try:
        return _rebalance()
    except Exception as e:
        return JSONResponse({"error": f"could not compute (portfolio unavailable?): {e}"}, status_code=502)


# ---------------- Retirement / FIRE Planner (proactive, read-only, informational) ----------------
def _read_retire():
    try:
        with open(RETIRE_FILE, encoding="utf-8") as f:
            return {**DEFAULT_RETIRE, **(json.load(f) or {})}
    except Exception:
        return dict(DEFAULT_RETIRE)


@app.get("/api/retirement-config")
def get_retire_config():
    return _read_retire()


@app.post("/api/retirement-config")
async def post_retire_config(req: Request):
    """Set retirement assumptions (local config). Keys: withdrawal_rate_pct, expected_real_return_pct,
    annual_expenses_override, annual_savings_override, current_age, target_age, exclude_account_types."""
    body = await req.json()
    cfg = {**_read_retire(), **{k: v for k, v in body.items() if k in DEFAULT_RETIRE}}
    os.makedirs(os.path.dirname(RETIRE_FILE) or ".", exist_ok=True)
    with open(RETIRE_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return {"ok": True, "config": cfg}


# ---- Tax engine wiring (G5) --------------------------------------------------
# One bracket model (tax.py) feeds every place that used to carry a flat tax knob.
# Env vars still override the derived value when explicitly set, so nothing breaks.

def _tax_ctx(cfg=None):
    """Filing status + ages for the tax engine, from retirement config / env."""
    cfg = cfg if cfg is not None else _read_retire()
    status = os.environ.get("TAX_FILING_STATUS") or cfg.get("filing_status") or "mfj"
    return {"status": tax.norm_status(status),
            "age": cfg.get("current_age"),
            "spouse_age": cfg.get("spouse_age"),
            "fill_to_rate": float(cfg.get("convert_fill_to_rate") or 0.12)}


def _withdrawal_gross(net, status="mfj", age=None, spouse_age=None, iters=8):
    """Solve gross ordinary withdrawal G so that G - federal_tax(G) == net.
    Fixed-point iteration (converges fast; tax is piecewise-linear)."""
    net = max(0.0, float(net))
    g = net
    for _ in range(iters):
        t = tax.effective_rate(g, 0.0, status, age=age, spouse_age=spouse_age,
                               include_niit=False)["total_tax"]
        g = net + t
    eff = (t / g * 100.0) if g else 0.0
    return round(g, 2), round(eff, 2)


def _withdrawal_gross_with_ss(net, gross_ss, status="mfj", age=None, spouse_age=None, iters=12):
    """Gross ORDINARY portfolio withdrawal G needed so that, WITH a gross Social
    Security benefit of `gross_ss`, after-tax income nets `net`:
        (G + gross_ss) - federal_tax(taxable_ord = G + taxable_SS - deduction) == net
    The taxable portion of SS depends on G (provisional income), so this is a fixed
    point — solved by iteration (converges fast; everything is piecewise-linear).
    SS's non-taxable portion is spendable tax-free, which is why a benefit reduces
    the required withdrawal by MORE than its taxable share. NIIT is excluded (a
    Traditional-IRA withdrawal is ordinary income, not net investment income)."""
    net = max(0.0, float(net))
    gross_ss = max(0.0, float(gross_ss))
    ded = tax.standard_deduction(status, age, spouse_age)
    g = max(0.0, net - gross_ss)
    t = taxable_ss = 0.0
    for _ in range(iters):
        taxable_ss = ss.taxable_portion(gross_ss, g, status)
        taxable_ord = max(0.0, g + taxable_ss - ded)
        t = tax.ordinary_tax(taxable_ord, status)
        g = max(0.0, net - gross_ss + t)
    return round(g, 2), round(t, 2), round(taxable_ss, 2)


@app.get("/api/tax")
def get_tax(gross_ordinary: float = 0.0, ltcg: float = 0.0, magi: float = None):
    """Bracket-aware federal tax picture (2026): effective rate, Roth-conversion
    'fill-the-bracket' headroom, 0%-LTCG gain-harvest budget, and low-year flags.
    Read-only / educational — not tax advice. Federal only (TX = no state tax)."""
    ctx = _tax_ctx()
    # default the income input to the household's modeled expenses if not supplied
    if not gross_ordinary:
        try:
            gross_ordinary = float(_read_retire().get("annual_expenses_override") or 0) or 0.0
        except Exception:
            gross_ordinary = 0.0
    try:
        return tax.summary(gross_ordinary=gross_ordinary, ltcg=ltcg,
                           status=ctx["status"], age=ctx["age"], spouse_age=ctx["spouse_age"],
                           fill_to_rate=ctx["fill_to_rate"], magi=magi)
    except Exception as e:
        return JSONResponse({"error": f"tax engine failed: {e}"}, status_code=500)


def _aca_ctx(cfg=None):
    """Household size + benchmark premium for the ACA estimator, from config/env."""
    cfg = cfg if cfg is not None else _read_retire()
    hh = int(os.environ.get("ACA_HOUSEHOLD_SIZE") or cfg.get("household_size") or 2)
    slcsp = os.environ.get("ACA_SLCSP_ANNUAL") or cfg.get("aca_slcsp_annual")
    return {"household_size": hh, "slcsp_annual": float(slcsp) if slcsp else None}


@app.get("/api/aca")
def get_aca(magi: float = None, household_size: int = None, slcsp_annual: float = None,
            planned_conversion: float = 0.0, planned_gains: float = 0.0,
            ira_401k_withdrawals: float = None, dividends: float = 0.0,
            capital_gains: float = 0.0, social_security_total: float = 0.0,
            taxable_interest: float = 0.0, tax_exempt_interest: float = 0.0):
    """ACA MAGI ledger + 2026 premium-subsidy estimate + 400%-FPL cliff picture.
    Pass MAGI components (or a single `magi=`) and a planned conversion/harvest to
    see whether it crosses the cliff. Only relevant pre-Medicare. Educational."""
    ctx = _aca_ctx()
    hh = int(household_size or ctx["household_size"])
    slcsp = slcsp_annual if slcsp_annual is not None else ctx["slcsp_annual"]
    if magi is not None:
        components = {"wages_other_ordinary": float(magi)}   # treat the lump as MAGI
    elif ira_401k_withdrawals is not None or dividends or capital_gains or social_security_total \
            or taxable_interest or tax_exempt_interest:
        components = {"ira_401k_withdrawals": float(ira_401k_withdrawals or 0),
                      "dividends": float(dividends or 0), "capital_gains": float(capital_gains or 0),
                      "social_security_total": float(social_security_total or 0),
                      "taxable_interest": float(taxable_interest or 0),
                      "tax_exempt_interest": float(tax_exempt_interest or 0)}
    else:
        # default: model a bridge-year MAGI as ~one year of expenses funded by a Roth
        # conversion / pre-tax withdrawal (the dominant MAGI source in early retirement)
        exp = float(_read_retire().get("annual_expenses_override") or 0) or 0.0
        components = {"ira_401k_withdrawals": exp}
    try:
        return aca.summary(components=components, household_size=hh, slcsp_annual=slcsp,
                           planned_conversion=planned_conversion, planned_gains=planned_gains)
    except Exception as e:
        return JSONResponse({"error": f"aca estimator failed: {e}"}, status_code=500)


# ---- Social Security wiring (G2) --------------------------------------------
# ss.py estimates an inflation-indexed benefit; here we assemble the household
# inputs from config/env and expose them to the endpoint and the Monte Carlo.

def _ss_ctx(cfg=None):
    """Household Social Security inputs from retirement config / env. `enabled` is
    True only when we have enough to estimate the primary earner (a birth year plus
    at least one benefit input)."""
    cfg = cfg if cfg is not None else _read_retire()

    def _f(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def _i(v):
        try:
            return int(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    self_kwargs = {
        "birth_year": _i(os.environ.get("SS_BIRTH_YEAR") or cfg.get("birth_year")),
        "claim_age": _f(os.environ.get("SS_CLAIM_AGE") or cfg.get("ss_claim_age")),
        "estimated_fra_benefit": _f(os.environ.get("SS_ESTIMATED_FRA_BENEFIT")
                                    or cfg.get("ss_estimated_fra_benefit")),
        "pia_monthly": _f(cfg.get("ss_pia_monthly")),
        "aime": _f(cfg.get("ss_aime")),
    }
    spouse = None
    sby = _i(cfg.get("spouse_birth_year"))
    if sby and (cfg.get("spouse_ss_estimated_fra_benefit") or cfg.get("spouse_ss_pia_monthly")):
        spouse = {"birth_year": sby, "claim_age": _f(cfg.get("spouse_ss_claim_age")),
                  "estimated_fra_benefit": _f(cfg.get("spouse_ss_estimated_fra_benefit")),
                  "pia_monthly": _f(cfg.get("spouse_ss_pia_monthly"))}
    enabled = bool(self_kwargs["birth_year"] and (self_kwargs["estimated_fra_benefit"]
                   or self_kwargs["pia_monthly"] or self_kwargs["aime"]))
    return {"enabled": enabled, "self": self_kwargs, "spouse": spouse}


@app.get("/api/ss")
def get_ss(claim_age: float = None, other_income: float = None):
    """Social Security estimate (2026): benefit at the chosen claim age (62/FRA/70),
    the claim factor vs PIA, FRA by birth year, and the federally taxable portion
    given your other income. Feeds the retirement Monte Carlo as a real income floor.
    Read-only / educational — verify at ssa.gov/myaccount."""
    cfg = _read_retire()
    ctx = _ss_ctx(cfg)
    if not ctx["enabled"]:
        return {"available": False,
                "note": ("Set birth_year and a benefit input in the retirement config: "
                         "ss_estimated_fra_benefit ($/mo at FRA from ssa.gov/myaccount — best), "
                         "or ss_pia_monthly / ss_aime.")}
    tctx = _tax_ctx(cfg)
    self_kwargs = dict(ctx["self"])
    if claim_age is not None:
        self_kwargs["claim_age"] = claim_age
    oi = other_income if other_income is not None else float(cfg.get("annual_expenses_override") or 0)
    try:
        return ss.summary(retire_age=cfg.get("target_age"), status=tctx["status"],
                          other_income=oi, self_kwargs=self_kwargs, spouse=ctx["spouse"])
    except Exception as e:
        return JSONResponse({"error": f"ss estimate failed: {e}"}, status_code=500)


def _investable_assets(d, exclude_types):
    ex = {str(t).lower() for t in (exclude_types or [])}
    total = 0.0
    for a in (d.get("accounts") or []):
        if a.get("is_asset") and str(a.get("type") or "").lower() not in ex:
            total += a.get("balance") or 0
    return round(total, 2)


def _cashflow_annual():
    """Trailing-12-month (income, expenses) as POSITIVE numbers from Monarch's get_cashflow,
    or (None, None). Monarch shape: {cashflow:{summary:[{summary:{sumIncome, sumExpense(neg),
    savings}}], byCategory:[{groupBy.category.group.type, summary.sum}], ...}}."""
    end = datetime.date.today()
    start = end.replace(year=end.year - 1)
    try:
        data = _post("get_cashflow", {"start_date": start.isoformat(), "end_date": end.isoformat()})
    except Exception:
        return None, None
    cf = (data or {}).get("cashflow") or data or {}
    # Preferred: the top-level summary aggregate (sumIncome / sumExpense).
    summ = cf.get("summary")
    node = None
    if isinstance(summ, list) and summ:
        node = (summ[0] or {}).get("summary") or (summ[0] or {})
    elif isinstance(summ, dict):
        node = summ.get("summary") or summ
    if isinstance(node, dict) and ("sumIncome" in node or "sumExpense" in node):
        inc, exp = node.get("sumIncome"), node.get("sumExpense")
        return (abs(inc) if inc is not None else None), (abs(exp) if exp is not None else None)
    # Fallback: sum byCategory by the category-group type.
    inc = exp = 0.0
    found = False
    for e in (cf.get("byCategory") or []):
        t = (((e.get("groupBy") or {}).get("category") or {}).get("group") or {}).get("type")
        s = (e.get("summary") or {}).get("sum") or 0
        if t == "income":
            inc += s
            found = True
        elif t == "expense":
            exp += s
            found = True
    return (abs(inc), abs(exp)) if found else (None, None)


def _years_to_fi(assets, annual_savings, real_return, target):
    """Years until assets + contributions (end-of-year) compound to the FI target. None if >80yrs."""
    if assets >= target:
        return 0
    bal = assets
    for n in range(1, 81):
        bal = bal * (1 + real_return) + (annual_savings or 0)
        if bal >= target:
            return n
    return None


# ---- G3: risk-based guardrails — success-probability lookup table --------------------
# The risk-based policy needs P(plan succeeds | current withdrawal rate, years left) EVERY
# simulated year. A nested Monte Carlo (an inner MC per outer path-year) is ~100M+ path-years
# — infeasible in pure Python — so we precompute a coarse (withdrawal-rate × years-remaining)
# success grid ONCE with the same return model and bilinearly interpolate. Cached in-memory
# per (mean, vol, runs) so repeated /api/retirement calls don't rebuild it.
_SUCCESS_TABLE_CACHE = {}
_RB_RATES = (0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.14, 0.20)   # withdrawal-rate grid
_RB_YEARS = (1, 3, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50)            # years-remaining grid


def _success_table(mean_real, vol, max_years, inner_runs=None):
    """Build (or fetch cached) the success grid and return a lookup fn(rate, years_left)->p.
    success = a portfolio of 1.0 funding a constant real spend of `rate` for `years_left`
    years, returns ~ Normal(mean_real, vol) — the same model as the headline MC."""
    inner_runs = int(inner_runs or _knob("rb_inner_runs"))
    ygrid = tuple(y for y in _RB_YEARS if y <= max(1, int(max_years))) or (1,)
    if ygrid[-1] < max_years:
        ygrid = ygrid + (int(max_years),)
    key = (round(mean_real, 4), round(vol, 4), inner_runs, ygrid[-1])
    if key in _SUCCESS_TABLE_CACHE:
        grid, ygrid = _SUCCESS_TABLE_CACHE[key]
    else:
        grid = {}
        for rate in _RB_RATES:
            for yrs in ygrid:
                succ = 0
                for _ in range(inner_runs):
                    b = 1.0
                    for _y in range(yrs):
                        b -= rate
                        if b <= 0:
                            break
                        b *= (1 + max(-0.95, random.gauss(mean_real, vol)))
                    if b > 0:
                        succ += 1
                grid[(rate, yrs)] = succ / inner_runs
        _SUCCESS_TABLE_CACHE[key] = (grid, ygrid)
        if len(_SUCCESS_TABLE_CACHE) > 8:      # bound the cache (assumption changes evict oldest)
            _SUCCESS_TABLE_CACHE.pop(next(iter(_SUCCESS_TABLE_CACHE)))

    def _interp(axis, v):
        """Neighbors + weight for linear interpolation along a sorted grid axis (clamped)."""
        if v <= axis[0]:
            return axis[0], axis[0], 0.0
        if v >= axis[-1]:
            return axis[-1], axis[-1], 0.0
        for i in range(1, len(axis)):
            if v <= axis[i]:
                lo, hi = axis[i - 1], axis[i]
                return lo, hi, (v - lo) / (hi - lo)
        return axis[-1], axis[-1], 0.0

    def lookup(rate, years_left):
        if years_left <= 0:
            return 1.0
        if rate <= 0:
            return 1.0
        r0, r1, wr = _interp(_RB_RATES, rate)
        y0, y1, wy = _interp(ygrid, years_left)
        p00, p01 = grid[(r0, y0)], grid[(r0, y1)]
        p10, p11 = grid[(r1, y0)], grid[(r1, y1)]
        return (p00 * (1 - wr) + p10 * wr) * (1 - wy) + (p01 * (1 - wr) + p11 * wr) * wy

    return lookup


def _monte_carlo(start, annual_savings, annual_expenses, mean_real, vol, acc_years, ret_years, runs=None,
                 gk_band=0.20, gk_adjust=0.10, draw_mode="normal", spend_schedule=None,
                 rb_success_fn=None, rb_lower=0.70, rb_upper=0.99, rb_adjust=0.10, rb_floor_frac=None):
    """Monte Carlo retirement success probability in REAL (today's-dollar) terms. Each path runs
    `acc_years` of contributions then `ret_years` of withdrawals, annual real returns ~ Normal(mean,vol).
    Runs TWO withdrawal policies over the SAME return sequence per path for a fair comparison:
      • STATIC — constant real withdrawal (the classic 4%-style fixed-spend plan).
      • GUARDRAILS — Guyton-Klinger: if the current withdrawal RATE rises above (1+gk_band)x the
        initial rate (portfolio fell), cut spending gk_adjust; if it falls below (1-gk_band)x
        (portfolio grew), raise spending gk_adjust. Flexible spending lifts the success rate at the
        cost of variable income, which we quantify. Success = portfolio never depleted.

    `spend_schedule` (Social Security floor, G2): optional sorted list of
    (start_year, gross_withdrawal) steps. From `start_year` into retirement the
    portfolio only has to fund `gross_withdrawal` instead of the full `annual_expenses`,
    because an inflation-indexed SS benefit covers the rest. The difference
    (annual_expenses - scheduled draw) is the SS 'cut' applied to BOTH policies —
    static funds the reduced draw directly; guardrails still flex the full lifestyle
    but the portfolio draw is offset by the (fixed) SS floor. Omitting SS understates
    success, which is exactly what this corrects."""
    runs = int(runs or _knob("mc_runs"))
    acc_years = max(0, int(acc_years or 0))
    ret_years = max(1, int(ret_years or 0))
    sav = annual_savings or 0
    _sched = sorted(spend_schedule or [], key=lambda t: t[0])

    def _ss_cut(y):
        """SS-funded reduction (gross $) to the portfolio draw in retirement year y."""
        base = annual_expenses
        for sy, sv in _sched:
            if y >= sy:
                base = sv
        return max(0.0, annual_expenses - base)

    def _dr():
        if draw_mode == "t":                      # Student-t(5), variance-matched to vol → fatter tails
            z = random.gauss(0.0, 1.0)
            g = random.gammavariate(2.5, 2.0)     # chi-square(5) = gamma(shape 2.5, scale 2)
            return mean_real + (z / ((g / 5.0) ** 0.5)) * (vol / (5.0 / 3.0) ** 0.5)
        return random.gauss(mean_real, vol)

    s_succ, s_finals, s_depl = 0, [], []
    g_succ, g_finals = 0, []
    g_final_spend, g_min_spend, g_cut_paths = [], [], 0
    r_succ, r_finals = 0, []
    r_final_spend, r_min_spend, r_cut_paths, r_raise_paths = [], [], 0, 0
    _block_years = int(_knob("mc_block_years"))
    for _ in range(runs):
        # BLOCK-BOOTSTRAP (additive 3rd draw mode): sample one contiguous historical sequence per
        # path for the whole horizon, preserving multi-year serial structure. normal/t paths below
        # are UNCHANGED (still call _dr() inline).
        _seq = (mcblock.block_sequence(acc_years + ret_years, random, mean_real, vol, _block_years)
                if draw_mode == "block" else None)
        bal = start
        for _y in range(acc_years):
            bal = bal * (1 + max(-0.95, (_seq[_y] if _seq is not None else _dr()))) + sav
        start_ret = bal
        draws = ([max(-0.95, x) for x in _seq[acc_years:]] if _seq is not None
                 else [max(-0.95, _dr()) for _ in range(ret_years)])   # shared sequence
        # --- STATIC withdrawal ---
        b, dep = start_ret, None
        for y in range(ret_years):
            b -= max(0.0, annual_expenses - _ss_cut(y))
            if b <= 0:
                dep = y
                b = 0
                break
            b *= (1 + draws[y])
        if dep is None and b > 0:
            s_succ += 1
        else:
            s_depl.append(dep if dep is not None else ret_years)
        s_finals.append(b)
        # --- GUARDRAILS withdrawal (same draws) ---
        init_rate = (annual_expenses / start_ret) if start_ret > 0 else 0
        upper, lower = init_rate * (1 + gk_band), init_rate * (1 - gk_band)
        b, w, dep, minr, cut = start_ret, annual_expenses, None, 1.0, False
        for y in range(ret_years):
            if b > 0 and init_rate > 0:
                rate = w / b
                if rate > upper:
                    w *= (1 - gk_adjust)
                    cut = True
                elif rate < lower:
                    w *= (1 + gk_adjust)
            b -= max(0.0, w - _ss_cut(y))
            if annual_expenses:
                minr = min(minr, w / annual_expenses)
            if b <= 0:
                dep = y
                b = 0
                break
            b *= (1 + draws[y])
        if dep is None and b > 0:
            g_succ += 1
        g_finals.append(b)
        g_final_spend.append((w / annual_expenses) if annual_expenses else 1.0)
        g_min_spend.append(minr)
        if cut:
            g_cut_paths += 1
        # --- RISK-BASED guardrails (G3, same draws): spend flexes on PLAN HEALTH, not on a
        #     fixed rate band. Each year the current withdrawal rate + remaining horizon map
        #     (via the precomputed success table) to P(success); below rb_lower the spend is
        #     cut rb_adjust, above rb_upper it is raised. Cuts FLOOR at rb_floor_frac of the
        #     initial spend — the G1 "needs" line — so flexibility never eats essentials.
        #     Unlike Guyton-Klinger this only cuts when the PLAN is actually at risk, which
        #     avoids GK's needlessly deep cuts after recoverable drawdowns. ---
        if rb_success_fn is not None:
            b, w, dep, minr = start_ret, annual_expenses, None, 1.0
            cut = raised = False
            floor_w = (annual_expenses * rb_floor_frac) if rb_floor_frac else 0.0
            for y in range(ret_years):
                if b > 0:
                    p = rb_success_fn(w / b, ret_years - y)
                    if p is not None:
                        if p < rb_lower and w > floor_w:
                            w = max(floor_w, w * (1 - rb_adjust))
                            cut = True
                        elif p > rb_upper:
                            w *= (1 + rb_adjust)
                            raised = True
                b -= max(0.0, w - _ss_cut(y))
                if annual_expenses:
                    minr = min(minr, w / annual_expenses)
                if b <= 0:
                    dep = y
                    b = 0
                    break
                b *= (1 + draws[y])
            if dep is None and b > 0:
                r_succ += 1
            r_finals.append(b)
            r_final_spend.append((w / annual_expenses) if annual_expenses else 1.0)
            r_min_spend.append(minr)
            if cut:
                r_cut_paths += 1
            if raised:
                r_raise_paths += 1
    s_finals.sort()
    g_finals.sort()
    r_finals.sort()

    def pctl(xs, p):
        if not xs:
            return None
        return round(xs[min(len(xs) - 1, max(0, int(round(p / 100 * (len(xs) - 1)))))], 2)

    def med(xs):
        return round(sorted(xs)[len(xs) // 2], 3) if xs else None

    med_dep = None
    if s_depl:
        s_depl.sort()
        med_dep = s_depl[len(s_depl) // 2]
    res = {"runs": runs, "success_pct": round(s_succ / runs * 100, 1) if runs else None,
           "accumulation_years": acc_years, "retirement_years": ret_years,
           "mean_real_return_pct": round(mean_real * 100, 2), "volatility_pct": round(vol * 100, 2),
           "ending_balance_real": {"p10": pctl(s_finals, 10), "p50": pctl(s_finals, 50), "p90": pctl(s_finals, 90)},
           "median_years_into_retirement_if_depleted": med_dep,
           "guardrails": {
               "success_pct": round(g_succ / runs * 100, 1) if runs else None,
               "band_pct": round(gk_band * 100), "adjust_pct": round(gk_adjust * 100),
               "median_final_spend_pct_of_initial": round((med(g_final_spend) or 1) * 100, 1),
               "median_worst_spend_pct_of_initial": round((med(g_min_spend) or 1) * 100, 1),
               "paths_that_cut_spending_pct": round(g_cut_paths / runs * 100, 1) if runs else None,
               "ending_balance_real": {"p10": pctl(g_finals, 10), "p50": pctl(g_finals, 50), "p90": pctl(g_finals, 90)}}}
    if rb_success_fn is not None and runs:
        res["risk_based"] = {
            "success_pct": round(r_succ / runs * 100, 1),
            "lower_band_pct": round(rb_lower * 100), "upper_band_pct": round(rb_upper * 100),
            "adjust_pct": round(rb_adjust * 100),
            "spend_floor_pct_of_initial": round(rb_floor_frac * 100, 1) if rb_floor_frac else None,
            "median_final_spend_pct_of_initial": round((med(r_final_spend) or 1) * 100, 1),
            "median_worst_spend_pct_of_initial": round((med(r_min_spend) or 1) * 100, 1),
            "paths_that_cut_spending_pct": round(r_cut_paths / runs * 100, 1),
            "paths_that_raised_spending_pct": round(r_raise_paths / runs * 100, 1),
            "ending_balance_real": {"p10": pctl(r_finals, 10), "p50": pctl(r_finals, 50), "p90": pctl(r_finals, 90)},
            "note": ("PRIMARY withdrawal policy: each year the current withdrawal rate + remaining "
                     "horizon map to a probability the plan still succeeds (precomputed success grid, "
                     "same return model); spending cuts "
                     f"{round(rb_adjust * 100)}% only when success drops below {round(rb_lower * 100)}% "
                     f"and rises when it exceeds {round(rb_upper * 100)}%. Cuts never go below "
                     "the essential-needs floor. Guyton-Klinger is kept below as a comparison — its "
                     "fixed rate bands force deeper cuts than plan health actually requires.")}
    return res


def _portfolio_signals(d):
    """Value-weighted analyst-implied 12-mo return and dividend yield, pulled from the stored daily
    briefs (analyst price targets + Finnhub yields) — the 'portfolio intelligence' for the planner."""
    briefs = (_read_analysis().get("briefs") or {})
    cons = d.get("consolidated") or []
    total = sum((c.get("value") or 0) for c in cons)
    if not total:
        return None
    w_ret = ret_cov = w_yld = yld_cov = 0.0
    for c in cons:
        v = c.get("value") or 0
        px = c.get("price")
        b = briefs.get(c.get("ticker")) or {}
        tgt = (b.get("grounded") or {}).get("target_mean")
        if tgt and px and px > 0:
            implied = (tgt - px) / px * 100
            if -90 < implied < 300:          # ignore absurd implied moves
                w_ret += v * implied
                ret_cov += v
        dy = (b.get("valuation") or {}).get("dividend_yield_pct")
        if dy is not None:
            w_yld += v * dy
            yld_cov += v
    return {"analyst_implied_12mo_return_pct": round(w_ret / ret_cov, 2) if ret_cov else None,
            "analyst_coverage_pct": round(ret_cov / total * 100, 1),
            "portfolio_dividend_yield_pct": round(w_yld / total, 4),       # blended over WHOLE portfolio (non-payers = 0)
            "annual_dividend_income": round(w_yld / 100, 2),               # ACTUAL $ dividends from the payers
            "payer_dividend_yield_pct": round(w_yld / yld_cov, 4) if yld_cov else None,
            "dividend_coverage_pct": round(yld_cov / total * 100, 1)}


# ---- G1: Funded ratio — the household-balance-sheet solvency gauge -----------------
# funded ratio = PV(investable + future savings) / PV(retirement spending liability).
# Spending splits into NEEDS (essential — discounted at the RISK-FREE real TIPS yield,
# because an essential liability shouldn't be funded by hoping equity returns show up)
# and WANTS (flexible — discounted at your expected real return). The needs-only ratio
# >= 1.0 is the actuarial "your essentials are funded without equity risk" line.
TIPS_CACHE_FILE = os.environ.get("TIPS_CACHE_FILE", "/data/tips_yield.json")
TIPS_SERIES = os.environ.get("TIPS_SERIES", "DFII10")   # FRED: 10-yr TIPS constant-maturity real yield


def _tips_real_yield():
    """Latest 10-yr TIPS real yield (%) from FRED's public fredgraph CSV (no API key),
    fetched at most once per day (cached to /data). Falls back to the `tips_fallback_pct`
    knob when FRED is unreachable. Returns (yield_pct, source, observation_date)."""
    today = datetime.date.today()
    try:
        with open(TIPS_CACHE_FILE, encoding="utf-8") as f:
            c = json.load(f) or {}
        if c.get("fetched") == today.isoformat() and c.get("yield_pct") is not None:
            return c["yield_pct"], c.get("source") or f"FRED {TIPS_SERIES}", c.get("obs_date")
    except Exception:
        pass
    try:
        r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv",
                         params={"id": TIPS_SERIES,
                                 "cosd": (today - datetime.timedelta(days=45)).isoformat()},
                         timeout=20)
        r.raise_for_status()
        obs = []
        for ln in r.text.strip().splitlines()[1:]:
            parts = ln.split(",")
            if len(parts) >= 2 and parts[1].strip() not in (".", ""):
                obs.append((parts[0].strip(), float(parts[1])))
        if obs:
            dt, y = obs[-1]
            try:
                os.makedirs(os.path.dirname(TIPS_CACHE_FILE) or ".", exist_ok=True)
                with open(TIPS_CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump({"fetched": today.isoformat(), "yield_pct": y,
                               "obs_date": dt, "source": f"FRED {TIPS_SERIES}"}, f)
            except Exception:
                pass
            return y, f"FRED {TIPS_SERIES}", dt
    except Exception:
        pass
    return _knob("tips_fallback_pct"), "fallback knob (FRED unreachable)", None


def _funded_ratio(investable, annual_savings, gross_draw, acc_yrs, ret_yrs,
                  needs_pct, exp_real_pct, ss_schedule=None):
    """Actuarial funded ratio in REAL terms, matching the Monte Carlo's cash flows:
    the liability is each retirement year's GROSS portfolio draw (tax gross-up and
    Social-Security offsets already applied via `gross_draw`/`ss_schedule`), split
    needs/wants and discounted from TODAY. Assets = investable now + future savings
    discounted risk-free (conservative: paychecks aren't equity). Returns a dict."""
    tips_pct, tips_src, tips_asof = _tips_real_yield()
    r_needs = max(0.0, float(tips_pct or 0)) / 100.0
    r_wants = max(r_needs, float(exp_real_pct or 0) / 100.0)   # never discount wants below risk-free
    needs_frac = min(1.0, max(0.0, float(needs_pct or 65) / 100.0))
    sched = sorted(ss_schedule or [], key=lambda t: t[0])

    def _draw(y):
        base = gross_draw
        for sy, sv in sched:
            if y >= sy:
                base = sv
        return max(0.0, base)

    pv_needs = pv_wants = 0.0
    for y in range(int(ret_yrs)):
        t = acc_yrs + y + 1                       # end-of-year convention, years from today
        dr = _draw(y)
        pv_needs += (dr * needs_frac) / ((1 + r_needs) ** t)
        pv_wants += (dr * (1 - needs_frac)) / ((1 + r_wants) ** t)
    pv_savings = sum((annual_savings or 0) / ((1 + r_needs) ** t)
                     for t in range(1, int(acc_yrs) + 1))
    pv_assets = (investable or 0) + pv_savings
    pv_liab = pv_needs + pv_wants
    fr = round(pv_assets / pv_liab, 3) if pv_liab > 0 else None
    fr_ess = round(pv_assets / pv_needs, 3) if pv_needs > 0 else None
    return {"funded_ratio": fr, "essential_funded_ratio": fr_ess,
            "pv_assets": round(pv_assets, 2), "pv_investable": round(investable or 0, 2),
            "pv_future_savings": round(pv_savings, 2),
            "pv_liability": round(pv_liab, 2), "pv_needs": round(pv_needs, 2),
            "pv_wants": round(pv_wants, 2),
            "needs_pct_of_expenses": round(needs_frac * 100, 1),
            "discount": {"needs_real_pct": round(r_needs * 100, 2),
                         "wants_real_pct": round(r_wants * 100, 2),
                         "tips_source": tips_src, "tips_asof": tips_asof},
            "note": ("Funded ratio = PV(investable + future savings) ÷ PV(retirement spending, "
                     "gross of tax, net of the Social Security floor), all in today's dollars. "
                     "NEEDS are discounted at the live 10-yr TIPS real yield (risk-free matching "
                     "for essentials); WANTS at your expected real return. ≥1.0 = fully funded; "
                     "the ESSENTIAL ratio ≥1.0 means needs are covered without equity risk. "
                     "Tracked daily beside net worth. Informational, not advice.")}


def _snapshot_funded(fr):
    """Merge today's funded ratio into the daily value-history row (idempotent per day),
    so solvency accrues a history right beside net worth. Best-effort, never raises."""
    if fr is None:
        return
    try:
        hist = _read_value_history()
        today = datetime.date.today().isoformat()
        if hist and hist[-1].get("date") == today:
            hist[-1]["funded_ratio"] = fr
            os.makedirs(os.path.dirname(VALUE_HISTORY_FILE) or ".", exist_ok=True)
            with open(VALUE_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(hist, f)
    except Exception:
        pass


def _retirement():
    d = _data()
    cfg = _read_retire()
    wr = (cfg.get("withdrawal_rate_pct") or 4.0) / 100.0
    r = (cfg.get("expected_real_return_pct") or 5.0) / 100.0
    investable = _investable_assets(d, cfg.get("exclude_account_types"))

    exp_override = cfg.get("annual_expenses_override")
    sav_override = cfg.get("annual_savings_override")
    cf_inc, cf_exp = (None, None)
    if exp_override is None or sav_override is None:
        cf_inc, cf_exp = _cashflow_annual()
    annual_expenses = exp_override if exp_override is not None else cf_exp
    exp_source = "override" if exp_override is not None else ("monarch_cashflow" if cf_exp is not None else None)
    if sav_override is not None:
        annual_savings = sav_override
    elif cf_inc is not None and cf_exp is not None:
        annual_savings = round(cf_inc - cf_exp, 2)
    else:
        annual_savings = None

    notes = [
        "Investable assets exclude your home equity (you live there) per FIRE convention.",
        "FI number = annual expenses / withdrawal rate; projection is in today's dollars using a REAL return.",
        "For an early (40+ year) retirement, consider a 3.25-3.5% withdrawal rate vs. the classic 4% for safety.",
        "Accessing retirement funds before 59.5 (Pub 590-B): a 72(t)/SEPP or a Roth conversion ladder avoids the "
        "10% penalty — the ladder needs ~5 years of lead time and taxable/Roth funds to bridge the gap.",
        "Tax location matters: taxable brokerage is accessible anytime; Traditional 401k/IRA is pre-tax; Roth "
        "contributions (not earnings) are withdrawable anytime.",
        "Markets vary and sequence-of-returns risk is real near retirement. Informational only, not advice.",
    ]
    assumptions = {"withdrawal_rate_pct": cfg.get("withdrawal_rate_pct"),
                   "expected_real_return_pct": cfg.get("expected_real_return_pct"),
                   "annual_expenses": annual_expenses, "expenses_source": exp_source,
                   "annual_savings": annual_savings, "current_age": cfg.get("current_age"),
                   "target_age": cfg.get("target_age"),
                   "income_target_yield_pct": cfg.get("income_target_yield_pct"),
                   "optimistic_real_return_pct": cfg.get("optimistic_real_return_pct"),
                   "volatility_pct": cfg.get("volatility_pct") or MC_VOLATILITY_PCT,
                   "needs_pct_of_expenses": cfg.get("needs_pct_of_expenses"),
                   # Tax / ACA / SS config echoes so the PLAN editor can prefill them (G1/G2/G5).
                   "filing_status": cfg.get("filing_status") or "mfj",
                   "convert_fill_to_rate": cfg.get("convert_fill_to_rate"),
                   "household_size": cfg.get("household_size"),
                   "aca_slcsp_annual": cfg.get("aca_slcsp_annual"),
                   "birth_year": cfg.get("birth_year"),
                   "ss_claim_age": cfg.get("ss_claim_age"),
                   "ss_estimated_fra_benefit": cfg.get("ss_estimated_fra_benefit"),
                   "spouse_birth_year": cfg.get("spouse_birth_year"),
                   "spouse_ss_claim_age": cfg.get("spouse_ss_claim_age"),
                   "spouse_ss_estimated_fra_benefit": cfg.get("spouse_ss_estimated_fra_benefit")}

    if not annual_expenses:
        return {"generated_at": int(time.time()), "investable_assets": investable, "fi_number": None,
                "assumptions": assumptions, "notes": notes,
                "note": "Set annual_expenses_override in the retirement config (Monarch cash-flow expenses "
                        "weren't available) — the FI number needs your target annual expenses."}

    fi_number = round(annual_expenses / wr, 2)
    progress_pct = round(investable / fi_number * 100, 1) if fi_number else None
    years = _years_to_fi(investable, annual_savings or 0, r, fi_number)
    fi_year = (datetime.date.today().year + years) if years is not None else None
    age = cfg.get("current_age")
    fi_age = (age + years) if (age is not None and years is not None) else None

    coast = None
    if age is not None and cfg.get("target_age"):
        yrs = max(0, int(cfg["target_age"]) - int(age))
        coast_number = round(fi_number / ((1 + r) ** yrs), 2)
        coast = {"target_age": cfg["target_age"], "coast_number": coast_number,
                 "reached": investable >= coast_number,
                 "note": f"Coast-FI: if you stop contributing today, ${investable:,.0f} grows toward the FI "
                         f"number by age {cfg['target_age']} (need ~${coast_number:,.0f})."}

    sens = []
    if annual_savings:
        for mult, lbl in ((1.0, "current"), (1.25, "+25%"), (1.5, "+50%")):
            sens.append({"savings_rate": lbl, "annual_savings": round(annual_savings * mult, 0),
                         "years_to_fi": _years_to_fi(investable, annual_savings * mult, r, fi_number)})

    # ---- portfolio-aware scenarios + income mode (from stored analyst targets & dividend yields) ----
    signals = _portfolio_signals(d)
    scenarios = [{"name": "conservative", "real_return_pct": cfg.get("expected_real_return_pct"),
                  "years_to_fi": years, "basis": "flat assumed real return"}]
    opt = cfg.get("optimistic_real_return_pct")
    if opt:
        scenarios.append({"name": "optimistic", "real_return_pct": opt,
                          "years_to_fi": _years_to_fi(investable, annual_savings or 0, opt / 100, fi_number),
                          "basis": "your optimistic real-return assumption — illustrative, NOT a forecast; a "
                                   "concentrated growth portfolio is volatile and won't compound smoothly."})
    if signals and signals.get("analyst_implied_12mo_return_pct") is not None:
        impl = signals["analyst_implied_12mo_return_pct"]
        real_impl = round(max(0.0, min(impl - 3.0, 12.0)), 2)   # target upside minus ~3% inflation, capped 12% real
        scenarios.append({"name": "analyst-implied", "real_return_pct": real_impl,
                          "raw_12mo_target_upside_pct": impl,
                          "years_to_fi": _years_to_fi(investable, annual_savings or 0, real_impl / 100, fi_number),
                          "basis": "analyst 12-mo price targets, inflation-adjusted & capped at 12% real",
                          "caveat": "Optimistic — assumes analyst targets repeat annually; NOT a forecast. Your "
                                    "concentration in a few high-growth names makes the forward path volatile."})

    target_yield = cfg.get("income_target_yield_pct") or 3.5
    cur_yield = (signals or {}).get("portfolio_dividend_yield_pct")        # blended, honest (~0.3% for growth)
    cur_income = (signals or {}).get("annual_dividend_income")             # actual $ from payers
    realloc_income = round(investable * target_yield / 100, 2)
    div_fi_number = round(annual_expenses / (target_yield / 100), 2)
    dsplit = _dividend_drip_split()
    income = {"current_weighted_yield_pct": cur_yield, "current_annual_income": cur_income,
              "target_yield_pct": target_yield, "income_at_target_yield": realloc_income,
              "covers_expenses_pct": round(realloc_income / annual_expenses * 100, 1) if annual_expenses else None,
              "dividend_fi_number": div_fi_number,
              # DRIP split: reinvested dividends compound but are NOT spendable now; only
              # cash (DRIP-off) dividends are income you can actually live on today.
              "dividend_reinvested_annual": dsplit["reinvested"],
              "dividend_cash_annual": dsplit["cash"],
              "spendable_covers_expenses_pct": round(dsplit["cash"] / annual_expenses * 100, 1) if annual_expenses else None,
              "any_drip": dsplit["any_drip"],
              "note": f"Your equity is growth-tilted, so today's portfolio yields only ~${(cur_income or 0):,.0f}/yr "
                      f"in dividends" + (f", of which ~${dsplit['cash']:,.0f} is CASH you could spend and "
                      f"~${dsplit['reinvested']:,.0f} is auto-reinvested (DRIP — compounding, not spendable now). "
                      if dsplit["any_drip"] else ". ") +
                      f"Reallocating investable assets to a {target_yield}% yield would generate "
                      f"~${realloc_income:,.0f}/yr vs ${annual_expenses:,.0f} of expenses; you'd need "
                      f"~${div_fi_number:,.0f} invested to live on dividends alone at that yield. To live on "
                      f"dividends you'd also turn DRIP OFF so they arrive as cash."}

    # ---- Monte Carlo success probability (real terms; sequence-of-returns risk) ----
    monte_carlo = None
    bridge = None
    social_security = None
    funded = None
    if annual_expenses:
        if age is not None and cfg.get("target_age"):
            acc_yrs = max(0, int(cfg["target_age"]) - int(age))
            retire_age = int(cfg["target_age"])
        elif years is not None:
            acc_yrs = years
            retire_age = (age + years) if age is not None else None
        else:
            acc_yrs, retire_age = 0, age
        ret_yrs = (MC_END_AGE - retire_age) if retire_age is not None else MC_RETIREMENT_YEARS
        vol_pct = cfg.get("volatility_pct") or MC_VOLATILITY_PCT
        # decumulation taxes: most of the money is pre-tax (Traditional), so to NET expenses you must
        # withdraw more — gross the spend up by the tax on the withdrawal (else success% is optimistic).
        # Derived from the bracket engine (G5) so the rate tracks the actual spend level, not a flat guess.
        # MC_WITHDRAWAL_TAX_PCT still overrides when explicitly set.
        _tctx = _tax_ctx(cfg)
        if os.environ.get("MC_WITHDRAWAL_TAX_PCT"):
            mc_tax = float(os.environ["MC_WITHDRAWAL_TAX_PCT"])
            mc_expenses = annual_expenses / (1 - mc_tax / 100.0) if mc_tax else annual_expenses
        else:
            mc_expenses, mc_tax = _withdrawal_gross(annual_expenses, _tctx["status"],
                                                    _tctx["age"], _tctx["spouse_age"])

        # ---- Social Security floor (G2): reduce the portfolio draw from each claim age ----
        # An inflation-indexed benefit is a real income floor, so from a person's claim
        # age onward the portfolio only has to gross-up (annual_expenses - net SS). We
        # build a phase schedule of gross withdrawals and feed it to the Monte Carlo so
        # success isn't understated by pretending SS doesn't exist.
        social_security = None
        ss_schedule = None
        try:
            _ssctx = _ss_ctx(cfg)
            if _ssctx["enabled"] and retire_age is not None:
                _ssum = ss.summary(retire_age=retire_age, status=_tctx["status"],
                                   other_income=annual_expenses,
                                   self_kwargs=_ssctx["self"], spouse=_ssctx["spouse"])
                claimants = sorted(_ssum.get("mc_floor_schedule") or [],
                                   key=lambda c: c["start_year"])
                cum_gross_ss = 0.0
                sched = []
                for c in claimants:
                    cum_gross_ss += (c.get("annual_benefit") or 0)
                    gw, _t, _txss = _withdrawal_gross_with_ss(
                        annual_expenses, cum_gross_ss, _tctx["status"],
                        _tctx["age"], _tctx["spouse_age"])
                    sched.append((int(c["start_year"]), gw))
                ss_schedule = sched or None
                _ssum["mc_spend_schedule"] = [
                    {"start_year": s, "gross_portfolio_withdrawal": w,
                     "annual_ss_offset": round(mc_expenses - w, 2)} for s, w in (sched or [])]
                social_security = _ssum
        except Exception as e:
            social_security = {"available": False, "error": f"ss floor failed: {e}"}
            ss_schedule = None

        # G3: precompute the success grid for the risk-based policy (cached; coarse inner MC).
        try:
            _rb_fn = _success_table(r, vol_pct / 100.0, ret_yrs)
        except Exception:
            _rb_fn = None
        monte_carlo = _monte_carlo(investable, annual_savings or 0, mc_expenses, r,
                                   vol_pct / 100.0, acc_yrs, ret_yrs, spend_schedule=ss_schedule,
                                   rb_success_fn=_rb_fn,
                                   rb_lower=_knob("rb_lower_band") / 100.0,
                                   rb_upper=_knob("rb_upper_band") / 100.0,
                                   rb_adjust=_knob("rb_adjust_pct") / 100.0,
                                   rb_floor_frac=(cfg.get("needs_pct_of_expenses") or 65) / 100.0)
        monte_carlo["withdrawal_tax_pct"] = mc_tax
        monte_carlo["gross_annual_withdrawal"] = round(mc_expenses, 2)
        monte_carlo["tax_model"] = ("bracket-engine 2026 (%s)" % _tctx["status"]
                                    if not os.environ.get("MC_WITHDRAWAL_TAX_PCT")
                                    else "env MC_WITHDRAWAL_TAX_PCT")
        monte_carlo["horizon"] = {"current_age": age, "retirement_age": retire_age,
                                  "end_age": (retire_age + ret_yrs) if retire_age is not None else None}
        if social_security and social_security.get("household_annual_benefit"):
            monte_carlo["social_security_floor"] = {
                "household_annual_benefit": social_security["household_annual_benefit"],
                "schedule": social_security.get("mc_spend_schedule"),
                "note": ("Portfolio draw is reduced by an inflation-indexed Social Security floor "
                         "from each claim age onward; without it this success rate would be understated."),
            }
        monte_carlo["note"] = (
            f"Probability the portfolio is never depleted across {monte_carlo['runs']} simulated paths "
            f"(REAL/today's dollars; annual returns drawn from Normal({round(r * 100, 1)}%, "
            f"{vol_pct:.0f}%), constant real spending of ${annual_expenses:,.0f}/yr). Unlike the "
            f"flat-return projection, this captures sequence-of-returns risk. Your portfolio is concentrated "
            f"in a few high-growth names, so real-world volatility is likely HIGHER than the assumption here "
            f"— treat the success rate as optimistic. Informational, not advice.")
        try:
            _ft = _monte_carlo(investable, annual_savings or 0, mc_expenses, r,
                               vol_pct / 100.0, acc_yrs, ret_yrs, draw_mode="t",
                               spend_schedule=ss_schedule)
            monte_carlo["fat_tail"] = {
                "draw": "student_t_nu5",
                "success_pct": _ft.get("success_pct"),
                "guardrails_success_pct": (_ft.get("guardrails") or {}).get("success_pct"),
                "note": "Same plan under fatter-tailed Student-t(5) draws instead of Normal (variance matched). "
                        "The gap below the headline Normal success rate is the tail/sequence risk the Normal "
                        "model hides — treat this lower number as the more conservative read."}
        except Exception:
            monte_carlo["fat_tail"] = None
        try:
            _bb = _monte_carlo(investable, annual_savings or 0, mc_expenses, r,
                               vol_pct / 100.0, acc_yrs, ret_yrs, draw_mode="block",
                               spend_schedule=ss_schedule)
            _bs = mcblock.source_stats()
            monte_carlo["block_bootstrap"] = {
                "draw": "block_bootstrap_hist_sp500",
                "block_years": int(_knob("mc_block_years")),
                "success_pct": _bb.get("success_pct"),
                "guardrails_success_pct": (_bb.get("guardrails") or {}).get("success_pct"),
                "source": _bs["source"],
                "note": "Same plan, but each path samples CONTIGUOUS multi-year blocks of the real "
                        "historical return series (rescaled to your mean/vol) instead of independent "
                        "Normal draws — so a simulated retirement can inherit an actual bad run "
                        "(1929-32, 1973-74, 2000-02, 2007-09). Captures the sequence clustering the "
                        "Normal model can't; a lower success rate here is the more honest read."}
        except Exception:
            monte_carlo["block_bootstrap"] = None
        try:
            bridge = _bridge_plan(annual_expenses, age, retire_age)
        except Exception:
            bridge = None
        # ---- Funded ratio (G1): same cash flows as the MC, valued as a balance sheet ----
        try:
            funded = _funded_ratio(investable, annual_savings, mc_expenses, acc_yrs, ret_yrs,
                                   cfg.get("needs_pct_of_expenses"),
                                   cfg.get("expected_real_return_pct") or r * 100,
                                   ss_schedule=ss_schedule)
            _snapshot_funded((funded or {}).get("funded_ratio"))
        except Exception:
            funded = None

    return {"generated_at": int(time.time()), "investable_assets": investable, "fi_number": fi_number,
            "progress_pct": progress_pct, "years_to_fi": years, "fi_year": fi_year, "fi_age": fi_age,
            "annual_savings": annual_savings, "coast_fi": coast, "sensitivity": sens,
            "portfolio_signals": signals, "scenarios": scenarios, "income": income,
            "monte_carlo": monte_carlo, "bridge": bridge, "social_security": social_security,
            "funded_ratio": funded,
            "assumptions": assumptions, "notes": notes,
            "note": "Informational FIRE projection — not financial advice. Tune assumptions in the config."}


@app.get("/api/retirement")
def get_retirement():
    """Proactive retirement / financial-independence projection. Read-only, informational."""
    try:
        return _retirement()
    except Exception as e:
        return JSONResponse({"error": f"could not compute (portfolio/cashflow unavailable?): {e}"}, status_code=502)


def _income_glide():
    """Glide path from a low-yield growth book to a TARGET ANNUAL INCOME without drawing principal.
    Models a year-by-year shift growth->income sleeve, capped by a realized-capital-gains budget, and
    reports income coverage by year. Read-only, educational — NOT tax/investment advice."""
    d = _data()
    invested = d.get("invested_value") or 0
    if not invested:
        return {"status": "no_data", "note": "No investable assets to model."}
    rcfg = _read_retire()
    target = float(os.environ.get("INCOME_TARGET") or rcfg.get("income_target") or 200000)
    yld = float(os.environ.get("INCOME_YIELD_PCT", "4.0")) / 100.0
    # Gains budget: derived from the bracket engine (G5) as the 0%-LTCG harvest room
    # given the household's other ordinary income — the amount of gains realizable
    # tax-free each year. ANNUAL_GAINS_BUDGET still overrides when explicitly set.
    _tctx = _tax_ctx(rcfg)
    _other_ord = float(rcfg.get("annual_expenses_override") or 0) or 0.0
    _hb = tax.gains_harvest_budget(_other_ord, _tctx["status"],
                                   age=_tctx["age"], spouse_age=_tctx["spouse_age"])
    gains_budget_src = "bracket-engine 0%-LTCG room (%s)" % _tctx["status"]
    if os.environ.get("ANNUAL_GAINS_BUDGET"):
        gains_budget = float(os.environ["ANNUAL_GAINS_BUDGET"])
        gains_budget_src = "env ANNUAL_GAINS_BUDGET"
    else:
        gains_budget = _hb["gains_at_0pct_budget"]
    grow = float(os.environ.get("INCOME_GROWTH_REAL_PCT", "5.0")) / 100.0
    horizon = int(os.environ.get("INCOME_HORIZON_YEARS", "30"))
    briefs = (_read_analysis().get("briefs") or {})
    cur_income = 0.0
    for c in d.get("consolidated", []):
        v = c.get("value") or 0
        y = ((briefs.get(c.get("ticker")) or {}).get("valuation") or {}).get("dividend_yield_pct")
        if y:
            cur_income += v * y / 100.0
    capital_needed = round(target / yld, 2) if yld else None
    tot_gain = d.get("total_unrealized_gain") or 0
    gain_frac = max(0.0, min(0.95, tot_gain / invested)) if invested else 0.0
    income_cap = (cur_income / yld) if yld else 0.0              # capital already generating income (implied)
    growth_cap = max(0.0, invested - income_cap)
    max_shift = (gains_budget / gain_frac) if gain_frac > 0.01 else growth_cap
    sched, ic, gc, yr_full = [], income_cap, growth_cap, None
    year0 = datetime.date.today().year
    for y in range(0, horizon + 1):
        income = ic * yld
        cov = round(income / target * 100, 1) if target else None
        sched.append({"year": year0 + y, "t": y, "income": round(income, 0),
                      "coverage_pct": cov, "income_capital": round(ic, 0), "growth_capital": round(gc, 0)})
        if cov is not None and cov >= 100 and yr_full is None:
            yr_full = year0 + y
        if y == horizon or (yr_full and year0 + y >= yr_full):
            break
        shift = min(max_shift, gc)
        gc = max(0.0, gc - shift) * (1 + grow)
        ic = ic + shift                                          # income sleeve held flat in real terms
    return {"status": "ready", "income_target": target, "target_yield_pct": round(yld * 100, 2),
            "capital_needed": capital_needed, "current_income": round(cur_income, 0),
            "current_coverage_pct": round(cur_income / target * 100, 1) if target else None,
            "annual_gains_budget": gains_budget, "annual_gains_budget_source": gains_budget_src,
            "embedded_gain_pct": round(gain_frac * 100, 1),
            "annual_shift_cap": round(max_shift, 0), "year_full_coverage": yr_full,
            "years_to_full_coverage": (yr_full - year0) if yr_full else None, "schedule": sched,
            "note": ("Path from your low-yield growth book to $%s/yr income (at a %.1f%%-yield income sleeve) "
                     "WITHOUT selling principal: each year shift up to ~$%s of value growth->income, capped so "
                     "realized gains stay within your ~$%s/yr budget (embedded gain ~%.0f%% of value). Income "
                     "sleeve held flat in real terms (conservative). Coverage = income / target. Set INCOME_* "
                     "and ANNUAL_GAINS_BUDGET to your real numbers; a CPA should size the gains budget to your "
                     "bracket. Educational, not advice.")
                    % (f"{int(target):,}", yld * 100, f"{int(max_shift):,}", f"{int(gains_budget):,}", gain_frac * 100)}


@app.get("/api/income-plan")
def get_income_plan():
    try:
        return _income_glide()
    except Exception as e:
        return JSONResponse({"error": f"could not compute: {e}"}, status_code=502)


# ---------------- Opportunity Scanner (proactive, read-only, research ideas only) ----------------
def _read_scanner():
    try:
        with open(SCANNER_FILE, encoding="utf-8") as f:
            cfg = {**DEFAULT_SCANNER, **(json.load(f) or {})}
        cfg["criteria"] = {**DEFAULT_SCANNER["criteria"], **(cfg.get("criteria") or {})}
        return cfg
    except Exception:
        return json.loads(json.dumps(DEFAULT_SCANNER))


@app.get("/api/scanner-config")
def get_scanner_config():
    return _read_scanner()


@app.post("/api/scanner-config")
async def post_scanner_config(req: Request):
    """Set the opportunity-scanner config (local). Keys: watchlist (list of tickers),
    include_holding_peers (bool), criteria (thresholds), strategy_description."""
    body = await req.json()
    cfg = {**_read_scanner(), **{k: v for k, v in body.items() if k in DEFAULT_SCANNER}}
    if "criteria" in body and isinstance(body["criteria"], dict):
        cfg["criteria"] = {**DEFAULT_SCANNER["criteria"], **body["criteria"]}
    os.makedirs(os.path.dirname(SCANNER_FILE) or ".", exist_ok=True)
    with open(SCANNER_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return {"ok": True, "config": cfg}


def _read_accounts_cfg():
    """Per-account settings: {"defaults": {drip, fractional_ok}, "accounts": {name: {...}}}."""
    try:
        with open(ACCOUNTS_FILE, encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except Exception:
        cfg = {}
    return {"defaults": {**DEFAULT_ACCOUNT_FLAGS, **(cfg.get("defaults") or {})},
            "accounts": cfg.get("accounts") or {}}


def _account_flags(name, cfg=None):
    """Resolved DRIP / fractional flags for one account (stored override else default)."""
    cfg = cfg or _read_accounts_cfg()
    a = (cfg.get("accounts") or {}).get(name) or {}
    dfl = cfg.get("defaults") or DEFAULT_ACCOUNT_FLAGS
    return {"drip": bool(a.get("drip", dfl.get("drip", False))),
            "fractional_ok": bool(a.get("fractional_ok", dfl.get("fractional_ok", True)))}


@app.get("/api/accounts-config")
def get_accounts_config():
    """Every known account with its resolved DRIP / fractional flags, for the UI toggles."""
    d = _data()
    cfg = _read_accounts_cfg()
    rows = []
    for a in d.get("accounts", []):
        name = a.get("name")
        st = _account_tax_status(name, a.get("type"))
        fl = _account_flags(name, cfg)
        rows.append({"name": name, "type": a.get("type"), "institution": a.get("institution"),
                     "tax_status": st, "balance": a.get("balance"),
                     "drip": fl["drip"], "fractional_ok": fl["fractional_ok"],
                     "locatable": st != "na"})
    rows.sort(key=lambda x: -(x.get("balance") or 0))
    return {"defaults": cfg["defaults"], "accounts": rows}


@app.post("/api/accounts-config")
async def post_accounts_config(req: Request):
    """Set per-account DRIP / fractional flags (local config; not a money action).
    Body: {"account": name, "drip": bool, "fractional_ok": bool} for one account,
    and/or {"accounts": {name: {...}}} bulk, and/or {"defaults": {...}}."""
    body = await req.json()
    cfg = _read_accounts_cfg()
    if isinstance(body.get("defaults"), dict):
        cfg["defaults"] = {**cfg["defaults"],
                           **{k: bool(v) for k, v in body["defaults"].items() if k in DEFAULT_ACCOUNT_FLAGS}}
    if body.get("account"):
        cur = dict(cfg["accounts"].get(body["account"]) or {})
        for k in DEFAULT_ACCOUNT_FLAGS:
            if k in body:
                cur[k] = bool(body[k])
        cfg["accounts"][body["account"]] = cur
    if isinstance(body.get("accounts"), dict):
        for nm, v in body["accounts"].items():
            if not isinstance(v, dict):
                continue
            cur = dict(cfg["accounts"].get(nm) or {})
            for k in DEFAULT_ACCOUNT_FLAGS:
                if k in v:
                    cur[k] = bool(v[k])
            cfg["accounts"][nm] = cur
    os.makedirs(os.path.dirname(ACCOUNTS_FILE) or ".", exist_ok=True)
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return {"ok": True, "config": cfg}


@app.get("/api/settings")
def get_settings():
    """Advanced knobs with resolved value + source (env/settings/default) for the SETTINGS tab.
    Secrets / provider credentials are NOT exposed here — they stay in .env (Phase 2)."""
    stored = _read_settings()
    out = []
    for key, spec in SETTING_SPECS.items():
        env_set = os.environ.get(spec["env"]) not in (None, "")
        source = "env" if env_set else ("settings" if stored.get(key) is not None else "default")
        out.append({"key": key, "label": spec["label"], "group": spec["group"],
                    "value": _knob(key), "default": spec["default"],
                    "source": source, "env_locked": env_set, "env": spec["env"]})
    cfg_out = []
    for spec in CONFIG_SPECS:
        k = spec["key"]
        env_locked = k in _ENV_SECRETS_AT_BOOT
        if env_locked:
            val, source = os.environ.get(k), "env"
        elif stored.get(k) not in (None, ""):
            val, source = stored.get(k), "settings"
        else:
            val, source = spec["default"], "default"
        cfg_out.append({"key": k, "label": spec["label"], "group": spec["group"], "value": val,
                        "default": spec["default"], "source": source, "env_locked": env_locked,
                        "editable": not env_locked, "applies": "restart"})
    return {"settings": out, "config": cfg_out,
            "note": "Analysis knobs apply at request time (no restart). Model config applies on the next "
                    "RESTART. ENV vars always win. API keys / provider credentials live in the SECRETS tab / .env."}


@app.post("/api/settings")
async def post_settings(req: Request):
    """Set advanced knobs (local settings.json). ENV-set knobs are ignored (env wins) and
    reported as env_locked. Blank clears back to default. Config only — no money action."""
    body = await req.json()
    stored = _read_settings()
    applied, ignored = [], []
    for key, val in (body or {}).items():
        if key not in SETTING_SPECS:
            continue
        if os.environ.get(SETTING_SPECS[key]["env"]) not in (None, ""):
            ignored.append(key)   # env override wins — don't shadow it
            continue
        if val in (None, ""):
            stored.pop(key, None)
            applied.append(key)
        else:
            try:
                stored[key] = SETTING_SPECS[key]["cast"](val)
                applied.append(key)
            except (TypeError, ValueError):
                continue
    # Non-secret model/infra config (stored under ENV name; applies on RESTART).
    cfg_keys = {s["key"] for s in CONFIG_SPECS}
    restart_needed = []
    for key, val in (body or {}).items():
        if key not in cfg_keys:
            continue
        if key in _ENV_SECRETS_AT_BOOT:
            ignored.append(key)   # real .env wins
            continue
        if val in (None, ""):
            stored.pop(key, None)
        else:
            stored[key] = str(val)
        applied.append(key)
        restart_needed.append(key)
    os.makedirs(os.path.dirname(SETTINGS_FILE) or ".", exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(stored, f, indent=2)
    return {"ok": True, "applied": applied, "ignored_env_locked": ignored,
            "restart_required": restart_needed, "settings": stored}


# ---- Secrets editor (N6 Phase 2): write-only, masked. Values are NEVER returned. ----
# scope "app"  = portfolio-api reads it directly -> editable here, applies on RESTART.
# scope "mcpo" = used by the mcpo container -> NOT editable here (set in .env + restart mcpo).
SECRET_SPECS = [
    {"key": "FINNHUB_API_KEY",       "label": "Finnhub API key",                 "group": "Market data",            "scope": "app"},
    {"key": "SEC_USER_AGENT",        "label": "SEC EDGAR User-Agent (your email)", "group": "Market data",          "scope": "app"},
    {"key": "ANALYSIS_TOKEN",        "label": "Analysis token (guards /api/run-*)", "group": "Internal",            "scope": "app"},
    {"key": "SNAPTRADE_CLIENT_ID",   "label": "SnapTrade client ID",             "group": "Portfolio (SnapTrade)",  "scope": "app"},
    {"key": "SNAPTRADE_CONSUMER_KEY","label": "SnapTrade consumer key",          "group": "Portfolio (SnapTrade)",  "scope": "app"},
    {"key": "SNAPTRADE_USER_ID",     "label": "SnapTrade user ID",               "group": "Portfolio (SnapTrade)",  "scope": "app"},
    {"key": "SNAPTRADE_USER_SECRET", "label": "SnapTrade user secret",           "group": "Portfolio (SnapTrade)",  "scope": "app"},
    {"key": "MONARCH_EMAIL",         "label": "Monarch email",                   "group": "Portfolio (Monarch)",    "scope": "mcpo"},
    {"key": "MONARCH_PASSWORD",      "label": "Monarch password",                "group": "Portfolio (Monarch)",    "scope": "mcpo"},
    {"key": "MONARCH_MFA_SECRET",    "label": "Monarch MFA / TOTP secret",       "group": "Portfolio (Monarch)",    "scope": "mcpo"},
    {"key": "TAVILY_API_KEY",        "label": "Tavily API key (news)",           "group": "Market data",            "scope": "mcpo"},
    {"key": "MCPO_API_KEY",          "label": "mcpo API key",                    "group": "Internal",               "scope": "mcpo"},
]


def _read_secrets():
    try:
        with open(SECRETS_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


@app.get("/api/secrets")
def get_secrets():
    """Status ONLY — never returns secret VALUES. Per secret: whether it's set and from where
    (env / saved / unset), whether it's env-locked (real .env wins), and whether it's editable here."""
    stored = _read_secrets()
    out = []
    for spec in SECRET_SPECS:
        k = spec["key"]
        env_locked = k in _ENV_SECRETS_AT_BOOT
        in_file = bool(stored.get(k))
        source = "env" if env_locked else ("saved" if in_file else "unset")
        out.append({"key": k, "label": spec["label"], "group": spec["group"], "scope": spec["scope"],
                    "set": bool(env_locked or in_file), "source": source, "env_locked": env_locked,
                    "editable": (spec["scope"] == "app") and not env_locked})
    return {"secrets": out,
            "note": "Write-only — values are never shown. 'app' secrets you set here save to a gitignored "
                    "secrets.json and apply on the NEXT RESTART (real .env vars always win). 'mcpo' secrets "
                    "(Monarch / Tavily / mcpo key) are used by the mcpo container and can't be applied from "
                    "here — set those in .env and restart."}


@app.post("/api/secrets")
async def post_secrets(req: Request):
    """Set portfolio-api 'app' secrets (write-only -> gitignored secrets.json, 0600). Ignores
    env-locked and 'mcpo'-scope keys. Blank clears. Applies on next restart. Values never echoed."""
    body = await req.json()
    stored = _read_secrets()
    by_key = {s["key"]: s for s in SECRET_SPECS}
    saved, ignored = [], []
    for k, v in (body or {}).items():
        spec = by_key.get(k)
        if not spec or spec["scope"] != "app" or k in _ENV_SECRETS_AT_BOOT:
            ignored.append(k)
            continue
        if v in (None, ""):
            stored.pop(k, None)
        else:
            stored[k] = str(v)
        saved.append(k)
    os.makedirs(os.path.dirname(SECRETS_FILE) or ".", exist_ok=True)
    with open(SECRETS_FILE, "w", encoding="utf-8") as f:
        json.dump(stored, f, indent=2)
    try:
        os.chmod(SECRETS_FILE, 0o600)
    except Exception:
        pass
    return {"ok": True, "saved": saved, "ignored": ignored, "restart_required": bool(saved)}


def _dividend_drip_split():
    """Per-account annual dividend income (value x research yield) split into reinvested
    (DRIP-on accounts, compounding, not spendable now) vs cash (DRIP-off, spendable), and
    the TAXABLE reinvested slice (phantom income taxed yearly). All estimates, read-only."""
    d = _data()
    briefs = (_read_analysis().get("briefs") or {})
    accts_cfg = _read_accounts_cfg()
    acct_status = {a.get("name"): _account_tax_status(a.get("name"), a.get("type"))
                   for a in d.get("accounts", [])}
    out = {"total": 0.0, "reinvested": 0.0, "cash": 0.0,
           "taxable_reinvested": 0.0, "advantaged_reinvested": 0.0,
           "by_account": [], "any_drip": False, "coverage_note":
           "Dividend estimates use the daily research cache; holdings without a brief score 0."}
    for acct in d.get("by_account", []):
        name = acct.get("account")
        fl = _account_flags(name, accts_cfg)
        status = acct_status.get(name) or _account_tax_status(name, "Investments")
        inc = 0.0
        for h in acct.get("holdings", []):
            yld = ((briefs.get(h.get("ticker")) or {}).get("valuation") or {}).get("dividend_yield_pct")
            if yld and (h.get("value") or 0) > 0:
                inc += h["value"] * yld / 100.0
        if inc <= 0:
            continue
        out["total"] += inc
        if fl["drip"]:
            out["any_drip"] = True
            out["reinvested"] += inc
            if status == "taxable":
                out["taxable_reinvested"] += inc
            else:
                out["advantaged_reinvested"] += inc
        else:
            out["cash"] += inc
        out["by_account"].append({"account": name, "tax_status": status, "drip": fl["drip"],
                                  "est_annual_dividend": round(inc, 2)})
    for k in ("total", "reinvested", "cash", "taxable_reinvested", "advantaged_reinvested"):
        out[k] = round(out[k], 2)
    out["by_account"].sort(key=lambda x: -x["est_annual_dividend"])
    return out


def _screen_one(sym, crit):
    """Score one ticker against the strategy criteria using fundamentals + valuation + analyst consensus."""
    fund = edgar.fundamentals(sym)
    val = _valuation(sym) or {}
    g = _grounded(sym)
    price = _price(sym)
    m = (fund.get("metrics") or {}) if fund.get("available") else {}
    r = (fund.get("ratios") or {}) if fund.get("available") else {}
    rev_g = m.get("revenue_growth_pct")
    fcf_m = r.get("fcf_margin_pct")
    pe_fwd = val.get("pe_forward") or val.get("pe_ttm")
    counts = g.get("counts") or {}
    tot = g.get("analyst_total") or 0
    buys = (counts.get("strongBuy") or 0) + (counts.get("buy") or 0)
    buy_pct = round(buys / tot * 100, 1) if tot else None

    checks = []

    def chk(name, ok, value):
        checks.append({"criterion": name, "pass": bool(ok), "value": value})
    pio = ((fund.get("scores") or {}).get("piotroski_f") or {}) if fund.get("available") else {}
    pio_score = pio.get("score")
    chk("rev_growth", rev_g is not None and rev_g >= crit.get("min_rev_growth_pct", 0), rev_g)
    chk("fcf_margin", fcf_m is not None and fcf_m >= crit.get("min_fcf_margin_pct", 0), fcf_m)
    chk("valuation", pe_fwd is not None and pe_fwd <= crit.get("max_pe_forward", 1e12), pe_fwd)
    chk("analyst", buy_pct is not None and buy_pct >= crit.get("min_analyst_buy_pct", 0), buy_pct)
    # Piotroski F is an OPT-IN 5th criterion (only when min_piotroski>0), so the default 4-check
    # fit_score / OPP_MIN_FIT gate is unchanged for users who don't set a floor.
    if crit.get("min_piotroski", 0) > 0:
        chk("piotroski", pio_score is not None and pio_score >= crit.get("min_piotroski", 0), pio_score)
    # Forward expected return = analyst-target-implied 12mo upside (same sane bound as the
    # portfolio signals). NOT a promise — analyst targets are noisy and mean-reverting.
    tgt = g.get("target_mean")
    exp_ret = None
    if tgt and price and price > 0:
        ir = (tgt - price) / price * 100.0
        if -90 < ir < 300:
            exp_ret = round(ir, 1)
    fit_score = sum(1 for c in checks if c["pass"])
    max_score = len(checks)
    fit_frac = (fit_score / max_score) if max_score else 0.0
    # Composite ranking signal: forward upside WEIGHTED by quality fit, so a high-upside
    # low-quality name doesn't outrank a high-quality one. Heuristic, per-dollar (a % return),
    # explicitly NOT influenced by share price. Educational only.
    opp_score = round((exp_ret or 0.0) * fit_frac, 1)
    # Transparent 3-factor composite (quality + value + momentum), 0-100, reusing data already
    # fetched (no extra calls). QUALITY = criteria fit fraction; VALUE = a monotone map of forward
    # P/E (cheaper = higher, saturates by ~50x); MOMENTUM = normalized analyst upside. Averaged
    # over the sub-scores that ARE available. Additive — does NOT change the fit gate / opp_score.
    q_sub = round(fit_frac, 2)
    v_sub = round(max(0.0, min(1.0, 1.0 - pe_fwd / 50.0)), 2) if (pe_fwd and pe_fwd > 0) else None
    m_sub = round(max(0.0, min(1.0, (exp_ret + 20.0) / 70.0)), 2) if exp_ret is not None else None
    _subs = [x for x in (q_sub, v_sub, m_sub) if x is not None]
    composite = round(sum(_subs) / len(_subs) * 100, 1) if _subs else None
    return {"ticker": sym, "available": bool(fund.get("available")), "price": price,
            "composite_score": composite,
            "composite_subscores": {"quality": q_sub, "value": v_sub, "momentum": m_sub},
            "rev_growth_pct": rev_g, "fcf_margin_pct": fcf_m, "net_margin_pct": r.get("net_margin_pct"),
            "piotroski": pio_score, "pe_forward": pe_fwd, "analyst_rating": g.get("rating"), "analyst_buy_pct": buy_pct,
            "target_mean": tgt, "expected_return_pct": exp_ret, "opportunity_score": opp_score,
            "accruals_ratio_pct": _accruals_ratio(fund),   # earnings-quality flag (Sloan) — info, not a gate
            "fit_score": fit_score, "max_score": max_score, "checks": checks}


def _opportunities():
    cfg = _read_scanner()
    crit = cfg.get("criteria") or {}
    tickers = list(dict.fromkeys([str(t).upper() for t in (cfg.get("watchlist") or []) if t]))
    held = set()
    try:
        held = {c.get("ticker") for c in _data()["consolidated"] if c.get("ticker")}
    except Exception:
        pass
    if cfg.get("include_holding_peers"):
        for h in list(held)[:10]:
            for p in (_finnhub("stock/peers", h) or [])[:5]:
                pu = str(p).upper()
                if pu and pu not in tickers and pu not in held:
                    tickers.append(pu)
    ideas = []
    for t in tickers[:25]:
        try:
            ideas.append({**_screen_one(t, crit), "already_held": t in held})
        except Exception as e:
            ideas.append({"ticker": t, "available": False, "error": str(e)})
    # Rank by the quality-weighted forward return (opportunity_score), then raw fit, then
    # expected return — replaces the old "fit then revenue-growth" order so the best
    # expected return PER DOLLAR (never per share) floats up. Fit gate still applies below.
    ideas.sort(key=lambda x: (-(x.get("opportunity_score") if x.get("opportunity_score") is not None else -1e9),
                              -(x.get("fit_score") or 0),
                              -(x.get("expected_return_pct") if x.get("expected_return_pct") is not None else -1e9)))
    # Curate: enforce the price cap as a HARD filter, keep only good fits, cap the count.
    maxp, minp = crit.get("max_share_price"), crit.get("min_share_price")

    def _price_ok(i):
        p = i.get("price")
        if maxp is not None and (p is None or p > maxp):
            return False
        if minp is not None and (p is None or p < minp):
            return False
        return True

    screened = len(ideas)
    ideas = [i for i in ideas if i.get("available") and _price_ok(i)
             and (i.get("fit_score") or 0) >= OPP_MIN_FIT][:OPP_MAX_IDEAS]
    return {"generated_at": int(time.time()), "criteria": crit, "strategy": cfg.get("strategy_description"),
            "watchlist": cfg.get("watchlist") or [], "include_holding_peers": bool(cfg.get("include_holding_peers")),
            "discovery_queries": cfg.get("discovery_queries") or [], "alert_holdings_limit": cfg.get("alert_holdings_limit"),
            "screened": screened, "count": len(ideas), "min_fit": OPP_MIN_FIT, "ideas": ideas,
            "note": f"Top {len(ideas)} of {screened} screened — only names scoring >= {OPP_MIN_FIT}/4 within your "
                    "price cap. Ranked by 'opportunity_score' = analyst-target-implied forward return WEIGHTED "
                    "by quality fit (a per-DOLLAR % return, never per share; a wild target on a low-quality name "
                    "won't top a solid one). Analyst targets are noisy and mean-reverting — research ideas, NOT "
                    "advice; ask the Fiduciary to deep-dive any name."}


_opp_lock = threading.Lock()


def _read_opps():
    try:
        with open(OPPORTUNITIES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _do_opportunities():
    """Recompute the (slow) live scan and cache it to disk. Single-flight."""
    if not _opp_lock.acquire(blocking=False):
        return
    try:
        res = _opportunities()
        os.makedirs(os.path.dirname(OPPORTUNITIES_FILE) or ".", exist_ok=True)
        with open(OPPORTUNITIES_FILE, "w", encoding="utf-8") as f:
            json.dump(res, f)
    except Exception:
        pass
    finally:
        _opp_lock.release()


@app.get("/api/opportunities")
def get_opportunities():
    """Proactive opportunity screen, CACHED. Returns the last scan instantly and refreshes in the
    background when stale, so the dashboard never blocks on the live EDGAR/Finnhub scan."""
    cached = _read_opps()
    fresh = bool(cached) and (time.time() - (cached.get("generated_at") or 0) < OPP_TTL)
    if not fresh and not _opp_lock.locked():
        threading.Thread(target=_do_opportunities, daemon=True).start()
    if cached:
        return {**cached, "cached": True, "refreshing": (not fresh)}
    # nothing cached yet — return an error-shaped placeholder so the dashboard hides the
    # (empty) tab until the first background scan finishes, rather than blocking the page.
    return {"error": "first opportunity scan is running — refresh in a moment", "computing": True,
            "count": 0, "ideas": []}


@app.post("/api/run-opportunities")
def run_opportunities(x_analysis_token: str = Header(default="")):
    """Recompute + cache the opportunity scan (for the weekly n8n schedule). Token-guarded."""
    if ANALYSIS_TOKEN and x_analysis_token != ANALYSIS_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if _opp_lock.locked():
        return {"ok": True, "status": "already running"}
    threading.Thread(target=_do_opportunities, daemon=True).start()
    return {"ok": True, "status": "started"}


# ---------------- Tax: loss-harvesting + asset-location (read-only audits) ----------------
TLH_MIN_LOSS = float(os.environ.get("TLH_MIN_LOSS", "50"))       # ignore trivial unrealized losses ($)
LOC_MIN_YIELD = float(os.environ.get("LOC_MIN_YIELD", "3.0"))    # % dividend yield => "tax-inefficient"


def _account_tax_status(name, atype):
    """Classify an account: taxable | roth | deferred | hsa | na (non-investment).
    Roth/HSA/IRA/401k/etc. = tax-advantaged; a plain brokerage = taxable."""
    n = (name or "").upper()
    if "ROTH" in n:
        return "roth"
    if "HSA" in n or "HEALTH SAVINGS" in n:
        return "hsa"
    if any(k in n for k in ("IRA", "401", "403(B)", "403B", "457", "TSP", " SEP", "SIMPLE", "529")):
        return "deferred"
    if (atype or "") in ("Investments", "Investment", "Brokerage"):
        return "taxable"
    return "na"   # cash / credit / loan / real-estate accounts hold no locatable securities


def _tax_audit():
    """Tax-loss-harvesting candidates (losses in taxable accounts) + asset-location audit
    (tax-inefficient high-yield holdings sitting in taxable accounts). Reuses portfolio + research data."""
    d = _data()
    briefs = (_read_analysis().get("briefs") or {})
    acct_status = {a.get("name"): _account_tax_status(a.get("name"), a.get("type"))
                   for a in d.get("accounts", [])}
    accts_cfg = _read_accounts_cfg()
    harvest, location = [], []
    # Dividend INCOME accrues in EVERY account (a dividend paid inside an IRA/401k is still
    # income — it compounds — it just isn't taxed *now*), so the income estimate must span ALL
    # tax statuses. The TLH + asset-location audits below stay TAXABLE-only, because that's the
    # only place the distributions are actually taxed / worth relocating. Previously this loop
    # `continue`d on every non-taxable account, so the "income" number silently dropped every
    # dividend held in tax-deferred/Roth space and read far too low.
    income_by_status = {"taxable": 0.0, "deferred": 0.0, "roth": 0.0, "hsa": 0.0}
    income_by_ticker = {}
    for acct in d.get("by_account", []):
        name = acct.get("account")
        status = acct_status.get(name) or _account_tax_status(name, "Investments")
        drip_on = _account_flags(name, accts_cfg)["drip"]
        is_taxable = (status == "taxable")
        for h in acct.get("holdings", []):
            tkr, val, gain = h.get("ticker"), h.get("value") or 0, h.get("gain")
            yld = ((briefs.get(tkr) or {}).get("valuation") or {}).get("dividend_yield_pct")
            # --- all-accounts dividend income (the un-skewed estimate) ---
            if yld is not None and yld > 0 and val > 0:
                est_all = val * yld / 100.0
                if status in income_by_status:
                    income_by_status[status] += est_all
                row = income_by_ticker.setdefault(
                    tkr, {"ticker": tkr, "value": 0.0, "est_annual_income": 0.0,
                          "dividend_yield_pct": round(yld, 2), "accounts": set()})
                row["value"] += val
                row["est_annual_income"] += est_all
                row["accounts"].add(name)
            # --- TLH + asset-location remain TAXABLE-only (tax-exposure lens) ---
            if not is_taxable:
                continue
            if gain is not None and gain <= -_knob("tlh_min_loss"):
                harvest.append({"ticker": tkr, "account": name, "value": round(val, 2),
                                "cost": h.get("cost_total"), "loss": round(gain, 2),
                                "loss_pct": h.get("gain_pct")})
            if yld is not None and yld >= _knob("loc_min_yield") and val > 0:
                est = round(val * yld / 100, 2)
                location.append({"ticker": tkr, "account": name, "value": round(val, 2),
                                 "dividend_yield_pct": round(yld, 2), "est_annual_income": est,
                                 "drip": drip_on,
                                 "est_annual_taxable_reinvestment": est if drip_on else 0.0})
    harvest.sort(key=lambda x: x["loss"])                       # most negative first
    location.sort(key=lambda x: -(x.get("est_annual_income") or 0))
    # consolidate the all-accounts dividend-income view: one row per ticker, summed across
    # every account it's held in (this is the "combine holdings across accounts" the audit lacked)
    income_rows = []
    for r in income_by_ticker.values():
        income_rows.append({"ticker": r["ticker"], "value": round(r["value"], 2),
                            "dividend_yield_pct": r["dividend_yield_pct"],
                            "est_annual_income": round(r["est_annual_income"], 2),
                            "accounts": sorted(a for a in r["accounts"] if a),
                            "account_count": len(r["accounts"])})
    income_rows.sort(key=lambda x: -(x["est_annual_income"] or 0))
    income_by_status = {k: round(v, 2) for k, v in income_by_status.items()}
    income_total = round(sum(income_by_status.values()), 2)
    return {
        "generated_at": int(time.time()),
        "accounts": {n: s for n, s in acct_status.items() if s != "na"},
        "tax_loss_harvest": {
            "candidates": harvest, "count": len(harvest),
            "total_harvestable_loss": round(sum(x["loss"] for x in harvest), 2),
            "note": "Unrealized losses in TAXABLE accounts you could realize to offset capital gains "
                    "(or up to $3,000 of ordinary income/yr). WASH-SALE RULE: don't buy the same or a "
                    "substantially identical security within 30 days before/after the sale or the loss is "
                    "disallowed — use a similar-but-not-identical replacement. Educational, not tax advice; "
                    "confirm tax lots & holding periods with your brokerage/CPA."},
        "dividend_income": {
            "est_annual_income_all_accounts": income_total,
            "by_tax_status": income_by_status,
            "taxable": income_by_status["taxable"],
            "tax_advantaged": round(income_by_status["deferred"] + income_by_status["roth"]
                                    + income_by_status["hsa"], 2),
            "by_ticker": income_rows, "count": len(income_rows),
            "note": "Estimated ANNUAL dividend income across ALL accounts (taxable + IRA/401k + Roth + "
                    "HSA), consolidated to one row per ticker. A dividend paid inside a tax-advantaged "
                    "account is still income — it compounds — it simply isn't taxed now, so it belongs "
                    "in your income estimate even though it's correctly excluded from the taxable-"
                    "distribution / asset-location audit below. Only payers with a daily research brief "
                    "carrying a yield are scored; names without one (or where the data feed returns no "
                    "yield — common for some mREITs/BDCs/foreign listings) read $0 and are not counted. "
                    "Educational, not advice.",
        },
        "asset_location": {
            "misplaced": location, "count": len(location),
            "est_annual_income_in_taxable": round(sum(x.get("est_annual_income") or 0 for x in location), 2),
            "est_taxable_drip_reinvestment": round(sum(x.get("est_annual_taxable_reinvestment") or 0 for x in location), 2),
            "drip_split": _dividend_drip_split(),
            "note": "High-yield / income holdings sitting in TAXABLE accounts — their distributions are "
                    "taxed every year. Holding them in tax-DEFERRED (IRA/401k) space instead shelters that "
                    "income; keep low-yield growth in taxable. DRIP makes taxable dividends WORSE: each "
                    "reinvestment is 'phantom income' (taxed even though you receive no cash) and creates a "
                    "new tax lot with its own basis & date — track lots and use SPECIFIC-LOT identification "
                    "when selling so you don't pay tax on the same dollars twice. DRIP inside tax-advantaged "
                    "accounts has no such drag. Set per-account DRIP under ACCOUNTS. Yields come from the "
                    "daily research cache, so holdings without a brief aren't scored. Educational, not tax advice."},
    }


SEPP_RATE_PCT = float(os.environ.get("SEPP_RATE_PCT", "5.0"))    # 72(t) rate = max(5%, 120% mid-term AFR); 5% floor binds (IRS Notice 2022-6)
BRIDGE_TAX_PCT = float(os.environ.get("BRIDGE_TAX_PCT", "15.0"))  # effective ordinary-income rate on Roth conversions


def _balances_by_tax_status():
    """Investable securities value grouped by account tax status (taxable/roth/deferred/hsa)."""
    d = _data()
    acct_status = {a.get("name"): _account_tax_status(a.get("name"), a.get("type"))
                   for a in d.get("accounts", [])}
    bal = {"taxable": 0.0, "roth": 0.0, "deferred": 0.0, "hsa": 0.0}
    for acct in d.get("by_account", []):
        name = acct.get("account")
        status = acct_status.get(name) or _account_tax_status(name, "Investments")
        if status in bal:
            bal[status] += sum((h.get("value") or 0) for h in acct.get("holdings", []))
    return {k: round(v, 2) for k, v in bal.items()}


def _bridge_plan(annual_expenses, current_age, retire_age, tax_ctx=None):
    """Early-retirement BRIDGE to age 59.5: models a Roth-conversion ladder (convert Traditional->Roth
    each year; each conversion is penalty-free 5 tax-years later) seeded by taxable/Roth funds, plus a
    72(t)/SEPP fallback estimate. Read-only, educational — NOT tax advice."""
    if not annual_expenses or retire_age is None:
        return {"status": "insufficient_data",
                "note": "Set annual_expenses and current/target age in the retirement config to model the bridge."}
    bridge_age = 59.5
    if retire_age >= bridge_age:
        return {"status": "no_bridge_needed", "retire_age": retire_age,
                "note": "Retiring at/after 59.5 — Traditional IRA/401k is penalty-free, so no early-access bridge is required."}
    bal = _balances_by_tax_status()
    # Conversion tax rate: derived from the bracket engine (G5) on a one-year conversion
    # (~one year of expenses) in an otherwise low-income bridge year, so it reflects the
    # real effective rate of "fill the low brackets" instead of a flat guess.
    # BRIDGE_TAX_PCT still overrides when explicitly set in the environment.
    if os.environ.get("BRIDGE_TAX_PCT"):
        eff_rate = BRIDGE_TAX_PCT / 100.0
        rate_src = "env BRIDGE_TAX_PCT"
    else:
        tctx = tax_ctx or _tax_ctx()
        _e = tax.effective_rate(annual_expenses, 0.0, tctx["status"],
                                age=tctx["age"], spouse_age=tctx["spouse_age"], include_niit=False)
        eff_rate = _e["effective_rate_pct"] / 100.0
        rate_src = "bracket-engine 2026 (%s)" % tctx["status"]
    bridge_years = int(math.ceil(bridge_age - retire_age))
    season_years = min(5, bridge_years)                 # Roth conversions need 5 tax-years to season
    ladder_years = max(0, bridge_years - season_years)  # yrs funded by seasoned conversions
    conversions_needed = round(annual_expenses * ladder_years, 2)     # Roth principal to build (nets expenses)
    conversion_tax = round(conversions_needed * eff_rate, 2)          # ordinary-income tax, paid from taxable
    # seed must cover living expenses through the seasoning window PLUS the tax on every conversion
    seed_need = round(annual_expenses * season_years + conversion_tax, 2)
    accessible = round(bal["taxable"] + bal["roth"], 2)       # taxable anytime; Roth contributions anytime
    total_bridge_need = round(annual_expenses * bridge_years + conversion_tax, 2)
    # 72(t) SEPP amortization estimate on the Traditional balance
    r = SEPP_RATE_PCT / 100.0
    n = max(1, int(round(86 - retire_age)))             # ~ IRS single-life expectancy, rough
    deferred = bal["deferred"]
    sepp = round(deferred * r / (1 - (1 + r) ** (-n)), 2) if deferred > 0 and r > 0 else 0.0
    seed_covered = accessible >= seed_need
    conversions_covered = deferred >= conversions_needed
    # ACA cliff (G1): each ladder year you convert ~one year of expenses Traditional->Roth,
    # which is MAGI. Pre-Medicare (retire<65), that MAGI drives the premium subsidy — and the
    # 2026 400%-FPL cliff. Model the bridge-year MAGI as the annual conversion and check it.
    aca_block = None
    if retire_age is not None and retire_age < 65:
        try:
            actx = _aca_ctx()
            aca_block = aca.summary(components={"roth_conversions": annual_expenses},
                                    household_size=actx["household_size"],
                                    slcsp_annual=actx["slcsp_annual"])
            aca_block["interpretation"] = (
                "Converting ~one year of expenses (${:,.0f}) each bridge year puts MAGI at "
                "{}% FPL — {} the 2026 400% ACA subsidy cliff. Roth conversions and the "
                "0%-LTCG gain harvest (see /api/tax) both raise MAGI, so the ACA cliff can bind "
                "BEFORE the tax bracket does; the smaller of the two headrooms is your real "
                "conversion ceiling.").format(
                    annual_expenses, aca_block["subsidy"].get("fpl_pct"),
                    "OVER" if aca_block["cliff"]["over_cliff"] else "under")
        except Exception:
            aca_block = None
    return {
        "status": "bridge_needed",
        "retire_age": retire_age, "bridge_to_age": bridge_age, "bridge_years": bridge_years,
        "annual_expenses": round(annual_expenses, 2), "total_bridge_need": total_bridge_need,
        "balances": bal, "aca": aca_block,
        "roth_ladder": {
            "seasoning_years": season_years,
            "seed_need_taxable_roth": seed_need,
            "accessible_taxable_plus_roth": accessible,
            "seed_covered": seed_covered,
            "seed_gap": round(max(0.0, seed_need - accessible), 2),
            "annual_conversion": round(annual_expenses, 2),
            "conversions_needed_total": conversions_needed,
            "effective_tax_rate_pct": round(eff_rate * 100, 2),
            "tax_rate_source": rate_src,
            "conversion_tax_est": conversion_tax,
            "deferred_balance": deferred,
            "conversions_covered": conversions_covered,
            "note": "Convert ~one year of expenses from Traditional->Roth each year starting at retirement; "
                    "each conversion is ORDINARY INCOME the year you do it, so the seed (taxable/Roth) must "
                    f"also fund ~{eff_rate * 100:.0f}% tax on every conversion (est ${conversion_tax:,.0f} total) "
                    f"on top of living expenses (rate from {rate_src}; see /api/tax for the bracket detail). "
                    "each conversion is withdrawable penalty-free 5 tax-years later, so the FIRST "
                    f"{season_years} year(s) must be funded from taxable/Roth-basis money "
                    "(the 'seed'). Each conversion is ordinary income in the year you do it — ideally in "
                    "low/no-wage years to fill up low brackets. Confirm the 5-year rule applies per "
                    "conversion and mind Roth ordering rules with a CPA."},
        "sepp_72t": {
            "annual_estimate": sepp,
            "covers_expenses_pct": round(sepp / annual_expenses * 100, 1) if annual_expenses else None,
            "assumed_rate_pct": SEPP_RATE_PCT, "life_expectancy_years": n,
            "note": "Alternative: 72(t) Substantially Equal Periodic Payments let you tap the Traditional "
                    "IRA before 59.5 penalty-free, but payments are LOCKED for 5 years or until 59.5 "
                    "(whichever is longer) — breaking them triggers retroactive penalties. Estimate uses the "
                    f"amortization method at {SEPP_RATE_PCT}%; the real figure depends on the IRS max rate "
                    "and method. Confirm with a CPA."},
        "assessment": (
            ("Roth ladder looks fundable: " if (seed_covered and conversions_covered)
             else ("Seed shortfall — you'd need more taxable/Roth to cover the first "
                   f"{season_years} yrs before conversions season. " if not seed_covered
                   else "Traditional balance may not cover all conversions. ")) +
            f"Bridge {retire_age}->59.5 is {bridge_years} yr(s), ~${total_bridge_need:,.0f} of real spending; "
            f"seed need ${seed_need:,.0f} vs ${accessible:,.0f} accessible."),
        "note": "Educational early-retirement bridge model — NOT tax advice. Roth-basis is approximated by "
                "your Roth balance (true accessible contributions may differ); confirm with a CPA.",
    }


@app.get("/api/tax-audit")
def get_tax_audit():
    """Tax-loss-harvesting candidates + asset-location audit (read-only; no new external calls).
    NOTE: previously registered at /api/tax, which collided with the 2026 bracket-engine route above
    (FastAPI first-match) and shadowed this endpoint AND the get_tax name. Moved to /api/tax-audit."""
    try:
        return _tax_audit()
    except Exception as e:
        return JSONResponse({"error": f"tax audit failed: {e}"}, status_code=500)


# ---------------- Fees + Liquidity (read-only audit) ----------------
_fl_lock = threading.Lock()


def _fee_liquidity():
    """Liquidity audit: flag tradable holdings that are thinly traded (low average daily DOLLAR
    volume), micro-cap, or slow to exit (days to liquidate at a modest participation rate). Mutual
    funds (redeem at NAV) and cash are excluded — they're not a market-liquidity concern. The fund
    fee (expense-ratio) audit is added separately via the SEC mutual-fund Risk/Return feed."""
    d = _data()
    rows = []
    for c in d.get("consolidated", []):
        tkr = c.get("ticker")
        if not tkr or c.get("type") in ("Cash", "Mutual Fund"):
            continue
        val = c.get("value") or 0
        price = c.get("price")
        v = _valuation(tkr) or {}
        avgvol_m = v.get("avg_volume_3m_m") or v.get("avg_volume_10d_m")        # millions of shares/day
        mcap = v.get("market_cap_musd")                                          # millions USD
        ddv = round(avgvol_m * 1e6 * price) if (avgvol_m and price) else None    # avg daily $ volume
        days = round(val / (ddv * LIQ_PARTICIPATION), 1) if ddv else None        # days to exit
        flags = []
        if ddv is not None and ddv < _knob("liq_min_ddv"):
            flags.append("thin volume")
        if mcap is not None and mcap < _knob("microcap_musd"):
            flags.append("micro-cap")
        if avgvol_m is None or price is None:
            flags.append("no market data (foreign/OTC?)")
        if days is not None and days >= 1:
            flags.append(f"~{days}d to exit")
        if flags:
            rows.append({"ticker": tkr, "name": c.get("name"), "value": round(val, 2),
                         "weight_pct": c.get("weight_pct"), "price": price,
                         "avg_daily_dollar_volume": ddv,
                         "avg_volume_shares_per_day": round(avgvol_m * 1e6) if avgvol_m else None,
                         "market_cap_musd": mcap, "days_to_exit": days, "flags": flags})
    rows.sort(key=lambda x: -(x.get("value") or 0))

    # ---- fund / ETF expense-ratio (fee-drag) audit ----
    fee_rows = []
    for c in d.get("consolidated", []):
        t2 = c.get("ticker")
        if not t2 or (c.get("type") or "") not in ("Mutual Fund", "ETF"):
            continue   # individual stocks have no expense ratio
        v2 = c.get("value") or 0
        er = edgar.fund_expense_ratio(t2)
        fee_rows.append({"ticker": t2, "name": c.get("name"), "type": c.get("type"),
                         "value": round(v2, 2), "expense_ratio_pct": er,
                         "annual_fee": round(v2 * er / 100, 2) if er is not None else None,
                         "high": bool(er is not None and er >= _knob("fee_high_pct"))})
    fee_rows.sort(key=lambda x: -((x.get("annual_fee") or 0)))
    fees_block = {
        "available": bool(fee_rows),
        "funds": fee_rows, "count": len(fee_rows),
        "total_annual_fee": round(sum(r["annual_fee"] or 0 for r in fee_rows), 2),
        "unknown": [r["ticker"] for r in fee_rows if r["expense_ratio_pct"] is None],
        "note": "Annual expense-ratio drag on your funds/ETFs (value x net expense ratio). Small "
                "percentages compound into real money over decades, and active funds usually cost far "
                "more than index funds. Ratios come from a maintained map (edgar.py FUND_EXPENSE_RATIOS) "
                "— add any 'unknown' tickers from the fund's prospectus. Educational, not advice.",
    }

    return {"generated_at": int(time.time()),
            "liquidity": {"flagged": rows, "count": len(rows),
                          "thresholds": {"min_daily_dollar_volume": _knob("liq_min_ddv"),
                                         "microcap_market_cap_musd": _knob("microcap_musd"),
                                         "participation_rate": LIQ_PARTICIPATION},
                          "note": "Thinly-traded / micro-cap / slow-to-exit holdings. 'days to exit' assumes "
                                  "trading at most %d%% of average daily volume. Volume & market cap are from "
                                  "Finnhub — sanity-check the dollar-volume on a liquid name (a mega-cap should "
                                  "read in the billions/day). Informational, not advice." % int(LIQ_PARTICIPATION * 100)},
            "fees": fees_block}


def _read_fl():
    try:
        with open(FL_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _do_fl():
    if not _fl_lock.acquire(blocking=False):
        return
    try:
        res = _fee_liquidity()
        os.makedirs(os.path.dirname(FL_FILE) or ".", exist_ok=True)
        with open(FL_FILE, "w", encoding="utf-8") as f:
            json.dump(res, f)
    except Exception:
        pass
    finally:
        _fl_lock.release()


@app.get("/api/fees-liquidity")
def get_fees_liquidity():
    """Fees + liquidity audit, CACHED (the live scan makes a Finnhub call per holding). Returns the
    last result instantly and refreshes in the background when stale, so the dashboard never blocks."""
    cached = _read_fl()
    fresh = bool(cached) and (time.time() - (cached.get("generated_at") or 0) < FL_TTL)
    if not fresh and not _fl_lock.locked():
        threading.Thread(target=_do_fl, daemon=True).start()
    if cached:
        return {**cached, "cached": True, "refreshing": (not fresh)}
    return {"error": "first liquidity scan is running — refresh in a moment", "computing": True,
            "liquidity": {"flagged": [], "count": 0}, "fees": {"available": False}}


@app.post("/api/run-fees-liquidity")
def run_fees_liquidity(x_analysis_token: str = Header(default="")):
    """Recompute + cache the fees/liquidity audit (for a scheduled refresh). Token-guarded."""
    if ANALYSIS_TOKEN and x_analysis_token != ANALYSIS_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if _fl_lock.locked():
        return {"ok": True, "status": "already running"}
    threading.Thread(target=_do_fl, daemon=True).start()
    return {"ok": True, "status": "started"}


# ---------------- Exposure: sector allocation + portfolio beta + concentration ----------------
EXP_FILE = os.environ.get("EXP_FILE", "/data/exposure.json")
EXP_TTL = int(os.environ.get("EXP_TTL", "43200"))    # cache exposure ~12h
_exp_lock = threading.Lock()
_profile_cache = {}   # ticker -> (ts, profile dict)


def _profile(sym):
    """Finnhub company profile (sector via finnhubIndustry). Cached in-process ~1 day."""
    if not FINNHUB:
        return {}
    now = time.time()
    hit = _profile_cache.get(sym)
    if hit and now - hit[0] < 86400:
        return hit[1]
    try:
        r = requests.get("https://finnhub.io/api/v1/stock/profile2",
                         params={"symbol": sym, "token": FINNHUB}, timeout=15)
        prof = r.json() if r.status_code < 400 else {}
    except Exception:
        prof = {}
    _profile_cache[sym] = (now, prof if isinstance(prof, dict) else {})
    return _profile_cache[sym][1]


STRESS_SCENARIOS = [
    {"name": "2008 Global Financial Crisis", "market_pct": -37, "detail": "S&P 500 calendar-2008 total return"},
    {"name": "2000-02 Dot-com bust", "market_pct": -49, "detail": "S&P 500 peak-to-trough"},
    {"name": "2020 COVID crash", "market_pct": -34, "detail": "Feb-Mar 2020 peak-to-trough"},
    {"name": "2022 rate-shock bear", "market_pct": -25, "detail": "2022 peak-to-trough S&P 500"},
    {"name": "1987 Black Monday (1 day)", "market_pct": -20, "detail": "single-session crash"},
]

# Asset-class-aware HYPOTHETICAL shocks — the fix for the beta stress test's hidden assumption
# that bonds always hedge. Each applies a separate shock to equity / bond / cash sleeves, so a
# stock-AND-bond-both-down regime (2022) shows its true bite. Shocks are round-number planning
# stresses, not forecasts. (equity, bond, cash) in %.
MACRO_SCENARIOS = [
    {"name": "2022 replay — stocks & bonds BOTH down", "equity": -18, "bond": -13, "cash": 0,
     "detail": "the beta method wrongly assumes bonds hedge; in 2022 they fell WITH stocks"},
    {"name": "Rates +300bp", "equity": -8, "bond": -15, "cash": 0,
     "detail": "duration ~5 hits bonds; higher discount rate pressures long-duration equity"},
    {"name": "1970s-style stagflation", "equity": -20, "bond": -18, "cash": -8,
     "detail": "real value: equity & bonds fall, inflation erodes cash's purchasing power"},
    {"name": "2008-style GFC (flight to quality)", "equity": -37, "bond": 5, "cash": 0,
     "detail": "the contrast case — here Treasuries DID rally as equities crashed"},
]


def _macro_stress(invested, positions):
    """Per-sleeve hypothetical stress. Splits the book equity/bond/cash from the SEC look-through
    asset_class_value when available, else a heuristic; applies each scenario's per-class shocks."""
    lt = _read_lt() or {}
    acv = lt.get("asset_class_value") or {}
    if acv and (acv.get("equity") or acv.get("bond")):
        eq, bd, csh = acv.get("equity", 0.0), acv.get("bond", 0.0), acv.get("cash", 0.0)
        src = "SEC N-PORT look-through asset classes"
    else:
        csh = sum((p.get("value") or 0) for p in positions if p.get("sector") == "Cash")
        eq, bd = invested - csh, 0.0
        src = "heuristic (no look-through cache — bond funds counted as equity; run /api/run-lookthrough)"
    rows = []
    for s in MACRO_SCENARIOS:
        imp = eq * s["equity"] / 100.0 + bd * s["bond"] / 100.0 + csh * s["cash"] / 100.0
        rows.append({"name": s["name"], "detail": s["detail"],
                     "equity_pct": s["equity"], "bond_pct": s["bond"], "cash_pct": s["cash"],
                     "dollar_impact": round(imp), "pct_impact": round(imp / invested * 100, 1) if invested else None})
    rows.sort(key=lambda r: (r["dollar_impact"] or 0))
    return {"equity_value": round(eq, 2), "bond_value": round(bd, 2), "cash_value": round(csh, 2),
            "source": src, "scenarios": rows,
            "note": "Per-sleeve shocks (equity/bond/cash separately) so a stock-AND-bond-both-down "
                    "regime shows its true impact — unlike the beta test, which implicitly assumes "
                    "bonds hedge. Round-number planning stresses, not forecasts."}


def _exposure():
    """True sector allocation, value-weighted portfolio beta, and concentration metrics.
    Direct stocks are classified by sector; funds are bucketed (look-through is a later upgrade)."""
    d = _data()
    cons = [c for c in d.get("consolidated", []) if c.get("ticker")]
    invested = round(sum(c.get("value") or 0 for c in cons), 2)
    by_sector, beta_num, beta_den, positions = {}, 0.0, 0.0, []
    for c in cons:
        tkr, typ, val = c["ticker"], (c.get("type") or ""), (c.get("value") or 0)
        if typ == "Cash":
            sector, beta = "Cash", None
        elif typ in ("Mutual Fund", "ETF"):
            sector, beta = "Funds (no look-through)", (_valuation(tkr) or {}).get("beta")
        else:
            sector = ((_profile(tkr).get("finnhubIndustry") or "").strip() or "Other")
            beta = (_valuation(tkr) or {}).get("beta")
        by_sector[sector] = round(by_sector.get(sector, 0) + val, 2)
        if beta is not None:
            beta_num += val * beta
            beta_den += val
        positions.append({"ticker": tkr, "value": round(val, 2),
                          "weight_pct": round(val / invested * 100, 2) if invested else None,
                          "sector": sector, "beta": beta})
    sectors = sorted(({"sector": s, "value": v,
                       "weight_pct": round(v / invested * 100, 2) if invested else None}
                      for s, v in by_sector.items()), key=lambda x: -(x["value"] or 0))
    weights = sorted((p["weight_pct"] or 0 for p in positions), reverse=True)
    hhi = round(sum((w / 100.0) ** 2 for w in weights), 4)
    conc = {"top_position_pct": (weights[0] if weights else None),
            "top5_pct": round(sum(weights[:5]), 2),
            "hhi": hhi, "effective_holdings": round(1 / hhi, 1) if hhi else None, "flags": []}
    if weights and weights[0] > 20:
        conc["flags"].append(f"top position {weights[0]:.0f}% (> 20%)")
    if sum(weights[:5]) > 60:
        conc["flags"].append(f"top-5 {sum(weights[:5]):.0f}% (> 60%)")
    if sectors and (sectors[0]["weight_pct"] or 0) > 35 and sectors[0]["sector"] not in ("Cash",):
        conc["flags"].append(f"{sectors[0]['sector']} {sectors[0]['weight_pct']:.0f}% (> 35%)")
    # Look-through concentration (R3): reuse the already-computed fund look-through cache to report
    # TRUE single-name HHI/top-position — which can be materially higher than the direct view when
    # your funds hold names you also own directly. Additive; the direct `conc` above is unchanged.
    lt = _read_lt()
    lt_conc = None
    if lt and lt.get("true_exposure"):
        ltw = sorted((r.get("weight_pct") or 0 for r in lt["true_exposure"]), reverse=True)
        if ltw:
            lt_hhi = round(sum((w / 100.0) ** 2 for w in ltw), 4)
            lt_conc = {"top_position_pct": ltw[0], "top5_pct": round(sum(ltw[:5]), 2),
                       "hhi": lt_hhi, "effective_holdings": round(1 / lt_hhi, 1) if lt_hhi else None,
                       "funds_resolved": lt.get("funds_resolved"), "funds_total": lt.get("funds_total"),
                       "as_of": lt.get("generated_at"),
                       "note": "TRUE single-name concentration INCLUDING holdings inside your funds "
                               "(SEC N-PORT look-through). Exceeds the direct-view HHI when funds overlap "
                               "your direct names; computed over the look-through cache (top holdings), so "
                               "run /api/run-lookthrough to refresh it."}
            top_d = weights[0] if weights else 0
            if ltw[0] - top_d >= 3:      # true single-name exposure is materially bigger than it looks
                conc["flags"].append(f"true top name {ltw[0]:.0f}% via look-through (direct view {top_d:.0f}%)")
    pbeta = round(beta_num / beta_den, 2) if beta_den else None
    stress = [{**s,
               "est_portfolio_pct": round(pbeta * s["market_pct"], 1) if pbeta is not None else None,
               "est_dollar_impact": round(invested * pbeta * s["market_pct"] / 100.0) if (pbeta is not None and invested) else None}
              for s in STRESS_SCENARIOS]
    return {"generated_at": int(time.time()), "invested_value": invested,
            "portfolio_beta": pbeta,
            "portfolio_beta_coverage_pct": round(beta_den / invested * 100, 1) if invested else None,
            "portfolio_beta_cash_diluted": round(beta_num / invested, 2) if invested else None,
            "by_sector": sectors, "concentration": conc,
            "lookthrough_concentration": lt_conc,
            "stress_test": stress,
            "macro_stress": _macro_stress(invested, positions),
            "positions": sorted(positions, key=lambda p: -(p["value"] or 0)),
            "note": "True sector allocation (direct stocks classified via Finnhub; funds NOT yet looked "
                    "through), value-weighted portfolio beta (1.0 = market), concentration (HHI, effective "
                    "# holdings), and a LINEAR beta-based stress test (est move ≈ beta × the scenario's "
                    "market drop). The stress test UNDERSTATES risk for a concentrated book — your largest "
                    "single names can fall far more than beta implies. Informational, not advice."}


def _read_exp():
    try:
        with open(EXP_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _do_exp():
    if not _exp_lock.acquire(blocking=False):
        return
    try:
        res = _exposure()
        os.makedirs(os.path.dirname(EXP_FILE) or ".", exist_ok=True)
        with open(EXP_FILE, "w", encoding="utf-8") as f:
            json.dump(res, f)
    except Exception:
        pass
    finally:
        _exp_lock.release()


@app.get("/api/exposure")
def get_exposure():
    """Sector allocation + portfolio beta + concentration, CACHED (Finnhub call per holding)."""
    cached = _read_exp()
    fresh = bool(cached) and (time.time() - (cached.get("generated_at") or 0) < EXP_TTL)
    if not fresh and not _exp_lock.locked():
        threading.Thread(target=_do_exp, daemon=True).start()
    if cached:
        return {**cached, "cached": True, "refreshing": (not fresh)}
    return {"error": "first exposure scan is running — refresh in a moment", "computing": True,
            "by_sector": [], "concentration": {}}


@app.post("/api/run-exposure")
def run_exposure(x_analysis_token: str = Header(default="")):
    if ANALYSIS_TOKEN and x_analysis_token != ANALYSIS_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if _exp_lock.locked():
        return {"ok": True, "status": "already running"}
    threading.Thread(target=_do_exp, daemon=True).start()
    return {"ok": True, "status": "started"}


# ---------------- Fund look-through: TRUE single-name exposure (direct + inside funds) ----------------
LT_FILE = os.environ.get("LT_FILE", "/data/lookthrough.json")
LT_TTL = int(os.environ.get("LT_TTL", "86400"))     # cache look-through ~1 day (N-PORT is monthly)
_lt_lock = threading.Lock()


_NAME_DROP = {"INC", "CORP", "CORPORATION", "CO", "COMPANY", "COS", "LTD", "LIMITED", "PLC", "SA",
              "NV", "AG", "SE", "AB", "OYJ", "SPA", "ASA", "THE", "ORDINARY", "ORD", "SHARES",
              "SHARE", "CLASS", "CL", "COM", "COMMON", "STOCK", "SPONSORED", "UNSPONSORED", "ADR",
              "ADS", "REIT", "LP", "NPV", "REGISTERED", "REG", "HLDGS", "HOLDING", "HOLDINGS",
              "GROUP", "GRP", "PARTNERS", "TR", "FUND", "INDEX"}


def _norm_name(s):
    """Normalize a security name for cross-source matching (fund holding name <-> direct holding).
    Uppercases, strips ISIN/SEDOL tails and punctuation, and drops corporate/share-class filler."""
    if not s:
        return ""
    s = s.upper()
    s = re.split(r"\b(ISIN|SEDOL|CUSIP)\b", s)[0]          # cut trailing identifier junk
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    toks = [t for t in s.split() if t not in _NAME_DROP and not (len(t) == 1 and t.isalpha())]
    return " ".join(toks).strip()


def _assetcat_bucket(ac):
    """Map an SEC N-PORT assetCat code to a coarse sleeve bucket (equity / bond / cash / other).
    assetCat gives asset CLASS but not country, so US-vs-Intl is left to the fund's name heuristic."""
    ac = (ac or "").upper().strip()
    if ac in ("EC", "EP"):
        return "equity"
    if ac in ("DBT", "LON", "SN") or ac.startswith("ABS"):
        return "bond"
    if ac in ("STIV", "RA"):
        return "cash"
    return "other"


def _lookthrough():
    """Aggregate TRUE single-name exposure: direct positions PLUS the portfolio's proportional
    share of each fund's underlying holdings (from SEC NPORT-P). Merges fund holdings into direct
    positions by ticker where the fund reports one, otherwise by normalized company NAME (many
    funds report holdings by name/CUSIP only); unmatched holdings appear as their own rows; any
    undistributed fund value is kept as a per-fund residual so the table sums to the invested total."""
    d = _data()
    cons = [c for c in d.get("consolidated", []) if c.get("ticker")]
    invested = round(sum(c.get("value") or 0 for c in cons), 2)
    agg = {}   # key -> {ticker,name,direct,fund,sources:set}
    acv = {}   # asset-class sleeve $ (look-through-refined: funds split by N-PORT assetCat)
    # map normalized direct-holding names -> their ticker, so name-only fund holdings can fold in
    direct_name_to_ticker = {}
    for c in cons:
        if (c.get("type") or "") not in ("Mutual Fund", "ETF", "Cash") and c.get("ticker"):
            nn = _norm_name(c.get("name") or "")
            if nn:
                direct_name_to_ticker.setdefault(nn, (c["ticker"] or "").upper().strip())

    def add(key, name, ticker, direct=0.0, fund=0.0, src=None):
        e = agg.setdefault(key, {"ticker": ticker, "name": name, "direct": 0.0, "fund": 0.0, "sources": set()})
        e["direct"] += direct
        e["fund"] += fund
        if src:
            e["sources"].add(src)
        if ticker and not e["ticker"]:
            e["ticker"] = ticker
        if name and not e["name"]:
            e["name"] = name

    funds_status = []
    for c in cons:
        tkr = (c["ticker"] or "").upper().strip()
        typ = c.get("type") or ""
        val = c.get("value") or 0
        if typ in ("Mutual Fund", "ETF"):
            fh = None
            try:
                fh = edgar.fund_holdings(tkr, name=c.get("name"))
            except Exception as e:
                fh = {"resolved": False, "reason": "look-through error: " + str(e)[:80]}
            if fh and fh.get("resolved"):
                # the fund's equity "home" sleeve (assetCat lacks country) — keeps intl detection
                equity_home = "Intl Equity" if _sleeve(c) == "Intl Equity" else "US Equity"
                distributed = 0.0
                # NPORT pctVal can sum to >100% (cash sweeps / leverage counted on top of equity);
                # scale down so a fund never distributes more than its own market value.
                tp = fh.get("total_pct") or 0
                scale = (100.0 / tp) if tp > 100 else 1.0
                for h in fh.get("holdings", []):
                    pct = h.get("pct") or 0
                    contrib = val * pct / 100.0 * scale
                    if contrib <= 0:
                        continue
                    utkr = (h.get("ticker") or "").upper().strip()
                    nn = _norm_name(h.get("name") or "")
                    merged = direct_name_to_ticker.get(nn) if nn else None
                    if utkr:
                        key, ktkr = utkr, utkr
                    elif merged:                      # fund holding matched one of your direct names
                        key, ktkr = merged, merged
                    elif h.get("cusip"):              # else dedupe cross-fund by CUSIP
                        key, ktkr = "CUSIP:" + h["cusip"], None
                    elif nn:                          # else dedupe by normalized name
                        key, ktkr = "NAME:" + nn, None
                    else:
                        continue
                    add(key, h.get("name"), ktkr, fund=contrib, src=tkr)
                    distributed += contrib
                    # asset-class sleeve split by N-PORT assetCat (equity → the fund's home sleeve)
                    _bkt = _assetcat_bucket(h.get("asset_cat"))
                    _sl = equity_home if _bkt == "equity" else (
                        "Bond" if _bkt == "bond" else ("Cash" if _bkt == "cash" else "Other"))
                    acv[_sl] = acv.get(_sl, 0.0) + contrib
                residual = round(val - distributed, 2)
                if residual > 1:
                    add("RESID:" + tkr, tkr + " — other holdings", None, fund=residual, src=tkr)
                    acv[_sleeve(c)] = acv.get(_sleeve(c), 0.0) + residual   # undistributed → fund's heuristic sleeve
                funds_status.append({"ticker": tkr, "resolved": True, "as_of": fh.get("as_of"),
                                     "count": fh.get("count"),
                                     "ticker_coverage_pct": fh.get("ticker_coverage_pct"),
                                     "distributed_pct": round(distributed / val * 100, 1) if val else None})
            else:
                add("FUND:" + tkr, tkr + " (not looked through)", tkr, fund=val, src=tkr)
                acv[_sleeve(c)] = acv.get(_sleeve(c), 0.0) + val   # unresolved fund → heuristic sleeve
                funds_status.append({"ticker": tkr, "resolved": False,
                                     "reason": (fh or {}).get("reason", "unresolved")})
        elif typ == "Cash":
            add("CASH", "Cash", None, direct=val)
            acv["Cash"] = acv.get("Cash", 0.0) + val
        else:
            add(tkr, c.get("name") or tkr, tkr, direct=val, src="direct")
            acv[_sleeve(c)] = acv.get(_sleeve(c), 0.0) + val   # direct holding → its sleeve

    rows = []
    for e in agg.values():
        total = round(e["direct"] + e["fund"], 2)
        rows.append({"ticker": e["ticker"], "name": e["name"],
                     "direct_value": round(e["direct"], 2), "fund_value": round(e["fund"], 2),
                     "total_value": total,
                     "weight_pct": round(total / invested * 100, 2) if invested else None,
                     "held_via": sorted(s for s in e["sources"] if s != "direct"),
                     "also_direct": e["direct"] > 0})
    rows.sort(key=lambda r: -(r["total_value"] or 0))
    # names whose TRUE weight is materially bigger than the direct-only view would show
    hidden = [r for r in rows
              if r["fund_value"] > 0 and (r["weight_pct"] or 0) >= 3
              and not (r["ticker"] and r["ticker"].startswith(("FUND", "RESID")))
              and r["ticker"] and r["name"] not in ("Cash",)]
    hidden.sort(key=lambda r: -(r["fund_value"] or 0))
    resolved_n = sum(1 for f in funds_status if f.get("resolved"))
    return {"generated_at": int(time.time()), "invested_value": invested,
            "true_exposure": rows[:40], "funds": funds_status,
            "asset_class_value": {k: round(v, 2) for k, v in acv.items()},
            "funds_resolved": resolved_n, "funds_total": len(funds_status),
            "hidden_exposure": [{"ticker": r["ticker"], "name": r["name"],
                                 "total_value": r["total_value"], "fund_value": r["fund_value"],
                                 "weight_pct": r["weight_pct"], "held_via": r["held_via"],
                                 "also_direct": r["also_direct"]} for r in hidden[:12]],
            "note": "TRUE single-name exposure = direct holdings + your proportional share of each "
                    "fund's underlying holdings (SEC NPORT-P, filed monthly with a lag). Fund holdings "
                    "fold into your direct positions by ticker, or by normalized company NAME when the "
                    "fund reports no ticker (most do); otherwise they dedupe by CUSIP/name as their own "
                    "rows. Undistributed fund value is a per-fund residual so the table sums to invested. "
                    "'hidden_exposure' = names whose TRUE weight the direct-only view understates. "
                    "Informational, not advice."}


def _read_lt():
    try:
        with open(LT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _do_lt():
    if not _lt_lock.acquire(blocking=False):
        return
    try:
        res = _lookthrough()
        os.makedirs(os.path.dirname(LT_FILE) or ".", exist_ok=True)
        with open(LT_FILE, "w", encoding="utf-8") as f:
            json.dump(res, f)
    except Exception:
        pass
    finally:
        _lt_lock.release()


@app.get("/api/lookthrough")
def get_lookthrough():
    """TRUE single-name exposure via SEC fund look-through, CACHED (many SEC calls per fund)."""
    cached = _read_lt()
    fresh = bool(cached) and (time.time() - (cached.get("generated_at") or 0) < LT_TTL)
    if not fresh and not _lt_lock.locked():
        threading.Thread(target=_do_lt, daemon=True).start()
    if cached:
        return {**cached, "cached": True, "refreshing": (not fresh)}
    return {"error": "first fund look-through is running (fetching NPORT-P filings) — refresh in a moment",
            "computing": True, "true_exposure": [], "funds": []}


@app.post("/api/run-lookthrough")
def run_lookthrough(x_analysis_token: str = Header(default="")):
    if ANALYSIS_TOKEN and x_analysis_token != ANALYSIS_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if _lt_lock.locked():
        return {"ok": True, "status": "already running"}
    threading.Thread(target=_do_lt, daemon=True).start()
    return {"ok": True, "status": "started"}


# ---------------- News-driven Discovery + Holdings Alerts (proactive, read-only) ----------------
NEWS_MATERIAL_KW = ("earnings", "guidance", "beats", "misses", "upgrade", "downgrade", "raises", "cuts", "slash",
                    "acquisition", "merger", "buyout", "stake", "lawsuit", "investigation", "probe", "fine",
                    "recall", "contract", "deal", "partnership", "approval", "wins", "results", "forecast",
                    "outlook", "dividend", "split", "bankrupt", "delist", "layoff", "selloff", "plunge",
                    "plummet", "surge", "soar", "jump", "slump", "slide", "tumble", "rally", "short", "52-week",
                    "decline", "meltdown", "record", "target", "analyst", "launch", "secures", "award",
                    "selected", "funding", "order", "sec ")
_disc_lock = threading.Lock()
_disc_state = {"status": "idle", "started": 0}


def _parse_tavily_text(text):
    """mcpo's tavily_news_search returns a TEXT blob: per-article 'Title: ..', 'URL: ..', 'Content: ..',
    'Published: ..' lines. Parse it into article dicts."""
    arts, cur = [], {}
    for line in (text or "").split("\n"):
        s = line.strip()
        if s.startswith("Title:"):
            if cur.get("title"):
                arts.append(cur)
                cur = {}
            cur["title"] = s[6:].strip()
        elif s.startswith("URL:"):
            cur["url"] = s[4:].strip()
        elif s.startswith("Content:"):
            cur["content"] = s[8:].strip()
        elif s.startswith("Published:"):
            cur["published_date"] = s[10:].strip()
    if cur.get("title"):
        arts.append(cur)
    return arts


def _tavily_results(query, days=14, max_results=5):
    """Tavily news search via mcpo -> list of article dicts. mcpo returns a TEXT blob (not JSON)."""
    try:
        r = requests.post(f"{MCPO}/tavily/tavily_news_search",
                          headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
                          json={"query": query, "days": days, "max_results": max_results}, timeout=45)
        if r.status_code >= 400:
            return []
        try:
            data = r.json()
        except ValueError:
            data = r.text
    except Exception:
        return []
    if isinstance(data, str):
        return _parse_tavily_text(data)
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        return data["results"]
    found = []                                  # fallback: first list of article-like dicts

    def walk(o):
        if found:
            return
        if isinstance(o, list) and o and isinstance(o[0], dict) and any(k in o[0] for k in ("title", "url", "content")):
            found.extend(o)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    return found


def _company_news(sym, name, days=14, max_results=4):
    out = []
    for a in _tavily_results(f"{sym} {name} stock news", days, max_results)[:max_results]:
        title = (a.get("title") or "").strip()
        if not title:
            continue
        low = title.lower()
        out.append({"title": title[:200], "url": a.get("url"),
                    "date": a.get("published_date") or a.get("publishedDate") or a.get("date"),
                    "material": any(kw in low for kw in NEWS_MATERIAL_KW)})
    return out


def _holdings_alerts(d, limit):
    alerts = []
    for c in (d.get("consolidated") or [])[:limit]:
        t = c.get("ticker")
        if not t:
            continue
        material = [n for n in _company_news(t, c.get("name") or "") if n["material"]]
        filings = edgar.recent_8k(t)               # authoritative SEC 8-K material-event filings
        if material or filings:
            alerts.append({"ticker": t, "name": c.get("name"), "weight_pct": c.get("weight_pct"),
                           "headlines": material[:3], "filings": filings[:3]})
    return alerts


def _discover(d, crit):
    """Tavily news + one LLM extraction -> candidate tickers, then screen them (excludes held names)."""
    cfg = _read_scanner()
    snippets = []
    for q in (cfg.get("discovery_queries") or [])[:4]:
        for a in _tavily_results(q, days=10, max_results=5):
            ttl = (a.get("title") or "")
            if ttl:
                snippets.append(f"- {ttl}. {(a.get('content') or a.get('snippet') or '')[:180]}")
    if not snippets:
        return []
    held = {c.get("ticker") for c in (d.get("consolidated") or []) if c.get("ticker")}
    prompt = ("From the financial news snippets below, extract up to 10 US-listed stock TICKER symbols that "
              "represent potential growth/opportunity candidates a growth investor might research. Prefer specific, "
              "lesser-known names over mega-caps. Return ONLY JSON: "
              "{\"candidates\":[{\"ticker\":\"XXX\",\"reason\":\"...\"}]}\n" + "\n".join(snippets[:40]))
    raw, _ = _ollama_generate(prompt)
    parsed = _parse_json_obj(raw) if raw else None
    cands = (parsed or {}).get("candidates") if isinstance(parsed, dict) else None
    out, seen = [], set()
    for c in (cands or [])[:12]:
        t = str((c or {}).get("ticker") or "").upper().strip()
        if not t or t in seen or t in held:
            continue
        seen.add(t)
        try:
            sc = _screen_one(t, crit)
            sc["discovery_reason"] = (c.get("reason") or "")[:160]
            out.append(sc)
        except Exception:
            continue
    maxp, minp = crit.get("max_share_price"), crit.get("min_share_price")
    if maxp:
        out = [x for x in out if not x.get("price") or x["price"] <= maxp]
    if minp:
        out = [x for x in out if not x.get("price") or x["price"] >= minp]
    out.sort(key=lambda x: -(x.get("fit_score") or 0))
    out = out[:OPP_MAX_IDEAS]
    # Attach a FULL research brief (thesis / bull / bear / risks) to the top discovered names,
    # same pipeline the daily analysis runs on holdings. LLM per ticker -> capped by DISC_BRIEF_LIMIT.
    for sc in out[:DISC_BRIEF_LIMIT]:
        t = sc["ticker"]
        try:
            fund = edgar.fundamentals(t)
            val = _valuation(t)
            g = _grounded(t)
            nm = (fund.get("entity") if isinstance(fund, dict) else None) or t
            sc["name"] = nm
            sc["fundamentals"] = fund
            sc["valuation"] = val
            sc["grounded"] = g
            sc["llm"] = _llm(t, nm, None, None, g, _news(t, nm), fund, val)
        except Exception as e:
            sc["llm"] = {"error": str(e)}
    return out


def _do_discovery():
    if not _disc_lock.acquire(blocking=False):
        return
    try:
        _disc_state.update(status="running", started=int(time.time()))
        d = _data()
        cfg = _read_scanner()
        out = {"generated_at": int(time.time()),
               "alerts": _holdings_alerts(d, cfg.get("alert_holdings_limit") or 15),
               "discovered": _discover(d, cfg.get("criteria") or {}),
               "note": "News-discovered ideas + material-news alerts on holdings. Read-only research, not advice."}
        os.makedirs(os.path.dirname(DISCOVERY_FILE) or ".", exist_ok=True)
        with open(DISCOVERY_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        _disc_state["status"] = "idle"
    finally:
        _disc_lock.release()


@app.get("/api/discovery")
def get_discovery():
    """Stored news-discovery report: ideas found from the newsfeed + material-news alerts on holdings."""
    try:
        with open(DISCOVERY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"generated_at": None, "discovered": [], "alerts": [],
                "note": "No discovery run yet — POST /api/run-discovery (or the scheduled n8n job)."}


@app.post("/api/run-discovery")
def run_discovery(x_analysis_token: str = Header(default="")):
    """Trigger the background news discovery + holdings-alerts run (n8n schedules this)."""
    if ANALYSIS_TOKEN and x_analysis_token != ANALYSIS_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if _disc_lock.locked():
        return {"ok": True, "status": "already running", **_disc_state}
    threading.Thread(target=_do_discovery, daemon=True).start()
    return {"ok": True, "status": "started"}


@app.get("/api/run-discovery/status")
def run_discovery_status():
    return _disc_state


# ---------------- Paper-trading ledger for the What-If / Strategist agent (HYPOTHETICAL, sandboxed) ----------------
def _apply_recurring(p):
    """Lazily apply any due recurring contributions — a self-contained 'cron' that runs on read,
    so recurring deposits work without an external scheduler. Adds cash, tracks contributed capital,
    logs a deposit transaction per period, and advances the next date."""
    rec = p.get("recurring")
    if not rec or not rec.get("amount") or not rec.get("next_ts"):
        return False
    now = int(time.time())
    changed = False
    n = 0
    while rec["next_ts"] <= now and n < 120:
        amt = float(rec["amount"])
        p["cash"] = round((p.get("cash") or 0) + amt, 2)
        p["contributed"] = round((p.get("contributed") or 0) + amt, 2)
        p.setdefault("transactions", []).append({"ts": rec["next_ts"], "action": "deposit",
            "ticker": "CASH", "shares": 0, "price": 0, "value": round(amt, 2), "recurring": True})
        rec["next_ts"] += int(rec.get("cadence_days") or 30) * 86400
        n += 1
        changed = True
    return changed


def _read_paper():
    try:
        with open(PAPER_FILE, encoding="utf-8") as f:
            p = json.load(f)
    except Exception:
        p = {"starting_cash": DEFAULT_PAPER_CASH, "cash": DEFAULT_PAPER_CASH, "contributed": 0.0,
             "positions": {}, "transactions": [], "reports": [], "created": int(time.time())}
    if _apply_recurring(p):
        _write_paper(p)
    return p


def _write_paper(p):
    os.makedirs(os.path.dirname(PAPER_FILE) or ".", exist_ok=True)
    with open(PAPER_FILE, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)


def _paper_marked(p):
    """Mark the hypothetical ledger to current market prices."""
    positions, holdings_val = [], 0.0
    for t, pos in (p.get("positions") or {}).items():
        sh = pos.get("shares") or 0
        if sh <= 0:
            continue
        px = _price(t)
        val = round(sh * px, 2) if px else None
        cost = round(sh * (pos.get("avg_cost") or 0), 2)
        gain = round(val - cost, 2) if val is not None else None
        if val:
            holdings_val += val
        positions.append({"ticker": t, "shares": round(sh, 4), "avg_cost": pos.get("avg_cost"),
                          "price": px, "value": val, "cost": cost, "gain": gain,
                          "gain_pct": round(gain / cost * 100, 2) if (gain is not None and cost) else None})
    positions.sort(key=lambda x: -(x["value"] or 0))
    cash = round(p.get("cash", 0), 2)
    total = round(cash + holdings_val, 2)
    start = p.get("starting_cash")
    start = DEFAULT_PAPER_CASH if start is None else start   # a seeded book can legitimately hold $0 cash
    contributed = round(p.get("contributed") or 0.0, 2)
    seed_basis = round(p.get("seed_basis") or 0.0, 2)    # cost of positions the book was seeded with
    basis = start + contributed + seed_basis             # capital actually put in (cash + deposits + seeded cost)
    return {"starting_cash": start, "contributed": contributed, "seed_basis": seed_basis,
            "seeded_from": p.get("seeded_from"), "invested_capital": round(basis, 2),
            "cash": cash, "holdings_value": round(holdings_val, 2),
            "total_value": total, "total_return": round(total - basis, 2),
            "return_pct": round((total - basis) / basis * 100, 2) if basis else None,
            "recurring": p.get("recurring"),
            "positions": positions, "transactions": (p.get("transactions") or [])[-40:],
            "reports": (p.get("reports") or [])[-12:], "created": p.get("created"),
            "note": "HYPOTHETICAL paper-trading portfolio — no real money, no real orders. Return is "
                    "measured against invested capital (starting cash + contributions)."}


@app.get("/api/paper")
def get_paper():
    """The Strategist's hypothetical paper portfolio, marked to current prices."""
    return _paper_marked(_read_paper())


def _exec_trade(action, ticker, dollars=None, shares=None):
    """Core paper-trade logic at the current price. Returns (result_dict, error_str)."""
    action = str(action or "").lower()
    t = str(ticker or "").upper().strip()
    if action not in ("buy", "sell") or not t:
        return None, "need action (buy|sell) and ticker"
    px = _price(t)
    if not px:
        return None, f"no price available for {t}"
    p = _read_paper()
    positions = p.setdefault("positions", {})
    pos = positions.get(t) or {"shares": 0.0, "avg_cost": 0.0}
    if action == "buy":
        if dollars:
            sh = float(dollars) / px
        elif shares:
            sh = float(shares)
        else:
            return None, "buy needs dollars or shares"
        cost = sh * px
        if cost > p.get("cash", 0) + 0.01:
            return None, f"insufficient cash: need ${cost:,.2f}, have ${p.get('cash', 0):,.2f}"
        new_sh = pos["shares"] + sh
        pos["avg_cost"] = round((pos["shares"] * pos["avg_cost"] + cost) / new_sh, 4) if new_sh else px
        pos["shares"] = round(new_sh, 6)
        positions[t] = pos
        p["cash"] = round(p.get("cash", 0) - cost, 2)
        traded = sh
    else:
        sh = pos["shares"] if (shares in (None, "all", "ALL", "")) else float(shares)
        sh = min(sh, pos["shares"])
        if sh <= 0:
            return None, f"no shares of {t} held to sell"
        p["cash"] = round(p.get("cash", 0) + sh * px, 2)
        pos["shares"] = round(pos["shares"] - sh, 6)
        if pos["shares"] <= 0.0001:
            positions.pop(t, None)
        else:
            positions[t] = pos
        traded = sh
    p.setdefault("transactions", []).append({"ts": int(time.time()), "action": action, "ticker": t,
                                             "shares": round(traded, 6), "price": round(px, 4),
                                             "value": round(traded * px, 2)})
    _write_paper(p)
    return {"ok": True, "action": action, "ticker": t, "shares": round(traded, 6),
            "price": px, "cash": p["cash"]}, None


@app.post("/api/paper/trade")
async def paper_trade(req: Request):
    """Execute a HYPOTHETICAL trade. Body: {action: buy|sell, ticker, dollars|shares}. Paper only."""
    body = await req.json()
    res, err = _exec_trade(body.get("action"), body.get("ticker"), body.get("dollars"), body.get("shares"))
    return res if res else JSONResponse({"error": err}, status_code=400)


@app.post("/api/paper/reset")
async def paper_reset(req: Request):
    """Reset the hypothetical portfolio to a starting cash balance. Body: {starting_cash}."""
    body = await req.json()
    cash = float(body.get("starting_cash") or DEFAULT_PAPER_CASH)
    _write_paper({"starting_cash": cash, "cash": cash, "contributed": 0.0, "seed_basis": 0.0,
                  "positions": {}, "transactions": [], "reports": [], "recurring": None,
                  "seeded_from": None, "created": int(time.time())})
    return {"ok": True, "starting_cash": cash}


@app.post("/api/paper/seed")
async def paper_seed(req: Request):
    """RESET the paper portfolio pre-seeded with a selection of the owner's REAL holdings (real share
    counts). Body: {tickers: [...] | "all", cash: 0, basis: "current"|"real"}. basis 'current' seeds
    each position at today's price (return starts ~0 — measures the Strategist's forward decisions);
    'real' carries your actual per-share cost basis (return then reflects embedded gains). Mutual funds
    and cash are skipped (no live price to trade). HYPOTHETICAL — no real orders."""
    body = await req.json()
    sel = body.get("tickers")
    try:
        cash = float(body.get("cash") or 0)
    except (TypeError, ValueError):
        cash = 0
    basis_mode = str(body.get("basis") or "current").lower()
    d = _data()
    by_ticker = {c.get("ticker"): c for c in d.get("consolidated", []) if c.get("ticker")}
    if sel in (None, "all", "ALL", "*"):
        want = [t for t, c in by_ticker.items() if c.get("type") not in ("Cash", "Mutual Fund")]
    else:
        want = [str(t).upper().strip() for t in sel if t]
    positions, txns, seed_cost, seed_mkt, skipped = {}, [], 0.0, 0.0, []
    ts = int(time.time())
    for t in want:
        c = by_ticker.get(t)
        px = (c.get("price") if c else None) or _price(t)
        qty = c.get("quantity") if c else None
        if not c or not px or not qty or c.get("type") in ("Cash", "Mutual Fund"):
            skipped.append(t)
            continue
        avg = round(c["cost_total"] / qty, 4) if (basis_mode == "real" and c.get("cost_total")) else round(px, 4)
        positions[t] = {"shares": round(qty, 6), "avg_cost": avg}
        seed_cost += qty * avg
        seed_mkt += qty * px
        txns.append({"ts": ts, "action": "seed", "ticker": t, "shares": round(qty, 6),
                     "price": round(px, 4), "value": round(qty * px, 2)})
    _write_paper({"starting_cash": round(cash, 2), "cash": round(cash, 2), "contributed": 0.0,
                  "seed_basis": round(seed_cost, 2), "positions": positions, "transactions": txns,
                  "reports": [], "recurring": None, "created": ts,
                  "seeded_from": f"real_holdings ({basis_mode} basis)"})
    return {"ok": True, "seeded": len(positions), "skipped": skipped,
            "seed_market_value": round(seed_mkt, 2), "seed_cost_basis": round(seed_cost, 2),
            "cash": round(cash, 2), "invested_capital": round(seed_cost + cash, 2), "basis_mode": basis_mode}


def _log_report(summary):
    """Append a weekly report + snapshot total value. Returns {ok, report_count}."""
    p = _read_paper()
    marked = _paper_marked(p)
    p.setdefault("reports", []).append({"ts": int(time.time()), "total_value": marked["total_value"],
                                        "return_pct": marked["return_pct"],
                                        "summary": str(summary or "")[:4000]})
    p["reports"] = p["reports"][-26:]
    _write_paper(p)
    return {"ok": True, "report_count": len(p["reports"])}


@app.post("/api/paper/report")
async def paper_report(req: Request):
    """Log a weekly strategy report (the agent self-documents). Body: {summary}. Snapshots total value."""
    body = await req.json()
    return _log_report(body.get("summary"))


@app.post("/api/paper/contribute")
async def paper_contribute(req: Request):
    """Add a ONE-TIME cash contribution to the hypothetical portfolio. Body: {amount}. Tracked as
    contributed capital so the return % stays honest (measured vs starting cash + contributions)."""
    body = await req.json()
    try:
        amt = float(body.get("amount") or 0)
    except (TypeError, ValueError):
        amt = 0
    if amt <= 0:
        return JSONResponse({"error": "amount must be > 0"}, status_code=400)
    p = _read_paper()
    p["cash"] = round((p.get("cash") or 0) + amt, 2)
    p["contributed"] = round((p.get("contributed") or 0) + amt, 2)
    p.setdefault("transactions", []).append({"ts": int(time.time()), "action": "deposit",
        "ticker": "CASH", "shares": 0, "price": 0, "value": round(amt, 2)})
    _write_paper(p)
    return {"ok": True, "added": round(amt, 2), "cash": p["cash"], "contributed": p["contributed"]}


@app.post("/api/paper/recurring")
async def paper_recurring(req: Request):
    """Set or clear a RECURRING cash contribution. Body: {amount, cadence: weekly|biweekly|monthly}.
    amount<=0 clears it. Applied lazily on read (no external scheduler needed)."""
    body = await req.json()
    try:
        amt = float(body.get("amount") or 0)
    except (TypeError, ValueError):
        amt = 0
    p = _read_paper()
    if amt <= 0:
        p["recurring"] = None
        _write_paper(p)
        return {"ok": True, "recurring": None}
    cadence = str(body.get("cadence") or "monthly").lower()
    days = {"weekly": 7, "biweekly": 14, "monthly": 30}.get(cadence, 30)
    p["recurring"] = {"amount": round(amt, 2), "cadence": cadence, "cadence_days": days,
                      "next_ts": int(time.time()) + days * 86400}
    _write_paper(p)
    return {"ok": True, "recurring": p["recurring"]}


@app.post("/api/paper/run")
def paper_run():
    """Dashboard trigger for the Strategist's weekly review (local/unguarded; single-flighted by the
    same lock as /api/run-strategist, so it can't pile up concurrent runs)."""
    if _strat_lock.locked():
        return {"ok": True, "status": "already running", **_strat_state}
    threading.Thread(target=_do_strategist, daemon=True).start()
    return {"ok": True, "status": "started"}


# ---------------------------------------------------------------------------
# Date grounding — prepended to EVERY LLM persona/analysis system prompt.
# The local models' training data predates "today" (left to themselves they
# guess ~early 2025), so without an explicit current date they misresolve
# "this year", the current tax year / standard deduction / brackets, TTM
# windows, "recent" news, etc. The OWUI-era personas carried a
# `TODAY IS {{CURRENT_DATE}}` line; the in-app rewrite dropped it — this is the
# single chokepoint that restores it for the chat (Fiduciary + Strategist),
# the paper-trading run, the advisory run, and the research briefs.
# ---------------------------------------------------------------------------
def _today_line():
    t = datetime.date.today()
    human = t.strftime("%A, %B ") + str(t.day) + ", " + str(t.year)
    return (f"TODAY IS {t.isoformat()} ({human}). Resolve all relative dates (\"this year\", "
            "\"this quarter\", \"YTD\", \"last week\", and the \"current\" tax year / standard "
            "deduction / tax brackets) against it. Your training data predates today, so DISTRUST "
            "any date, tax-year, or \"current\" figure you recall — trust this date and your tools, "
            "and use web_search when a value may have changed since your training.")


def _sys(prompt):
    """System prompt with the current date grounded in front of it."""
    return _today_line() + "\n\n" + prompt


# ---------------------------------------------------------------------------
# STRATEGIST — server-side autonomous tool loop (replaces the OWUI/n8n path).
# Drives Ollama /api/chat directly; every tool is an in-process function, so
# the loop runs to completion under OUR iteration cap (no OWUI pass-through,
# no per-turn tool-call limit). HYPOTHETICAL paper account only — no real money.
# ---------------------------------------------------------------------------
STRATEGIST_PROMPT = (
    "You are the Strategist — an autonomous portfolio manager running a HYPOTHETICAL paper-trading "
    "account (a SIMULATION; no real money, no real orders are ever placed). Your job is to grow a small "
    "starting balance through disciplined, well-reasoned buy/sell decisions and to report honestly.\n"
    "ACCOUNT TOOLS: get_paper_portfolio (your cash, positions, P/L, return vs. start — call this FIRST); "
    "paper_buy(ticker, dollars); paper_sell(ticker, shares — omit shares to sell the whole position); "
    "log_report(summary — call this LAST, exactly once).\n"
    "RESEARCH TOOLS: get_opportunities and get_discovery (idea sources); get_fundamentals(ticker) and "
    "get_valuation(ticker); get_research(ticker) (a Buy/Sell/Hold brief); get_holdings (The user's REAL "
    "portfolio, for context only — never trade it); web_search(query) (LIVE web/news via Tavily — use it "
    "to surface NEW opportunities beyond get_discovery and to check what's moving a name now). web_search "
    "is BUDGET-LIMITED per run: spend it on high-value discovery, not on every name, and once it reports "
    "the budget is spent, decide from what you already have and finish.\n"
    "When you read fundamentals, prefer the `ttm` (trailing-twelve-month) margins over a single annual "
    "figure — a trough or peak fiscal year misleads; check TTM and the multi-year trend before judging.\n"
    "ROUTINE each run: (1) get_paper_portfolio. (2) Research a FEW promising ideas — do NOT deep-dive "
    "every name; you have a limited number of steps, so be efficient. (3) Make the trades you judge "
    "improve risk-adjusted return: stay within available cash, size positions sensibly, favor conviction "
    "over churn. (4) log_report: the trades you made and WHY, current value and return vs. start, your "
    "market read, and what you're watching. For every buy, state a one-line thesis and the key risk. "
    "SIZING: return scales with DOLLARS invested x % return, NOT with share count or share price — a "
    "$360 stock is not worse 'bang for the buck' than a $12 one, so never prefer a name just because its "
    "shares are cheaper. What DOES matter: (a) make each position MATERIAL — big enough that a good move "
    "moves the book (avoid dust positions too small to matter); (b) deploy the capital — don't leave a "
    "meaningful slug of cash idle; you buy fractional shares here, so a dollar amount always fully invests. "
    "Acknowledge uncertainty honestly. This is hypothetical and educational — NOT financial advice."
)

STRATEGIST_TOOLS = [
    {"type": "function", "function": {"name": "get_paper_portfolio",
        "description": "Your hypothetical paper portfolio marked to current prices: cash, positions, P/L, "
                       "total value and return vs. your starting balance. Call FIRST.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "paper_buy",
        "description": "Hypothetically buy a dollar amount of a ticker at the current price (fractional "
                       "shares allowed). Fails if you lack the cash.",
        "parameters": {"type": "object", "properties": {
            "ticker": {"type": "string", "description": "Ticker symbol, e.g. NVDA."},
            "dollars": {"type": "number", "description": "Dollar amount to invest."}},
            "required": ["ticker", "dollars"]}}},
    {"type": "function", "function": {"name": "paper_sell",
        "description": "Hypothetically sell shares of a held ticker at the current price. Omit shares to "
                       "sell the entire position.",
        "parameters": {"type": "object", "properties": {
            "ticker": {"type": "string"},
            "shares": {"type": "number", "description": "Shares to sell; omit to sell all."}},
            "required": ["ticker"]}}},
    {"type": "function", "function": {"name": "log_report",
        "description": "Record your weekly report and snapshot total value. Call LAST, once.",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string", "description": "Your written weekly report."}},
            "required": ["summary"]}}},
    {"type": "function", "function": {"name": "get_opportunities",
        "description": "Screened idea list (watchlist + peers) scored against the strategy criteria.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_discovery",
        "description": "News-discovered ideas plus news/SEC-8K alerts on current holdings.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_fundamentals",
        "description": "SEC GAAP fundamentals and ratios (growth, margins, leverage, FCF) for a ticker.",
        "parameters": {"type": "object", "properties": {"ticker": {"type": "string"}},
                       "required": ["ticker"]}}},
    {"type": "function", "function": {"name": "get_valuation",
        "description": "Valuation multiples (P/E, P/B, P/S, dividend yield, beta) for a ticker.",
        "parameters": {"type": "object", "properties": {"ticker": {"type": "string"}},
                       "required": ["ticker"]}}},
    {"type": "function", "function": {"name": "get_research",
        "description": "The latest daily Buy/Sell/Hold research brief for a ticker, if one exists.",
        "parameters": {"type": "object", "properties": {"ticker": {"type": "string"}},
                       "required": ["ticker"]}}},
    {"type": "function", "function": {"name": "get_holdings",
        "description": "The user's REAL portfolio (net worth + consolidated holdings) — context only.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "web_search",
        "description": "Live web/news search via Tavily — use it to UNCOVER fresh opportunities (themes, "
                       "sector catalysts, analyst moves, names not already in get_discovery) and to check "
                       "what's driving a stock right now. Returns recent article titles/snippets with links.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "days": {"type": "number", "description": "Look-back window in days (default 14)."},
            "max_results": {"type": "number", "description": "Max articles (default 5)."}},
            "required": ["query"]}}},
]

STRAT_NUM_CTX = int(os.environ.get("STRAT_NUM_CTX", "32768"))         # full context — don't truncate the agent's working memory
STRAT_LLM_TIMEOUT = int(os.environ.get("STRAT_LLM_TIMEOUT", "1800"))   # per-call ceiling; weekly bg job, reasoning ON is slow on Pascal
_strat_lock = threading.Lock()
_strat_state = {"status": "idle", "iterations": 0, "started": 0, "finished": 0}

# --- Strategist run telemetry (N13 candidate 3): a durable, capped JSONL audit log of every
#     autonomous weekly run so the ReAct loop is OBSERVABLE, not a black box. One record per run:
#     iterations used, how it ended (report / no_tool_calls / cap / error), whether it converged,
#     repeated ("wandering") tool calls, trades placed, search-budget burn, per-tool histogram,
#     duration. Best-effort — never raises, never influences the run. ---
STRAT_RUN_LOG_FILE = os.environ.get("STRAT_RUN_LOG_FILE", "/data/strategist_runs.jsonl")
STRAT_RUN_LOG_MAX = int(os.environ.get("STRAT_RUN_LOG_MAX", "200"))


def _log_strategist_run(rec):
    try:
        os.makedirs(os.path.dirname(STRAT_RUN_LOG_FILE) or ".", exist_ok=True)
        try:
            with open(STRAT_RUN_LOG_FILE, encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
        except Exception:
            lines = []
        lines.append(json.dumps(rec, default=str))
        lines = lines[-STRAT_RUN_LOG_MAX:]
        with open(STRAT_RUN_LOG_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


def _strategist_dispatch(name, args):
    """Run one tool call in-process and return a JSON-serializable result."""
    args = args or {}
    try:
        if name == "get_paper_portfolio":
            return _paper_marked(_read_paper())
        if name == "paper_buy":
            res, err = _exec_trade("buy", args.get("ticker"), dollars=args.get("dollars"),
                                   shares=args.get("shares"))
            return res or {"error": err}
        if name == "paper_sell":
            res, err = _exec_trade("sell", args.get("ticker"), shares=args.get("shares"))
            return res or {"error": err}
        if name == "log_report":
            return _log_report(args.get("summary"))
        if name == "get_opportunities":
            return _opportunities()
        if name == "get_discovery":
            try:
                with open(DISCOVERY_FILE, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {"discovered": [], "alerts": []}
        if name == "get_fundamentals":
            return edgar.fundamentals(str(args.get("ticker") or "").upper())
        if name == "get_valuation":
            return _valuation(str(args.get("ticker") or "").upper()) or {"available": False}
        if name == "get_research":
            t = str(args.get("ticker") or "").upper()
            return (_read_analysis().get("briefs") or {}).get(t) or {"note": f"no brief for {t}"}
        if name == "get_holdings":
            d = _data()
            top = sorted((d.get("consolidated") or []), key=lambda c: c.get("value") or 0, reverse=True)
            return {"net_worth": d.get("net_worth"),
                    "holdings": [{"ticker": c.get("ticker"), "name": c.get("name"),
                                  "weight_pct": c.get("weight_pct"), "gain_pct": c.get("gain_pct")}
                                 for c in top[:25] if c.get("ticker")]}
        if name == "web_search":
            return {"results": _tavily_results(str(args.get("query") or ""),
                                               days=int(args.get("days") or 14),
                                               max_results=int(args.get("max_results") or 5))}
        return {"error": f"unknown tool {name}"}
    except Exception as e:
        return {"error": f"{name} failed: {e}"}


def _do_strategist(max_iter=20):
    if not _strat_lock.acquire(blocking=False):
        return
    _t0 = time.time()
    # Per-run telemetry — pure instrumentation; it records what the loop did, never changes decisions.
    telem = {"ts": int(_t0), "max_iter": max_iter, "iterations": 0, "tool_calls": {},
             "repeat_calls": 0, "trades": 0, "search_calls": 0, "search_budget": None,
             "nudged": False, "converged": False, "ended": "unknown", "error": None}
    _seen_sigs = set()
    try:
        _strat_state.update(status="running", iterations=0, started=int(time.time()), finished=0)
        messages = [
            {"role": "system", "content": _sys(STRATEGIST_PROMPT)},
            {"role": "user", "content": "Perform your weekly review now: call get_paper_portfolio, research "
             "a few opportunities, make any buy/sell trades you judge worthwhile, then call log_report with "
             "your summary. Be efficient — don't deep-dive every name."},
        ]
        reported = False
        # Per-run web-search budget (settings knob, default 20). Caps ONLY this unattended weekly
        # run so an agentic loop can't spray Tavily calls / crowd the free-tier pool it shares with
        # the daily pipelines. The interactive chat + on-demand advisory runs are deliberately NOT
        # capped here (they're user-paced).
        search_budget = _knob("strategist_search_budget")
        telem["search_budget"] = search_budget
        search_calls = 0
        for i in range(max_iter):
            _strat_state["iterations"] = i + 1
            telem["iterations"] = i + 1
            # Force convergence before we run out of steps: tell it to stop researching and finish.
            if i == max_iter - 5:
                telem["nudged"] = True
                messages.append({"role": "system", "content": "You have used most of your available steps. "
                    "Stop researching now — place any final trades you want, then call log_report with your "
                    "summary on this turn."})
            try:
                r = requests.post(f"{OLLAMA}/api/chat",
                                  json={"model": ANALYSIS_MODEL, "messages": messages,
                                        "tools": STRATEGIST_TOOLS, "stream": False,
                                        "keep_alive": LLM_KEEP_ALIVE,
                                        "options": {"temperature": 0.4, "num_ctx": STRAT_NUM_CTX}},
                                  timeout=STRAT_LLM_TIMEOUT)
                if r.status_code >= 400:
                    telem["ended"] = "ollama_error"
                    telem["error"] = f"ollama {r.status_code}"
                    _strat_state.update(status=f"error: ollama {r.status_code}: {r.text[:200]}",
                                        finished=int(time.time()))
                    return
                msg = (r.json() or {}).get("message") or {}
            except Exception as e:
                telem["ended"] = "exception"
                telem["error"] = str(e)[:200]
                _strat_state.update(status=f"error: {e}", finished=int(time.time()))
                return
            tcs = msg.get("tool_calls") or []
            asst = {"role": "assistant", "content": msg.get("content") or ""}
            if tcs:
                asst["tool_calls"] = tcs
            messages.append(asst)
            if not tcs:
                telem["ended"] = "no_tool_calls"   # model stopped calling tools without logging a report
                break
            for tc in tcs:
                fn = tc.get("function") or {}
                nm = fn.get("name")
                a = fn.get("arguments") or {}
                if isinstance(a, str):
                    try:
                        a = json.loads(a)
                    except Exception:
                        a = {}
                # telemetry: per-tool count, wandering (identical call repeated in this run), trades placed
                telem["tool_calls"][nm] = telem["tool_calls"].get(nm, 0) + 1
                try:
                    _sig = (nm, json.dumps(a, sort_keys=True, default=str))
                except Exception:
                    _sig = (nm, str(a))
                if _sig in _seen_sigs:
                    telem["repeat_calls"] += 1
                else:
                    _seen_sigs.add(_sig)
                if nm in ("paper_buy", "paper_sell"):
                    telem["trades"] += 1
                if nm == "web_search":
                    if search_calls >= search_budget:
                        result = {"error": f"web-search budget for this run is spent "
                                           f"({search_budget} calls). Stop searching — decide from the "
                                           "research you've already gathered and finish your review."}
                    else:
                        search_calls += 1
                        result = _strategist_dispatch(nm, a)
                else:
                    result = _strategist_dispatch(nm, a)
                if nm == "log_report" and isinstance(result, dict) and result.get("ok"):
                    reported = True
                # Trim large tool results by SHRINKING FIELDS, never by chopping the serialized JSON
                # mid-object (which corrupts what the model reads). Keep it valid JSON always.
                _cap = int(os.environ.get("STRAT_TOOL_RESULT_MAX", "8000"))
                _tr = json.dumps({"tool": nm, "result": result})
                if len(_tr) > _cap:
                    _r = {k: (v[:20] if isinstance(v, list) else v) for k, v in result.items()} \
                        if isinstance(result, dict) else result
                    _tr = json.dumps({"tool": nm, "result": _r, "_note": "large result trimmed to fit context"})
                    if len(_tr) > _cap:
                        _tr = json.dumps({"tool": nm, "_note": "result too large; summarized",
                                          "keys": (list(result.keys()) if isinstance(result, dict) else "list")})
                messages.append({"role": "tool", "content": _tr})
            telem["search_calls"] = search_calls
            if reported:       # report logged — run is complete, don't burn another slow turn
                telem["ended"] = "report"
                break
        if not reported:
            if telem["ended"] == "unknown":
                telem["ended"] = "cap"   # exhausted all iterations without a natural stop or a report
            final = next((m["content"] for m in reversed(messages)
                          if m.get("role") == "assistant" and m.get("content")), "")
            _log_report(final or "Weekly run ended without an explicit report (may have hit the step cap).")
        telem["converged"] = reported
        _strat_state.update(status="idle", finished=int(time.time()))
    finally:
        telem["duration_s"] = round(time.time() - _t0, 1)
        _log_strategist_run(telem)
        _strat_lock.release()


@app.post("/api/run-strategist")
def run_strategist(x_analysis_token: str = Header(default="")):
    """Kick off the autonomous weekly Strategist run (server-side tool loop). Token-guarded; returns
    immediately and runs in the background. Poll /api/run-strategist/status or /api/paper for results."""
    if ANALYSIS_TOKEN and x_analysis_token != ANALYSIS_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if _strat_lock.locked():
        return {"ok": True, "status": "already running", **_strat_state}
    threading.Thread(target=_do_strategist, daemon=True).start()
    return {"ok": True, "status": "started"}


@app.get("/api/run-strategist/status")
def run_strategist_status():
    return _strat_state


@app.get("/api/strategist-runs")
def strategist_runs(limit: int = 50):
    """Telemetry for recent autonomous Strategist runs — observability for the ReAct loop (is it
    converging, wandering, or hitting the step cap?). Read-only. `summary` aggregates the window so
    'does it wander?' is answerable at a glance; `runs` is the newest-first per-run detail."""
    try:
        with open(STRAT_RUN_LOG_FILE, encoding="utf-8") as f:
            rows = [json.loads(x) for x in f.read().splitlines() if x.strip()]
    except Exception:
        rows = []
    rows = rows[-max(1, min(limit, STRAT_RUN_LOG_MAX)):][::-1]
    done = [r for r in rows if r.get("iterations")]
    summary = None
    if done:
        n = len(done)
        summary = {
            "runs": n,
            "avg_iterations": round(sum(r.get("iterations", 0) for r in done) / n, 1),
            "max_iter": done[0].get("max_iter"),
            "converged_pct": round(100 * sum(1 for r in done if r.get("converged")) / n, 1),
            "hit_cap_pct": round(100 * sum(1 for r in done if r.get("ended") == "cap") / n, 1),
            "no_report_pct": round(100 * sum(1 for r in done if r.get("ended") == "no_tool_calls") / n, 1),
            "avg_repeat_calls": round(sum(r.get("repeat_calls", 0) for r in done) / n, 1),
            "avg_trades": round(sum(r.get("trades", 0) for r in done) / n, 1),
            "avg_search_calls": round(sum(r.get("search_calls", 0) for r in done) / n, 1),
            "note": "avg_iterations near max_iter, or non-trivial hit_cap_pct / avg_repeat_calls, is the "
                    "signal that the ReAct loop wanders and a plan-then-execute wrapper would help.",
        }
    return {"summary": summary, "runs": rows}


# ---------------------------------------------------------------------------
# STRATEGIST — ADVISORY mode. Same tool loop, model and data as the weekly run,
# but the three mutating tools (paper_buy / paper_sell / log_report) are BOTH
# withheld from the tool list AND hard-blocked in dispatch, so an advisory run
# can never write to the paper ledger. Answers "what would you do with $X"
# using full research/data capability. Shares _strat_lock so it never competes
# with the weekly run for the GPUs. Read-only.
# ---------------------------------------------------------------------------
STRATEGIST_ADVICE_PROMPT = (
    "You are the Strategist, operating in ADVISORY mode. This is analysis ONLY: you place NO trades and "
    "record nothing — no buy, sell, or report tools exist in this mode. Jason is asking what you would do "
    "with a specific amount of NEW cash. Reason it through with your research and data tools.\n"
    "TOOLS: get_holdings (The user's REAL portfolio — the positions your plan MUST consider); "
    "get_paper_portfolio (the hypothetical book, extra context only); get_opportunities and get_discovery "
    "(idea sources); get_fundamentals(ticker), get_valuation(ticker), get_research(ticker).\n"
    "Prefer `ttm` (trailing-twelve-month) margins over any single annual figure, and check the multi-year "
    "trend before judging a name.\n"
    "DELIVER a concrete deployment plan: specific tickers with dollar sizing that sums to roughly the "
    "amount; for each, state whether you are ADDING to an existing position or OPENING a new one, with a "
    "one-line thesis and the key risk; note any cash you would hold back and why. These are small orders — "
    "weigh fees and liquidity. Be honest about uncertainty. Hypothetical and educational — NOT financial "
    "advice."
)

# Read/research tools only — the three mutating tools are stripped out here.
STRATEGIST_ADVICE_TOOLS = [t for t in STRATEGIST_TOOLS
                           if t["function"]["name"] not in ("paper_buy", "paper_sell", "log_report")]

STRAT_ADVICE_MAX_ITER = int(os.environ.get("STRAT_ADVICE_MAX_ITER", "12"))
_advice_state = {"status": "idle", "iterations": 0, "started": 0, "finished": 0,
                 "amount": None, "question": None, "advice": ""}


def _advice_dispatch(name, args):
    """Dispatch for advisory mode. Hard-blocks any mutating tool even if the model hallucinates one,
    so an advisory run can NEVER write to the paper ledger (defense in depth behind the withheld tools)."""
    if name in ("paper_buy", "paper_sell", "log_report"):
        return {"error": "advisory mode: trading and reporting are disabled — analysis only"}
    return _strategist_dispatch(name, args)


def _do_advice(amount, question, max_iter=None):
    if not _strat_lock.acquire(blocking=False):
        _advice_state.update(status="busy: a strategist run is already in progress")
        return
    try:
        max_iter = STRAT_ADVICE_MAX_ITER if max_iter is None else max_iter
        try:
            amt = float(amount)
        except (TypeError, ValueError):
            amt = None
        ask = (f"I have ${amt:,.0f} of new cash to deploy. " if amt is not None
               else "I have some new cash to deploy. ")
        if question:
            ask += question.strip() + " "
        ask += ("What would you do with it? Consider BOTH adding to my existing positions and opening new "
                "ones. Give specific tickers and dollar sizing. Do not place any trades — advisory only.")
        _advice_state.update(status="running", iterations=0, started=int(time.time()), finished=0,
                             amount=amt, question=question or None, advice="")
        messages = [
            {"role": "system", "content": _sys(STRATEGIST_ADVICE_PROMPT)},
            {"role": "user", "content": ask},
        ]
        for i in range(max_iter):
            _advice_state["iterations"] = i + 1
            if i == max_iter - 3:
                messages.append({"role": "system", "content": "You have used most of your available steps. "
                    "Stop researching now and write your final deployment plan on this turn."})
            try:
                r = requests.post(f"{OLLAMA}/api/chat",
                                  json={"model": ANALYSIS_MODEL, "messages": messages,
                                        "tools": STRATEGIST_ADVICE_TOOLS, "stream": False,
                                        "keep_alive": LLM_KEEP_ALIVE,
                                        "options": {"temperature": 0.4, "num_ctx": STRAT_NUM_CTX}},
                                  timeout=STRAT_LLM_TIMEOUT)
                if r.status_code >= 400:
                    _advice_state.update(status=f"error: ollama {r.status_code}: {r.text[:200]}",
                                         finished=int(time.time()))
                    return
                msg = (r.json() or {}).get("message") or {}
            except Exception as e:
                _advice_state.update(status=f"error: {e}", finished=int(time.time()))
                return
            tcs = msg.get("tool_calls") or []
            asst = {"role": "assistant", "content": msg.get("content") or ""}
            if tcs:
                asst["tool_calls"] = tcs
            messages.append(asst)
            if not tcs:
                break
            for tc in tcs:
                fn = tc.get("function") or {}
                nm = fn.get("name")
                a = fn.get("arguments") or {}
                if isinstance(a, str):
                    try:
                        a = json.loads(a)
                    except Exception:
                        a = {}
                result = _advice_dispatch(nm, a)
                # Same field-shrinking trim as the weekly loop — never chop serialized JSON mid-object.
                _cap = int(os.environ.get("STRAT_TOOL_RESULT_MAX", "8000"))
                _tr = json.dumps({"tool": nm, "result": result})
                if len(_tr) > _cap:
                    _r = {k: (v[:20] if isinstance(v, list) else v) for k, v in result.items()} \
                        if isinstance(result, dict) else result
                    _tr = json.dumps({"tool": nm, "result": _r, "_note": "large result trimmed to fit context"})
                    if len(_tr) > _cap:
                        _tr = json.dumps({"tool": nm, "_note": "result too large; summarized",
                                          "keys": (list(result.keys()) if isinstance(result, dict) else "list")})
                messages.append({"role": "tool", "content": _tr})
        final = next((m["content"] for m in reversed(messages)
                      if m.get("role") == "assistant" and m.get("content")), "")
        _advice_state.update(status="done", advice=final or "(no analysis produced)",
                             finished=int(time.time()))
    finally:
        _strat_lock.release()


@app.post("/api/strategist/advise")
async def strategist_advise(req: Request):
    """ADVISORY Strategist run — full research/data tools, ZERO ledger mutation. Body: {amount, question}.
    Returns immediately; poll /api/strategist/advise/status for the analysis. Read-only."""
    try:
        body = await req.json()
    except Exception:
        body = {}
    if _strat_lock.locked():
        return JSONResponse({"ok": False, "status": "strategist busy — a run is in progress",
                             **_advice_state}, status_code=409)
    amount = (body or {}).get("amount")
    question = (body or {}).get("question")
    threading.Thread(target=_do_advice, args=(amount, question), daemon=True).start()
    return {"ok": True, "status": "started"}


@app.get("/api/strategist/advise/status")
def strategist_advise_status():
    return _advice_state


# ===========================================================================
# DASHBOARD CHAT — native multi-turn, streaming chat with the Fiduciary and
# Strategist personas (the OWUI replacement). Reuses the proven in-process
# Ollama tool loop; SSE token streaming; SQLite-backed session history in the
# persistent /data volume. READ-ONLY: no persona is ever given a mutating tool,
# and dispatch hard-blocks the paper-ledger writers. See docs/DASHBOARD-CHAT-DESIGN.md
# ===========================================================================
import sqlite3
import uuid
from fastapi.responses import StreamingResponse

CHAT_DB = os.environ.get("CHAT_DB", "/data/chat.db")
CHAT_MAX_HISTORY = int(os.environ.get("CHAT_MAX_HISTORY", "24"))   # user/assistant turns fed back to the model
CHAT_MAX_ITER = int(os.environ.get("CHAT_MAX_ITER", "12"))         # tool-loop steps per user message
CHAT_QUEUE_TIMEOUT = int(os.environ.get("CHAT_QUEUE_TIMEOUT", "900"))  # max seconds a chat waits for the GPU lock before giving up


def _chat_conn():
    c = sqlite3.connect(CHAT_DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _chat_init():
    os.makedirs(os.path.dirname(CHAT_DB) or ".", exist_ok=True)
    with _chat_conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, persona TEXT, title TEXT, "
                  "created INTEGER, updated INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                  "session_id TEXT, role TEXT, content TEXT, tool_json TEXT, ts INTEGER)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id)")


def _chat_create_session(persona, title):
    sid = uuid.uuid4().hex[:16]
    now = int(time.time())
    with _chat_conn() as c:
        c.execute("INSERT INTO sessions(id, persona, title, created, updated) VALUES(?,?,?,?,?)",
                  (sid, persona, title, now, now))
    return sid


def _chat_add_message(sid, role, content, tool_json=None):
    with _chat_conn() as c:
        c.execute("INSERT INTO messages(session_id, role, content, tool_json, ts) VALUES(?,?,?,?,?)",
                  (sid, role, content, tool_json, int(time.time())))


def _chat_touch_session(sid, title=None):
    with _chat_conn() as c:
        if title:
            c.execute("UPDATE sessions SET updated=?, title=? WHERE id=?", (int(time.time()), title, sid))
        else:
            c.execute("UPDATE sessions SET updated=? WHERE id=?", (int(time.time()), sid))


def _chat_sessions():
    with _chat_conn() as c:
        rows = c.execute("SELECT id, persona, title, created, updated FROM sessions "
                         "ORDER BY updated DESC LIMIT 200").fetchall()
    return [dict(r) for r in rows]


def _chat_messages(sid):
    with _chat_conn() as c:
        rows = c.execute("SELECT role, content, tool_json, ts FROM messages WHERE session_id=? "
                         "ORDER BY id ASC", (sid,)).fetchall()
    return [dict(r) for r in rows]


def _chat_delete_session(sid):
    with _chat_conn() as c:
        c.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        c.execute("DELETE FROM sessions WHERE id=?", (sid,))


_chat_init()


# ---- Fiduciary persona: full read-only analytics + web, no mutation ----
FIDUCIARY_PROMPT = (
    "You are the Fiduciary — Jason's read-only, holistic financial advisor. You analyze his REAL "
    "portfolio and financial plan; you place no trades and change nothing. Ground every answer in your "
    "tools rather than assumption, and cite the figures you used.\n"
    "START by calling get_holdings and get_policy (his Investment Policy Statement) so your advice fits "
    "his actual positions and targets. Then reach for the tool that fits the question: get_exposure "
    "(sector/beta/concentration), get_rebalance (IPS sleeve drift), get_risk_metrics (realized vol / "
    "drawdown / Sharpe), get_retirement and get_income_plan (long-range plan, Monte Carlo, glide path), "
    "get_tax_audit (tax-loss-harvesting + asset-location), get_aca (ACA/MAGI subsidy + cliff), "
    "get_social_security, get_fees_liquidity, get_lookthrough (fund holdings), get_scorecard, "
    "get_opportunities and get_discovery (ideas), get_fundamentals / get_valuation / get_research for a "
    "specific ticker, search_knowledge for tax rules / IRA-Roth mechanics / investing-policy guidance from "
    "Jason's local reference library (cite the source doc), and web_search for current news or context.\n"
    "Think in terms of the whole plan: tax location, IPS drift, concentration risk, retirement funding, "
    "the ACA cliff. Prefer trailing-twelve-month figures over a single fiscal year. DRIP: accounts carry "
    "a per-account DRIP flag; get_tax_audit shows the reinvested/cash dividend split and the TAXABLE "
    "reinvested slice (phantom income + new tax lots — flag specific-lot tracking), get_rebalance shows "
    "DRIP drift-pressure (reinvested dividends raise a name's weight and aren't available to rebalance), "
    "and get_income_plan splits spendable-cash vs reinvested dividends. When sizing or comparing ideas, "
    "remember return scales with dollars x % — never favor a name for a lower share price; do flag positions "
    "too small to matter or material idle cash. Be honest about uncertainty and about what the data does and "
    "does not show. Educational analysis — NOT financial, tax, or legal advice."
)

FIDUCIARY_TOOLS = [
    {"type": "function", "function": {"name": "get_holdings",
        "description": "The user's REAL portfolio: net worth and consolidated holdings with weights and "
                       "gains. Call this FIRST.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_policy",
        "description": "The Investment Policy Statement (IPS): target asset-class sleeves and policy.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_exposure",
        "description": "True exposure: sector weights, portfolio beta, and concentration (HHI).",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_rebalance",
        "description": "Rebalancing view: drift of actual vs IPS asset-class sleeves, with suggested moves.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_risk_metrics",
        "description": "Realized risk from the accrued daily value history: annualized volatility, max "
                       "drawdown, Sharpe.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_retirement",
        "description": "Retirement projection: funding, Monte Carlo success, FI timeline, withdrawal picture.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_income_plan",
        "description": "Income glide path: coverage of target income by year and the shift schedule.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_tax_audit",
        "description": "Tax-loss-harvesting candidates and asset-location (tax-status) audit. Read-only.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_aca",
        "description": "ACA MAGI ledger, premium-subsidy estimate, and the 400%-FPL subsidy-cliff picture.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_social_security",
        "description": "Social Security estimate: PIA/FRA/COLA and benefit taxation.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_fees_liquidity",
        "description": "Fee drag (fund/ETF expense ratios) and liquidity flags on thinly-traded holdings.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_lookthrough",
        "description": "Fund/ETF look-through: the underlying holdings your funds actually hold.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_scorecard",
        "description": "Research scorecard: accrued Buy/Sell/Hold ratings across holdings over time.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_opportunities",
        "description": "Screened idea list (watchlist + peers) scored against the strategy criteria.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_discovery",
        "description": "News-discovered ideas plus news/SEC-8K alerts on current holdings.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_fundamentals",
        "description": "SEC GAAP fundamentals and ratios (growth, margins, leverage, FCF) for a ticker.",
        "parameters": {"type": "object", "properties": {"ticker": {"type": "string"}},
                       "required": ["ticker"]}}},
    {"type": "function", "function": {"name": "get_valuation",
        "description": "Valuation multiples (P/E, P/B, P/S, dividend yield, beta) for a ticker.",
        "parameters": {"type": "object", "properties": {"ticker": {"type": "string"}},
                       "required": ["ticker"]}}},
    {"type": "function", "function": {"name": "get_research",
        "description": "The latest daily Buy/Sell/Hold research brief for a ticker, if one exists.",
        "parameters": {"type": "object", "properties": {"ticker": {"type": "string"}},
                       "required": ["ticker"]}}},
    {"type": "function", "function": {"name": "search_knowledge",
        "description": "Search the user's local Knowledge library (their Investment Policy Statement + curated "
                       "IRS/SEC/Vanguard tax & investing reference docs) for relevant passages. Use this for "
                       "tax rules, IRA/Roth mechanics, and investing-principle / policy guidance. Returns the "
                       "top passages with their source document — cite the source and verify against current "
                       "official guidance.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "k": {"type": "number", "description": "How many passages to return (default 4)."}},
            "required": ["query"]}}},
    {"type": "function", "function": {"name": "web_search",
        "description": "Search recent web/news via Tavily for current context on a company, sector or topic.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "days": {"type": "number", "description": "Look-back window in days (default 14)."},
            "max_results": {"type": "number", "description": "Max articles (default 5)."}},
            "required": ["query"]}}},
]


def _fiduciary_dispatch(name, args):
    """Read-only dispatch for the Fiduciary. Every branch calls an existing analytics function; no
    branch mutates anything."""
    args = args or {}
    try:
        if name == "get_holdings":
            d = _data()
            top = sorted((d.get("consolidated") or []), key=lambda c: c.get("value") or 0, reverse=True)
            return {"net_worth": d.get("net_worth"),
                    "holdings": [{"ticker": c.get("ticker"), "name": c.get("name"), "value": c.get("value"),
                                  "weight_pct": c.get("weight_pct"), "gain_pct": c.get("gain_pct")}
                                 for c in top[:30] if c.get("ticker")]}
        if name == "get_policy":          return _read_policy()
        if name == "get_exposure":        return get_exposure()
        if name == "get_rebalance":       return get_rebalance()
        if name == "get_risk_metrics":    return get_risk_metrics()
        if name == "get_retirement":      return get_retirement()
        if name == "get_income_plan":     return get_income_plan()
        if name == "get_tax_audit":       return _tax_audit()
        if name == "get_aca":             return get_aca()
        if name == "get_social_security": return get_ss()
        if name == "get_fees_liquidity":  return get_fees_liquidity()
        if name == "get_lookthrough":     return get_lookthrough()
        if name == "get_scorecard":       return get_scorecard()
        if name == "get_opportunities":   return _opportunities()
        if name == "get_discovery":
            try:
                with open(DISCOVERY_FILE, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {"discovered": [], "alerts": []}
        if name == "get_fundamentals":    return edgar.fundamentals(str(args.get("ticker") or "").upper())
        if name == "get_valuation":       return _valuation(str(args.get("ticker") or "").upper()) or {"available": False}
        if name == "get_research":
            t = str(args.get("ticker") or "").upper()
            return (_read_analysis().get("briefs") or {}).get(t) or {"note": f"no brief for {t}"}
        if name == "search_knowledge":
            return knowledge.search(str(args.get("query") or ""), k=int(args.get("k") or 4))
        if name == "web_search":
            return {"results": _tavily_results(str(args.get("query") or ""),
                                               days=int(args.get("days") or 14),
                                               max_results=int(args.get("max_results") or 5))}
        return {"error": f"unknown tool {name}"}
    except Exception as e:
        return {"error": f"{name} failed: {e}"}


# ---- Strategist persona for chat: advisory, conversational, no trading ----
STRATEGIST_CHAT_PROMPT = (
    "You are the Strategist in conversational ADVISORY mode. You discuss ideas, opportunities and "
    "portfolio moves, but you place NO trades and record nothing — no buy/sell/report tools exist here. "
    "Use get_holdings for The user's REAL positions (context), get_paper_portfolio for the hypothetical "
    "book, get_opportunities and get_discovery for ideas, get_fundamentals / get_valuation / "
    "get_research to dig into a name, and web_search(query) for LIVE web/news via Tavily — use it to "
    "uncover fresh opportunities and to answer 'what's driving this stock right now / why is it up or "
    "down today' questions. Prefer trailing-twelve-month figures; weigh fees and liquidity on "
    "small orders. Give specific, reasoned takes and acknowledge uncertainty.\n"
    "TOOL HONESTY: your capabilities are exactly the tools listed above. If asked to do something no "
    "tool supports, say so plainly in ONE turn and offer the closest thing you CAN do — do NOT loop or "
    "deliberate at length about a capability you lack. Hypothetical and educational — NOT financial advice."
)

PERSONAS = {
    "fiduciary": {"prompt": FIDUCIARY_PROMPT, "tools": FIDUCIARY_TOOLS,
                  "dispatch": _fiduciary_dispatch, "model": ANALYSIS_MODEL, "temp": 0.3},
    "strategist": {"prompt": STRATEGIST_CHAT_PROMPT, "tools": STRATEGIST_ADVICE_TOOLS,
                   "dispatch": _advice_dispatch, "model": ANALYSIS_MODEL, "temp": 0.4},
}


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _trim_tool_result(nm, result):
    _cap = int(os.environ.get("STRAT_TOOL_RESULT_MAX", "8000"))
    _tr = json.dumps({"tool": nm, "result": result}, default=str)
    if len(_tr) > _cap:
        _r = {k: (v[:20] if isinstance(v, list) else v) for k, v in result.items()} \
            if isinstance(result, dict) else result
        _tr = json.dumps({"tool": nm, "result": _r, "_note": "large result trimmed to fit context"}, default=str)
        if len(_tr) > _cap:
            _tr = json.dumps({"tool": nm, "_note": "result too large; summarized",
                              "keys": (list(result.keys()) if isinstance(result, dict) else "list")}, default=str)
    return _tr


def _chat_stream(persona_key, session_id, user_msg):
    """SSE generator: streams assistant tokens and tool events for one user message, persisting the
    turn to SQLite. Holds _strat_lock for the duration so a chat never competes with the weekly run
    or an advisory run for the GPUs."""
    persona = PERSONAS.get(persona_key)
    if not persona:
        yield _sse("error", {"error": f"unknown persona '{persona_key}'"})
        return
    if not _strat_lock.acquire(blocking=False):
        # Queue behind the running strategist/advisory job instead of forcing a retry.
        # Single-flight on the GPU by design — DON'T parallelize (two big generations would
        # contend on the Pascal cards). Tell the client we're waiting, then block until free.
        yield _sse("status", {"state": "waiting",
                              "message": "A strategist/advisory run is using the model — your message is "
                                         "queued and will start automatically when it finishes."})
        if not _strat_lock.acquire(timeout=CHAT_QUEUE_TIMEOUT):
            yield _sse("error", {"error": "still busy after waiting a while — please try again shortly"})
            return
    try:
        if not session_id:
            title = (user_msg or "New chat").strip().replace("\n", " ")[:60] or "New chat"
            session_id = _chat_create_session(persona_key, title)
        _chat_add_message(session_id, "user", user_msg)
        yield _sse("session", {"session_id": session_id, "persona": persona_key})

        history = _chat_messages(session_id)[-CHAT_MAX_HISTORY:]
        messages = [{"role": "system", "content": _sys(persona["prompt"])}]
        for m in history:
            if m["role"] in ("user", "assistant"):
                messages.append({"role": m["role"], "content": m["content"] or ""})

        final_content = ""
        for i in range(CHAT_MAX_ITER):
            content_buf = ""
            tool_calls = []
            try:
                with requests.post(f"{OLLAMA}/api/chat",
                                   json={"model": persona["model"], "messages": messages,
                                         "tools": persona["tools"], "stream": True,
                                         "keep_alive": LLM_KEEP_ALIVE,
                                         "options": {"temperature": persona.get("temp", 0.3),
                                                     "num_ctx": STRAT_NUM_CTX}},
                                   timeout=STRAT_LLM_TIMEOUT, stream=True) as resp:
                    if resp.status_code >= 400:
                        yield _sse("error", {"error": f"ollama {resp.status_code}: {resp.text[:200]}"})
                        return
                    for line in resp.iter_lines():
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                        except Exception:
                            continue
                        m = chunk.get("message") or {}
                        if m.get("thinking"):     # qwen3 reasoning phase — stream as an activity indicator
                            yield _sse("thinking", {"t": m["thinking"]})
                        if m.get("content"):
                            content_buf += m["content"]
                            yield _sse("token", {"t": m["content"]})
                        for tc in (m.get("tool_calls") or []):
                            tool_calls.append(tc)
                        if chunk.get("done"):
                            break
            except Exception as e:
                yield _sse("error", {"error": f"generation failed: {e}"})
                return

            asst = {"role": "assistant", "content": content_buf}
            if tool_calls:
                asst["tool_calls"] = tool_calls
            messages.append(asst)

            if not tool_calls:
                final_content = content_buf
                break

            for tc in tool_calls:
                fn = tc.get("function") or {}
                nm = fn.get("name")
                a = fn.get("arguments") or {}
                if isinstance(a, str):
                    try:
                        a = json.loads(a)
                    except Exception:
                        a = {}
                yield _sse("tool", {"name": nm, "args": a})
                result = persona["dispatch"](nm, a)
                messages.append({"role": "tool", "content": _trim_tool_result(nm, result)})
                yield _sse("tool_done", {"name": nm})
        else:
            final_content = final_content or "(reached the step limit before finishing — ask me to continue.)"

        _chat_add_message(session_id, "assistant", final_content)
        _chat_touch_session(session_id)
        yield _sse("done", {"session_id": session_id})
    finally:
        _strat_lock.release()


@app.post("/api/chat")
async def chat(req: Request):
    """Streaming multi-turn chat with a persona (fiduciary|strategist). Body: {persona, message, session_id?}.
    Returns text/event-stream: events session, token, tool, tool_done, done, error. READ-ONLY."""
    try:
        body = await req.json()
    except Exception:
        body = {}
    persona = str((body or {}).get("persona") or "fiduciary").strip()
    session_id = (body or {}).get("session_id")
    message = str((body or {}).get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "empty message"}, status_code=400)
    return StreamingResponse(_chat_stream(persona, session_id, message), media_type="text/event-stream")


@app.get("/api/chat/sessions")
def chat_sessions():
    return {"sessions": _chat_sessions()}


@app.get("/api/chat/session/{sid}")
def chat_session(sid: str):
    return {"session_id": sid, "messages": _chat_messages(sid)}


@app.delete("/api/chat/session/{sid}")
def chat_session_delete(sid: str):
    _chat_delete_session(sid)
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True, "mcpo": MCPO, "route": ROUTE, "cache_ttl_s": TTL,
            "finnhub": bool(FINNHUB), "analysis_model": ANALYSIS_MODEL}


@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))
