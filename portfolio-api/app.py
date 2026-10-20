# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
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
import hashlib   # 2026-08-06: session-cookie fingerprinting for the session.expired audit event
                 # (the cookie VALUE is never logged -- only a truncated digest, so repeated
                 # sightings of the same dead cookie can be deduped without storing a credential)
import datetime
import threading
import queue
import uuid

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, Response
from fastapi.exceptions import RequestValidationError
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Dict, List

import edgar   # SEC EDGAR / GAAP fundamentals
import states  # 50-state + DC residency-aware facts registry (RESUME-TRACKER item 3)
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
import backtest as backtest_mod  # decision backtest (P0-2) — fixed-horizon call scoring vs SPY
import insurance   # protection desk (P0-3) — life/DI/umbrella/P&C/LTC gap analysis vs policy register
import options as options_mod  # G10 (P1) — Yahoo delayed chains + local BS greeks: covered calls & collars
import shortint    # G15 deferred half (P1) — FINRA consolidated short interest (free API-console creds)
import cashdebt    # GAP-2 (P1) — emergency fund, idle-cash drag, payoff-vs-invest, refi, avalanche
import performance # GAP-3 (P1) — TWR + policy-benchmark relative returns over standard periods
import washsale    # GAP-4 (P1) — share-event ledger → wash-sale verdicts + replacement pairs for TLH
import benefits    # GAP-5 (P1) — 401k match/headroom, HSA, Roth IRA, mega-backdoor capacity (2026-grounded)
import thesis       # per-holding thesis & exit-trigger registry (G14) — exit-discipline-as-code
import realestate   # rental real-estate deal underwriter (G9) — NOI/cap/DSCR/CoC/after-tax IRR
import calculators  # CALCULATORS tab (2026-10-19) — mortgage TCO, loans, land, DRIP, TVM, what-if, …
import bizacq       # small-business acquisition underwriter (G11) — SDE mult/DSCR/CoC/QSBS/after-tax IRR
import mcblock      # block-bootstrap MC draw mode (Part-4) — real historical return sequences
import insider      # SEC Form 4 insider-transaction signal (G15) — context-only net open-market buy/sell
import inst13f      # SEC Form 13F institutional holdings (G15) — forward whale-tracking, context-only
import auth         # LOCAL-HOSTING-HARDENING-PLAN.md §2/§3 (2026-07-23c) — session auth + TOTP MFA
import audit        # SEC-1 (2026-07-24) — append-only, encrypted, hash-chained security event log
import audit_anchor # SEC-9 (2026-09-01) — RFC 3161 + Merkle-batched external timestamp anchoring for audit.py's chain
import detections   # SEC-4 (2026-07-25) — rules engine over the audit log (failed-login bursts, etc.)
import demo_seed     # Demo harness (2026-07-25) — synthetic-data seeder + scheduled reset, DEMO_MODE-gated (no-op otherwise)
import notifications  # Outbound awareness layer (2026-07-26) — VAPID web push + generic SMTP email, triggered from audit.emit()
import backup        # SEC-7a (2026-07-27) — UI-triggered encrypted backup/staged-restore, same format as scripts/backup.sh
import store        # LOCAL-HOSTING-HARDENING-PLAN.md §4 (2026-07-23e) — Storage Stage A, flat JSON -> SQLite
import llm          # RESUME-TRACKER 2026-07-24 — LLM-agnostic backend adapter (_llm_chat/_llm_embed)
import tls          # in-app HTTPS cert management (2026-07-23f) — self-signed / ACME / manual CSR
import secrets_crypto  # 2026-07-24 — Fernet encryption at rest for SECRET_SPECS values in the DB
import vault          # TASK-001 — encrypted document vault
import vault_api      # TASK-001 — /api/vault/* handlers
import emergency      # TASK-002 — break-glass emergency access
import kit            # TASK-002 — offline Emergency Kit
import emergency_api  # TASK-002 — /api/emergency/* + /api/vault/kit*
import plaid_integration  # TASK-005 — Plaid provider (balances/holdings/liabilities; never transactions)

# store.init_db() has to run before ANYTHING below reads a *_FILE via the store (starting with
# _boot_merge two lines down) — store.py itself does no work at import time, so this is the
# single earliest safe point to call it.
store.init_db()
# TASK-001/002 tables, created BEFORE any background thread (detections, emergency) can query them
vault.init_db()
emergency.init_db()
kit.init_db()

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
# 2026-07-24: added the 6 LLM-provider/Claude keys alongside the original 3 -- anything added to
# CONFIG_SPECS below MUST also be listed here, or a UI-saved value silently never reaches
# os.environ at boot (CONFIG_SPECS itself is display/edit metadata only; THIS tuple is what
# _boot_merge actually consults).
_CONFIG_ENV_KEYS = ("ANALYSIS_MODEL", "LLM_TIMEOUT", "LLM_KEEP_ALIVE",
                    "LLM_API_DIALECT", "LLM_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_STRATEGIST_MODEL",
                    "EMBED_API_DIALECT", "EMBED_BASE_URL",
                    "PORTFOLIO_PROVIDER",   # 2026-07-24 -- see CONFIG_SPECS below
                    "TRUST_PROXY_HEADERS")  # 2026-07-27 -- see CONFIG_SPECS below; audit.py reads
                                            # this lazily specifically so this boot-merge path works
# Keys present in the REAL environment at boot (from .env / compose). Used so the UI can
# honestly mark those "env-locked" — env wins and the UI must not pretend to override it.
_ENV_SECRETS_AT_BOOT = {_k for _k, _v in os.environ.items() if _v not in (None, "")}


def _boot_merge(_path, _only=None, _decrypt=False):
    """Fill env GAPS from the store (Stage A, 2026-07-23e — was a raw JSON file read directly)
    BEFORE the config constants below read os.environ. Real env always wins (we never overwrite
    a set var). _only restricts to a key allow-list.

    `_decrypt` (2026-08-06 fix) is REQUIRED for SECRETS_FILE and must stay off for
    _SETTINGS_FILE_BOOT. Background: 2026-07-24's encryption-at-rest change made _write_secrets()
    Fernet-encrypt each SECRETS_FILE value INDIVIDUALLY (store.py's own DB-layer _enc is a
    separate, outer layer that read_json_strict already undoes). This function was written a day
    earlier and reads the blob raw instead of going through _read_secrets(), so from the first
    container recreate after that change it exported CIPHERTEXT as the env value — e.g.
    os.environ["FINNHUB_API_KEY"] = "gAAAAAB...". Every consumer that reads such a key as a module
    constant (FINNHUB at the bottom of this block; llm.py's LLM_API_KEY/EMBED_API_KEY;
    shortint.py's FINRA_*; RENTCAST_API_KEY) then sent a garbage token upstream. Crucially the
    ciphertext is TRUTHY, so every `if not FINNHUB:` guard passed and the failure was silent —
    Finnhub simply answered "Please use an API key." and _grounded()/_valuation() swallowed it to
    None/{}, which surfaced only as a blank ANALYST column and "low" confidence briefs (the
    coverage line in _llm() honestly reported 1-of-4 inputs available). Secrets read LIVE through
    _read_secrets() (SMTP_*, BACKUP_PASSPHRASE) were never affected, which is why the breakage
    looked oddly selective. settings.json is NOT per-value encrypted (see _read_settings /
    post_settings — plain store.read_json_strict / store.write_json), hence _decrypt=False there;
    turning it on would be harmless anyway since decrypt_or_legacy() passes non-tokens through
    untouched, but the flag documents which store is which."""
    try:
        for _k, _v in (store.read_json_strict(_path) or {}).items():
            if _only is not None and _k not in _only:
                continue
            if _decrypt:
                # never raises; a legacy plaintext value (pre-2026-07-24) comes back as-is
                _v = secrets_crypto.decrypt_or_legacy(_v)
            if _v not in (None, "") and not os.environ.get(_k):
                os.environ[_k] = str(_v)
    except Exception:
        pass


_boot_merge(SECRETS_FILE, _decrypt=True)           # secrets (all keys) — values are encrypted at rest
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
                  "targets_by_sleeve": {"US Equity": 70, "Intl Equity": 10, "Bond": 15, "Cash": 5},
                  # Manual Intl-Equity override for direct foreign stocks Finnhub's country field
                  # doesn't cover well (thin-float OTC/ADR names) — e.g. {"FGPHF": true}. The
                  # _sleeve() heuristic checks this before falling back to the Finnhub profile lookup.
                  "intl_tickers": {}}
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
                  # Estate (G2): cost basis (purchase price + capital improvements) of the
                  # PRIMARY residence, so the step-up ledger can quantify the gain §1014 wipes.
                  # Applied to the largest real-estate account. None = show value, gain unknown.
                  "estate_home_basis": None,
                  # State of residence (RESUME-TRACKER item 3): drives estate.py (community
                  # property / TOD deeds / state estate tax), aca.py (Medicaid expansion), and
                  # the tax.py/cashdebt.py state-tax notes. None = auto-detect from the largest
                  # real-estate account's address, falling back to TX — see _state_of_residence().
                  "state_of_residence": None,
                  # Tax engine (G5): federal brackets are the same everywhere; state estimate
                  # layered on top via states.py (item 3) — see tax.state_tax_estimate().
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


# ============================================================================
# Pydantic request models (API hardening, 2026-07-24 -- CLOUD-PORTABILITY-ROADMAP.md /
# LOCAL-HOSTING-HARDENING-PLAN.md follow-on item: "Pydantic request validation on
# config-mutating POSTs -- still ad hoc await req.json() + manual coercion everywhere; do
# incrementally, highest-traffic endpoints first"). Two of the config-mutating POST endpoints
# converted this pass -- /api/policy and /api/retirement-config -- as the first, most
# representative slice: both previously did `{k: v for k, v in body.items() if k in DEFAULT_X}`
# with ZERO type/range checking, so a wrong-shaped value (a string where a percentage was
# expected, a negative age, an out-of-range allocation target) would silently persist into the
# stored config and only surface as a confusing failure somewhere else downstream (the Monte
# Carlo, the tax engine, the rebalance drift calc). The remaining config-mutating endpoints
# (insurance-config, accounts-config, settings, secrets) are intentionally left on their existing
# ad hoc validation for now -- their bodies are more dynamically shaped (bulk/defaults/single-item
# variants, or genuinely open-ended settings/secrets registries) and are a reasonable next
# increment, not skipped by oversight.
#
# `exclude_unset=True` at each call site (not `exclude_none`) preserves the ORIGINAL merge
# semantics exactly: a key the client didn't send is left alone (old value persists), but a key
# explicitly sent as `null` DOES apply (clearing an override back to None) -- matching the old
# `if k in body` check, which triggered on presence, not on truthiness.
# ============================================================================
class PolicyUpdate(BaseModel):
    targets_by_type: Optional[Dict[str, float]] = None
    max_position_pct: Optional[float] = Field(None, gt=0, le=100)
    drift_threshold_pct: Optional[float] = Field(None, gt=0, le=100)
    position_caps: Optional[Dict[str, float]] = None
    targets_by_sleeve: Optional[Dict[str, float]] = None
    intl_tickers: Optional[Dict[str, bool]] = None

    @field_validator("targets_by_type", "targets_by_sleeve", "position_caps")
    @classmethod
    def _pct_map_in_range(cls, v):
        if v is None:
            return v
        for k, val in v.items():
            if not isinstance(val, (int, float)) or isinstance(val, bool) or not (0 <= val <= 100):
                raise ValueError(f"{k!r}: expected a percentage between 0 and 100, got {val!r}")
        return v


class RetirementConfigUpdate(BaseModel):
    withdrawal_rate_pct: Optional[float] = Field(None, ge=0, le=25)
    expected_real_return_pct: Optional[float] = Field(None, ge=-20, le=30)
    annual_expenses_override: Optional[float] = Field(None, ge=0)
    annual_savings_override: Optional[float] = Field(None, ge=0)
    current_age: Optional[int] = Field(None, ge=0, le=120)
    target_age: Optional[int] = Field(None, ge=0, le=120)
    exclude_account_types: Optional[List[str]] = None
    income_target_yield_pct: Optional[float] = Field(None, ge=0, le=25)
    optimistic_real_return_pct: Optional[float] = Field(None, ge=-20, le=30)
    volatility_pct: Optional[float] = Field(None, ge=0, le=100)
    needs_pct_of_expenses: Optional[float] = Field(None, ge=0, le=100)
    estate_donees: Optional[float] = Field(None, ge=0)
    estate_home_basis: Optional[float] = Field(None, ge=0)
    state_of_residence: Optional[str] = None
    filing_status: Optional[str] = None
    spouse_age: Optional[int] = Field(None, ge=0, le=120)
    convert_fill_to_rate: Optional[float] = Field(None, ge=0, le=1)
    household_size: Optional[int] = Field(None, ge=1, le=20)
    aca_slcsp_annual: Optional[float] = Field(None, ge=0)
    birth_year: Optional[int] = Field(None, ge=1900, le=2100)
    ss_claim_age: Optional[int] = Field(None, ge=62, le=70)
    ss_estimated_fra_benefit: Optional[float] = Field(None, ge=0)
    ss_pia_monthly: Optional[float] = Field(None, ge=0)
    ss_aime: Optional[float] = Field(None, ge=0)
    spouse_birth_year: Optional[int] = Field(None, ge=1900, le=2100)
    spouse_ss_claim_age: Optional[int] = Field(None, ge=62, le=70)
    spouse_ss_estimated_fra_benefit: Optional[float] = Field(None, ge=0)
    spouse_ss_pia_monthly: Optional[float] = Field(None, ge=0)


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
    "tbill_fallback_pct": {"env": "TBILL_FALLBACK_PCT", "default": 4.0, "cast": float, "label": "3-mo T-bill fallback (%) when FRED is unreachable (cash/debt desk)", "group": "Cash & debt"},
    "ef_target_months": {"env": "EF_TARGET_MONTHS", "default": 6.0, "cast": float, "label": "Emergency fund target (months of essential spend)", "group": "Cash & debt"},
    "ef_stress_drawdown_pct": {"env": "EF_STRESS_DRAWDOWN_PCT", "default": 20.0, "cast": float, "label": "Emergency fund stress test: assumed equity drawdown (%) applied to market-risk EF sources", "group": "Cash & debt"},
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
# applied on RESTART via _boot_merge). NOT read live. app.py reads them into module constants
# AFTER _boot_merge runs; llm.py (imported BEFORE the merge) resolves its own keys lazily per call.
CONFIG_SPECS = [
    {"key": "ANALYSIS_MODEL", "label": "Analysis / Strategist model (Ollama tag)", "group": "Model", "default": "qwen3.6:27b"},
    {"key": "LLM_TIMEOUT",    "label": "LLM per-call timeout (seconds)",           "group": "Model", "default": "1200"},
    {"key": "LLM_KEEP_ALIVE", "label": "Keep model resident (e.g. 30m)",           "group": "Model", "default": "30m"},
    # ---- AI / LLM provider (2026-07-24, Claude integration) ----
    # "options" is a UI-only hint (settingsContent() renders a <select> instead of a free-text
    # input when present) -- ignored by the backend, which just stores/returns a plain string.
    {"key": "LLM_API_DIALECT", "label": "Chat/analysis provider", "group": "AI / LLM provider",
     "default": "ollama", "options": ["ollama", "openai", "anthropic"]},
    {"key": "LLM_BASE_URL", "label": "Chat/analysis host URL (blank = provider default)",
     "group": "AI / LLM provider", "default": ""},
    {"key": "ANTHROPIC_MODEL", "label": "Claude model — everyday (chat, narrative)",
     "group": "AI / LLM provider", "default": "claude-sonnet-5"},
    {"key": "ANTHROPIC_STRATEGIST_MODEL", "label": "Claude model — Strategist + advice (deeper reasoning)",
     "group": "AI / LLM provider", "default": "claude-opus-5"},
    {"key": "EMBED_API_DIALECT", "label": "Knowledge/RAG embeddings provider (Claude has none — stays local)",
     "group": "AI / LLM provider", "default": "ollama", "options": ["ollama", "openai"]},
    {"key": "EMBED_BASE_URL", "label": "Embeddings host URL (blank = provider default)",
     "group": "AI / LLM provider", "default": ""},
    # ---- Portfolio data source (2026-07-24) -- was .env-only; a new CSV user previously had no
    # in-app way to even pick "csv" without editing .env by hand. See /api/holdings-csv for the
    # matching in-app editor that goes with this.
    {"key": "PORTFOLIO_PROVIDER", "label": "Portfolio data source", "group": "Portfolio provider",
     "default": "csv", "options": ["csv", "snaptrade", "monarch", "plaid"]},
    # ---- Notifications (2026-07-26) -- VAPID requires *a* contact address in its claim (the push
    # service, e.g. Chrome's/Mozilla's, may use it to reach you about a misbehaving sender); it
    # does not need to be monitored or even real-looking, so this is not in SECRET_SPECS.
    # Edited from SETTINGS -> NOTIFICATIONS (hidden in ADVANCED KNOBS). Blank = use the SMTP
    # alert address (notifications._vapid_claim_email). Apple needs a real-looking address.
    {"key": "NOTIFY_VAPID_CONTACT_EMAIL", "label": "Push notification contact email (VAPID claim — not shown to users)",
     "group": "Notifications", "default": ""},
    # ---- Plaid (TASK-005): sandbox = Plaid's fake banks (free, for trying it); production = real banks.
    {"key": "PLAID_ENV", "label": "Plaid environment (sandbox = test data, production = your real banks)",
     "group": "Portfolio (Plaid)", "default": "sandbox", "options": ["sandbox", "production"]},
    # ---- Reverse proxy (2026-07-27) -- found live on the AWS demo: request.client.host is always
    # the proxy/load-balancer's own address once one sits in front of this app, never the real
    # visitor, which silently breaks the audit log's source IP, the SEC-4 first-seen-IP detection
    # rule, and per-IP rate limiting alike. OFF by default -- only turn this on if this exact
    # instance sits behind a reverse proxy/load balancer you control, where nothing else can reach
    # this app directly. Enabling it on a directly-reachable instance lets ANY caller forge their
    # logged IP via a hand-set X-Forwarded-For header -- see audit.py's _resolve_client_ip docstring.
    {"key": "TRUST_PROXY_HEADERS",
     "label": "Trust X-Forwarded-For (ONLY if a reverse proxy you control sits in front of this app)",
     "group": "Network / reverse proxy", "default": "0", "options": ["0", "1"]},
    # ---- Audit log external anchoring (2026-09-01, SEC-9) -- RFC 3161 trusted timestamping,
    # Merkle-batched over audit.py's hash-chained event log. See audit_anchor.py's module
    # docstring for the full design. Read LAZILY by audit_anchor.py (same ordering-hazard reason
    # as TRUST_PROXY_HEADERS above), so these apply on the next container recreate, same as every
    # other CONFIG_SPECS knob. freetsa.org is a FREE, unaudited TSA suitable for development only
    # -- production should point at a QUALIFIED TSA on the eIDAS (EU) or UK trusted list: a
    # qualified electronic timestamp carries a legal presumption of the accuracy and integrity of
    # the date/time it indicates, under eIDAS Regulation (EU) No 910/2014 Article 41 -- an
    # unqualified/free TSA's timestamp is still cryptographically meaningful evidence, just without
    # that specific legal presumption attached.
    {"key": "TSA_PRIMARY_URL", "label": "RFC 3161 timestamp authority -- primary URL (dev default: freetsa.org, a free/unaudited TSA)",
     "group": "Audit log anchoring", "default": "https://freetsa.org/tsr"},
    {"key": "TSA_FALLBACK_URL", "label": "RFC 3161 timestamp authority -- fallback URL (optional, tried if the primary fails)",
     "group": "Audit log anchoring", "default": ""},
    {"key": "TSA_PRIMARY_ROOT_CA_FILE", "label": "Pinned root CA PEM path for the primary TSA (blank = trust-on-first-use from its own response chain)",
     "group": "Audit log anchoring", "default": ""},
    {"key": "TSA_FALLBACK_ROOT_CA_FILE", "label": "Pinned root CA PEM path for the fallback TSA (blank = trust-on-first-use)",
     "group": "Audit log anchoring", "default": ""},
    {"key": "TSA_POLICY_OID", "label": "Expected TSA policy OID (optional -- blank skips this check)",
     "group": "Audit log anchoring", "default": ""},
    {"key": "ANCHOR_INTERVAL_HOURS", "label": "How often to anchor the audit log externally (hours; the fixed daily job)",
     "group": "Audit log anchoring", "default": "24"},
    {"key": "ANCHOR_VERIFY_INTERVAL_HOURS", "label": "How often to independently re-verify existing anchors (hours; the detection job)",
     "group": "Audit log anchoring", "default": "6"},
    {"key": "ANCHOR_RENEWAL_INTERVAL_DAYS", "label": "Re-anchor archived evidence with fresh crypto after this many days (long-term validation)",
     "group": "Audit log anchoring", "default": "365"},
]


def _read_settings():
    try:
        return store.read_json_strict(SETTINGS_FILE) or {}
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
store.ensure_paper_migrated(PAPER_FILE)   # Storage Stage B (2026-07-24) -- one-time import of every scenario's transactions/reports arrays into real tables, no-ops after the first run
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
# ANALYSIS_TOKEN constant removed 2026-07-24 -- retired along with its 23 endpoint checks and the
# page-injection mechanism; the session auth gate is now the sole guard on every route. If a
# deployment still has ANALYSIS_TOKEN set in its .env, it's simply ignored now (harmless).
FINNHUB = os.environ.get("FINNHUB_API_KEY", "")
# (OLLAMA = llm.BASE_URL alias removed: llm.py now resolves its URL/dialect lazily per call so
#  SETTINGS-tab values merged by _boot_merge take effect; error text calls llm._base_url().)
ANALYSIS_MODEL = os.environ.get("ANALYSIS_MODEL", "qwen3.6:27b")
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "1200"))     # big models on Pascal are slow
LLM_KEEP_ALIVE = os.environ.get("LLM_KEEP_ALIVE", "30m")     # keep model resident between briefs
ANALYSIS_NUM_CTX = int(os.environ.get("ANALYSIS_NUM_CTX", "16384"))   # fit full fundamentals+valuation+news without truncation
ANALYSIS_THINK = os.environ.get("ANALYSIS_THINK", "0").lower() in ("1", "true", "yes", "on")  # reasoning ON = deeper briefs, slower
# ---- Claude (Anthropic) integration, 2026-07-24 -- two-tier model selection ------------------
# Only consulted when llm._dialect() == "anthropic" (SETTINGS -> AI / LLM PROVIDER, or LLM_API_DIALECT
# in .env). ANALYSIS_MODEL above is untouched and still governs every ollama/openai-dialect call
# site exactly as before -- this is purely an additional branch for the anthropic dialect, not a
# replacement. Two tiers, not one: the autonomous weekly Strategist run + the on-demand cash-
# deployment advice planner (both long, multi-step, tool-heavy ReAct loops) can use a stronger/
# pricier model than everyday chat + narrative/brief generation, which are shorter and more
# frequent. See llm._text_model below and NEW-USER-SETUP-GUIDE.md for the cost rationale.
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
ANTHROPIC_STRATEGIST_MODEL = os.environ.get("ANTHROPIC_STRATEGIST_MODEL", "claude-opus-5")


def _text_model(strategist=False):
    """Model tag for a text-generation call site. Ollama/OpenAI dialects: unchanged, always
    ANALYSIS_MODEL (no behavior change from before this function existed). Anthropic dialect:
    ANTHROPIC_STRATEGIST_MODEL for the weekly Strategist run + advice deployment planner,
    ANTHROPIC_MODEL for everything else (chat personas, narrative, single-shot JSON extraction)."""
    if llm._dialect() == "anthropic":
        return ANTHROPIC_STRATEGIST_MODEL if strategist else ANTHROPIC_MODEL
    return ANALYSIS_MODEL
# Chat image input (RESUME-TRACKER 2026-07-18j): a SEPARATE small multimodal model, not a
# replacement for ANALYSIS_MODEL (text-only, no vision support). qwen2.5vl:7b is confirmed to fit
# even an 8GB test-user card at 4-bit and comfortably fits Jason's dual 1080 Tis; pull it once via
# `ollama pull qwen2.5vl:7b` before this feature works (a missing model surfaces as a normal Ollama
# 404 from _vision_identify_tickers, not a crash).
VISION_MODEL = os.environ.get("VISION_MODEL", "qwen2.5vl:7b")
VISION_LLM_TIMEOUT = int(os.environ.get("VISION_LLM_TIMEOUT", "180"))   # single non-streaming call, bounded

_run_lock = threading.Lock()
_run_state = {"status": "idle", "analyzed": 0, "total": 0, "started": 0}

# ---- Global GPU serialization (Increment A) --------------------------------
# Every feature has its OWN lock (blocking=False skip-if-running), so DIFFERENT
# features (analysis vs strategist vs discovery) never serialized against each
# other at the GPU — two qwen-27b contexts could pile into Ollama's internal
# same-model queue, and a job stuck waiting there can outrun the client's
# LLM_TIMEOUT and fail. This single process-wide lock funnels EVERY heavy
# generation (qwen-27b: /api/generate + both /api/chat sites) through one at a
# time, so each job runs with a FRESH timeout the moment it holds the GPU.
# Deliberately does NOT cover embeddings (bge-m3 is light and co-resides fine
# under OLLAMA_MAX_LOADED_MODELS=2; gating it would just slow chat's RAG).
# Ordering is always feature-lock -> _GPU (acquired only at the leaf call
# sites), never the reverse, so no deadlock. Blocking acquire = no-drop; the
# underlying request timeout bounds how long the lock can be held.
_GPU = threading.Lock()
_gpu_waiters = 0
_gpu_waiters_lock = threading.Lock()

class _gpu_slot:
    """Context manager: serialize a heavy GPU call + track queue depth for /api/gpu."""
    def __enter__(self):
        global _gpu_waiters
        with _gpu_waiters_lock:
            _gpu_waiters += 1
        _GPU.acquire()
        return self
    def __exit__(self, *exc):
        global _gpu_waiters
        _GPU.release()
        with _gpu_waiters_lock:
            _gpu_waiters -= 1
        return False


def _gpu_acquire_visible(cancel_event, deadline):
    """Poll-acquire the same _GPU lock _gpu_slot() uses, instead of its plain blocking acquire() —
    for call sites (currently only /api/chat's _chat_stream, RESUME-TRACKER 2026-07-18l bug #2)
    that need the wait itself to be visible/cancellable rather than a silent hang. A job-queue run
    (analysis/discovery/narrative/etc, some observed at 19-29+ minutes) holds _GPU for a long time;
    _gpu_slot()'s bare `_GPU.acquire()` inside a `with` block just blocks the generator with zero
    SSE events for the whole remaining duration — indistinguishable from a dead connection.
    Polls every 1s so `cancel_event` (Stop, wired the same way the _strat_lock wait above already
    is) or `deadline` can interrupt the wait. Keeps the same `_gpu_waiters` bookkeeping /api/gpu
    already reports, so this doesn't change what that endpoint shows. Returns True if acquired
    (caller MUST call _gpu_release_visible() when done — NOT the `with _gpu_slot()` form, since
    this bypasses its __enter__), False if cancelled or the deadline passed (waiters already
    decremented in that case; nothing to release)."""
    global _gpu_waiters
    with _gpu_waiters_lock:
        _gpu_waiters += 1
    while True:
        if cancel_event is not None and cancel_event.is_set():
            with _gpu_waiters_lock:
                _gpu_waiters -= 1
            return False
        if _GPU.acquire(timeout=1):
            return True
        if time.time() >= deadline:
            with _gpu_waiters_lock:
                _gpu_waiters -= 1
            return False


def _gpu_release_visible():
    """Release a lock taken via _gpu_acquire_visible() — mirrors _gpu_slot.__exit__ exactly so
    /api/gpu's waiter count stays correct."""
    global _gpu_waiters
    _GPU.release()
    with _gpu_waiters_lock:
        _gpu_waiters -= 1

app = FastAPI(title="portfolio-terminal", version="0.1.0")
_cache = {"ts": 0.0, "data": None}


# ============================================================================
# SESSION AUTH + SECURITY HEADERS (2026-07-23c — LOCAL-HOSTING-HARDENING-PLAN.md §2/§3,
# Priority 1). Replaces the old trust model: previously only 23 of 127 endpoints checked
# ANALYSIS_TOKEN, and even that token was handed to the client by the page it's meant to
# protect (see index() below) — so it protected nothing once the page itself was reachable.
# Now EVERY route except a tiny public whitelist requires a real session.
# 2026-07-24 cleanup: the old ANALYSIS_TOKEN checks (redundant once this gate covers every
# route anyway) and the token-into-page injection have both been removed — this gate is now
# the ONLY auth check anywhere in the app. See RESUME-TRACKER.md for the retirement writeup.
#
# Middleware order matters and is COUNTERINTUITIVE in Starlette: add_middleware() inserts at
# the FRONT of the internal list, so the LAST middleware added ends up OUTERMOST (runs first
# on the way in). Verified empirically, not just by reading the docs — an earlier version of
# this code added SessionMiddleware first and the gate second, which put the gate OUTSIDE
# SessionMiddleware and crashed every request with "SessionMiddleware must be installed to
# access request.session". Fix: define the gate FIRST (below), add SessionMiddleware LAST
# (further down), so SessionMiddleware is outermost and has already parsed the cookie into
# request.session by the time the gate — running just inside it — reads that value.
# ============================================================================

# "/" stays public — it only ever serves the static shell + JS, never data. The JS itself
# calls /api/auth/status on load and renders a first-run/login form instead of the dashboard
# when unauthenticated, so no actual page CONTENT is exposed pre-auth despite the route being
# technically reachable. Every other route — including FastAPI's own /docs, /openapi.json —
# is gated by omission (deny unless explicitly whitelisted), not by an exclude-list.
_AUTH_PUBLIC_PATHS = {"/", "/health", "/api/auth/status", "/api/auth/first-run", "/api/auth/login",
                       # Account Recovery Tier 1 (RESUME-TRACKER.md 2026-07-23f design, built
                       # 2026-07-24): forgot-password via a valid MFA code, necessarily reachable
                       # pre-login. Neither endpoint grants a real "uid" session -- /start only
                       # sets a short-lived "recovery_uid" marker (checked explicitly by /reset,
                       # not by this gate), so being public here does not widen what an
                       # unauthenticated caller can actually do.
                       "/api/auth/recovery/start", "/api/auth/recovery/reset",
                       # PWA manifest + service worker (2026-07-26 notifications work) -- must be
                       # fetchable pre-login the same way "/" itself is: iOS Safari needs the
                       # manifest to offer "Add to Home Screen" at all, and the service worker is
                       # what makes the Push API available post-install. Neither file contains
                       # anything sensitive.
                       "/manifest.json", "/sw.js"}

# API hardening follow-up (2026-07-24): rate limiting was login-only until now -- extended to the
# heavy job-trigger endpoints too (every /api/run-* scheduled-job trigger, the knowledge reindex,
# and the Strategist advisory run), since a session already being authenticated doesn't mean it
# should be able to fire an unbounded number of GPU/network-heavy background jobs per minute.
# Uses the same in-process sliding-window limiter as login (auth.rate_limited/record_attempt),
# just in its own named "jobs" bucket so this can never lock a session out of logging back in,
# and vice versa. All 22 matching endpoints are covered centrally here rather than repeating a
# check in each one.
JOB_RATE_LIMIT_PER_MIN = int(os.environ.get("JOB_RATE_LIMIT_PER_MIN", "20"))
_JOB_RATE_LIMIT_EXACT = {"/api/knowledge/reindex", "/api/strategist/advise"}


def _is_job_trigger_path(path):
    return path.startswith("/api/run-") or path in _JOB_RATE_LIMIT_EXACT


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError):
    """API hardening (2026-07-24): as config-mutating POST endpoints incrementally move from ad
    hoc `await req.json()` + manual coercion to typed Pydantic request models (see PolicyUpdate/
    RetirementConfigUpdate below), a malformed body now 422s with FastAPI's default
    `{"detail": [...]}` shape instead of silently accepting garbage. The rest of this API's error
    responses are uniformly `{"error": "..."}`, and the frontend's error-display logic (`d.error`)
    only ever checks that shape -- this handler reformats Pydantic's validation errors into the
    same shape so a bad save surfaces a real, readable message in the UI instead of nothing."""
    msgs = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e.get("loc", []) if p != "body")
        msgs.append(f"{loc}: {e.get('msg')}" if loc else e.get("msg"))
    return JSONResponse({"error": "invalid request — " + "; ".join(msgs), "detail": exc.errors()},
                        status_code=422)


def _https_active():
    """True once this process is actually serving HTTPS. Auto-follows tls.py's own state (an
    active cert means run_server.py chose the HTTPS branch for THIS process — see
    tls.mark_applied()) OR the manual SESSION_HTTPS_ONLY override, for a deployment fronted by
    an external TLS-terminating reverse proxy this app doesn't manage itself. Either flips the
    session cookie's Secure attribute and the HSTS header — no separate manual toggle needed
    once one of the 3 in-app certificate methods is set up (2026-07-23f)."""
    return os.environ.get("SESSION_HTTPS_ONLY", "0") == "1" or tls.has_active_cert()


# SEC-5 (2026-07-25) RBAC: a 'viewer'-role session (a 2nd household member — see auth.create_viewer)
# gets full GET/read access everywhere (nothing below touches GET at all — "viewer" means
# read-only, not "less of the app"), but any WRITE (POST/PUT/PATCH/DELETE) is denied unless the
# path is in this small, explicit allowlist. Deliberately narrow and self-service-only: logging
# out, changing/securing YOUR OWN identity (password/MFA/recovery key), and self-service account
# closure (reset-account, which — per its own handler — only ever targets the CALLER's own row,
# never someone else's). Every config/secret/TLS/job-trigger/paper-trade/chat/knowledge-write
# endpoint is denied by omission, same "deny unless explicitly whitelisted" philosophy as
# _AUTH_PUBLIC_PATHS above. Chat (`/api/chat`) is deliberately NOT in this list for now — it can
# trigger paid LLM calls and agentic tool actions, which reads more like a privileged action than
# a plain data read; revisit if a real use case wants viewers to use chat (easy: add the path).
_VIEWER_ALLOWED_WRITE_PATHS = {
    "/api/auth/logout", "/api/auth/change-password", "/api/auth/change-username",
    "/api/auth/mfa/setup", "/api/auth/mfa/verify", "/api/auth/mfa/disable",
    "/api/auth/recovery-key/regenerate", "/api/auth/reset-account",
    # Notifications (2026-07-26) -- self-service, same category as the identity/security rows
    # above: a viewer manages their OWN device subscriptions/preferences, never anyone else's, and
    # none of these can read or change anything about the household's financial data or config.
    "/api/notifications/subscribe", "/api/notifications/unsubscribe",
    "/api/notifications/preferences", "/api/notifications/test",
    # Chat (2026-07-27, Jason's call): FIDUCIARY_TOOLS is entirely get_*/search_knowledge/
    # web_search (read-only by construction -- see _fiduciary_dispatch's own docstring), and
    # STRATEGIST_ADVICE_TOOLS explicitly excludes paper_buy/paper_sell/log_report (the only
    # mutating strategist tools) with a second hard-block in _advice_dispatch as defense in depth.
    # So sending/cancelling a chat message can never mutate financial data or config, regardless of
    # role -- same category as "research", not "write". Deliberately NOT included here:
    # DELETE /api/chat/session/{sid} -- chat.db's sessions table has no owner/uid column at all
    # (it's shared household history, same as the paper-trading ledger), so there is no way yet to
    # scope "delete your own session" vs "delete anyone's" -- allowing it here would let a viewer
    # delete the ADMIN's chat history too, a materially bigger permission than "use chat". Revisit
    # if/when chat sessions ever get a per-account owner column.
    "/api/chat", "/api/chat/cancel",
    # CALCULATORS (2026-10-19): POST only because the inputs are a JSON body. The handler is pure
    # arithmetic (calculators.run) — it reads no household data and writes nothing — so it is a
    # read in every sense that matters; a viewer can already see every figure these use. The
    # What-If panel's optional "save to account" uses /api/account-registry, which stays denied.
    "/api/calc",
    # TASK-002 emergency access: the designee is a VIEWER, so these two (and ONLY these two) are
    # reachable by viewers. Each handler re-checks "is the configured designee" itself
    # (emergency.request_access / cancel); every other emergency write is admin-only.
    "/api/emergency/request", "/api/emergency/cancel",
}


# SEC-1 leftover (2026-08-06): `session.expired` was declared in audit.EVENT_TYPES but never
# emitted -- the SEC-1 entry itself flagged it as "no real gap yet found where they'd fire."
# There IS one, it just isn't observable from a login handler: Starlette's SessionMiddleware
# silently DROPS a cookie whose signature has aged past SESSION_MAX_AGE, so `request.session`
# simply comes back empty and nothing anywhere notices the difference between "never logged in"
# and "was logged in, session aged out." The distinction matters when reading the log -- the
# latter explains an otherwise-suspicious burst of no_session denials from a known IP.
#
# Detected here: a `session` COOKIE was sent but decoded to no uid. Deduped by cookie value with
# a TTL, because the browser keeps re-sending that same dead cookie on every request until it's
# overwritten at next login -- emitting per request would flood the log with the same event,
# which is the exact failure mode the pre-login authz.denied noise item was about. Bounded so a
# hostile client can't grow it without limit.
_SESSION_EXPIRED_SEEN = {}          # cookie fingerprint -> last-emitted epoch
_SESSION_EXPIRED_DEDUPE_S = 900     # 15 min
_SESSION_EXPIRED_MAX = 512


def _note_session_expired(request, path):
    raw = request.cookies.get("session")
    if not raw:
        return                       # no cookie at all -> genuinely never authenticated, not an expiry
    fp = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:32]   # never log the cookie itself
    now = time.time()
    last = _SESSION_EXPIRED_SEEN.get(fp)
    if last is not None and (now - last) < _SESSION_EXPIRED_DEDUPE_S:
        return
    if len(_SESSION_EXPIRED_SEEN) >= _SESSION_EXPIRED_MAX:
        for _k, _ts in sorted(_SESSION_EXPIRED_SEEN.items(), key=lambda kv: kv[1])[:_SESSION_EXPIRED_MAX // 2]:
            _SESSION_EXPIRED_SEEN.pop(_k, None)
    _SESSION_EXPIRED_SEEN[fp] = now
    audit.emit("session.expired", outcome="failure", request=request, resource=path,
               detail={"reason": "session_cookie_present_but_no_longer_valid",
                       "cookie_fingerprint": fp, "max_age_days": auth.SESSION_MAX_AGE_DAYS})


@app.middleware("http")
async def _security_and_auth_gate(request: Request, call_next):
    path = request.url.path
    # TASK-001b (ghost session): a signed cookie proves only that WE issued it, not that the account
    # still exists or that the session is still wanted. Re-validate against the users table on every
    # request (SQLite PK lookup -- negligible). Missing user, or a session minted under an older
    # per-user epoch (password change / MFA disable), is cleared and treated as unauthenticated.
    _sess_uid = request.session.get("uid")
    if _sess_uid:
        _su = auth.get_user_by_id(_sess_uid)
        _why = None
        if not _su:
            _why = "user_missing"
        else:
            _uep = int(_su.get("session_epoch") or 0)
            _sep = request.session.get("ep")
            if _sep is None and _uep == 0:
                request.session["ep"] = 0          # pre-epoch cookie, never bumped: adopt, don't log out
            elif _sep != _uep:
                _why = "session_epoch_mismatch"
        if _why:
            request.session.clear()
            audit.emit("session.invalidated", outcome="denied", request=request, actor=_sess_uid,
                       resource=path, detail={"reason": _why, "method": request.method})
            if path == "/api/auth/logout":         # let the browser drop the dead cookie cleanly
                return JSONResponse({"ok": True})
            if path not in _AUTH_PUBLIC_PATHS:
                return JSONResponse({"error": "authentication required"}, status_code=401)
    if path not in _AUTH_PUBLIC_PATHS and not request.session.get("uid"):
        _note_session_expired(request, path)
        audit.emit("authz.denied", outcome="denied", request=request, resource=path,
                   detail={"reason": "no_session", "method": request.method})
        return JSONResponse({"error": "authentication required"}, status_code=401)
    uid = request.session.get("uid")
    if uid and request.method in ("POST", "PUT", "PATCH", "DELETE") and path not in _VIEWER_ALLOWED_WRITE_PATHS:
        user = auth.get_user_by_id(uid)
        if user and not auth.is_admin(user):
            audit.emit("authz.denied", outcome="denied", request=request, actor=uid,
                       actor_name=user.get("username"), resource=path,
                       detail={"reason": "viewer_role_write_denied", "method": request.method})
            return JSONResponse({"error": "this account is view-only — an admin must make this "
                                          "change"}, status_code=403)
    if request.method == "POST" and _is_job_trigger_path(path):
        # 2026-07-26: resolved client IP (audit.client_ip), not the raw TCP peer -- see audit.py's
        # TRUST_PROXY_HEADERS. Behind the AWS demo's ALB, the raw peer is constant for every
        # visitor, which would otherwise bucket everyone's job-trigger rate limit together.
        ip = audit.client_ip(request)
        if auth.rate_limited(ip, bucket="jobs", limit=JOB_RATE_LIMIT_PER_MIN):
            audit.emit("authz.denied", outcome="denied", request=request, resource=path,
                       actor=request.session.get("uid"),
                       detail={"reason": "job_rate_limited", "method": request.method})
            return JSONResponse({"error": "too many job triggers — wait a minute and try again"},
                                status_code=429)
        auth.record_attempt(ip, bucket="jobs")
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    if _https_active():
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp


def _session_secret():
    """SESSION_SECRET should be set explicitly in .env for a real deployment (see
    .env.example). If it isn't, generate one and PERSIST it next to the data volume so
    sessions survive a restart instead of silently invalidating every time the container
    restarts — better default behavior for a local single-user app than forcing the env var."""
    env = os.environ.get("SESSION_SECRET")
    if env:
        return env
    path = os.environ.get("SESSION_SECRET_FILE", "/data/.session_secret")
    try:
        with open(path, encoding="utf-8") as f:
            v = f.read().strip()
            if v:
                return v
    except Exception:
        pass
    v = uuid.uuid4().hex + uuid.uuid4().hex
    try:
        # atomic (2026-07-24, CLOUD-PORTABILITY-ROADMAP.md Phase 2): a torn write here on a
        # killed-mid-boot container would leave a corrupt-but-non-empty secret, which the read
        # branch above would then happily accept, silently invalidating every session on the
        # next restart instead of failing loudly.
        store.atomic_write_text(path, v, mode=0o600)
    except Exception:
        pass
    return v


app.add_middleware(SessionMiddleware, secret_key=_session_secret(),
                    max_age=auth.SESSION_MAX_AGE_DAYS * 86400, same_site="strict",
                    https_only=_https_active())

auth.init_db()
audit.init_db()  # SEC-1: security event log store (own DB, 0600) — safe if it fails (emit never raises)
audit.start_retention_thread()  # SEC-2: daily automated purge_expired() — see audit.py
audit_anchor.init_db()  # SEC-9: adds the anchors/anchor_renewals tables to the same DB file
audit_anchor.start_anchor_thread()  # SEC-9: fixed-schedule (default daily) RFC 3161 anchor job — see audit_anchor.py
audit_anchor.start_verify_thread()  # SEC-9: independent, more frequent offline re-verify/detection job
detections.start_detections_thread()  # SEC-4: periodic rules sweep over the audit log — see detections.py
demo_seed.seed_if_needed()  # Demo harness: no-op unless DEMO_MODE=1; refuses to touch a real account (see demo_seed.py)
demo_seed.start_demo_reset_thread()  # Demo harness: no-op unless DEMO_MODE=1

# Whatever certificate exists on disk right now is, by definition, what run_server.py just chose
# to serve for THIS process (see tls.py / run_server.py) — clears any stale "restart needed" flag
# left over from the operation that installed it, and starts the background thread that checks
# (every TLS_RENEWAL_CHECK_INTERVAL_S) whether an ACME-issued cert with auto-renew on needs
# renewing. A no-op in every other case (no cert, or a self-signed/manual cert with no renewal
# concept).
tls.mark_applied()
tls.start_renewal_thread()


def _require_uid(request: Request):
    """The global gate above already guarantees this is set for any route that reaches a
    handler at all — this just gives auth-scoped handlers a clean, obviously-named read."""
    return request.session.get("uid")


def _require_admin(request: Request):
    """SEC-5 (2026-07-25): returns the current session's user dict if they're an admin, else
    None. The global gate already blocks a viewer from reaching most WRITE endpoints by path, but
    the household-accounts management endpoints below need the stronger, explicit "must be admin"
    check regardless of method — GET /api/auth/users (listing accounts) is a read, so the gate's
    write-only restriction wouldn't touch it, yet account enumeration is exactly the kind of thing
    a viewer shouldn't see either."""
    uid = request.session.get("uid")
    user = auth.get_user_by_id(uid) if uid else None
    return user if (user and auth.is_admin(user)) else None


def _emit_config_changed(request, resource, old, new, summarize_keys=None):
    """SEC-1 broadening (2026-07-25) — shared diff-and-log helper for the app's config-mutating
    endpoints (retirement/insurance/scanner/accounts/settings/schedule/prompts/policy). Scalar
    fields are logged old->new in full (the whole detail payload is Fernet-encrypted at rest by
    audit.emit — see audit.py); keys named in `summarize_keys` are logged as before/after COUNTS
    only, not full contents. That's for list/dict-valued fields that hold bulk PII (insurance's
    policies/contacts/estate_docs/income_sources_*) — without this, every routine insurance save
    would duplicate a full copy of every policy/contact/estate-doc record into the audit log on
    top of the (already-encrypted) copy store.py already keeps, which is unnecessary duplication
    of sensitive data for no real audit value (the count changing is what matters for a security
    trail; the contents are the app's own data, not the audit log's job to hold twice).

    Caller contract: `old` MUST be captured from an INDEPENDENT read (a second call to the same
    read-from-store function used to build the working copy) taken before ANY mutation — several
    of these config blobs contain nested dicts that handlers mutate in place (e.g. schedule.py's
    `entry = cur.get(k, {})` pattern), so a shallow `dict(old)` copy of an already-mutated object
    would silently show no diff. Two independent store reads sidesteps that entirely regardless
    of each handler's own mutation style.

    Never raises — mirrors audit.emit's own fail-safe contract; a bad diff means the change goes
    unlogged, not that the underlying save fails."""
    try:
        summarize_keys = summarize_keys or set()
        changed = {}
        for k in set(old or {}) | set(new or {}):
            ov, nv = (old or {}).get(k), (new or {}).get(k)
            if ov == nv:
                continue
            if k in summarize_keys:
                changed[k] = {
                    "old_count": len(ov) if isinstance(ov, (list, dict)) else None,
                    "new_count": len(nv) if isinstance(nv, (list, dict)) else None,
                }
            else:
                changed[k] = {"old": ov, "new": nv}
        if changed:
            audit.emit("config.changed", request=request,
                       actor=(request.session.get("uid") if request is not None else None),
                       resource=resource, detail={"changed": changed})
        return changed
    except Exception as e:
        print(f"[audit] config-diff logging failed for {resource}: {e}", flush=True)
        return {}


@app.get("/api/auth/status")
def auth_status(request: Request):
    has_user = auth.has_any_user()
    uid = request.session.get("uid")
    user = auth.get_user_by_id(uid) if uid else None
    return {"has_user": has_user, "authenticated": bool(user),
            "user": auth.public_user(user) if user else None,
            # SEC-5 UX gap #2, completed 2026-08-06: the frontend needs to know WHICH writes a
            # viewer may perform so it can refuse the rest client-side (see the fetch guard in
            # static/index.html). Served from the server's own set rather than re-typed in JS --
            # a hand-copied duplicate would drift the first time a path is added here, and it
            # would drift SILENTLY (the UI would block something the backend allows, or worse,
            # allow something it doesn't, producing a confusing 403 instead of a clean message).
            # Exposing it leaks nothing: these are the paths a viewer is already permitted to
            # call, and the real enforcement stays server-side in _security_and_auth_gate.
            "viewer_allowed_write_paths": sorted(_VIEWER_ALLOWED_WRITE_PATHS)}


@app.post("/api/auth/first-run")
async def auth_first_run(request: Request):
    """Creates the ONE account this deployment will have. Refuses once any account exists —
    this is a bootstrap-only endpoint, not a general signup form (no multi-tenancy; a 2nd
    household member is a small manual addition later, not a self-serve flow)."""
    if auth.has_any_user():
        return JSONResponse({"error": "an account already exists — use /api/auth/login"}, status_code=400)
    body = await request.json()
    try:
        uid, recovery_key = auth.create_user(body.get("username"), body.get("password"))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    request.session["uid"] = uid
    request.session["ep"] = auth.session_epoch(uid)
    # 2026-08-06: session.created is emitted at the two (and only two) points that actually mint a
    # session cookie -- here and in auth_login below. SEC-1 noted this "would need to hook the
    # session-cookie-issuance point, not just login success"; that's exactly what this is. It is
    # deliberately NOT redundant with auth.login.success: first-run account creation issues a
    # session WITHOUT any login event at all, so without this the very first session in a
    # deployment's life has no issuance record anywhere in the chain.
    audit.emit("session.created", request=request, actor=uid, actor_name=body.get("username"),
               resource="/api/auth/first-run", detail={"via": "first_run_account_creation"})
    # recovery_key (2026-07-24, Account Recovery Tier 2) is returned ONCE, here, and never again --
    # only its argon2 hash is stored. This is the host-CLI fallback (`auth.py --reset-credentials`)
    # for when a user has no working MFA AND forgets the password; without it, host/container
    # access alone would be the only way back in, which silently grants full access with no second
    # factor -- see RESUME-TRACKER.md's Account Recovery entry for the full reasoning.
    return {"ok": True, "user": auth.public_user(auth.get_user_by_id(uid)), "recovery_key": recovery_key,
            "recovery_key_note": "Save this somewhere safe (a password manager, or printed and "
                                  "locked away) -- it's the ONLY way back into this account if you "
                                  "ever forget your password AND lose your MFA device/backup codes. "
                                  "It is never shown again and never stored anywhere retrievable, "
                                  "not even by whoever manages this deployment."}


@app.post("/api/auth/login")
async def auth_login(request: Request):
    # 2026-07-26: resolved client IP, not the raw TCP peer -- see audit.py's TRUST_PROXY_HEADERS.
    # Behind the AWS demo's ALB, request.client.host is the ALB's own constant address for every
    # visitor; using it here would bucket every visitor's login attempts under one shared rate
    # limit (one attacker's failures could lock out everyone else, and it wouldn't actually throttle
    # a specific abusive source at all).
    ip = audit.client_ip(request)
    if auth.rate_limited(ip):
        return JSONResponse({"error": "too many attempts — wait a minute and try again"}, status_code=429)
    body = await request.json()
    attempted = (body.get("username") or "")
    user = auth.get_user_by_username(attempted)
    if not user or not auth.verify_password(user, body.get("password") or ""):
        auth.record_login_attempt(ip)
        audit.emit("auth.login.failure", outcome="failure", request=request,
                   actor_name=attempted, resource="/api/auth/login",
                   detail={"reason": "invalid_username_or_password"})
        return JSONResponse({"error": "invalid username or password"}, status_code=401)
    if user["totp_enabled"]:
        if not auth.mfa_check_code(user["id"], body.get("code") or ""):
            auth.record_login_attempt(ip)
            audit.emit("auth.mfa.failure", outcome="failure", request=request,
                       actor=user["id"], actor_name=user.get("username"), resource="/api/auth/login",
                       detail={"reason": "invalid_or_missing_totp"})
            return JSONResponse({"error": "invalid or missing authenticator code", "mfa_required": True},
                                status_code=401)
    request.session["uid"] = user["id"]
    request.session["ep"] = int(user.get("session_epoch") or 0)
    audit.emit("auth.login.success", request=request,
               actor=user["id"], actor_name=user.get("username"), resource="/api/auth/login",
               detail={"mfa_used": bool(user["totp_enabled"])})
    # 2026-08-06 (see the first-run handler above): the credential check and the session issuance
    # are separate facts. Keeping them as separate events means a future change that authenticates
    # without minting a session (or vice versa) shows up as a mismatch in the log rather than
    # hiding inside one overloaded event.
    audit.emit("session.created", request=request, actor=user["id"], actor_name=user.get("username"),
               resource="/api/auth/login",
               detail={"via": "password_login", "mfa_used": bool(user["totp_enabled"]),
                       "role": user.get("role") or "admin"})
    return {"ok": True, "user": auth.public_user(user)}


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    _uid = request.session.get("uid")
    request.session.clear()
    audit.emit("auth.logout", request=request, actor=_uid, resource="/api/auth/logout")
    return {"ok": True}


@app.post("/api/auth/change-password")
async def auth_change_password(request: Request):
    uid = _require_uid(request)
    user = auth.get_user_by_id(uid)
    body = await request.json()
    if not auth.verify_password(user, body.get("current_password") or ""):
        return JSONResponse({"error": "current password is incorrect"}, status_code=401)
    try:
        auth.set_password(uid, body.get("new_password") or "")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    request.session["ep"] = auth.session_epoch(uid)   # this session stays signed in; all others die
    audit.emit("auth.password.changed", request=request, actor=uid, resource="/api/auth/change-password")
    return {"ok": True}


@app.post("/api/auth/mfa/setup")
def auth_mfa_setup(request: Request):
    """Generates a new TOTP secret (NOT yet enabled — see /verify) and a QR code any RFC
    6238 authenticator app can scan (Google Authenticator, Authy, 1Password, Bitwarden, ...).
    Calling this again before /verify simply replaces the pending, not-yet-active secret."""
    secret, uri, qr = auth.mfa_provision(_require_uid(request))
    return {"ok": True, "otpauth_uri": uri, "qr_data_uri": qr}


@app.post("/api/auth/mfa/verify")
async def auth_mfa_verify(request: Request):
    """Confirms the authenticator app is actually producing valid codes, THEN enables MFA and
    returns 10 single-use backup codes — shown here exactly once, never retrievable again."""
    body = await request.json()
    _uid = _require_uid(request)
    ok, codes = auth.mfa_verify_and_enable(_uid, body.get("code") or "")
    if not ok:
        return JSONResponse({"error": "incorrect code — check your authenticator app and try again"},
                            status_code=400)
    audit.emit("auth.mfa.enrolled", request=request, actor=_uid, resource="/api/auth/mfa/verify")
    return {"ok": True, "backup_codes": codes,
            "note": "Save these now — shown only once. Each works one time if you lose your authenticator."}


@app.post("/api/auth/mfa/disable")
async def auth_mfa_disable(request: Request):
    uid = _require_uid(request)
    user = auth.get_user_by_id(uid)
    body = await request.json()
    if not auth.verify_password(user, body.get("password") or ""):
        return JSONResponse({"error": "password is incorrect"}, status_code=401)
    auth.mfa_disable(uid)
    emergency.revalidate("designee MFA disabled")
    request.session["ep"] = auth.session_epoch(uid)
    audit.emit("auth.mfa.disabled", request=request, actor=uid, resource="/api/auth/mfa/disable")
    return {"ok": True}


@app.post("/api/auth/recovery-key/regenerate")
async def auth_recovery_key_regenerate(request: Request):
    """(Re)issues the Account Recovery Tier 2 host-CLI recovery key (2026-07-24) -- covers both a
    deliberate rotation and accounts migrated in from before this feature existed
    (recovery_key_hash starts NULL for those, see auth.init_db()'s migration). Password-gated
    like the other identity/security-affecting actions (change-password, mfa/disable,
    change-username). The plaintext key is returned ONCE here and never again."""
    uid = _require_uid(request)
    user = auth.get_user_by_id(uid)
    body = await request.json()
    if not auth.verify_password(user, body.get("password") or ""):
        return JSONResponse({"error": "password is incorrect"}, status_code=401)
    key = auth.set_recovery_key(uid)
    audit.emit("auth.recovery_key.regenerated", request=request, actor=uid,
               resource="/api/auth/recovery-key/regenerate")
    return {"ok": True, "recovery_key": key,
            "note": "Save this now -- shown only once, and this immediately replaces any "
                    "previous recovery key (the old one stops working)."}


@app.post("/api/auth/change-username")
async def auth_change_username(request: Request):
    """Rename the account (2026-07-24 backlog). Password-gated the same way as
    change-password/mfa-disable — an already-authenticated session alone isn't treated as
    sufficient for an identity change without re-proving the password."""
    uid = _require_uid(request)
    user = auth.get_user_by_id(uid)
    body = await request.json()
    if not auth.verify_password(user, body.get("password") or ""):
        return JSONResponse({"error": "password is incorrect"}, status_code=401)
    old_username = user["username"]
    try:
        auth.set_username(uid, body.get("new_username") or "")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    audit.emit("auth.username.changed", request=request, actor=uid, resource="/api/auth/change-username",
               detail={"old": old_username, "new": (body.get("new_username") or "").strip()})
    return {"ok": True, "user": auth.public_user(auth.get_user_by_id(uid))}


@app.post("/api/auth/reset-account")
async def auth_reset_account(request: Request):
    """Authenticated, in-app reset of auth.db ONLY (2026-07-24 backlog — Jason: "reset the
    auth.db... to simply test out the new changes, or if I wanted to change the name of the
    single user account"). Deliberately narrow: deletes just this account's row via
    auth.delete_user(), leaving store.db/chat.db/the knowledge index/all financial config
    untouched — the app returns to the first-run screen, not a blank app. This is NOT the same
    thing as the still-unbuilt Account Recovery Tier 2 `--wipe-all` host CLI (see
    RESUME-TRACKER.md) — that one is for when you're LOCKED OUT (no session, no password, no
    MFA); this one is for when you're already logged in and want a clean-slate account on
    purpose. Two independent confirmations required since it's irreversible from the UI: the
    current password, AND typing the username back exactly (guards against a stray/accidental
    click doing something a "are you sure?" dialog alone is too easy to click through).

    SEC-5 (2026-07-25) guard: if OTHER accounts exist (viewer accounts an admin created), the
    admin can't self-reset here — that would delete the only admin row while leaving one or more
    viewer accounts behind with literally no admin left to manage them (list/create/delete
    accounts are all admin-only). Delete the other accounts first, or use this endpoint only when
    this is genuinely the last remaining account, same as before SEC-5 existed."""
    uid = _require_uid(request)
    user = auth.get_user_by_id(uid)
    body = await request.json()
    if not auth.verify_password(user, body.get("password") or ""):
        return JSONResponse({"error": "password is incorrect"}, status_code=401)
    if (body.get("confirm_username") or "").strip() != user["username"]:
        return JSONResponse({"error": "typed username does not match — type it exactly to confirm"},
                             status_code=400)
    if auth.is_admin(user) and auth.user_count() > 1:
        return JSONResponse({"error": "other accounts still exist on this deployment — delete "
                                       "them first (SETTINGS → IAM → HOUSEHOLD ACCOUNTS), "
                                       "or this would leave them with no admin"}, status_code=400)
    audit.emit("auth.account.reset", outcome="success", request=request, actor=uid,
               actor_name=user["username"], resource="/api/auth/reset-account")
    auth.delete_user(uid)
    emergency.revalidate("account reset")
    request.session.clear()
    return {"ok": True}


@app.get("/api/auth/users")
def auth_list_users(request: Request):
    """SEC-5 (2026-07-25) — admin-only household-accounts list, for SETTINGS → IAM →
    "HOUSEHOLD ACCOUNTS" (moved from SECURITY 2026-07-26 — see iamContent()). A GET, so the
    global gate's write-only viewer restriction wouldn't otherwise touch it — enforced explicitly
    here via _require_admin instead."""
    if not _require_admin(request):
        return JSONResponse({"error": "admin only"}, status_code=403)
    return {"users": auth.list_users()}


@app.post("/api/auth/users")
async def auth_create_user(request: Request):
    """SEC-5 (2026-07-25) — admin creates an additional, restricted 'viewer' account (e.g. a
    spouse who should see the dashboards but not change config/secrets/security settings). Role
    is ALWAYS 'viewer' here, hardcoded server-side — this endpoint has no `role` body field to
    honor, so a client can never request/escalate to a second admin through it."""
    admin = _require_admin(request)
    if not admin:
        return JSONResponse({"error": "admin only"}, status_code=403)
    body = await request.json()
    try:
        uid, recovery_key = auth.create_viewer(body.get("username"), body.get("password"))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    new_user = auth.get_user_by_id(uid)
    audit.emit("auth.user.created", request=request, actor=admin["id"], actor_name=admin["username"],
               resource="/api/auth/users",
               detail={"new_user_id": uid, "new_username": new_user["username"], "role": "viewer"})
    return {"ok": True, "user": auth.public_user(new_user), "recovery_key": recovery_key,
            "recovery_key_note": "Save this somewhere safe — it's this new account's own recovery "
                                  "key (same one-time-shown, host-CLI-recoverable mechanism as the "
                                  "original account's). Give it to whoever will use this login."}


@app.post("/api/auth/users/{uid}/delete")
def auth_delete_user(uid: str, request: Request):
    """SEC-5 (2026-07-25) — admin removes a viewer account. auth.admin_delete_viewer() itself
    refuses to delete an 'admin'-role row (there is exactly one, the original account), so this
    endpoint can never be used to remove the admin — no separate "not yourself" check needed."""
    admin = _require_admin(request)
    if not admin:
        return JSONResponse({"error": "admin only"}, status_code=403)
    target = auth.get_user_by_id(uid)
    if not target:
        return JSONResponse({"error": "no such account"}, status_code=404)
    try:
        auth.admin_delete_viewer(uid)
        emergency.revalidate("household account deleted")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    audit.emit("auth.user.deleted", request=request, actor=admin["id"], actor_name=admin["username"],
               resource="/api/auth/users/{}/delete".format(uid),
               detail={"deleted_user_id": uid, "deleted_username": target["username"]})
    return {"ok": True}


@app.post("/api/auth/users/{uid}/transfer-admin")
async def auth_transfer_admin(uid: str, request: Request):
    """SEC-5 UX gap fix (2026-07-26) — RESUME-TRACKER.md flagged "no in-place role-change (delete +
    recreate works instead)" as a known gap; this is that fix. Admin-only, and re-authenticates the
    CALLER's own password (same bar as /api/auth/reset-account) since handing over the single admin
    seat is the most privileged action this app exposes. Swaps roles atomically via
    auth.transfer_admin_role() — no delete, no recreate, no new recovery key, no orphaned audit
    history under a dead account id."""
    admin = _require_admin(request)
    if not admin:
        return JSONResponse({"error": "admin only"}, status_code=403)
    body = await request.json()
    if not auth.verify_password(admin, body.get("password") or ""):
        return JSONResponse({"error": "password is incorrect"}, status_code=401)
    target = auth.get_user_by_id(uid)
    if not target:
        return JSONResponse({"error": "no such account"}, status_code=404)
    if (body.get("confirm_username") or "").strip() != target["username"]:
        return JSONResponse({"error": "typed username does not match — type it exactly to confirm "
                                       "you're handing the admin role to the right account"},
                             status_code=400)
    try:
        auth.transfer_admin_role(admin["id"], uid)
        emergency.revalidate("admin role transferred", force=True)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    audit.emit("auth.user.role_changed", request=request, actor=admin["id"], actor_name=admin["username"],
               resource="/api/auth/users/{}/transfer-admin".format(uid),
               detail={"previous_admin": admin["username"], "new_admin": target["username"]})
    request.session.clear()   # the caller is no longer admin -- force a fresh login
    return {"ok": True, "note": f"Admin role transferred to {target['username']}. "
                                  "You've been logged out — log back in; your account is now a viewer."}


@app.post("/api/auth/recovery/start")
async def auth_recovery_start(request: Request):
    """Account Recovery Tier 1 (RESUME-TRACKER.md 2026-07-23f design, built 2026-07-24):
    self-service password reset for a user who still has their authenticator app or a saved
    backup code, but forgot the password. Re-uses auth.mfa_check_code() — the same primitive
    that already validates a TOTP/backup code at login — as the re-authentication factor. Public
    by necessity (this runs pre-login), so it's rate-limited by IP using the same bucket as
    normal login attempts, and the error message is identical whether the username doesn't
    exist, exists without MFA enabled, or the code is wrong — doesn't confirm or deny account
    existence/MFA status to an unauthenticated caller."""
    ip = audit.client_ip(request)  # 2026-07-26: resolved, not the raw peer -- see audit.py TRUST_PROXY_HEADERS
    if auth.rate_limited(ip):
        return JSONResponse({"error": "too many attempts — wait a minute and try again"}, status_code=429)
    body = await request.json()
    user = auth.get_user_by_username(body.get("username") or "")
    generic_error = {"error": "invalid username or code, or MFA is not enabled on this account "
                               "(account recovery requires MFA — contact whoever manages this "
                               "deployment if MFA was never set up)"}
    if not user or not user["totp_enabled"]:
        auth.record_login_attempt(ip)
        audit.emit("auth.recovery.used", outcome="failure", request=request,
                   actor_name=(body.get("username") or ""), resource="/api/auth/recovery/start",
                   detail={"reason": "unknown_user_or_mfa_not_enabled"})
        return JSONResponse(generic_error, status_code=401)
    if not auth.mfa_check_code(user["id"], body.get("code") or ""):
        auth.record_login_attempt(ip)
        audit.emit("auth.recovery.used", outcome="failure", request=request,
                   actor=user["id"], actor_name=user.get("username"),
                   resource="/api/auth/recovery/start", detail={"reason": "invalid_code"})
        return JSONResponse(generic_error, status_code=401)
    audit.emit("auth.recovery.used", outcome="success", request=request,
               actor=user["id"], actor_name=user.get("username"), resource="/api/auth/recovery/start")
    # A short-lived marker distinct from the real "uid" session key -- this alone grants nothing
    # except the ability to call /recovery/reset once, within 5 minutes. The global auth gate
    # only ever checks "uid", never "recovery_uid", so this cannot be used to reach any other
    # endpoint.
    request.session["recovery_uid"] = user["id"]
    request.session["recovery_exp"] = int(time.time()) + 300
    return {"ok": True}


@app.post("/api/auth/recovery/reset")
async def auth_recovery_reset(request: Request):
    ruid = request.session.get("recovery_uid")
    rexp = request.session.get("recovery_exp") or 0
    if not ruid or time.time() > rexp:
        request.session.pop("recovery_uid", None)
        request.session.pop("recovery_exp", None)
        return JSONResponse({"error": "recovery session expired — verify your code again"}, status_code=401)
    body = await request.json()
    try:
        auth.set_password(ruid, body.get("new_password") or "")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    request.session.pop("recovery_uid", None)
    request.session.pop("recovery_exp", None)
    audit.emit("auth.recovery.reset", request=request, actor=ruid, resource="/api/auth/recovery/reset")
    return {"ok": True}


# ============================================================================
#  In-app HTTPS certificate management (2026-07-23f) — SETTINGS -> SECURITY -> Certificate.
#  All 3 methods converge on tls.py's status()/CERT_PATH/KEY_PATH; every mutating call here
#  triggers an automatic in-place restart (tls.restart_to_apply()) a couple seconds later so the
#  new certificate takes effect without a separate manual step. Gated by the same global auth
#  middleware as everything else — nothing here is in _AUTH_PUBLIC_PATHS.
# ============================================================================
@app.get("/api/tls/status")
def tls_status():
    return tls.status()


@app.post("/api/tls/self-signed")
async def tls_self_signed(req: Request):
    """Body: {"hostnames": ["192.168.1.50", "fiduciary.lan", ...]} — every LAN IP/hostname you
    actually use to reach this box, so the browser doesn't warn about a SAN mismatch on top of
    the expected self-signed trust warning. At least one entry required."""
    body = await req.json()
    hosts = [str(h).strip() for h in (body.get("hostnames") or []) if str(h).strip()]
    if not hosts:
        return JSONResponse({"error": "at least one hostname or IP is required (e.g. this "
                                       "box's LAN IP, a hostname you use to reach it, or "
                                       "'localhost')"}, status_code=400)
    try:
        status = tls.generate_self_signed(hosts)
        audit.emit("tls.cert.self_signed_generated", request=req, actor=req.session.get("uid"),
                   resource="/api/tls/self-signed", detail={"hostnames": hosts})
        return {"ok": True, "status": status}
    except Exception as e:
        audit.emit("tls.cert.self_signed_generated", outcome="error", request=req,
                   actor=req.session.get("uid"), resource="/api/tls/self-signed",
                   detail={"hostnames": hosts, "error": str(e)})
        return JSONResponse({"error": f"self-signed generation failed: {e}"}, status_code=500)


@app.post("/api/tls/csr")
async def tls_csr(req: Request):
    """Body: {"common_name": "dashboard.example.com", "sans": [...]}. Generates a NEW private
    key (stays on this server) + a CSR you can take to any Certificate Authority. Submit the
    returned PEM; once you have the signed certificate, POST it to /api/tls/import-cert."""
    body = await req.json()
    cn = str(body.get("common_name") or "").strip()
    if not cn:
        return JSONResponse({"error": "common_name is required"}, status_code=400)
    sans = [str(s).strip() for s in (body.get("sans") or []) if str(s).strip()]
    try:
        csr_pem = tls.generate_csr(cn, sans)
        audit.emit("tls.cert.csr_generated", request=req, actor=req.session.get("uid"),
                   resource="/api/tls/csr", detail={"common_name": cn, "sans": sans})
        return {"ok": True, "csr_pem": csr_pem,
                "note": "Submit this CSR to any Certificate Authority (or an internal/self-"
                        "signed CA for a LAN-only deployment). Once you have the signed "
                        "certificate back, POST it to /api/tls/import-cert to install it — the "
                        "private key never leaves this server."}
    except Exception as e:
        return JSONResponse({"error": f"CSR generation failed: {e}"}, status_code=500)


@app.post("/api/tls/import-cert")
async def tls_import_cert(req: Request):
    """Body: {"cert_pem": "...", "chain_pem": "..." (optional)}. Pairs the signed certificate
    with the pending CSR's private key — rejected with a clear error if they don't actually
    match, so a wrong/unrelated certificate can never get installed silently."""
    body = await req.json()
    cert_pem = body.get("cert_pem")
    if not cert_pem:
        return JSONResponse({"error": "cert_pem is required"}, status_code=400)
    try:
        status = tls.import_signed_certificate(cert_pem, body.get("chain_pem"))
        audit.emit("tls.cert.imported", request=req, actor=req.session.get("uid"),
                   resource="/api/tls/import-cert", detail={"had_chain": bool(body.get("chain_pem"))})
        return {"ok": True, "status": status}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"certificate import failed: {e}"}, status_code=400)


@app.post("/api/tls/acme/enroll")
async def tls_acme_enroll(req: Request):
    """Body: {"domain": "dashboard.example.com", "challenge": "http-01"|"dns-01",
    "extra_domains": [...], "dns_provider": "dns_cf", "dns_creds": {"CF_Token": "..."},
    "staging": bool}. http-01 needs a real public DNS name pointed at this host with port 80
    reachable from the internet; dns-01 needs no public reachability but needs `dns_provider`
    (acme.sh's own hook name — see https://github.com/acmesh-official/acme.sh/wiki/dnsapi) and
    the credentials THAT provider's hook expects in `dns_creds`. Installs + turns on auto-renew
    on success."""
    body = await req.json()
    domain = str(body.get("domain") or "").strip()
    if not domain:
        return JSONResponse({"error": "domain is required"}, status_code=400)
    dns_creds = body.get("dns_creds") if isinstance(body.get("dns_creds"), dict) else None
    extra_domains = body.get("extra_domains") if isinstance(body.get("extra_domains"), list) else None
    try:
        res = tls.acme_enroll(domain, challenge=(body.get("challenge") or "http-01"),
                               dns_provider=body.get("dns_provider"), dns_creds=dns_creds,
                               extra_domains=extra_domains, staging=body.get("staging"))
        # dns_creds deliberately excluded from the audit detail — those are third-party API tokens,
        # not this app's own secret store, but still credential material that shouldn't be
        # duplicated into a log even encrypted-at-rest. Domain/challenge/staging are not secrets.
        audit.emit("tls.acme.enrolled", request=req, actor=req.session.get("uid"),
                   resource="/api/tls/acme/enroll",
                   detail={"domain": domain, "challenge": body.get("challenge") or "http-01",
                           "staging": bool(body.get("staging"))})
        return {"ok": True, "status": res}
    except tls.AcmeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"ACME enrollment failed: {e}"}, status_code=500)


@app.post("/api/tls/acme/renew")
def tls_acme_renew(request: Request):
    """Manual 'renew now' — the background thread already does this automatically once the
    active ACME certificate is within TLS_RENEWAL_THRESHOLD_DAYS of expiring and auto_renew is
    on; this is for testing or renewing early on demand."""
    try:
        status = tls.acme_renew()
        audit.emit("tls.acme.renewed", request=request, actor=request.session.get("uid"),
                   resource="/api/tls/acme/renew")
        return {"ok": True, "status": status}
    except tls.AcmeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": f"renew failed: {e}"}, status_code=500)


@app.post("/api/tls/auto-renew")
async def tls_set_auto_renew(req: Request):
    body = await req.json()
    enabled = bool(body.get("enabled"))
    status = tls.set_auto_renew(enabled)
    audit.emit("tls.auto_renew.changed", request=req, actor=req.session.get("uid"),
               resource="/api/tls/auto-renew", detail={"enabled": enabled})
    return {"ok": True, "status": status}


@app.post("/api/tls/clear")
def tls_clear(request: Request):
    """Removes the active certificate — reverts to plain HTTP on the next (self-triggered)
    restart. Doesn't touch a prior ACME enrollment's account/renewal state, so re-enrolling
    later doesn't start over.

    2026-07-25 (Jason: a tester runs their own reverse proxy and wants no HTTPS internally —
    this endpoint already IS the "disable HTTPS, go back to plain HTTP" feature, wired to the
    SETTINGS → SECURITY "REMOVE CERTIFICATE (revert to plain HTTP)" button). Fixed a real gap
    while verifying that claim: HSTS (below, `_security_and_auth_gate`) is sent for ANY active
    cert including self-signed, and a browser that once trusted a self-signed cert here caches
    that HSTS policy for up to a year — simply removing the cert server-side does NOT undo an
    already-cached browser policy, so the browser can keep refusing plain HTTP on this host long
    after the server reverted (exactly the failure mode a self-signed-then-reverse-proxied
    deployment can hit). The fix: explicitly send `max-age=0` on THIS response — the standard way
    to tell a browser "forget the HSTS policy you cached for this host" — but ONLY when HTTPS is
    actually going away (`not _https_active()` after the clear). If SESSION_HTTPS_ONLY is
    force-set (an external TLS-terminating reverse proxy this app doesn't manage), clearing the
    in-app cert does NOT change the deployment's real HTTPS posture, so HSTS should keep being
    sent normally — the middleware below already does that in every other case; this override
    only fires for a genuine downgrade."""
    status = tls.clear_active_cert()
    audit.emit("tls.cert.cleared", request=request, actor=request.session.get("uid"),
               resource="/api/tls/clear")
    resp = JSONResponse({"ok": True, "status": status})
    if not _https_active():
        resp.headers["Strict-Transport-Security"] = "max-age=0"
    return resp


@app.post("/api/tls/restart")
def tls_restart():
    """Manually re-applies whatever certificate state is currently on disk — the mutating
    endpoints above already do this automatically, this is only for recovering from an edge
    case (e.g. a cert file dropped in by hand outside the app)."""
    tls.restart_to_apply()
    return {"ok": True, "note": "restarting now to apply the certificate configuration — this "
                                 "page will briefly disconnect"}


# ============================================================================
#  SEC-2 (2026-07-25) — read-only, redacted security audit-log viewer.
#  Gated by the same global session middleware as everything else (nothing here is in
#  _AUTH_PUBLIC_PATHS) — an authenticated household member can review their own security
#  history, but it's not publicly reachable. IP is masked by default (redact_ip=True in
#  read_recent()) since this is the surface most likely to get shown to a demo-site visitor —
#  see DEMO-SITE-BUILD-PLAN.md §3.3, "the demo site's centerpiece screen".
#
#  UNMASK (2026-07-27, Jason's follow-up after spotting the Shodan scan): the real household
#  instance's admin needs full-precision IPs to actually act on what they see here (e.g. copying an
#  offending range into scripts/aws-demo/setup-waf.sh's blocklist) -- permanently masking there too
#  would make the log strictly less useful with no matching privacy benefit, since the admin IS the
#  data subject on a private single-household deployment. `unmask=1` reveals full IPs (both the
#  event's own src_ip AND any IP-shaped strings inside a detection.alert's detail -- see
#  audit._redact_ips_in) -- but ONLY when BOTH: (a) this is NOT a DEMO_MODE deployment (the demo
#  site's whole point is protecting a VISITOR's IP, which has nothing to do with who's logged in as
#  the demo admin, so masking there is never negotiable regardless of caller), and (b) the caller is
#  an admin, not a viewer role (mirrors every other admin-only distinction in this app). Any other
#  combination silently falls back to the masked default rather than erroring -- fail-closed on the
#  privacy-sensitive default, same posture as the rest of this module.
# ============================================================================
@app.get("/api/audit/events")
def get_audit_events(limit: int = 200, unmask: bool = False, request: Request = None):
    """Most-recent-first security events: logins/logouts/MFA/recovery/config changes/TLS state/
    authz denials. IPs are masked to the /24 (last octet redacted) by default — full precision
    isn't needed to see WHAT happened or spot a pattern, and this endpoint's whole purpose is to be
    safe to show a visitor. `detail` (old->new config values etc.) is decrypted for display here
    since a logged-in household member is exactly who that detail is FOR — this isn't the
    encrypted-at-rest boundary, it's the same trust boundary as every other authenticated page in
    the app. Pass `unmask=1` for full-precision IPs — see the section docstring above for exactly
    when that's honored."""
    limit = max(1, min(int(limit or 200), 1000))
    do_unmask = False
    if unmask and not demo_seed.DEMO_MODE and request is not None and _require_admin(request):
        do_unmask = True
    return {"events": audit.read_recent(limit=limit, redact_ip=not do_unmask), "unmasked": do_unmask}


@app.get("/api/audit/verify")
def get_audit_verify():
    """Recomputes the HMAC hash chain across every stored event and reports whether it's intact —
    the control that makes the log trustworthy as evidence rather than just a list of rows (ISO
    A.8.15/NIST AU-9). Cheap enough to call on every viewer-panel load for a household-scale log;
    would need pagination/caching at a much larger scale than this app is designed for."""
    return audit.verify_chain()


@app.post("/api/audit/purge-expired")
def post_audit_purge(request: Request):
    """Manual 'run retention now' — the background thread (audit.start_retention_thread(),
    started at app boot) already does this once a day automatically; this is for testing the
    retention/re-anchor behavior on demand, mirroring /api/tls/acme/renew's same "automatic +
    manual trigger" pattern."""
    return audit.purge_expired(actor_name=(request.session.get("uid") or "manual"))


# ============================================================================
# SEC-9 (2026-09-01) — external timestamp anchoring API. The internal HMAC chain above (SEC-2)
# proves nothing was altered WITHOUT detection; it can't prove WHEN the chain existed, since an
# actor with full write access to this app's data directory could regenerate it wholesale and have
# it verify cleanly. These endpoints expose audit_anchor.py's RFC 3161 + Merkle-batched external
# anchoring: independent, third-party-signed proof that a given state of the log existed at a
# given time. See audit_anchor.py's module docstring for the full design.
# ============================================================================
@app.get("/api/audit/anchor-status")
def get_audit_anchor_status():
    """Summary for the SECURITY AUDIT LOG panel: the most recent successful external anchor, how
    many batches are queued/retrying (a stuck TSA never drops a batch — see audit_anchor.py), and
    which TSA endpoint(s) are configured."""
    return audit_anchor.anchor_status()


@app.get("/api/audit/anchors")
def get_audit_anchors(limit: int = 50):
    """Recent anchor batches, both completed and any still pending/retrying, most recent first."""
    return {"anchors": audit_anchor.list_anchors(limit=max(1, min(int(limit or 50), 500)))}


@app.get("/api/audit/verify-entry/{seq}")
def get_audit_verify_entry(seq: int):
    """Prove ONE audit-log entry's inclusion in an externally anchored batch: recomputes its
    Merkle leaf, walks the inclusion proof to the batch's root, confirms that root matches what
    was actually timestamped, and re-verifies the archived RFC 3161 token/cert chain offline.
    Returns a structured result with a per-check breakdown and the TSA-asserted time."""
    return audit_anchor.verify_entry(seq)


@app.get("/api/audit/verify-range")
def get_audit_verify_range(start: int, end: int):
    """Same as verify-entry but over a whole seq range: the internal chain check plus every
    covering anchor's token re-verified, with any not-yet-anchored sub-range called out
    explicitly (informational, not itself a failure — the most recent entries may simply not have
    reached the next scheduled anchor yet)."""
    if end < start:
        start, end = end, start
    return audit_anchor.verify_range(start, end)


@app.post("/api/audit/anchor-now")
def post_audit_anchor_now(request: Request):
    """Manual 'anchor now' — the background thread (audit_anchor.start_anchor_thread(), started
    at app boot) already does this once a day automatically; this is for testing/demoing the
    anchor pipeline on demand, mirroring /api/audit/purge-expired's same automatic+manual
    pattern. Retries any already-queued pending batch first, exactly like the scheduled job."""
    return audit_anchor.anchor_job()


@app.get("/api/detections/rules")
def get_detection_rules():
    """SEC-4 (2026-07-25) — the rule catalog (id/label/description) for a "what does this actually
    watch for" display. No sensitive data here (just rule metadata), so no admin gate needed
    beyond the standard authenticated-session requirement every route already has."""
    return {"rules": list(detections.RULES)}


@app.post("/api/detections/run")
def post_detections_run():
    """Manual 'check now' — the background thread (detections.start_detections_thread(), started
    at app boot) already sweeps every DETECTIONS_INTERVAL_S automatically; this is for testing/
    demoing the detect step on demand, same automatic+manual pattern as retention/ACME renewal.
    Viewer-denied by omission (not in _VIEWER_ALLOWED_WRITE_PATHS), same as every other mutating
    endpoint — no separate _require_admin check needed."""
    return detections.run_detections()


@app.get("/api/privacy/export")
def get_privacy_export(request: Request):
    """SEC-8 (2026-07-25) — self-service data access/export (the "right to access/portability"
    half of a DSAR workflow). A GET, so it's untouched by the write-only viewer gate — every
    authenticated session (admin OR viewer) can pull their OWN bundle, no admin check needed.

    SCOPE DECISION, stated plainly: this exports the requester's own IDENTITY + SECURITY-HISTORY
    data (account record + their own audit-log activity, core fields only — no decrypted `detail`,
    since a change payload can contain OTHER household members' shared financial context, not just
    the requester's own PII). It deliberately does NOT bundle the shared household financial ledger
    (holdings, retirement config, insurance/estate docs, etc.) — in a single-tenant household app
    that data isn't "personal data belonging to one data subject" in the DSAR sense, it's jointly-
    used household data with no single rightful requester. See compliance/ROPA.md for the full
    reasoning. (If this app is ever productized multi-tenant, that data WOULD need its own
    per-owner export scope — flagged there as a would-need-to-change-if item, not silently
    assumed away.)

    ERASURE ("right to be forgotten") is deliberately NOT a new endpoint here — it's already fully
    served by the existing self-service account deletion (`POST /api/auth/reset-account`, viewer-
    allowed) and admin-driven viewer removal (`POST /api/auth/users/{uid}/delete`) built under
    SEC-5. Building a second, parallel "delete my data" path would just be a second way to do the
    same thing with more surface area to keep correct."""
    uid = _require_uid(request)
    if not uid:
        return JSONResponse({"error": "authentication required"}, status_code=401)
    user = auth.get_user_by_id(uid)
    if not user:
        return JSONResponse({"error": "no such account"}, status_code=404)

    username = user.get("username")
    # Core-only (no decrypted detail — see docstring), filtered to events this account itself
    # generated. query_core has no actor_name filter param, so pull a generous window and filter
    # here — same pattern detections.py's own rules use for anything not natively indexed.
    all_events = audit.query_core(limit=5000)
    own_events = [e for e in all_events if e.get("actor_name") == username]

    bundle = {
        "export_generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "scope_note": "Identity + security-history data for this account only. Shared household "
                       "financial data is not included — see compliance/ROPA.md.",
        "account": auth.public_user(user),
        "security_history": own_events,
    }
    audit.emit("data.exported", request=request, actor=uid, actor_name=username,
               resource="/api/privacy/export", detail={"event_count": len(own_events)})
    return bundle


@app.get("/api/demo/status")
def get_demo_status():
    """Demo harness (2026-07-25) — tells the frontend whether this deployment is a public demo
    instance (so it can show a clear "SYNTHETIC DATA — DEMO INSTANCE" banner) vs. a real one.
    Deliberately public (in _AUTH_PUBLIC_PATHS is NOT needed since the whole point is this is
    read before/alongside login — but it IS behind the normal auth gate today; that's fine, the
    banner only needs to render once logged in, and demo credentials are published anyway)."""
    return {"demo_mode": demo_seed.DEMO_MODE,
            "demo_admin_username": demo_seed.DEMO_ADMIN_USERNAME if demo_seed.DEMO_MODE else None}


@app.post("/api/demo/reset-now")
def demo_reset_now(request: Request):
    """On-demand demo wipe+reseed (2026-07-26) -- the follow-up RESUME-TRACKER.md flagged after
    Jason had to do a manual `docker compose down -v` + `up` dance just to change
    DEMO_ADMIN_PASSWORD (demo_seed.seed_if_needed() only seeds once; there was previously no way to
    force a fresh seed short of destroying the volume). Admin-only (viewer-denied is also already
    the default for any POST not in _VIEWER_ALLOWED_WRITE_PATHS, but this adds the explicit,
    stronger check the same way the household-accounts endpoints do) AND DEMO_MODE-gated --
    demo_seed.reset_and_reseed() itself additionally refuses to run unless DEMO_MODE=1, so this can
    never be used to wipe a real deployment even if somehow called on one."""
    if not demo_seed.DEMO_MODE:
        return JSONResponse({"error": "not a demo instance (DEMO_MODE is not set)"}, status_code=404)
    admin = _require_admin(request)
    if not admin:
        return JSONResponse({"error": "admin only"}, status_code=403)
    result = demo_seed.reset_and_reseed()
    audit.emit("demo.reset", request=request, actor=admin["id"], actor_name=admin["username"],
               resource="/api/demo/reset-now", detail=result)
    return {"ok": bool(result.get("reset")), **result}


COMPLIANCE_DIR = os.environ.get("COMPLIANCE_DIR", "/compliance")

# ---- Risk Register: live, UI-editable data (2026-07-27) ----------------------------------------
# compliance/ is a read-only bind mount locally (docker-compose.yml's `./compliance:/compliance:ro`)
# and baked into the image at build time for the AWS demo (Dockerfile.demo's `COPY compliance/`) --
# there is no writable path back into that directory from a running container either way. So the
# Risk Register's LIVE data lives here instead, in the same writable /data volume as every other
# piece of app state (store.py's *_FILE convention), and compliance/RISK-REGISTER.md's rendered
# markdown is generated fresh from it on every read (_compliance_doc_data above) rather than being
# a static file — the Security Posture doc viewer and the chat get_compliance_doc tool always see
# the current data, and nothing can drift out of sync because there is no second copy to drift.
RISK_REGISTER_FILE = os.environ.get("RISK_REGISTER_FILE", "/data/risk_register.json")
_risk_register_lock = threading.Lock()

# One-time seed (2026-07-27) matching the register as it stood in compliance/RISK-REGISTER.md
# before this file became the live source of truth -- used ONLY to initialize RISK_REGISTER_FILE
# the first time it's read and found missing, never referenced again after that first write.
_RISK_REGISTER_SEED = [
    dict(id="R-1", asset="Financial/PII data (holdings, income, estate docs, chat transcripts)",
         threat="Unauthorized read via a compromised session or stolen backup",
         likelihood="Low", impact="High", inherent="Medium-High",
         treatment="Fernet encryption at rest across all sensitive tables/files (`secrets_crypto.py`); "
                    "Argon2+MFA-gated sessions; deny-by-default access gate",
         residual="Low-Medium", owner="Jason"),
    dict(id="R-2", asset="Master encryption key(s) (Fernet, HMAC audit key)",
         threat="Key file loss or theft (host compromise, bad backup hygiene)",
         likelihood="Low", impact="High", inherent="Medium-High",
         treatment="Key-versioning (SEC-3) limits blast radius of a *future* rotation, but does not "
                    "protect the *current* key file itself; documented \"back this file up "
                    "separately\" guidance",
         residual="Medium (residual risk is real — a stolen current-version key file still "
                   "decrypts current data)", owner="Jason"),
    dict(id="R-3", asset="Security audit log integrity",
         threat="An attacker (or operator mistake) alters or deletes historical events to hide activity",
         likelihood="Low", impact="Medium", inherent="Low-Medium",
         treatment="HMAC hash chain (separate key from Fernet) detects any alteration; "
                    "retention/disposal is itself logged",
         residual="Low", owner="Jason"),
    dict(id="R-4", asset="Account credentials (password, MFA, recovery key)",
         threat="Credential-stuffing / brute-force login attempts",
         likelihood="Medium", impact="Medium", inherent="Medium",
         treatment="Argon2 hashing, TOTP MFA, sliding-window login rate limiting, single-use hashed "
                    "backup codes",
         residual="Low", owner="Jason"),
    dict(id="R-5", asset="Household viewer account",
         threat="A viewer session used to attempt privileged actions (curiosity or compromise)",
         likelihood="Low", impact="Low", inherent="Low",
         treatment="Server-side role assignment (never client-supplied); deny-by-default write "
                    "gate; every denial logged (`authz.denied`)",
         residual="Low", owner="Jason"),
    dict(id="R-6", asset="TLS/transport",
         threat="Downgrade to plaintext HTTP exposing credentials/session cookies in transit",
         likelihood="Low", impact="High", inherent="Medium",
         treatment="In-app TLS provisioning + HSTS when active; explicit `max-age=0` fix so a "
                    "genuine downgrade doesn't leave a browser stuck refusing HTTP either",
         residual="Low", owner="Jason"),
    dict(id="R-7", asset="Host OS / container runtime",
         threat="Compromise of the underlying host (root access)",
         likelihood="Low", impact="High", inherent="Medium-High",
         treatment="Standard OS hardening assumed, containerized service boundaries "
                    "(docker-compose); explicitly documented that the audit hash chain does NOT "
                    "protect against a full root/host compromise (an attacker with both key files "
                    "could rebuild a consistent chain)",
         residual="Medium (real, named, un-mitigated by the app layer — a host-level control gap "
                   "by design)", owner="Jason"),
    dict(id="R-8", asset="Backup / disaster recovery",
         threat="No tested restore path today — a corrupted DB or lost host means real data loss",
         likelihood="Medium", impact="High", inherent="Medium-High",
         treatment="SEC-7a: encrypted backup/restore scripts exist with an evidenced, "
                    "byte-for-byte-verified restore test and documented RPO/RTO (`scripts/"
                    "backup.sh`/`restore.sh`), plus (2026-07-27) an actual scheduler — a systemd "
                    "timer installer for local hosting (`scripts/install-local-backup.sh`) and an "
                    "EventBridge Scheduler rule for the AWS demo (`scripts/aws-demo/"
                    "schedule-backup.sh`) — so backups run automatically instead of relying on "
                    "someone remembering to trigger them",
         residual="Medium-High — down from High now that automation exists, but not yet Low: "
                   "neither installer has been run for real and confirmed live in either "
                   "environment yet, and local backups aren't copied off-host (no off-site copy "
                   "= no protection if the host itself is lost, stolen, or destroyed)",
         owner="Jason"),
    dict(id="R-9", asset="Third-party API credentials (market data, LLM providers, SnapTrade)",
         threat="Leaked API key used for unauthorized calls (cost/abuse, not data breach — these "
                "are outbound-only, read-scoped)",
         likelihood="Low", impact="Low-Medium", inherent="Low",
         treatment="Stored via the same encrypted secrets store as everything else; scoped, "
                    "read-only credentials where the provider supports it",
         residual="Low", owner="Jason"),
    dict(id="R-10", asset="Dependency supply chain (Python/JS packages)",
         threat="A vulnerable or malicious transitive dependency",
         likelihood="Medium", impact="Medium", inherent="Low-Medium",
         treatment="SEC-7b CI dependency scanning is live: `pip-audit` on every push/PR touching "
                    "dependency files + a weekly Monday scan (catches newly-disclosed CVEs against "
                    "unchanged pins), and a Trivy image scan of the actual built container (OS "
                    "packages, not just Python deps) blocking on CRITICAL/HIGH per the 7/30-day "
                    "patch SLA. 6 CRITICAL findings with no vendor fix (essential Debian "
                    "`perl-base`, `zlib1g` — one `will_not_fix`) are formally risk-accepted via "
                    "`.trivyignore` with 90-day `exp:` dates (review due 2026-10-25), not silently "
                    "suppressed — see `dependency-scan.yml` and `.trivyignore` at repo root",
         residual="Low-Medium — real scanning + documented risk acceptance in place; residual risk "
                   "is the inherent gap between an upstream CVE's disclosure and a vendor patch "
                   "actually landing, which no scanning tool can close on its own", owner="Jason"),
    dict(id="R-11", asset="LLM/chat data path",
         threat="Sensitive financial context sent to an external LLM provider is logged/retained "
                "by that provider beyond Fiduciary's control",
         likelihood="Low-Medium", impact="Medium", inherent="Medium",
         treatment="Documented hybrid-LLM path exists (local-first where feasible); no "
                    "per-provider data-retention audit done yet",
         residual="Medium", owner="Jason"),
    dict(id="R-12", asset="Detection engine coverage",
         threat="A real attack pattern falls outside the 3 current detection rules and goes unnoticed",
         likelihood="Medium", impact="Medium", inherent="Medium",
         treatment="3 rules cover the highest-value, most checkable scenarios today (failed-login "
                    "burst, first-seen IP, MFA-disable-then-change); explicitly documented as v1, "
                    "not exhaustive",
         residual="Medium (residual gap is honest, not hidden — new incidents are meant to grow "
                   "this rule set, per the IR runbook's post-incident step)", owner="Jason"),
    dict(id="R-13", asset="Privacy/PII program maturity",
         threat="No formal DSAR/export-delete workflow, privacy notice, or DPIA yet for data that "
                "is, in substance, sensitive financial PII",
         likelihood="Medium", impact="Medium", inherent="Medium",
         treatment="SEC-8 built in full: `PRIVACY-NOTICE.md`, `ROPA.md` (11-row data inventory), "
                    "`DPIA.md`, and a working DSAR access/export flow (`GET /api/privacy/export`, "
                    "scoped to the caller's own identity + security history, verified end-to-end "
                    "via TestClient) — erasure reuses the existing SEC-5 account-deletion path "
                    "rather than a second parallel delete mechanism",
         residual="Low-Medium — strong technical + programmatic controls now both in place; "
                   "residual is mainly that this is a portfolio/demo-grade program, not a "
                   "third-party-audited one", owner="Jason"),
    dict(id="R-14", asset="Demo/portfolio instance itself",
         threat="Publishing a public demo instance widens the attack surface beyond the private "
                "household deployment",
         likelihood="Medium", impact="Low-Medium", inherent="Low-Medium",
         treatment="`DEMO_MODE` built and confirmed live at demo.bentleybuilds.tech: synthetic "
                    "pseudo-data only (no real financial data ever enters this instance), "
                    "isolated/throwaway admin credentials, and a periodic automated reset — plus "
                    "(2026-07-27) a real client-IP-aware audit log, so scanner/bot traffic against "
                    "the public instance (e.g. Shodan) is visible and denied-by-default like "
                    "everything else",
         residual="Low-Medium — the planned isolation is actually built and running, not just "
                   "designed; residual is the inherent reality of any public-internet-facing "
                   "instance being scanned/probed continuously, which DEMO_MODE isolation bounds "
                   "the impact of rather than eliminates", owner="Jason"),
    dict(id="R-15", asset="Document vault contents (wills, trusts, IDs, insurance)",
         threat="Disclosure of stored documents via stolen disk/backup, a viewer reading beyond their "
                "visibility, or a compromised host",
         likelihood="Low", impact="High", inherent="Medium",
         treatment="TASK-001: per-file AES-256-GCM envelope encryption (DEK wrapped by the versioned "
                    "keyring; delete = crypto-shred), encrypted titles/filenames, magic-byte upload "
                    "allowlist, per-item visibility enforced in every vault GET (the global gate lets "
                    "viewers read all GETs) with regression tests, audit events on every upload/"
                    "download/delete, vault inside the encrypted backup set",
         residual="Medium — server-side encryption only: root on the host plus the key file can "
                   "decrypt (disclosed; same residual as R-7)", owner="Jason"),
    dict(id="R-16", asset="Emergency access (break-glass) feature",
         threat="Abuse of the time-delayed grant: a designee or an attacker with the designee's "
                "session obtains access to emergency documents, or the admin role via TAKEOVER, "
                "without the owner's consent",
         likelihood="Low", impact="High", inherent="Medium",
         treatment="TASK-002: ships disabled; designee must be an existing viewer; takeover needs "
                    "designee MFA; 24 h minimum wait with every-channel always-on admin notification "
                    "(push + email, not mutable), admin can deny/revoke at any time; widening changes "
                    "cool down 24 h; atomic once-only takeover; detections for a silent admin and for "
                    "bulk downloads; full audit trail",
         residual="Medium — the control depends on the admin actually being reachable during the wait "
                   "(see the 'Admin reachability' risk)", owner="Jason"),
    dict(id="R-17", asset="Designee account",
         threat="The designated emergency contact's own login is compromised (password reuse, "
                "phished), giving an attacker a path to request access",
         likelihood="Low-Medium", impact="High", inherent="Medium",
         treatment="Designee must be a view-only account; MFA is mandatory for a takeover designee "
                    "and is re-checked on every request and by the scheduled tick (disabling MFA or "
                    "deleting the account auto-disables the feature and cancels the request); "
                    "session-epoch invalidation on password change (TASK-001b); the owner is told of "
                    "every request and can deny it",
         residual="Low-Medium", owner="Jason"),
    dict(id="R-18", asset="Offline Emergency Kit",
         threat="Loss or theft of the kit file or its passphrase, or a stale kit that misses "
                "recently-added documents",
         likelihood="Medium", impact="High", inherent="Medium-High",
         treatment="TASK-002: scrypt + AES-256-GCM chunked container; passphrase generated (>=128 "
                    "bits), shown once and never stored; kit and passphrase are meant to be kept "
                    "apart; single-use 10-minute download link; stale-kit badge and alert after 7 "
                    "days; only `emergency` items are included",
         residual="Medium — physical custody of the passphrase is the household's responsibility "
                   "(Shamir split is a deferred enhancement)", owner="Jason"),
    dict(id="R-19", asset="Admin reachability during the emergency wait",
         threat="The owner is travelling, offline, or has muted/lost their notification channel, so a "
                "request is granted automatically before they can deny it (or, conversely, a real "
                "emergency waits the full delay)",
         likelihood="Medium", impact="Medium", inherent="Medium",
         treatment="TASK-002: enabling requires a working channel (push device or SMTP) and a "
                    "successful test within 24 h; reminders at 50 % and 90 % of the wait; a "
                    "detection alerts when the admin has not signed in 24 h into a request; the wait "
                    "length is the owner's choice (>= 24 h)",
         residual="Medium — accepted trade-off of a time-delay design", owner="Jason"),
    dict(id="R-20", asset="Plaid connection (third-party aggregator)",
         threat="Plaid credentials or per-institution access tokens are stolen from the host, or the "
                "household's financial account data is exposed through the third-party aggregator",
         likelihood="Low", impact="High", inherent="Medium",
         treatment="TASK-005: official API only; balances/holdings/liabilities with the Transactions "
                    "product never requested; secret in the encrypted secrets store; access tokens in the "
                    "encrypted store blob and never returned to the browser, logged or audited; every "
                    "endpoint admin-only; institution logins happen on Plaid's hosted page, never on this "
                    "server; REMOVE calls /item/remove at Plaid before deleting the token; off unless "
                    "PORTFOLIO_PROVIDER=plaid",
         residual="Medium — host compromise yields live tokens (same class as R-7); Plaid is a "
                   "third-party processor holding the institution link", owner="Jason"),
]


def _read_risk_register():
    """Returns {"rows": [...], "next_number": int}. Seeds from _RISK_REGISTER_SEED (one time
    only) if RISK_REGISTER_FILE doesn't exist yet."""
    try:
        reg = store.read_json_strict(RISK_REGISTER_FILE)
        if reg and reg.get("rows"):
            # Additive migration (TASK-002): append any seeded risk whose ASSET this register has never
            # had (the vault/emergency rows arrived after existing installs were seeded). Matches on
            # asset text and takes the next free R-number, so a row the user added by hand can never be
            # collided with or overwritten; existing rows and closures are never touched.
            have = {r.get("asset") for r in reg["rows"]}
            nxt = max(int(reg.get("next_number") or 0),
                      max(int(str(r.get("id", "R-0")).split("-")[1]) for r in reg["rows"]) + 1)
            add = []
            for r in _RISK_REGISTER_SEED:
                if r["asset"] in have:
                    continue
                add.append(dict(r, id=f"R-{nxt}", status="open", closed_reason=None, closed_date=None,
                                created="2026-10-19", updated="2026-10-19"))
                nxt += 1
            if add:
                reg["rows"].extend(add)
                reg["next_number"] = nxt
                store.write_json(RISK_REGISTER_FILE, reg)
            return reg
    except Exception:
        pass
    seeded = {"rows": [dict(r, status="open", closed_reason=None, closed_date=None,
                             created="2026-06-23", updated="2026-07-27")
                       for r in _RISK_REGISTER_SEED],
              "next_number": 21}
    store.write_json(RISK_REGISTER_FILE, seeded)
    return seeded


def _write_risk_register(reg):
    store.write_json(RISK_REGISTER_FILE, reg)


def _render_risk_register_markdown(reg):
    """Renders the SAME document structure/prose the static file used to have, but from live
    data — see the module comment above RISK_REGISTER_FILE for why this can't just be a file on
    disk. Open rows go in the main table; closed/retired rows get their own section underneath so
    the register stays an honest audit trail (RISK-REGISTER-PROCESS.md §3) rather than losing
    history the moment something is resolved."""
    rows = reg.get("rows") or []
    open_rows = [r for r in rows if r.get("status", "open") == "open"]
    closed_rows = [r for r in rows if r.get("status") == "closed"]
    lines = []
    lines.append("# Fiduciary — Risk Register\n")
    lines.append(
        "**Scope:** the `portfolio-api` application and its data volume, self-hosted, "
        "single-household deployment. Written as a portfolio/demo GRC artifact — likelihood/impact "
        "ratings reflect this app's actual scale (one household, one host, no external tenants), "
        "not generic enterprise assumptions. Owner is \"Jason\" throughout since there is one "
        "operator; the column is kept for format-completeness and to make clear risk acceptance is "
        "a deliberate, named act, not a default.\n")
    lines.append(
        "**Rating scale:** Likelihood and Impact are each Low / Medium / High. Inherent risk = the "
        "combination before any treatment; Residual risk = after the mitigations actually in place "
        "today (not after planned/queued ones — a queued mitigation doesn't reduce risk until it's "
        "built).\n")
    lines.append("| # | Asset | Threat | Likelihood | Impact | Inherent | Existing treatment | "
                  "Residual | Owner |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in open_rows:
        lines.append(f"| {r['id']} | {r['asset']} | {r['threat']} | {r['likelihood']} | "
                      f"{r['impact']} | {r['inherent']} | {r['treatment']} | {r['residual']} | "
                      f"{r['owner']} |")
    lines.append("")
    if closed_rows:
        lines.append("## Closed / retired risks\n")
        lines.append("Retained per this register's own process (never deleted — see "
                      "`RISK-REGISTER-PROCESS.md` §3) so the register stays an honest audit trail "
                      "of what was assessed and when, not just current-day concerns.\n")
        lines.append("| # | Asset | Threat | Closed | Reason |")
        lines.append("|---|---|---|---|---|")
        for r in closed_rows:
            lines.append(f"| {r['id']} | {r['asset']} | {r['threat']} | "
                          f"{r.get('closed_date') or ''} | {r.get('closed_reason') or ''} |")
        lines.append("")
    lines.append("---")
    lines.append(
        "*Generated live from the app's own data store (SETTINGS → SECURITY → SECURITY POSTURE → "
        "Risk Register editor, admin-only) — see `RISK-REGISTER-PROCESS.md` for how rows get "
        "added, monitored, and closed. Companion documents: `compliance/STATEMENT-OF-"
        "APPLICABILITY.md` (control-by-control ISO 27001 Annex A status), `compliance/IR-"
        "RUNBOOK.md` (response procedure), `RESUME-TRACKER.md` (build history and the SEC-1..SEC-8 "
        "backlog that treats several of the rows above).*")
    return "\n".join(lines) + "\n"


@app.get("/api/compliance/controls")
def get_compliance_controls():
    """Demo harness / SEC-6 (2026-07-25) — serves compliance/controls.json (the ISO/IEC
    27001:2022 Annex A Statement of Applicability, machine-readable) to back the Security Posture
    page. Reads from a read-only bind mount (COMPLIANCE_DIR, see docker-compose.yml) rather than
    copying the file into the image at build time, so editing compliance/ on the host doesn't
    require a rebuild — same reasoning as the existing ./knowledge mount."""
    path = os.path.join(COMPLIANCE_DIR, "controls.json")
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return JSONResponse({"error": f"controls.json not found at {path} — is the compliance/ "
                                       "folder mounted? (see docker-compose.yml)"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": f"failed to read controls.json: {e}"}, status_code=500)


# 2026-07-26 — Jason: the Security Posture page showed the controls.json-derived table but nothing
# let a visitor actually read the underlying GRC prose docs (risk register, IR runbook, SoA,
# privacy program) that controls.json's "evidence" column points at — the more convincing artifacts
# for an interviewer audience, arguably, since they read like real program documentation rather than
# a checklist. Hardcoded allowlist (not a raw path join) so this can never be tricked into serving an
# arbitrary file from the mount or escaping it via "..".
_COMPLIANCE_DOCS = {
    "RISK-REGISTER.md": "Risk Register",
    "RISK-REGISTER-PROCESS.md": "Risk Register — Maintenance Process",
    "STATEMENT-OF-APPLICABILITY.md": "Statement of Applicability (ISO 27001 Annex A)",
    "DATA-RETENTION-SCHEDULE.md": "Data Retention Schedule",
    "IR-RUNBOOK.md": "Incident Response Runbook",
    "PRIVACY-NOTICE.md": "Privacy Notice",
    "ROPA.md": "Record of Processing Activities (ROPA)",
    "DPIA.md": "Data Protection Impact Assessment (DPIA)",
}


@app.get("/api/compliance/docs")
def list_compliance_docs():
    """Catalog of the servable GRC evidence documents (name + display title), for the Security
    Posture page's "GRC DOCUMENTS" section to build its link list without hardcoding the set
    twice (once here, once in the frontend)."""
    return {"docs": [{"name": k, "title": v} for k, v in _COMPLIANCE_DOCS.items()]}


def _compliance_doc_data(name):
    """Plain-dict core of the compliance-doc read, shared by the GET route below (which wraps
    errors in a JSONResponse) AND the chat get_compliance_doc tool (2026-07-27, see FIDUCIARY_TOOLS
    / _fiduciary_dispatch) which needs a plain dict either way — a tool result isn't an HTTP
    response. RISK-REGISTER.md is special-cased to render LIVE from the app's own writable data
    store (see _read_risk_register/_render_risk_register_markdown) instead of reading a static
    file, because compliance/ is a read-only bind mount (docker-compose.yml's `:ro`) / baked into
    the image at build time (Dockerfile.demo) — there is no on-disk file to write the register's
    edits back into, so the "file" is generated fresh on every read from the live JSON instead.
    Every other doc is still a genuinely static file, read as before."""
    if name not in _COMPLIANCE_DOCS:
        return {"error": "unknown document", "_status": 404}
    if name == "RISK-REGISTER.md":
        return {"name": name, "title": _COMPLIANCE_DOCS[name],
                "content": _render_risk_register_markdown(_read_risk_register())}
    path = os.path.join(COMPLIANCE_DIR, name)
    try:
        with open(path, "r") as f:
            return {"name": name, "title": _COMPLIANCE_DOCS[name], "content": f.read()}
    except FileNotFoundError:
        return {"error": f"{name} not found at {path} — is the compliance/ folder mounted? (see "
                          "docker-compose.yml)", "_status": 404}
    except Exception as e:
        return {"error": f"failed to read {name}: {e}", "_status": 500}


@app.get("/api/compliance/doc")
def get_compliance_doc(name: str):
    """Serves one compliance/*.md file's raw text, allowlisted by exact filename match against
    _COMPLIANCE_DOCS above — never a path join of caller input, so there's no traversal surface
    even though this is read-only. Same COMPLIANCE_DIR bind mount as controls.json above."""
    data = _compliance_doc_data(name)
    if "error" in data:
        return JSONResponse({"error": data["error"]}, status_code=data.get("_status", 500))
    return data


_RISK_REGISTER_REQUIRED_FIELDS = ["asset", "threat", "likelihood", "impact", "inherent",
                                   "treatment", "residual"]
_RISK_REGISTER_RATINGS = {"Low", "Medium", "High", "Low-Medium", "Medium-High"}


@app.get("/api/compliance/risk-register")
def get_risk_register_rows():
    """Structured rows (open + closed) for the Security Posture page's Risk Register editor panel
    — the table-shaped counterpart to /api/compliance/doc?name=RISK-REGISTER.md's rendered
    markdown. A GET, so no RBAC change needed: viewers already have full read access everywhere."""
    return _read_risk_register()


@app.post("/api/compliance/risk-register/row")
async def post_risk_register_row(req: Request):
    """Add a new risk. Admin-only by omission from _VIEWER_ALLOWED_WRITE_PATHS (see that set's own
    comment) — the global _security_and_auth_gate middleware already 403s a non-admin session
    before this handler ever runs, so there is deliberately no second role check duplicated here.
    Assigns the next sequential R-number per RISK-REGISTER-PROCESS.md §1 (never reuses a retired
    one — next_number only ever increments, even across closures)."""
    body = await req.json()
    missing = [f for f in _RISK_REGISTER_REQUIRED_FIELDS if not str(body.get(f) or "").strip()]
    if missing:
        return JSONResponse({"error": f"missing required field(s): {', '.join(missing)}"},
                            status_code=400)
    with _risk_register_lock:
        reg = _read_risk_register()
        rid = f"R-{reg['next_number']}"
        today = datetime.date.today().isoformat()
        row = {"id": rid, "status": "open", "closed_reason": None, "closed_date": None,
               "created": today, "updated": today, "owner": str(body.get("owner") or "Jason").strip()}
        for f in _RISK_REGISTER_REQUIRED_FIELDS:
            row[f] = str(body[f]).strip()
        reg["rows"].append(row)
        reg["next_number"] += 1
        _write_risk_register(reg)
    audit.emit("compliance.risk_register.row_added", request=req, actor=req.session.get("uid"),
               resource="/api/compliance/risk-register/row", detail={"id": rid, "asset": row["asset"]})
    return {"ok": True, "row": row}


@app.put("/api/compliance/risk-register/row/{row_id}")
async def put_risk_register_row(row_id: str, req: Request):
    """Edit an OPEN row's fields (partial update — only keys present in the body change).
    Deliberately rejects editing an already-closed row: RISK-REGISTER-PROCESS.md §3 treats a
    closure as a dated, reasoned historical fact, not something to silently rewrite later — reopen
    is not supported by design; if a closed risk genuinely recurs, it should become a NEW row so
    the closed one's history stays intact."""
    body = await req.json()
    with _risk_register_lock:
        reg = _read_risk_register()
        row = next((r for r in reg["rows"] if r["id"] == row_id), None)
        if not row:
            return JSONResponse({"error": f"no such row: {row_id}"}, status_code=404)
        if row.get("status") == "closed":
            return JSONResponse({"error": f"{row_id} is closed — closed rows are not editable, "
                                          "see RISK-REGISTER-PROCESS.md §3"}, status_code=409)
        editable = _RISK_REGISTER_REQUIRED_FIELDS + ["owner"]
        changed = {}
        for f in editable:
            if f in body and str(body[f]).strip() and str(body[f]).strip() != row.get(f):
                changed[f] = {"from": row.get(f), "to": str(body[f]).strip()}
                row[f] = str(body[f]).strip()
        if changed:
            row["updated"] = datetime.date.today().isoformat()
            _write_risk_register(reg)
    audit.emit("compliance.risk_register.row_updated", request=req, actor=req.session.get("uid"),
               resource=f"/api/compliance/risk-register/row/{row_id}",
               detail={"id": row_id, "changed_fields": list(changed.keys()) if changed else []})
    return {"ok": True, "row": row, "changed_fields": list(changed.keys()) if changed else []}


@app.post("/api/compliance/risk-register/row/{row_id}/close")
async def post_risk_register_row_close(row_id: str, req: Request):
    """Close (retire) a row in place. Body: {reason (required), date? (defaults to today)}. Never
    deletes — the row and its R-number stay in the register permanently under "Closed / retired
    risks" (RISK-REGISTER-PROCESS.md §3), same convention already used for RESUME-TRACKER.md's
    backlog-item closures."""
    body = await req.json()
    reason = str(body.get("reason") or "").strip()
    if not reason:
        return JSONResponse({"error": "reason is required to close a risk"}, status_code=400)
    with _risk_register_lock:
        reg = _read_risk_register()
        row = next((r for r in reg["rows"] if r["id"] == row_id), None)
        if not row:
            return JSONResponse({"error": f"no such row: {row_id}"}, status_code=404)
        if row.get("status") == "closed":
            return JSONResponse({"error": f"{row_id} is already closed"}, status_code=409)
        row["status"] = "closed"
        row["closed_reason"] = reason
        row["closed_date"] = str(body.get("date") or datetime.date.today().isoformat())
        row["updated"] = row["closed_date"]
        _write_risk_register(reg)
    audit.emit("compliance.risk_register.row_closed", request=req, actor=req.session.get("uid"),
               resource=f"/api/compliance/risk-register/row/{row_id}/close",
               detail={"id": row_id, "reason": reason})
    return {"ok": True, "row": row}


# ---- UI-triggered backup/restore (2026-07-27, SEC-7a) -------------------------------------------
# BACKUP_OUTPUT_DIR defaults to a bind-mounted host folder (docker-compose.yml's `./backups:/backups`)
# rather than somewhere under /data -- storing a backup archive on the SAME volume it protects
# defeats the point for a genuine host-loss scenario, only helping against accidental delete/
# corruption. If you also run scripts/install-local-backup.sh (the systemd-timer path), point ITS
# BACKUP_OUTPUT_DIR at the same host path (./backups, i.e. wherever this repo checkout lives) if you
# want one unified backups folder regardless of which trigger made a given file -- not required,
# they work independently either way.
BACKUP_OUTPUT_DIR = os.environ.get("BACKUP_OUTPUT_DIR", "/backups")
BACKUP_STAGING_DIR = os.environ.get("BACKUP_STAGING_DIR", "/backups/.restore-staging")
BACKUP_RETENTION_DAYS = int(os.environ.get("BACKUP_RETENTION_DAYS", "0") or "0")


@app.get("/api/backup/status")
def get_backup_status():
    """Existing backups (newest first) + whether a passphrase is configured yet. A GET, so no RBAC
    change needed -- viewers can see this like anything else read-only."""
    configured = bool(_read_secrets().get("BACKUP_PASSPHRASE"))
    try:
        files = backup.list_backups(BACKUP_OUTPUT_DIR)
    except Exception as e:
        return {"passphrase_configured": configured, "files": [], "error": str(e)}
    return {"passphrase_configured": configured, "files": files, "output_dir": BACKUP_OUTPUT_DIR}


@app.post("/api/backup/run")
def post_backup_run(request: Request):
    """Admin-only by omission from _VIEWER_ALLOWED_WRITE_PATHS, same as the Risk Register mutation
    endpoints above -- the global gate already 403s a non-admin session before this runs. Backs up
    the SAME /data this container itself uses (DATA_DIR-equivalent), so the running app's data
    volume is exactly what gets archived -- no separate mount needed for the source side, unlike
    BACKUP_OUTPUT_DIR above which deliberately IS a separate mount for the output side."""
    pw = _read_secrets().get("BACKUP_PASSPHRASE")
    try:
        result = backup.run_backup(pw, "/data", BACKUP_OUTPUT_DIR, retention_days=BACKUP_RETENTION_DAYS)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        audit.emit("backup.run", outcome="failure", request=request, actor=request.session.get("uid"),
                   resource="/api/backup/run", detail={"error": str(e)})
        return JSONResponse({"error": f"backup failed: {e}"}, status_code=500)
    audit.emit("backup.run", request=request, actor=request.session.get("uid"),
               resource="/api/backup/run",
               detail={"file": result["file"], "size_bytes": result["size_bytes"],
                       "pruned": result["pruned"]})
    return {"ok": True, **result}


@app.post("/api/backup/restore-stage")
async def post_backup_restore_stage(req: Request):
    """Admin-only (same gate). Body: {file: "fiduciary-backup-<ts>.tar.gz.enc"}. Decrypts + extracts
    into BACKUP_STAGING_DIR -- deliberately NOT a live restore over /data; see backup.py's module
    docstring for why. Refuses if the staging dir already has something in it (from a previous,
    not-yet-completed restore) rather than silently mixing two restores together."""
    body = await req.json()
    fname = str(body.get("file") or "").strip()
    if not fname:
        return JSONResponse({"error": "file is required (see GET /api/backup/status for names)"},
                            status_code=400)
    archive_path = os.path.join(BACKUP_OUTPUT_DIR, os.path.basename(fname))  # basename: no path traversal
    pw = _read_secrets().get("BACKUP_PASSPHRASE")
    if not pw:
        return JSONResponse({"error": "no backup passphrase configured (SETTINGS -> SECRETS)"},
                            status_code=400)
    try:
        result = backup.stage_restore(pw, archive_path, BACKUP_STAGING_DIR)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"restore staging failed: {e}"}, status_code=500)
    audit.emit("backup.restore_staged", request=req, actor=req.session.get("uid"),
               resource="/api/backup/restore-stage",
               detail={"file": fname, "file_count": len(result["files"])})
    return {"ok": True, "note": "Files extracted to a STAGING directory, not applied live. To "
                                 "actually restore: stop this container, copy the staged files "
                                 f"from {result['staging_dir']} over your real /data (back up the "
                                 "current /data first!), then start the container back up.",
            **result}


# ============================================================================
#  Serial heavy-job queue + internal scheduler (Increment B)
#  ------------------------------------------------------------------------
#  ONE FIFO worker runs heavy jobs strictly one-at-a-time, so GPU work never
#  overlaps (belt-and-suspenders with the _GPU lock) and there is a single
#  visible registry (GET /api/jobs) of what's queued/running/done. A duplicate
#  submission of a kind already queued/running is COALESCED (deduped), never
#  dropped-silently or run twice. The internal cron scheduler (stdlib only)
#  enqueues the same jobs on a timetable — replacing the 6 n8n cron workflows
#  so the whole schedule->queue->run->visibility loop lives in one process (the
#  external n8n container has been retired). Set INTERNAL_SCHEDULER=0 to disable
#  the scheduler and run every job on demand instead.
# ============================================================================
_JOBS = {}                       # job_id -> record
_JOBS_ORDER = []                 # insertion order (registry ring)
_JOBS_LOCK = threading.Lock()
_JOB_Q = queue.Queue()
_JOB_SEQ = 0
JOBS_MAX = int(os.environ.get("JOBS_MAX", "50"))

# kind -> (label, is_gpu, callable). Lambdas resolve _do_* / _rebalance /
# _retirement at CALL time (defined later in the file) so forward refs are fine.
_JOB_KINDS = {
    "analysis":      ("Daily analysis (briefs)",   True,  lambda: _do_analysis(0)),
    "discovery":     ("News discovery & alerts",   True,  lambda: _do_discovery()),
    "opportunities": ("Opportunity scan",          True,  lambda: _do_opportunities()),
    "strategist":    ("Strategist review",         True,  lambda: _do_strategist()),
    "rebalance":     ("Rebalance / drift refresh", False, lambda: _rebalance()),
    "retirement":    ("Retirement projection",     False, lambda: _retirement()),
    "backtest":      ("Decision backtest vs SPY",  False, lambda: _do_backtest()),
    "narrative":     ("State-of-the-Wealth brief", True,  lambda: _do_narrative()),
    "knowledge_reindex": ("Knowledge reindex",     True,  lambda: knowledge.ensure_index(force=True)),
    "earnings_brief": ("Post-earnings brief refresh", True, lambda: _do_earnings_brief()),
}

def _enqueue(kind, source="manual"):
    """Add a job unless one of the same kind is already queued/running (dedup).
    Returns (job_id, created:bool)."""
    spec = _JOB_KINDS.get(kind)
    if not spec:
        return None, False
    global _JOB_SEQ
    with _JOBS_LOCK:
        for j in _JOBS.values():
            if j["kind"] == kind and j["status"] in ("queued", "running"):
                return j["id"], False          # coalesce a duplicate
        _JOB_SEQ += 1
        jid = f"{kind[:4]}-{_JOB_SEQ}"
        _JOBS[jid] = {"id": jid, "kind": kind, "label": spec[0], "gpu": spec[1],
                      "status": "queued", "source": source,
                      "submitted": int(time.time()), "started": None,
                      "finished": None, "error": None}
        _JOBS_ORDER.append(jid)
        while len(_JOBS_ORDER) > JOBS_MAX:      # trim the oldest record
            _JOBS.pop(_JOBS_ORDER.pop(0), None)
    _JOB_Q.put(jid)
    return jid, True

def _job_worker():
    while True:
        jid = _JOB_Q.get()
        with _JOBS_LOCK:
            rec = _JOBS.get(jid)
        if not rec:
            continue
        rec["status"] = "running"; rec["started"] = int(time.time())
        fn = _JOB_KINDS[rec["kind"]][2]
        try:
            fn()
            rec["status"] = "done"
        except Exception as e:
            rec["status"] = "error"; rec["error"] = str(e)[:300]
        finally:
            rec["finished"] = int(time.time())

threading.Thread(target=_job_worker, daemon=True, name="job-worker").start()


def _job_typical_seconds(kind, jobs, exclude_id=None, n=5):
    """Median duration of the last N 'done' runs of this kind (excluding the in-flight job
    itself), from the SAME in-memory `_JOBS` history /api/jobs already carries — no new data
    source, no per-kind instrumentation. Returns None until at least one prior run exists
    (a job's very first run has nothing to compare against yet). RESUME-TRACKER item 5's
    cheapest option ("elapsed-time-vs-typical-duration as a rough ETA"), generalized across
    every job kind at once rather than the bespoke per-kind approach knowledge_reindex uses."""
    durs = sorted(
        (j["finished"] - j["started"]) for j in jobs
        if j["kind"] == kind and j["id"] != exclude_id and j["status"] == "done"
        and j.get("started") and j.get("finished")
    )
    if not durs:
        return None
    durs = durs[-n:]
    mid = len(durs) // 2
    return durs[mid] if len(durs) % 2 else (durs[mid - 1] + durs[mid]) / 2.0


def _fmt_secs(s):
    s = max(0, int(round(s)))
    m, r = divmod(s, 60)
    return f"{m}m {r}s" if m else f"{r}s"


def _gpu_wait_message():
    """Describe what's currently holding _GPU, for the `status` SSE event /api/chat yields while
    polling for it (RESUME-TRACKER 2026-07-18l bug #2). By the time _chat_stream reaches its GPU
    wait it already holds `_strat_lock` itself (see that gate earlier in the function), so the only
    other thing that can be holding `_GPU` is a job-queue run (analysis/discovery/narrative/etc,
    gated by `_gpu_slot()` directly, not `_strat_lock`) — reads the SAME in-memory `_JOBS` history
    `/api/jobs` already exposes, no new data source. Falls back to a generic message if `_GPU` is
    locked but nothing shows as `running` (a narrow race right at a job's start/finish)."""
    with _JOBS_LOCK:
        jobs = [dict(_JOBS[j]) for j in _JOBS_ORDER if j in _JOBS]
    running_job = next((j for j in jobs if j["status"] == "running"), None)
    if not running_job:
        return "the model is busy with another request — your message is queued and will start automatically"
    label = running_job.get("label") or running_job["kind"]
    elapsed = int(time.time()) - (running_job.get("started") or int(time.time()))
    typical = _job_typical_seconds(running_job["kind"], jobs, exclude_id=running_job["id"])
    eta = f"~{_fmt_secs(typical)} typical" if typical else "no history yet"
    return f'"{label}" is using the model ({_fmt_secs(elapsed)} so far, {eta}) — your message is queued and will start automatically when it finishes'


@app.get("/api/jobs")
def get_jobs():
    """Serial job-queue registry (Increment B): queued/running/done in order + current
    depth. A UI badge can poll this; pairs with /api/gpu.
    `strategist_busy` reflects _strat_lock (chat / weekly strategist / advisory runs) — those share
    a lock but DON'T go through this FIFO, so without this flag a live chat generation (or a weekly/
    advisory run) could hold the GPU with the badge showing blank. Cheap: in-memory only.

    `progress` on the RUNNING job (RESUME-TRACKER item 5): elapsed seconds vs. the median of
    its last 5 completed runs, plus a rough percent. None/None until history exists (fresh
    install, or a job kind's very first run) or the kind has no completed run yet — shown
    as "no history yet" rather than a fake 0%. This is a RUNTIME signal only; it does not
    diagnose WHY a run is slow (see item 6) — it just makes "this is taking way longer than
    usual" visible instead of an opaque spinner."""
    with _JOBS_LOCK:
        jobs = [dict(_JOBS[j]) for j in _JOBS_ORDER if j in _JOBS]
    depth = sum(1 for j in jobs if j["status"] == "queued")
    running_job = next((j for j in jobs if j["status"] == "running"), None)
    running = running_job["kind"] if running_job else None
    progress = None
    if running_job and running_job.get("started"):
        typical = _job_typical_seconds(running_job["kind"], jobs, exclude_id=running_job["id"])
        elapsed = int(time.time()) - running_job["started"]
        progress = {"kind": running_job["kind"], "elapsed_seconds": elapsed,
                    "typical_seconds": (round(typical) if typical is not None else None),
                    "pct_of_typical": (round(elapsed / typical * 100) if typical else None),
                    "over_typical": bool(typical and elapsed > typical * 1.5)}
    return {"queued": depth, "running": running, "progress": progress,
            "scheduler": INTERNAL_SCHEDULER,
            "jobs": list(reversed(jobs)),      # newest first
            "strategist_busy": _strat_lock.locked()}


# ---- internal cron scheduler (stdlib) — the built-in job timetable ----------
# Container-local time: set TZ=America/Chicago on portfolio-api (compose) so jobs
# fire in your local time. dow uses Python weekday(): Mon=0..Sun=6. Per-job hour/
# minute are user-overridable in the dashboard (Settings -> Task Schedule); the
# cadence (dom/dow) is structural. Overrides persist to SCHEDULE_FILE and are
# re-read every minute, so edits take effect without a restart.
INTERNAL_SCHEDULER = os.environ.get("INTERNAL_SCHEDULER", "1").lower() in ("1", "true", "yes", "on")
SCHEDULE_FILE = os.environ.get("SCHEDULE_FILE", "/data/schedule.json")
_SCHEDULE_ORDER = ["analysis", "discovery", "opportunities", "rebalance", "retirement", "strategist"]
_SCHEDULE_DEFAULTS = {
    "analysis":      {"label": "Daily analysis (B/S/H briefs)", "hour": 21, "minute": 0, "dom": "*", "dow": "*", "cadence": "daily"},
    "discovery":     {"label": "News discovery & alerts",       "hour":  7, "minute": 0, "dom": "*", "dow": "*", "cadence": "daily"},
    "opportunities": {"label": "Opportunity scan",              "hour": 20, "minute": 0, "dom": "*", "dow": "5", "cadence": "weekly · Saturday"},
    "rebalance":     {"label": "Rebalance / drift refresh",     "hour": 22, "minute": 0, "dom": "*", "dow": "6", "cadence": "weekly · Sunday"},
    "retirement":    {"label": "Retirement projection",         "hour":  8, "minute": 0, "dom":  1,  "dow": "*", "cadence": "monthly · 1st"},
    "strategist":    {"label": "Strategist review",             "hour": 19, "minute": 0, "dom": "*", "dow": "4", "cadence": "weekly · Friday"},
    "backtest":      {"label": "Decision backtest vs SPY",      "hour": 20, "minute": 0, "dom": "*", "dow": "6", "cadence": "weekly · Sunday"},
    "narrative":     {"label": "State-of-the-Wealth brief",     "hour":  7, "minute": 30, "dom": "*", "dow": "0", "cadence": "weekly · Monday"},
}
def _read_schedule():
    """Defaults with saved hour/minute overrides merged on top (validated 0-23 / 0-59)."""
    over = {}
    try:
        over = store.read_json_strict(SCHEDULE_FILE) or {}
    except Exception:
        over = {}
    out = {}
    for k, d in _SCHEDULE_DEFAULTS.items():
        o = over.get(k) or {}
        hh, mm = o.get("hour"), o.get("minute")
        out[k] = {**d,
                  "hour":   int(hh) if isinstance(hh, (int, float)) and 0 <= hh <= 23 else d["hour"],
                  "minute": int(mm) if isinstance(mm, (int, float)) and 0 <= mm <= 59 else d["minute"]}
    return out
def _sched_match(field, val):
    return field == "*" or int(field) == val
def _scheduler_loop():
    last = None
    while True:
        now = datetime.datetime.now()
        stamp = now.strftime("%Y%m%d%H%M")     # fire each due schedule once per minute
        if stamp != last:
            last = stamp
            for kind, s in _read_schedule().items():   # re-read each minute so edits apply live
                if (now.minute == s["minute"] and now.hour == s["hour"]
                        and _sched_match(s["dom"], now.day)
                        and _sched_match(s["dow"], now.weekday())):
                    _enqueue(kind, source="scheduler")
            # OPP-6 earnings-triggered briefs: event-driven, not a fixed time-of-day a user would
            # edit in TASK SCHEDULE (there's no "time" to set — either a print is out or it isn't),
            # so it's checked once daily at a fixed anchor rather than added to _SCHEDULE_DEFAULTS.
            # Only enqueues when something's actually due, unlike the generic loop above.
            if now.hour == 8 and now.minute == 15:
                try:
                    if _earnings_due_tickers():
                        _enqueue("earnings_brief", source="scheduler")
                except Exception:
                    pass
        time.sleep(20)
if INTERNAL_SCHEDULER:
    threading.Thread(target=_scheduler_loop, daemon=True, name="scheduler").start()


# 2026-08-08: shout about a dead EDGAR entry point at BOOT rather than letting it present as 28
# securities that are all coincidentally "not SEC-registered". A misconfigured SEC_USER_AGENT took
# out fundamentals, insider signals, 8-K events and fund look-through for weeks without one line
# of evidence anywhere. Warn only -- never block startup over a single degraded data source.
def _warn_if_edgar_misconfigured():
    try:
        if edgar.ua_is_placeholder():
            print("[startup] WARNING: SEC_USER_AGENT is blank or a placeholder "
                  f"({edgar.SEC_UA!r}). SEC returns 403 for generic User-Agents, which disables "
                  "ALL EDGAR data (fundamentals, Form 4 insider signals, 8-K events, NPORT fund "
                  "look-through). Set it in .env to a real contact string. See /api/edgar-status.",
                  flush=True)
    except Exception:
        pass


threading.Thread(target=_warn_if_edgar_misconfigured, daemon=True, name="edgar-cfg-warn").start()


@app.get("/api/schedule")
def get_schedule():
    """The built-in job timetable: each automated job with its (overridable) 24h time,
    cadence, and whether it's still on the default. Cheap: reads one small file."""
    sched = _read_schedule()
    jobs = []
    for k in _SCHEDULE_ORDER:
        s, dflt = sched[k], _SCHEDULE_DEFAULTS[k]
        jobs.append({"kind": k, "label": s["label"], "cadence": s["cadence"],
                     "hour": s["hour"], "minute": s["minute"],
                     "default_hour": dflt["hour"], "default_minute": dflt["minute"],
                     "is_default": (s["hour"] == dflt["hour"] and s["minute"] == dflt["minute"])})
    return {"scheduler_enabled": INTERNAL_SCHEDULER, "tz": os.environ.get("TZ", ""), "jobs": jobs}


@app.post("/api/schedule")
async def post_schedule(req: Request):
    """Override a job's fire time (24h). Body: {"<kind>": {"hour": H, "minute": M}, ...}
    or {"reset": "<kind>"} to restore a job's default. Config only — no money action."""
    body = await req.json()
    # Independent read for the audit diff, captured BEFORE any mutation. Deliberately NOT a
    # shallow dict(cur) copy taken after this point — the mutation loop below does
    # `entry = cur.get(k, {})` then mutates `entry` IN PLACE (no reassignment until `cur[k] =
    # entry`), so a shallow copy of `cur` would share that same nested dict object and silently
    # show no diff. A second, fully independent JSON read avoids this regardless of how the
    # mutation logic below is written.
    try:
        old = store.read_json_strict(SCHEDULE_FILE) or {}
    except Exception:
        old = {}
    try:
        cur = store.read_json_strict(SCHEDULE_FILE) or {}
    except Exception:
        cur = {}
    if isinstance(body, dict) and body.get("reset") in _SCHEDULE_DEFAULTS:
        cur.pop(body["reset"], None)
    else:
        for k, v in (body or {}).items():
            if k not in _SCHEDULE_DEFAULTS or not isinstance(v, dict):
                continue
            entry = cur.get(k, {})
            try:
                if "hour" in v and 0 <= int(v["hour"]) <= 23:
                    entry["hour"] = int(v["hour"])
            except (TypeError, ValueError):
                pass
            try:
                if "minute" in v and 0 <= int(v["minute"]) <= 59:
                    entry["minute"] = int(v["minute"])
            except (TypeError, ValueError):
                pass
            cur[k] = entry
    store.write_json(SCHEDULE_FILE, cur)
    _emit_config_changed(req, "/api/schedule", old, cur)
    return {"ok": True, "schedule": get_schedule()}


# ---------------- mcpo / Monarch ----------------
# ---- Monarch is OPTIONAL, UNOFFICIAL and OFF by default (TASK-003, 2026-10-19) ----
# Monarch has no public API; mcpo talks to its private GraphQL endpoint, and Monarch's terms
# restrict automated access. So every Monarch call goes through _post(), which refuses unless
# (a) PORTFOLIO_PROVIDER=monarch AND (b) an admin recorded the risk acknowledgment below. The
# ack lives in settings.json under MONARCH_RISK_ACK ({at, by, version}); enabling/disabling is
# audited (integration.monarch.enabled / .disabled). After any upstream failure we back off for
# _MONARCH_COOLDOWN_S instead of retrying, to avoid load on their service.
MONARCH_LABEL = "Monarch (unofficial, at your own risk)"
MONARCH_ACK_VERSION = 1
MONARCH_ACK_TEXT = (
    "Monarch has no official public API. This integration uses an unofficial, private API, and "
    "Monarch's terms restrict automated access. I accept the risk: it may break without notice, "
    "and my Monarch account may be restricted or closed."
)
_MONARCH_COOLDOWN_S = 300
_monarch_state = {"fail_ts": 0.0, "err": ""}


class MonarchDisabled(RuntimeError):
    """Monarch not enabled (not selected, or the risk acknowledgment is missing/cooling down)."""


def _monarch_ack():
    a = _read_settings().get("MONARCH_RISK_ACK")
    return a if isinstance(a, dict) and a.get("at") else None


def _monarch_enabled():
    return os.environ.get("PORTFOLIO_PROVIDER", "csv").lower() == "monarch" and _monarch_ack() is not None


def _monarch_guard():
    if os.environ.get("PORTFOLIO_PROVIDER", "csv").lower() != "monarch":
        raise MonarchDisabled("Monarch is not the selected portfolio data source.")
    if _monarch_ack() is None:
        raise MonarchDisabled(MONARCH_LABEL + " is selected but not enabled: an admin must accept the "
                              "risk acknowledgment in SETTINGS -> ACCOUNTS first.")
    wait = _MONARCH_COOLDOWN_S - (time.time() - _monarch_state["fail_ts"])
    if wait > 0:
        raise MonarchDisabled(f"Monarch failed recently ({_monarch_state['err']}); not retrying for "
                              f"{int(wait)}s to avoid load on their service.")


def _post(op, payload):
    _monarch_guard()
    url = f"{MCPO}/{ROUTE}/{op}"
    headers = {"Content-Type": "application/json"}
    if KEY:
        headers["Authorization"] = f"Bearer {KEY}"
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        _monarch_state["fail_ts"] = time.time()
        _monarch_state["err"] = f"{type(e).__name__}"
        raise


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


_CIT_NAME_RE = re.compile(
    r'\b(collective (investment )?trust|common trust fund|commingled (investment )?fund|\bCIT\b)\b',
    re.I)

# Known ticker overrides (2026-07-23) — Jason confirmed via Monarch's OWN web UI that it displays
# the correct ticker for these holdings (e.g. "SRFM" for Surf Air Mobility, pulled from a Wells
# Fargo/Finicity-sourced account); the gap is specifically in what Monarch's API exposes to this
# app for these securities (ticker/CUSIP both come back blank), not in Monarch's own knowledge of
# the security. Without this, `_hold()`'s existing `ticker or name` fallback substitutes the
# security NAME as a fake ticker — which both mis-triggers `_is_cit()` (a real, publicly-traded
# stock gets flagged as a non-traded CIT) AND silently breaks every other ticker-keyed lookup for
# that holding (SEC/EDGAR fundamentals, Finnhub quotes, Tavily news, Buy/Sell briefs) since none of
# those accept a company name string in place of a real ticker. Keyed by the exact security name as
# Monarch reports it (matched case-insensitively) — add an entry here whenever a real holding is
# mis-flagged or fails downstream analytics because its ticker/CUSIP came back blank from the feed.
KNOWN_TICKER_OVERRIDES = {
    "SURF AIR MOBILITY INC": "SRFM",
    "DRAGANFLY INC F": "DPRO",
}


def _resolve_ticker(s):
    """Real ticker if Monarch supplied one; else a known manual override by exact (case-
    insensitive) security name; else the bare name as a last-resort display fallback (pre-existing
    behavior). Used by BOTH `_hold()` (so overridden holdings flow the real ticker through every
    downstream lookup) and `_is_cit()` (so an override counts as "has a real ticker" and isn't
    flagged CIT)."""
    ticker = (s.get("ticker") or "").strip()
    if ticker:
        return ticker
    name = (s.get("name") or "").strip()
    return KNOWN_TICKER_OVERRIDES.get(name.upper()) or name or "?"


def _is_cit(s):
    """Heuristic Collective Investment Trust / non-traded-fund detector (RESUME-TRACKER
    2026-07-18k). A CIT (bank/trust-company-managed pooled vehicle for tax-exempt retirement
    plans — e.g. T. Rowe Price Retirement series, PGIM Jennison CIT share classes) has no real
    ticker/CUSIP; Monarch tracks it only via the recordkeeper's internal platform ID. `_hold()`'s
    existing fallback (`ticker or name`) quietly substitutes the fund NAME as a fake ticker,
    which is exactly what let a CIT slip into the stock-analytics pipeline (Buy/Sell briefs,
    EDGAR CIK lookups, Tavily news search) undetected until this fix.
    Primary signal (per the fix plan): no real ticker AND no CUSIP. CUSIP is checked via .get()
    in case Monarch's payload exposes one — unconfirmed as of this build (would need a live
    raw-holdings dump to verify); if absent it simply contributes nothing, degrading safely to
    the ticker-only check. `type == 'Cash'` is excluded (also lacks a ticker, but a different,
    already-understood case — out of scope here). Name-pattern match on "Collective (Investment)
    Trust" / "CIT" / "commingled" / "common trust fund" is a SUPPLEMENT (per the fix plan, NOT the
    primary signal — a REIT with "Trust" in its legal name that DOES have a real ticker must not
    be flagged), catching an explicitly-named CIT even in the case Monarch surfaces some other
    platform-style string in the ticker field."""
    ticker = (s.get("ticker") or "").strip()
    cusip = (s.get("cusip") or s.get("CUSIP") or "").strip()
    name = (s.get("name") or "").strip()
    typ = s.get("typeDisplay") or s.get("type") or ""
    if typ == "Cash":
        return False
    overridden = bool(name) and name.upper() in KNOWN_TICKER_OVERRIDES
    no_real_id = not ticker and not cusip and not overridden
    name_hit = bool(_CIT_NAME_RE.search(name)) if name else False
    return no_real_id or name_hit


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
        "ticker": _resolve_ticker(s),
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
        "is_cit": _is_cit(s),
    }


def _derive(accounts, positions, by_account, inv, net_worth, total_assets, total_liabilities,
            balance_only=None):
    """Source-agnostic derivation: given normalized accounts + positions (from ANY provider),
    compute the full dashboard payload (consolidation, weights, aggregations, movers).
    `balance_only` (optional): investment accounts with a balance but no line-item holdings — folded
    into the institution/type allocation and surfaced as accounts, but kept out of `positions` so
    per-security analysis is unaffected. See _build_monarch for how they're identified."""
    balance_only = balance_only or []
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
                                  "accounts": [], "suspect": False, "is_cit": False})
        c["quantity"] = round(c["quantity"] + (p["quantity"] or 0), 4)
        c["value"] = round(c["value"] + (p["value"] or 0), 2)
        if p.get("suspect") or p["cost_total"] is None:
            c["suspect"] = True   # any unreliable leg -> the merged cost is incomplete
        else:
            c["cost_total"] = round(c["cost_total"] + p["cost_total"], 2)
        if p.get("is_cit"):
            c["is_cit"] = True   # any leg flagged CIT -> treat the consolidated row as CIT (RESUME-TRACKER 2026-07-18k)
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

    # Institution allocation with balance-only investment accounts folded in, so a 401(k) (or
    # similar) that syncs only a balance still appears in the institution donut (2026-07-22).
    # `allocation_total` = detailed positions + those balances = the institution donut's denominator.
    # NOTE: by_type is deliberately LEFT positions-only — asset type is unknown for a holdings-less
    # balance, and /api/rebalance derives policy-drift-by-type from by_type, so folding the balance
    # in there would corrupt the drift math. The asset-type donut therefore stays on invested_value.
    bo_total = round(sum(a.get("balance") or 0 for a in balance_only), 2)
    allocation_total = round(invested + bo_total, 2)
    inst_alloc = {}
    for p in positions:
        k = p["institution"] or "?"
        inst_alloc[k] = round(inst_alloc.get(k, 0) + (p["value"] or 0), 2)
    for a in balance_only:
        k = a.get("institution") or "?"
        inst_alloc[k] = round(inst_alloc.get(k, 0) + (a.get("balance") or 0), 2)
    by_institution = [{"label": k, "value": v} for k, v in sorted(inst_alloc.items(), key=lambda x: -x[1])]
    by_type = agg(lambda p: p["type"] or "Other")
    investment_balance_accounts = [
        {"account": a.get("name"), "institution": a.get("institution"),
         "value": a.get("balance"), "type": a.get("type")}
        for a in sorted(balance_only, key=lambda a: -(a.get("balance") or 0))]

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
        "by_institution": by_institution,
        "by_type": by_type,
        "allocation_total": allocation_total,
        "investment_balance_accounts": investment_balance_accounts,
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
            "include_nw": a.get("includeBalanceInNetWorth"),
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

    # Balance-only investment accounts (2026-07-22): tax-advantaged / brokerage accounts whose
    # provider syncs a BALANCE but no line-item holdings (e.g. some 401(k)s — confirmed with a
    # an employer 401(k) that landed under "Other Assets"). They have holdings_count==0, so they
    # never enter positions/by_account and were shown only as balance-only "Other Assets", missing
    # from the ACCOUNTS list and the institution donut. Surface them as real investment accounts
    # (attributed to their institution) WITHOUT injecting a fake security into positions, so
    # securities analysis (exposure/risk/tax-lots/rebalance) stays clean. _account_tax_status != "na"
    # is the "this is an investment account" signal (401k/IRA/Roth/HSA/brokerage), excluding cash,
    # the home, and loans, which correctly remain under Other Assets / Liabilities.
    balance_only = [a for a in accounts
                    if (a.get("holdings_count") or 0) == 0
                    and a.get("include_nw")
                    and a.get("is_asset") is not False
                    and round(a.get("balance") or 0) != 0
                    and _account_tax_status(a.get("name"), a.get("type")) != "na"]
    return _derive(accounts, positions, by_account, inv, net_worth, total_assets,
                   total_liabilities, balance_only=balance_only)


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


def _plaid_creds():
    """(client_id, secret, env) -- read fresh each call so a saved secret applies without a restart.
    .env still wins if someone set PLAID_CLIENT_ID/PLAID_SECRET there."""
    st = _read_secrets()
    cid = os.environ.get("PLAID_CLIENT_ID") or st.get("PLAID_CLIENT_ID")
    sec = os.environ.get("PLAID_SECRET") or st.get("PLAID_SECRET")
    env = os.environ.get("PLAID_ENV") or _read_settings().get("PLAID_ENV") or "sandbox"
    return cid, sec, env


plaid_integration.configure(_plaid_creds)


def _build_plaid():
    """Portfolio via Plaid (PORTFOLIO_PROVIDER=plaid): balances, holdings and liabilities from every
    connected Item (NO transactions). Item-level failures are recorded on the Item, not fatal."""
    m = plaid_integration.build_portfolio()
    d = _derive(m["accounts"], m["positions"], m["by_account"], m["inv"], m["net_worth"],
                m["total_assets"], m["total_liabilities"], balance_only=m["balance_only"])
    d["plaid_item_errors"] = m.get("item_errors") or []
    return d


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
    """Dispatch to the configured portfolio provider (PORTFOLIO_PROVIDER=monarch|csv|snaptrade).

    2026-07-26: wrapped in try/except to feed notifications.record_integration_result() -- the
    "communication outage" half of the notifications work. csv is deliberately excluded (a local
    file has no "outage" to detect); snaptrade/monarch failures accumulate toward the
    integration_outage detection.alert (notifications.py), which only ever fires for the provider
    actually selected here, so a household that's never touched SnapTrade never generates a
    SnapTrade alert in the first place."""
    p = os.environ.get("PORTFOLIO_PROVIDER", "csv").lower()
    if p == "monarch" and _monarch_ack() is None:
        _monarch_guard()   # raises a clear "not enabled" error; not an outage, so no notification
    try:
        if p == "snaptrade":
            d = _build_snaptrade()
        elif p == "monarch":
            d = _build_monarch()
        elif p == "plaid":
            d = _build_plaid()
        else:
            p = "csv"
            d = _build_csv()
    except Exception as e:
        try:
            notifications.record_integration_result(p, ok=False, error=e)
        except Exception:
            pass
        raise
    try:
        notifications.record_integration_result(p, ok=True)
    except Exception:
        pass
    d = _apply_manual_liabilities(d)   # 2026-07-30 -- overlay hand-entered loans/credit cards
                                       # (see the "Manual liability accounts" section below)
    d["portfolio_provider"] = p   # 2026-07-24 -- lets the frontend know which provider is active
                                  # (e.g. to badge the HOLDINGS panel as live vs. staged-but-inactive)
    _snapshot_value(d)          # record today's value for the accruing risk-metrics history
    _snapshot_shares(d)         # record today's per-ticker shares for the wash-sale ledger (GAP-4)
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


# ============ CSV holdings management (2026-07-24) ============
# Previously PORTFOLIO_PROVIDER=csv users had NO in-app way to set holdings.csv at all -- it had
# to be hand-placed on the host filesystem before this app existed for them at all, a real gap
# for the "no brokerage account needed" onboarding path INSTALL.md advertises. These endpoints
# read/write the exact same file _build_csv() consumes (HOLDINGS_CSV, default /data/holdings.csv)
# -- there is no separate storage path to keep in sync.
HOLDINGS_CSV_COLUMNS = ["ticker", "name", "quantity", "cost_basis", "account", "institution", "type"]
HOLDINGS_TYPE_HINTS = ["Stock", "ETF", "Mutual Fund", "Bond", "Cash", "Real Estate", "Other"]


def _holdings_csv_path():
    return os.environ.get("HOLDINGS_CSV", "/data/holdings.csv")


def _read_holdings_csv_rows():
    """Raw saved rows (no live pricing) for the editable table -- distinct from _build_csv(),
    which additionally fetches live Finnhub quotes for the dashboard itself."""
    import csv as _csv
    try:
        with open(_holdings_csv_path(), newline="", encoding="utf-8-sig") as f:
            rows = list(_csv.DictReader(f))
    except FileNotFoundError:
        return []
    return [{
        "ticker": (r.get("ticker") or "").strip().upper(),
        "name": (r.get("name") or "").strip(),
        "quantity": (r.get("quantity") or "").strip(),
        "cost_basis": (r.get("cost_basis") or "").strip(),
        "account": (r.get("account") or "").strip(),
        "institution": (r.get("institution") or "").strip(),
        "type": (r.get("type") or "").strip(),
    } for r in rows]


def _validate_holdings_rows(rows):
    """Returns (clean_rows, errors). errors flags exactly what _build_csv() would otherwise
    silently drop from the portfolio with zero explanation (blank ticker, non-numeric/zero/
    negative quantity) or what would silently corrupt cost-basis math (non-numeric cost_basis) --
    all-or-nothing on the write side (see post_holdings_csv) so a typo in one row can't quietly
    vanish a position instead of failing loudly."""
    clean, errors = [], []
    for i, r in enumerate(rows, start=1):
        tkr = (r.get("ticker") or "").strip().upper()
        if not tkr:
            errors.append({"row": i, "message": "Ticker is required."})
            continue
        try:
            qty = float(r.get("quantity") or 0)
        except (TypeError, ValueError):
            errors.append({"row": i, "message": f"{tkr}: quantity must be a number."})
            continue
        if qty <= 0:
            errors.append({"row": i, "message": f"{tkr}: quantity must be greater than 0."})
            continue
        cost_raw = r.get("cost_basis")
        cost = None
        if cost_raw not in (None, ""):
            try:
                cost = float(cost_raw)
            except (TypeError, ValueError):
                errors.append({"row": i, "message": f"{tkr}: cost basis must be a number."})
                continue
            if cost < 0:
                errors.append({"row": i, "message": f"{tkr}: cost basis can't be negative."})
                continue
        clean.append({
            "ticker": tkr,
            "name": (r.get("name") or "").strip(),
            "quantity": qty,
            "cost_basis": cost if cost is not None else "",
            "account": (r.get("account") or "").strip() or "Brokerage",
            "institution": (r.get("institution") or "").strip(),
            "type": (r.get("type") or "").strip() or "Stock",
        })
    return clean, errors


def _write_holdings_csv_rows(clean_rows):
    import csv as _csv, io as _io
    buf = _io.StringIO()
    w = _csv.DictWriter(buf, fieldnames=HOLDINGS_CSV_COLUMNS)
    w.writeheader()
    for r in clean_rows:
        w.writerow(r)
    store.atomic_write_text(_holdings_csv_path(), buf.getvalue())
    _cache["data"] = None   # force the next /api/portfolio read to re-parse the file instead of
                            # serving a stale cached build for up to TTL seconds


@app.get("/api/holdings-csv")
def get_holdings_csv():
    """Current CSV-provider holdings for the in-app HOLDINGS editor. Available regardless of the
    ACTIVE PORTFOLIO_PROVIDER, so a user can populate holdings.csv before switching to it (SETTINGS
    -> Portfolio provider)."""
    return {"rows": _read_holdings_csv_rows(), "path": _holdings_csv_path(),
            "active_provider": os.environ.get("PORTFOLIO_PROVIDER", "csv").lower(),
            "type_hints": HOLDINGS_TYPE_HINTS}


@app.post("/api/holdings-csv")
async def post_holdings_csv(req: Request):
    """Replace holdings.csv wholesale from the in-app table editor. Body: {"rows": [...]}.
    All-or-nothing: if ANY row fails validation, NOTHING is written and every error comes back
    together -- a save that fails loudly with a clear reason beats one that silently drops a row."""
    body = await req.json()
    rows = body.get("rows")
    if not isinstance(rows, list):
        return JSONResponse({"error": "Body must be {\"rows\": [...]}."}, status_code=400)
    clean, errors = _validate_holdings_rows(rows)
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)
    _write_holdings_csv_rows(clean)
    return {"ok": True, "rows_written": len(clean)}


@app.post("/api/holdings-csv/upload")
async def post_holdings_csv_upload(req: Request):
    """Wholesale replace from an uploaded .csv file. The frontend reads the file client-side with
    FileReader and POSTs the raw text as the request body -- deliberately avoiding multipart/
    form-data so this doesn't need the python-multipart dependency just for one endpoint."""
    import csv as _csv, io as _io
    raw = (await req.body()).decode("utf-8-sig", errors="replace")
    if not raw.strip():
        return JSONResponse({"error": "Uploaded file is empty."}, status_code=400)
    try:
        rows = list(_csv.DictReader(_io.StringIO(raw)))
    except Exception as e:
        return JSONResponse({"error": f"Could not parse CSV: {e}"}, status_code=400)
    if not rows or "ticker" not in (rows[0].keys() if rows else []):
        got = list(rows[0].keys()) if rows else []
        return JSONResponse({"error": "CSV must have a header row including at least 'ticker' and "
                                       f"'quantity' columns (found: {', '.join(got) or 'none'})."},
                            status_code=400)
    clean, errors = _validate_holdings_rows(rows)
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)
    _write_holdings_csv_rows(clean)
    return {"ok": True, "rows_written": len(clean)}


@app.get("/api/holdings-csv/template")
def get_holdings_csv_template():
    """Downloadable starter CSV — header + two example rows, same columns _build_csv() reads."""
    sample = ("ticker,name,quantity,cost_basis,account,institution,type\n"
              "AAPL,Apple Inc,25,4200,Taxable Brokerage,Fidelity,Stock\n"
              "VOO,Vanguard S&P 500 ETF,10,4100,Roth IRA,Fidelity,ETF\n")
    return Response(content=sample, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=holdings_template.csv"})


# ---------------- Daily value snapshot -> realized risk metrics (accrues over time) ----------------
VALUE_HISTORY_FILE = os.environ.get("VALUE_HISTORY_FILE", "/data/value_history.json")
RISK_MIN_DAYS = int(os.environ.get("RISK_MIN_DAYS", "20"))
store.ensure_value_history_migrated(VALUE_HISTORY_FILE)   # Storage Stage B (2026-07-24) -- one-time import from the Stage-A JSON blob into the real `value_history` table, no-ops after the first run


def _read_value_history():
    """Ascending by date. Storage Stage B (2026-07-24): now backed by store.py's `value_history`
    table (real SQLite rows, no 2500-entry cap) instead of one growing JSON blob -- name/shape
    unchanged so every consumer (_risk_metrics, _vol_regime_signal, performance.py, the
    Portfolio-vs-SPY chart via /api/value-history) needed zero changes."""
    try:
        return store.value_history_read_all()
    except Exception:
        return []


SHARELOG_FILE = os.environ.get("SHARELOG_FILE", "/data/share_log.json")


def _read_sharelog():
    try:
        return store.read_json_strict(SHARELOG_FILE) or {}
    except Exception:
        return {}


def _snapshot_shares(d):
    """Daily per-ticker total shares (ALL accounts — §1091 spans IRAs) for the wash-sale
    ledger; buys are inferred from day-over-day increases (catches DRIP). Best-effort."""
    try:
        q = {}
        for c in d.get("consolidated") or []:
            t = c.get("ticker")
            if t and (c.get("type") or "") != "Cash" and (c.get("quantity") or 0) > 0:
                q[t] = q.get(t, 0.0) + float(c["quantity"])
        if not q:
            return
        log = washsale.append_snapshot(_read_sharelog(), q)
        store.write_json(SHARELOG_FILE, log)
    except Exception:
        pass


def _snapshot_value(d):
    """Upsert one {date, net_worth, invested_value, spy} row per calendar day so a real return
    history accrues for realized volatility / max-drawdown / Sharpe. Idempotent per day (updates
    today's row). Storage Stage B (2026-07-24): queries just today's row instead of reading the
    whole history to check the cached SPY close -- behaviorally identical, cheaper. No cap on the
    stored history anymore (the old `hist[-2500:]` trim is gone; SQL has no reason to discard
    years-old rows just to bound a JSON blob's parse cost)."""
    if not d:
        return
    try:
        today = datetime.date.today().isoformat()
        # benchmark close (SPY), fetched at most once/day (reuse today's if already stored)
        spy = store.value_history_today_spy(today)
        if not spy:
            try:
                spy = _price(os.environ.get("BENCHMARK_TICKER", "SPY"))
            except Exception:
                spy = None
        store.value_history_upsert_today(today, d.get("net_worth"), d.get("invested_value"), spy)
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


def _vol_regime_signal(short_days=21, baseline_days=45, hot_ratio=1.5, cool_ratio=0.5):
    """Alerting layer (2026-07-18): realized volatility over the last `short_days` trading days
    vs a trailing `baseline_days` window immediately before it — from the SAME daily
    invested_value/net_worth snapshot series _risk_metrics() already uses, so no new data source.
    A materially hotter or calmer regime than THIS BOOK'S OWN recent normal is itself a signal,
    independent of any absolute VaR/vol threshold (which needs a peer/history baseline this
    single-book history doesn't have). None if there isn't enough history yet (needs
    short_days+baseline_days+1 recorded days, i.e. ~66 days at the defaults)."""
    hist = _read_value_history()
    vals = [h.get("invested_value") or h.get("net_worth") for h in hist
            if (h.get("invested_value") or h.get("net_worth"))]
    need = short_days + baseline_days + 1
    if len(vals) < need:
        return None
    rets = [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals)) if vals[i - 1]]
    if len(rets) < short_days + baseline_days:
        return None
    recent, baseline = rets[-short_days:], rets[-(short_days + baseline_days):-short_days]

    def ann_vol(xs):
        if not xs:
            return None
        m = sum(xs) / len(xs)
        var = sum((x - m) ** 2 for x in xs) / len(xs)
        return (var ** 0.5) * (252 ** 0.5)

    sv, bv = ann_vol(recent), ann_vol(baseline)
    if not sv or not bv:
        return None
    ratio = round(sv / bv, 2)
    if ratio >= hot_ratio:
        return {"flag": True, "level": "warn", "ratio": ratio,
                "msg": (f"Realized volatility over the last {short_days} trading days "
                        f"({sv * 100:.1f}% annualized) is {ratio}x its own {baseline_days}-day "
                        f"trailing baseline ({bv * 100:.1f}%) — a materially hotter regime than "
                        "normal for this book.")}
    if ratio <= cool_ratio:
        return {"flag": True, "level": "info", "ratio": ratio,
                "msg": (f"Realized volatility over the last {short_days} trading days "
                        f"({sv * 100:.1f}% annualized) has fallen to {ratio}x its own "
                        f"{baseline_days}-day trailing baseline ({bv * 100:.1f}%) — a materially "
                        "calmer regime than normal.")}
    return {"flag": False, "ratio": ratio}


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


def _fetch_stooq(safe):
    """Daily [date, close] from Stooq's free CSV endpoint, or [] on failure. NOTE (2026-07): Stooq
    began serving a JavaScript proof-of-work anti-bot page (HTML, not CSV) on some IPs, so this now
    often returns [] and callers fall back to Yahoo."""
    out = []
    try:
        r = requests.get("https://stooq.com/q/d/l/", params={"s": safe.lower() + ".us", "i": "d"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
        if r.status_code < 400 and r.text and "Date,Open" in r.text[:64]:
            for line in r.text.strip().splitlines()[1:]:
                parts = line.split(",")
                if len(parts) >= 5:
                    try:
                        out.append([parts[0], float(parts[4])])
                    except ValueError:
                        continue
    except Exception:
        out = []
    return out


def _fetch_yahoo(safe, years):
    """Daily [date, close] from the KEYLESS Yahoo chart API — the fallback when Stooq is unavailable.
    No API key; tries query1 then query2; returns [] on any failure. Skips null closes (holidays/
    halts)."""
    rng = f"{max(1, int(years))}y"
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        out = []
        try:
            r = requests.get(f"https://{host}/v8/finance/chart/{safe}",
                             params={"range": rng, "interval": "1d"},
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
            if r.status_code >= 400:
                continue
            res = (((r.json() or {}).get("chart") or {}).get("result") or [None])[0]
            if not res:
                continue
            ts = res.get("timestamp") or []
            quote = (((res.get("indicators") or {}).get("quote") or [{}])[0]) or {}
            closes = quote.get("close") or []
            for i, t in enumerate(ts):
                c = closes[i] if i < len(closes) else None
                if c is None:
                    continue
                try:
                    d = datetime.datetime.utcfromtimestamp(int(t)).strftime("%Y-%m-%d")
                    out.append([d, float(c)])
                except (ValueError, OverflowError, OSError):
                    continue
            if out:
                return out
        except Exception:
            continue
    return []


def _stooq_daily(sym):
    """Daily close history for a US-listed ticker, cached per ticker to PRICE_HIST_DIR and refetched
    only after risk_hist_max_age days. Tries Stooq's free CSV first, then falls back to the keyless
    Yahoo chart API (Stooq began JS-walling its CSV endpoint 2026-07 — HTML, not CSV). Returns
    [[date, close], ...] ascending, trimmed to ~risk_hist_years, or [] on total failure. Name kept
    for call-site compatibility across G4/G5/G6/G7. Defensive — deploy-iterates against the live
    sources on the host."""
    if not sym:
        return []
    safe = re.sub(r"[^A-Za-z0-9.\-]", "", sym).upper()
    path = os.path.join(PRICE_HIST_DIR, safe + ".json")
    max_age = _knob("risk_hist_max_age") * 86400
    cached = None
    try:
        with open(path, encoding="utf-8") as f:
            cached = json.load(f)
        if time.time() - (cached.get("fetched") or 0) < max_age and cached.get("series"):
            return cached["series"]
    except Exception:
        cached = None
    years = _knob("risk_hist_years")
    series = _fetch_stooq(safe)
    source = "stooq"
    if not series:
        series = _fetch_yahoo(safe, years)   # Stooq JS-walled → keyless Yahoo fallback
        source = "yahoo"
    if series:
        series.sort(key=lambda x: x[0])
        keep = years * 252 + 10
        series = series[-keep:]
        try:
            # atomic (2026-07-24, CLOUD-PORTABILITY-ROADMAP.md Phase 2) -- cheap insurance against
            # a torn write leaving a corrupt cache file; on failure the except below just means
            # this ticker re-fetches next time, same as any other cache-write failure already did.
            store.atomic_write_text(path, json.dumps({"fetched": int(time.time()), "series": series,
                                                      "source": source}))
        except Exception:
            pass
        return series
    # both sources failed — fall back to any stale cache we still have rather than nothing
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
            store.write_json(FF_FACTORS_FILE, {"fetched": int(time.time()), "factors": merged})
        except Exception:
            pass
    return merged


def _read_ff():
    """Merged FF factor map, from cache when fresh else refetched. {} if never obtained."""
    try:
        c = store.read_json_strict(FF_FACTORS_FILE)
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
    cit_tickers = set()   # RESUME-TRACKER 2026-07-18k: never resolvable (fake ticker = fund name),
                           # already surfaces via `unresolved`/coverage_pct below — skip the wasted Stooq call
    for c in cons:
        weights[c["ticker"]] = weights.get(c["ticker"], 0.0) + (c.get("value") or 0.0)
        if c.get("is_cit"):
            cit_tickers.add(c["ticker"])
    invested = round(sum(weights.values()), 2)
    if not weights or invested <= 0:
        return {"status": "unavailable", "generated_at": int(time.time()),
                "note": "No non-cash holdings to analyze."}
    series = {}
    for t in weights:
        if t in cit_tickers:
            continue
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
        return store.read_json_strict(RISK_SUITE_FILE)
    except Exception:
        return None


def _do_risk_suite():
    if not _risk_lock.acquire(blocking=False):
        return
    try:
        res = _risk_suite()
        store.write_json(RISK_SUITE_FILE, res)
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
def run_risk_suite():
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
        return store.read_json_strict(ATTRIB_FILE)
    except Exception:
        return None


def _do_attrib():
    if not _attrib_lock.acquire(blocking=False):
        return
    try:
        res = _attribution()
        store.write_json(ATTRIB_FILE, res)
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
def run_attribution():
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
        return store.read_json_strict(SIZING_FILE)
    except Exception:
        return None


def _do_sizing():
    if not _sizing_lock.acquire(blocking=False):
        return
    try:
        res = _sizing()
        store.write_json(SIZING_FILE, res)
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
def run_sizing():
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
    age = _derive_age(cfg.get("birth_year"), cfg.get("current_age"))
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
    age = _derive_age(cfg.get("birth_year"), cfg.get("current_age"))
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
        return store.read_json_strict(LEVERAGE_FILE)
    except Exception:
        return None


def _do_leverage():
    if not _leverage_lock.acquire(blocking=False):
        return
    try:
        res = _leverage()
        store.write_json(LEVERAGE_FILE, res)
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
def run_leverage():
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
        return store.read_json_strict(THESES_FILE) or {}
    except Exception:
        return {}


def _write_theses(reg):
    store.write_json(THESES_FILE, reg)


def _thesis_metrics(sym):
    """Current fundamentals for trigger evaluation — same edgar/valuation/reverse-DCF stack the
    scanner uses. Returns the metric keys thesis.py triggers reference."""
    fund = edgar.fundamentals(sym) or {}
    val = _valuation(sym) or {}
    m = (fund.get("metrics") or {}) if fund.get("available") else {}
    r = (fund.get("ratios") or {}) if fund.get("available") else {}
    rd = _reverse_dcf(val, fund) or {}
    pio = ((fund.get("scores") or {}).get("piotroski_f") or {}) if fund.get("available") else {}
    inst = _inst_for(sym) or {}
    return {"price": _price(sym), "fcf_margin_pct": r.get("fcf_margin_pct"),
            "rev_growth_pct": m.get("revenue_growth_pct"), "net_margin_pct": r.get("net_margin_pct"),
            "piotroski": pio.get("score"), "pe_forward": val.get("pe_forward") or val.get("pe_ttm"),
            "implied_growth_pct": rd.get("implied_growth_pct"),
            "accruals_ratio_pct": _accruals_ratio(fund),
            "valuation_percentile": (_valuation_percentile(sym) or {}).get("cheapness_percentile"),
            "insider_net_buy_pct": (_insider_signal(sym) or {}).get("net_buy_pct"),
            "inst_holders": inst.get("holder_count"),
            "inst_net_buyers": ((inst.get("buying") or 0) - (inst.get("selling") or 0)) if inst else None,
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
        return store.read_json_strict(THESES_EVAL_FILE)
    except Exception:
        return None


def _do_theses_eval():
    if not _theses_lock.acquire(blocking=False):
        return
    try:
        res = _evaluate_theses()
        store.write_json(THESES_EVAL_FILE, res)
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
def run_theses():
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
        last = store.read_json_strict(REALESTATE_FILE)
    except Exception:
        last = None
    holdings, total = _realestate_holdings()
    avm = None
    deed_fraud = None
    try:
        addr = _read_insurance_cfg().get("home_address")
        avm = _rentcast_avm(addr) if addr else None
    except Exception:
        avm = None
    try:
        deed_fraud = _deed_fraud_guidance(addr) if addr else None
    except Exception:
        deed_fraud = None
    return {"last": last, "existing_holdings": holdings, "existing_total": total,
            "home_avm": avm,
            "deed_fraud": deed_fraud,
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
        store.write_json(REALESTATE_FILE, rec)
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
        last = store.read_json_strict(BUSINESS_FILE)
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
        store.write_json(BUSINESS_FILE, rec)
    except Exception:
        pass
    return rec


# ============ Attention digest — materiality-filtered "what needs attention" ============
# The read-side of the alerting layer: aggregates signals the other modules ALREADY compute
# (exit-trigger trips, sizing loss-budget breaches, concentration/stress flags, grounding
# staleness) into one prioritized list. Reads cached outputs — cheap, read-only, no new data.


EARNINGS_CAL_FILE = os.environ.get("EARNINGS_CAL_FILE", "/data/earnings_cal.json")
EARNINGS_CAL_TTL = int(os.environ.get("EARNINGS_CAL_TTL", "86400"))


def _earnings_calendar_rows():
    """Raw cached calendar rows for held tickers, Finnhub free tier, refreshed daily. Window is
    yesterday through +7 days — ONE day of lookback beyond what _earnings_this_week() shows, so
    an after-market-close print can still be detected the next morning (OPP-6). Callers that want
    the forward-only view should filter to date >= today themselves; see _earnings_this_week()."""
    if not FINNHUB:
        return []
    try:
        cache = store.read_json_strict(EARNINGS_CAL_FILE) or {}
        if time.time() - (cache.get("ts") or 0) < EARNINGS_CAL_TTL:
            return cache.get("rows") or []
    except Exception:
        pass
    rows = []
    try:
        held = {c.get("ticker") for c in (_data().get("consolidated") or []) if c.get("ticker")}
        d0 = datetime.date.today()
        r = requests.get("https://finnhub.io/api/v1/calendar/earnings",
                         params={"from": (d0 - datetime.timedelta(days=1)).isoformat(),
                                 "to": (d0 + datetime.timedelta(days=7)).isoformat(),
                                 "token": FINNHUB}, timeout=25)
        if r.status_code < 400:
            for e in ((r.json() or {}).get("earningsCalendar") or []):
                sym = (e.get("symbol") or "").upper()
                if sym in held:
                    rows.append({"ticker": sym, "date": e.get("date"),
                                 "hour": e.get("hour"),
                                 "eps_estimate": e.get("epsEstimate")})
        rows.sort(key=lambda x: x.get("date") or "")
        store.write_json(EARNINGS_CAL_FILE, {"ts": int(time.time()), "rows": rows})
    except Exception:
        pass
    return rows


def _earnings_this_week():
    """Held tickers reporting in the next 7 days (forward-looking view for the ATTENTION digest
    and the narrative brief). [] when unkeyed/unavailable — the digest simply omits the section.
    Same underlying cache as _earnings_calendar_rows(), just filtered to date >= today so this
    function's existing behavior/callers are unaffected by the added lookback day."""
    today_s = datetime.date.today().isoformat()
    return [e for e in _earnings_calendar_rows() if (e.get("date") or "") >= today_s]


EARNINGS_TRIGGERED_FILE = os.environ.get("EARNINGS_TRIGGERED_FILE", "/data/earnings_triggered.json")


def _read_earnings_triggered():
    try:
        return store.read_json_strict(EARNINGS_TRIGGERED_FILE) or {}
    except Exception:
        return {}


def _mark_earnings_triggered(tickers):
    """Record that these tickers' CURRENT earnings print already fired a brief refresh, so the
    same report date doesn't re-trigger the job every day it's checked. Pruned to the last 30
    days so the file doesn't grow forever."""
    trig = _read_earnings_triggered()
    today_s = datetime.date.today().isoformat()
    for t in tickers:
        trig[t] = today_s
    cutoff = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
    trig = {k: v for k, v in trig.items() if v >= cutoff}
    try:
        store.write_json(EARNINGS_TRIGGERED_FILE, trig)
    except Exception:
        pass


def _earnings_due_tickers():
    """OPP-6 event-driven research: held tickers whose earnings print is FRESHLY OUT — today's
    before-market/during-market report, or YESTERDAY's after-close report (the one case
    _earnings_this_week()'s forward-only view can't see) — and that haven't already triggered a
    refresh for that exact report date. Known limitation: Finnhub's free calendar doesn't always
    populate `hour` (bmo/amc/dmt); a blank hour is treated as bmo/dmt (same-day), which is the
    common case and errs toward refreshing a day early rather than missing the print entirely."""
    today = datetime.date.today()
    today_s = today.isoformat()
    yday_s = (today - datetime.timedelta(days=1)).isoformat()
    trig = _read_earnings_triggered()
    due = []
    for e in _earnings_calendar_rows():
        t, d, hr = e.get("ticker"), e.get("date"), (e.get("hour") or "").lower()
        if not t or not d:
            continue
        is_out = (d == today_s and hr in ("bmo", "dmt", "")) or (d == yday_s and hr == "amc")
        if is_out and trig.get(t) != d:
            due.append(t)
    return due


ALERTS_SEEN_FILE = os.environ.get("ALERTS_SEEN_FILE", "/data/alerts_seen.json")


def _alert_key(a):
    """Stable-enough identity for one alert condition (category + message) so the digest can
    tell a genuinely NEW condition from one you've already seen and left open. Imperfect by
    design (a message with a dollar figure that ticks daily reads as 'new' again each time) —
    deliberately simple over exact-match hysteresis; still correctly distinguishes a brand-new
    category of problem from a stale, already-acknowledged one, which is the case that matters."""
    return f"{a.get('category', '')}::{a.get('message', '')}"[:300]


def _read_alerts_seen():
    try:
        return set(store.read_json_strict(ALERTS_SEEN_FILE).get("keys") or [])
    except Exception:
        return set()


def _estate_doc_staleness(icfg=None):
    """GAP-8 estate hygiene audit: per-document currency check against the review-cadence
    knob (default 3 years — a hygiene reminder, NOT a legal requirement; the real trigger for
    re-reviewing any estate document is a major life event — marriage, divorce, birth, death,
    home purchase/sale, or a move to a new state — regardless of how recently it was reviewed).
    `last_reviewed` is stored as a 4-digit year (same convention as a policy's term_expires),
    not a full date — good enough for an annual-cadence tripwire, avoids date-parsing edge cases.
    Returns one row per doc_type in ESTATE_DOC_TYPES (even if never recorded) plus any custom
    doc_type the user added beyond the standard set."""
    icfg = icfg if icfg is not None else _read_insurance_cfg()
    cadence = icfg.get("estate_doc_review_years") or 3
    by_type = {}
    for d in (icfg.get("estate_docs") or []):
        t = d.get("doc_type")
        if t:
            by_type[t] = d
    this_year = datetime.datetime.now().year
    types = list(ESTATE_DOC_TYPES) + [t for t in by_type if t not in ESTATE_DOC_TYPES]
    rows = []
    for t in types:
        d = by_type.get(t) or {}
        yr = None
        try:
            yr = int(str(d.get("last_reviewed"))[:4]) if d.get("last_reviewed") else None
        except (TypeError, ValueError):
            yr = None
        years_since = (this_year - yr) if yr else None
        if yr is None:
            status = "missing"
        elif years_since >= cadence:
            status = "stale"
        else:
            status = "ok"
        rows.append({"doc_type": t, "last_reviewed": yr, "years_since": years_since,
                     "status": status, "notes": d.get("notes"), "cadence_years": cadence})
    return rows


def _alerts_digest():
    """Alerting/monitoring layer (Tier 4 roadmap item, built 2026-07-18 — was explicitly deferred
    pending the G4 risk suite, which is now live). Materiality-filtered, not a raw dump: only
    conditions that already cross an established threshold elsewhere (drift bands, loss budgets,
    concentration caps, thesis triggers, grounding staleness) or a newly-added realized-vol
    regime-shift check make it in. `new` per-alert (+ `counts.new`) distinguishes a condition that
    appeared since your last visit to ⚠ ATTENTION from one you've already seen and left open —
    POST /api/alerts/ack (fired when the panel is opened) marks the current set as seen. The
    header badge polls this so it's visible from any tab, not just when you remember to check."""
    alerts = []

    def add(level, cat, msg, where):
        alerts.append({"level": level, "category": cat, "message": msg, "where": where})

    # 0) Earnings on holdings this week (P1 adoption — event awareness beats cron-only research)
    for e in _earnings_this_week():
        est = f", est EPS {e['eps_estimate']}" if e.get("eps_estimate") is not None else ""
        add("info", "Earnings",
            f"{e['ticker']} reports {e.get('date')}{(' (' + e['hour'] + ')') if e.get('hour') else ''}{est} "
            "— expect volatility; a post-print brief refresh is worth queuing", "RESEARCH → BRIEFS")

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
    # 5) Drift-band breach — policy allocation (by type) + IPS sleeve, vs the drift_threshold_pct
    # knob. _rebalance() is cheap (CPU + already-cached feed/lookthrough, no network) so it's
    # safe to call live here rather than reading a separate cache.
    try:
        rb = _rebalance()
        for a in (rb.get("allocation") or []):
            if a.get("flag"):
                verb = "over" if (a.get("drift_pct") or 0) > 0 else "under"
                add("warn", "Drift", f"{a['type']} is {abs(a['drift_pct'])}pts {verb} target "
                    f"({a['actual_pct']}% vs {a['target_pct']}%, ~${abs(a.get('drift_usd') or 0):,.0f})",
                    "INSIGHTS → POLICY · DRIFT")
        for a in (rb.get("ips_allocation") or []):
            if a.get("flag"):
                verb = "over" if (a.get("drift_pct") or 0) > 0 else "under"
                add("warn", "Drift", f"IPS sleeve {a['sleeve']} is {abs(a['drift_pct'])}pts {verb} "
                    f"target ({a['actual_pct']}% vs {a['target_pct']}%, ~${abs(a.get('drift_usd') or 0):,.0f})",
                    "INSIGHTS → POLICY · DRIFT")
    except Exception:
        pass
    # 6) Risk-regime change — realized vol vs the book's own trailing baseline (new 2026-07-18).
    try:
        reg = _vol_regime_signal()
        if reg and reg.get("flag"):
            add(reg["level"], "Risk regime", reg["msg"], "INSIGHTS → RISK")
    except Exception:
        pass
    # 7) Setup completeness — state of residence unconfirmed (RESUME-TRACKER item 8, found
    # while answering Jason's renter question). A guessed Texas presented as fact is worse
    # than the old single-state hardcode; surface it until the user confirms or it auto-detects.
    try:
        sinfo = _state_of_residence_info()
        if sinfo["source"] == "default_unconfirmed":
            add("warn", "Setup",
                "State of residence isn't set and no tracked real-estate account gave a state "
                "to auto-detect — Estate, ACA, Tax, and Cash·Debt are all showing TEXAS "
                "assumptions (community property, TOD deeds, Medicaid gap) as a GUESS, not a "
                "confirmed fact. Set it once in ASSUMPTIONS.", "PLAN → ASSUMPTIONS")
    except Exception:
        pass
    # 8) Estate document currency (GAP-8) — G13-style recurring staleness tripwire, not a
    # one-time report: a will/POA/beneficiary review that's never recorded or overdue per the
    # cadence knob surfaces here every time, the same way a stale tax constant does.
    try:
        for row in _estate_doc_staleness():
            label = row["doc_type"].replace("_", " ")
            if row["status"] == "missing":
                add("info", "Estate hygiene",
                    f"{label}: no review date on file — record when it was last reviewed "
                    "(or confirm it doesn't apply) in PLAN → CONTINUITY.", "PLAN → CONTINUITY")
            elif row["status"] == "stale":
                add("warn", "Estate hygiene",
                    f"{label}: last reviewed {row['last_reviewed']} ({row['years_since']}y ago, "
                    f"cadence is {row['cadence_years']}y) — due for a look, sooner if a life "
                    "event happened since (marriage, divorce, birth, death, move).", "PLAN → CONTINUITY")
    except Exception:
        pass

    # TASK-005: a connected Plaid institution needs the user to log in again
    try:
        if os.environ.get("PORTFOLIO_PROVIDER", "csv").lower() == "plaid":
            for _pi in plaid_integration.public_items():
                if _pi.get("status") == "login_required":
                    add("action", "Plaid", f"{_pi.get('institution') or 'A connected institution'} needs you to log in "
                        "again (SETTINGS → ACCOUNTS → Plaid → RECONNECT) — its balances are not updating", "SETTINGS → ACCOUNTS")
    except Exception:
        pass
    # TASK-002: emergency kit staleness + an open emergency request (visible from the cockpit)
    try:
        _ks = kit.stale_alert()
        if _ks:
            add("warn", "Emergency kit", _ks + " — generate a fresh kit", "PLAN → VAULT")
        _er = emergency.open_request()
        if _er and _er["status"] == "pending":
            add("action", "Emergency access",
                f"An emergency-access request is pending and will be granted automatically unless you deny it "
                f"(by {time.strftime('%Y-%m-%d %H:%M', time.localtime(_er['requested_at'] + _er['wait_seconds']))})",
                "SETTINGS → IAM → EMERGENCY ACCESS")
    except Exception:
        pass

    order = {"action": 0, "warn": 1, "info": 2}
    alerts.sort(key=lambda a: order.get(a["level"], 9))
    seen = _read_alerts_seen()
    for a in alerts:
        a["new"] = _alert_key(a) not in seen
    return {"generated_at": int(time.time()), "alerts": alerts,
            "counts": {**{lvl: sum(1 for a in alerts if a["level"] == lvl) for lvl in ("action", "warn", "info")},
                       "new": sum(1 for a in alerts if a["new"])},
            "note": "Materiality-filtered digest of what needs attention, aggregated from the exit-trigger "
                    "registry, the sizing loss budget, concentration/stress, policy/IPS drift bands, a "
                    "realized-vol regime-shift check, and the grounding watchdog. Reads cached results "
                    "(build the RISK/SIZING/THESES panels to populate). Read-only, not advice."}


def usd_neg(n):
    try:
        return ("-$" + format(abs(round(n)), ",")) if n is not None else "—"
    except Exception:
        return "—"


@app.get("/api/alerts")
def get_alerts():
    """Attention digest — prioritized action/warn/info items aggregated across the modules.
    Each alert carries `new` (unseen since the last ack); `counts.new` is what the header badge
    polls from every tab."""
    return _alerts_digest()


@app.post("/api/alerts/ack")
def ack_alerts():
    """Mark the CURRENT digest as seen — call when the ⚠ ATTENTION panel is opened. Not
    token-guarded (it's a local read-state marker, not a GPU job trigger). Recomputes the digest
    (cheap, no LLM/network beyond already-cached readers) rather than trusting a client-supplied
    list, so a stale client can't accidentally mark something unseen as seen."""
    keys = [_alert_key(a) for a in (_alerts_digest().get("alerts") or [])]
    try:
        store.write_json(ALERTS_SEEN_FILE, {"keys": keys, "acked_at": int(time.time())})
    except Exception:
        return JSONResponse({"error": "could not persist ack"}, status_code=500)
    return {"ok": True, "acked": len(keys)}


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
            log = store.read_json_strict(RATING_LOG_FILE) or []
        except Exception:
            log = []
        log = [e for e in log if not (e.get("ticker") == ticker and e.get("date") == today)]
        log.append({"date": today, "ts": int(time.time()), "ticker": ticker, "rating": rating,
                    "confidence": (llm or {}).get("confidence"), "price": round(px, 4)})
        log = log[-5000:]
        store.write_json(RATING_LOG_FILE, log)
    except Exception:
        pass


def _scorecard(min_age_days=None):
    """Score the model's historical calls: forward return from each dated rating's price to the
    current price, aggregated by rating and by confidence. Read-only, educational."""
    min_age = int(min_age_days or os.environ.get("SCORECARD_MIN_AGE_DAYS", "14"))
    try:
        log = store.read_json_strict(RATING_LOG_FILE) or []
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


# ============ P0-2: decision backtest — fixed-horizon call scoring vs SPY (backtest.py) ============
# The scorecard above answers "how do old calls look TO NOW"; this answers the fiduciary question:
# at fixed 1/3/6/12-month horizons, did Buys BEAT SPY and Sells LAG it, and is stated confidence
# calibrated against that market-relative outcome? Heavy only in network (per-ticker history via
# the same _stooq_daily cache G4 uses) — background thread + cache, same pattern as the risk suite.
BACKTEST_FILE = os.environ.get("BACKTEST_FILE", "/data/backtest.json")
BACKTEST_TTL = int(os.environ.get("BACKTEST_TTL", str(7 * 86400)))   # weekly is plenty
_backtest_lock = threading.Lock()


def _do_backtest():
    if not _backtest_lock.acquire(blocking=False):
        return
    try:
        try:
            log = store.read_json_strict(RATING_LOG_FILE) or []
        except Exception:
            log = []
        res = backtest_mod.run(log, _read_paper(), _stooq_daily)
        store.write_json(BACKTEST_FILE, res)
    except Exception:
        pass
    finally:
        _backtest_lock.release()


@app.get("/api/backtest")
def get_backtest():
    """Decision backtest, CACHED (first build fetches per-ticker daily history in the background)."""
    try:
        cached = store.read_json_strict(BACKTEST_FILE)
    except Exception:
        cached = None
    fresh = bool(cached) and (time.time() - (cached.get("generated_at") or 0) < BACKTEST_TTL)
    if not fresh and not _backtest_lock.locked():
        threading.Thread(target=_do_backtest, daemon=True).start()
    if cached:
        return {**cached, "cached": True, "refreshing": (not fresh)}
    return {"status": "computing", "note": "First decision-backtest build is running (fetching "
            "price history per rated ticker) — refresh in a moment.", "computing": True}


@app.post("/api/run-backtest")
def run_backtest_ep():
    threading.Thread(target=_do_backtest, daemon=True).start()
    return {"status": "started"}


@app.get("/api/gpu")
def get_gpu():
    """GPU serialization status (Increment A). `busy` = a heavy qwen-27b job holds
    the GPU; `queued` = jobs waiting behind it (waiters minus the one running).
    Cheap: two ints, no LLM. A UI badge can poll this for visible queue depth."""
    busy = _GPU.locked()
    with _gpu_waiters_lock:
        w = _gpu_waiters
    return {"busy": busy, "waiters": w, "queued": max(0, w - (1 if busy else 0))}


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
        sinfo = _state_of_residence_info(cfg, d)
        st = sinfo["code"]
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
        # Real assets (primary residence + other real estate): these get a §1014 step-up at
        # death but hold no securities, so they're absent from by_account (and _account_tax_status
        # returns "na"). Pull them from the full account list so the ledger reflects the home —
        # usually the largest single step-up in the estate. Basis (residence purchase + improvements)
        # is user-supplied via estate_home_basis; applied to the largest RE account (the residence).
        home_basis = cfg.get("estate_home_basis")
        re_accts = [a for a in d.get("accounts", [])
                    if (a.get("type") or "").strip().lower() in ("real estate", "real_estate", "realestate")
                    and a.get("is_asset") is not False and (a.get("balance") or 0) > 0]
        re_accts.sort(key=lambda a: -(a.get("balance") or 0))
        for i, a in enumerate(re_accts):
            val = a.get("balance") or 0.0
            basis = (float(home_basis) if (i == 0 and home_basis not in (None, "", 0)) else None)
            gain = (max(0.0, val - basis) if basis is not None else None)
            accounts.append({"name": a.get("name"), "tax_status": "real_estate", "value": val,
                             "gain_total": gain, "basis_total": basis})
        snap = estate.snapshot(d.get("net_worth"), married, growth, state=st)
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
                "state_of_residence": st, "state_of_residence_source": sinfo["source"],
                "snapshot": snap,
                "home_basis": home_basis,
                "stepup": estate.stepup_ledger(accounts, state=st),
                "gift_vs_hold": {"positions": gvh,
                                 "note": "Top embedded-gain TAXABLE positions, modeled gift-now vs "
                                         "hold-for-step-up over 20 years at your assumed real return."},
                "gifting": estate.gifting_capacity(cfg.get("estate_donees") or 0, married,
                                                   years=10, growth_pct=growth),
                "titling": estate.titling_audit(accounts, state=st),
                "trusts": estate.trust_triggers(d.get("net_worth"), married,
                                                cross_year=snap.get("years_until_exemption_crossed"),
                                                state=st),
                "note": "Educational decision-support, not legal/tax advice — estate documents need "
                        "a licensed attorney; confirm figures with a CPA."}
    except Exception as e:
        return JSONResponse({"error": f"estate view unavailable: {e}"}, status_code=502)


@app.get("/api/continuity")
def get_continuity():
    """OPP-5 continuity binder: an always-current, auto-generated estate-OPERATIONS doc for
    whoever has to handle things if you can't — account map (what passes how), the step-up
    ledger, key contacts, and an action checklist. Composes estate.py's existing
    titling_audit()/stepup_ledger() outputs plus the contacts register; computes nothing new
    — this is a document view, not an analysis engine. Regenerates fresh on every load, so
    unlike a printed binder it can't go stale (though the PRINTED version, once you print it,
    obviously can — reprint after any major account change)."""
    try:
        d = _data()
        cfg = _read_retire()
        icfg = _read_insurance_cfg()
        sinfo = _state_of_residence_info(cfg, d)
        st = sinfo["code"]
        married = tax.norm_status(cfg.get("filing_status") or "mfj") == "mfj"
        # Same account-list construction as /api/estate (kept as a second copy deliberately —
        # this endpoint's shape is a document, not an analysis, and shouldn't couple to any
        # future change in estate's own response shape).
        acct_status = {a.get("name"): _account_tax_status(a.get("name"), a.get("type"))
                       for a in d.get("accounts", [])}
        accounts = []
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
        home_basis = cfg.get("estate_home_basis")
        re_accts = [a for a in d.get("accounts", [])
                    if (a.get("type") or "").strip().lower() in ("real estate", "real_estate", "realestate")
                    and a.get("is_asset") is not False and (a.get("balance") or 0) > 0]
        re_accts.sort(key=lambda a: -(a.get("balance") or 0))
        for i, a in enumerate(re_accts):
            val = a.get("balance") or 0.0
            basis = (float(home_basis) if (i == 0 and home_basis not in (None, "", 0)) else None)
            gain = (max(0.0, val - basis) if basis is not None else None)
            accounts.append({"name": a.get("name"), "tax_status": "real_estate", "value": val,
                             "gain_total": gain, "basis_total": basis})
        # The survivor needs what's OWED too, not just what's owned — titling/step-up don't
        # cover liabilities, so pull them separately.
        liabilities = [{"name": a.get("name"), "balance": round(a.get("balance") or 0, 2),
                        "type": a.get("type")}
                       for a in d.get("accounts", [])
                       if a.get("is_asset") is False and (a.get("balance") or 0) > 0]
        liabilities.sort(key=lambda x: -x["balance"])

        titling = estate.titling_audit(accounts, state=st)
        stepup = estate.stepup_ledger(accounts, state=st)
        # Merge the two per-account views by name into one row for the binder — presentation
        # only, both sides come straight from estate.py's already-computed output.
        stepup_by_name = {r["account"]: r for r in stepup.get("accounts", [])}
        account_map = []
        for row in titling.get("accounts", []):
            sr = stepup_by_name.get(row["account"], {})
            account_map.append({"account": row["account"], "tax_status": row["tax_status"],
                                "value": row["value"], "passes_by": row["passes_by"],
                                "action": row["action"], "embedded_gain": sr.get("embedded_gain")})

        contacts = icfg.get("contacts") or []
        executor = icfg.get("executor_name")
        will_loc = icfg.get("will_location")
        doc_currency = _estate_doc_staleness(icfg)
        u = lambda v: "—" if v is None else f"${v:,.0f}"

        checklist = [
            {"step": "Locate the estate documents",
             "detail": ("Will" + (f" (kept: {will_loc})" if will_loc else
                                   " — location not on file, add it in PLAN→CONTINUITY") +
                        ", durable power of attorney, medical directive, and any trust documents. "
                        "This app doesn't store or draft them — only where they are and how "
                        "accounts transfer.")},
            {"step": "Notify Social Security (if applicable)",
             "detail": "Report the death to the SSA — a surviving spouse/dependent may be "
                       "eligible for a survivor benefit; claiming timing affects the amount."},
            {"step": "Confirm every beneficiary-designated account below is current",
             "detail": "Beneficiary designations and TOD/POD registrations pay out DIRECTLY and "
                       "OVERRIDE the will — verify each one before assuming the will controls it."},
        ]
        if executor:
            checklist.append({"step": "Contact the executor", "detail": f"Named executor: {executor}."})
        stale_or_missing = [r for r in doc_currency if r["status"] in ("stale", "missing")]
        if stale_or_missing:
            checklist.append({"step": "Estate document currency needs attention",
                              "detail": "; ".join(
                                  f"{r['doc_type'].replace('_', ' ')} "
                                  + ("never recorded" if r["status"] == "missing"
                                     else f"last reviewed {r['last_reviewed']} ({r['years_since']}y ago)")
                                  for r in stale_or_missing)})
        for row in account_map:
            checklist.append({"step": f"{row['account']} ({u(row['value'])})",
                              "detail": f"Passes by: {row['passes_by']}. {row['action']}"})
        if liabilities:
            checklist.append({"step": "Debts to settle or assume",
                              "detail": "; ".join(f"{l['name']} ({u(l['balance'])})" for l in liabilities)})
        if stepup.get("taxable_embedded_gain"):
            checklist.append({"step": "Do not sell appreciated taxable positions before the step-up posts",
                              "detail": f"~{u(stepup['taxable_embedded_gain'])} of embedded gain gets "
                                        "wiped by basis step-up at death (IRC §1014) — selling first "
                                        f"forfeits it. {stepup.get('community_property_note', '')}"})
        if contacts:
            checklist.append({"step": "Call the professional team",
                              "detail": "; ".join(f"{c.get('role')}: {c.get('name') or '—'}"
                                                   + (f" {c.get('phone')}" if c.get('phone') else "")
                                                   for c in contacts)})
        else:
            checklist.append({"step": "No professional contacts on file",
                              "detail": "Add your estate attorney / CPA / financial advisor / "
                                        "insurance agent in PLAN→CONTINUITY so this isn't empty "
                                        "when it matters."})

        return {"generated_at": int(time.time()),
                "state_of_residence": st, "state_of_residence_source": sinfo["source"],
                "net_worth": d.get("net_worth"),
                "household": {"married": married, "filing_status": cfg.get("filing_status") or "mfj"},
                "executor_name": executor, "will_location": will_loc,
                "account_map": account_map, "liabilities": liabilities,
                "taxable_embedded_gain": stepup.get("taxable_embedded_gain"),
                "community_property_note": stepup.get("community_property_note"),
                "contacts": contacts, "doc_currency": doc_currency, "checklist": checklist,
                "note": "Auto-generated from your live balance sheet + the titling/step-up engine "
                        "every time this page loads — unlike a printed binder it can't silently go "
                        "stale. Educational decision-support, not legal advice; the will/POA/trust "
                        "documents themselves still need a licensed attorney."}
    except Exception as e:
        return JSONResponse({"error": f"continuity binder unavailable: {e}"}, status_code=502)


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
def raw_holdings(account_id: str = "", limit: int = 2):
    """DIAGNOSTIC (read-only): dump untrimmed Monarch aggregateHoldings node fields so
    we can verify cost-basis semantics (is `basis` per-share or a total?). Session-gated like
    every other endpoint (the global auth middleware) -- the old separate ANALYSIS_TOKEN check
    was retired 2026-07-24 as redundant, see app.py's SESSION AUTH block docstring.
    Compare each node's basis vs totalValue vs security.currentPrice * quantity."""
    try:
        accts = [a for a in _accounts(_post("get_accounts", {})) if (a.get("holdingsCount") or 0) > 0]
    except MonarchDisabled as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except MonarchDisabled as e:
        return JSONResponse({"error": str(e)}, status_code=409)
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
def raw_accounts():
    """DIAGNOSTIC (read-only): untrimmed account fields to debug the net-worth math —
    shows includeBalanceInNetWorth, isAsset, and the displayBalance sign for liabilities.
    Session-gated like every other endpoint; the old separate ANALYSIS_TOKEN check was retired
    2026-07-24 as redundant, see app.py's SESSION AUTH block docstring."""
    out = []
    try:
        accts_raw = _accounts(_post("get_accounts", {}))
    except MonarchDisabled as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except MonarchDisabled as e:
        return JSONResponse({"error": str(e)}, status_code=409)
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


# --- On-demand research persistence (2026-07-22) -------------------------------------------
# GET /api/research is a one-shot synchronous call with (previously) zero server-side persistence:
# an analysis that finished after the user navigated away from the RESEARCH→IDEAS sub-tab left no
# trace anywhere, and the only client cache (LAST_SCAN_ANALYSIS) was populated AFTER a live-DOM
# bail-out check, so it too was lost off-tab. This cache stores every completed on-demand brief on
# disk keyed by ticker, so the frontend can retrieve whatever finished (even after a reload) and a
# short history of past on-demand tickers exists instead of only the single most-recent one.
RESEARCH_CACHE_FILE = os.environ.get("RESEARCH_CACHE_FILE", "/data/research_ondemand.json")
_RESEARCH_CACHE_MAX = 20   # keep the last N on-demand tickers

def _read_research_cache():
    try:
        d = store.read_json_strict(RESEARCH_CACHE_FILE) or {}
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("items", {})   # ticker -> {ticker, generated_at, data}
    d.setdefault("order", [])   # recent tickers, newest first
    return d

def _save_research_result(t, result):
    """Persist a completed on-demand brief; trim to the most-recent _RESEARCH_CACHE_MAX tickers."""
    try:
        d = _read_research_cache()
        d["items"][t] = {"ticker": t,
                         "generated_at": (result or {}).get("generated_at") or int(time.time()),
                         "data": result}
        d["order"] = [t] + [x for x in d.get("order", []) if x != t]
        keep = d["order"][:_RESEARCH_CACHE_MAX]
        d["order"] = keep
        d["items"] = {k: v for k, v in d["items"].items() if k in keep}
        store.write_json(RESEARCH_CACHE_FILE, d)
    except Exception:
        pass   # cache is best-effort; never fail the request over it


@app.get("/api/research-cache")
def get_research_cache(ticker: str = "", history: int = 0):
    """Retrieve persisted on-demand research. With ?ticker=SYM returns that ticker's cached brief
    (so a page that navigated away/reloaded can recover a run that finished off-tab). With no ticker
    returns the recent-ticker order (newest first); ?history=1 additionally returns the full cached
    items so the IDEAS sub-tab can render past analyses without re-running the model."""
    d = _read_research_cache()
    if ticker:
        t = (ticker or "").upper().strip()
        it = d["items"].get(t)
        return {"ticker": t, "found": bool(it), "item": it}
    recent = [{"ticker": t, "generated_at": (d["items"].get(t) or {}).get("generated_at")}
              for t in d.get("order", [])]
    out = {"recent": recent}
    if history:
        out["items"] = d["items"]
    return out


@app.get("/api/research")
def get_research(ticker: str):
    """On-demand B/S/H research brief for ANY ticker (not just holdings): analyst consensus +
    SEC/GAAP fundamentals + valuation + recent news -> LLM rating/thesis/bull/bear/risks. SLOW
    (one model call, minutes on Pascal). Read-only, educational — NOT advice. Result is persisted
    to RESEARCH_CACHE_FILE (see _save_research_result) so it survives navigation/reload."""
    t = (ticker or "").upper().strip()
    if not t:
        return JSONResponse({"error": "ticker required"}, status_code=400)
    try:
        g = _grounded(t)
        val = _valuation(t) or {}
        fund = edgar.fundamentals(t)
        nm, _ = _company_identity(t, t, fund)
        news, news_meta = _news(t, nm, with_meta=True)
        llm = _llm(t, nm, None, None, g, news, fund, val)
        llm = _verify_and_apply(t, nm, llm, g, news, fund, val, force=True)   # on-demand → always verify
        ins = inst = None
        try:
            ins = _insider_signal(t)   # G15 context-only insider read (cached ~6h)
        except Exception:
            ins = None
        try:
            inst = _inst_for(t)        # G15 forward-13F watched-holders slice (from cached build)
        except Exception:
            inst = None
        # RESUME-TRACKER (2026-07-30, "last-known-good cache" Part 2): the OLDEST successful fetch
        # among the cached external inputs actually used for this brief -- fundamentals, insider
        # signal, news -- so the frontend can show "Based on data retrieved <date>" and flag it if
        # ANY component fell back to a stale/last-known-good value. Distinct from `generated_at`
        # below, which is just when the LLM synthesis ran (always "now" -- it's never cached).
        fetch_times = [m for m in (fund.get("_cache_fetched_at"), (ins or {}).get("_cache_fetched_at"),
                                    news_meta.get("fetched_at")) if m is not None]
        # 2026-08-08: same two-flag split as the daily briefs (see _do_analysis) -- "fell back to
        # data that has aged out" and "could not be retrieved at all" are different problems.
        _srcs = (("SEC fundamentals", fund.get("_cache_error"), fund.get("_cache_stale")),
                 ("insider signal", (ins or {}).get("_cache_error"), (ins or {}).get("_cache_stale")),
                 ("news", news_meta.get("error"), news_meta.get("stale")))
        _missing = sorted({n for n, err, _s in _srcs if err})
        _dated = sorted({n for n, _e, st in _srcs if st})
        result = {"ticker": t, "generated_at": int(time.time()),
                  "cache_fetched_at": min(fetch_times) if fetch_times else None,
                  "cache_stale": bool(_dated), "cache_dated": _dated,
                  "cache_rules_version": CACHE_RULES_VERSION,
                  "cache_degraded": bool(_missing), "cache_missing": _missing,
                  "grounded": g, "valuation": val, "fundamentals": fund, "llm": llm,
                  "insider": ins, "institutional": inst}
        _save_research_result(t, result)   # persist so navigating away/reloading can recover it
        return result
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
    """Queue a rebuild of the knowledge vector index (embeds new/changed docs via the LLM adapter).
    Non-blocking — runs on the serial job queue (Increment B) so it shares GPU-serialization +
    /api/jobs visibility with the other heavy jobs. Poll GET /api/knowledge for live chunk-level
    progress ({reindexing, done_chunks, total_chunks, current_file})."""
    try:
        jid, created = _enqueue("knowledge_reindex", source="api")
        return {"ok": True, "status": "queued" if created else "already queued/running",
                "job_id": jid, **knowledge.status()}
    except Exception as e:
        return JSONResponse({"error": f"reindex failed: {e}"}, status_code=500)


@app.post("/api/knowledge/delete")
async def post_knowledge_delete(req: Request):
    """RESUME-TRACKER 2026-07-24h — the new in-app removal action from the Knowledge/RAG
    DB-backed storage design: drops a document (by its content hash) + its embedded chunks from
    the DB. The source file, if still sitting in the read-only knowledge folder, is untouched —
    it will be flagged in `reimported` on whatever future reindex next notices it's still there.
    Body: {hash}."""
    try:
        body = await req.json()
    except Exception:
        body = {}
    result = knowledge.delete_doc((body or {}).get("hash"))
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return result


def _read_analysis():
    try:
        return store.read_json_strict(ANALYSIS_FILE)
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
async def post_analysis(req: Request):
    """External-writer endpoint for briefs (kept for API compatibility). Body:
    {"ticker": "...", "grounded": {...}, "llm": {...}} (one) or {"briefs": {ticker: {...}}}
    (bulk). Merges into the stored file. The internal analysis job writes briefs directly."""
    body = await req.json()
    briefs = (_read_analysis().get("briefs") or {})
    if isinstance(body.get("briefs"), dict):
        briefs.update(body["briefs"])
    elif body.get("ticker"):
        briefs[body["ticker"]] = {k: v for k, v in body.items() if k != "ticker"}
    else:
        return JSONResponse({"error": "expected {ticker,...} or {briefs:{...}}"}, status_code=400)
    out = {"updated_at": int(time.time()), "briefs": briefs}
    store.write_json(ANALYSIS_FILE, out)
    return {"ok": True, "count": len(briefs)}


# ---------------- daily analysis (Buy/Sell/Hold: grounded + LLM, side by side) ----------------
# 2026-08-06: _grounded()/_valuation() were the LAST two external fetchers still bypassing
# store.cached_fetch -- every other call site (EDGAR, Tavily, insider, 13F, valuation-percentile)
# got the last-known-good treatment on 2026-07-30 and these two were simply missed. They had no
# cache, no fallback, and swallowed every failure to None/{}, which is exactly why the boot-merge
# ciphertext bug (see _boot_merge) degraded silently for two weeks: Finnhub answered
# `{"error":"Please use an API key."}` and the app just rendered a blank ANALYST column and
# honestly-low-confidence briefs instead of surfacing an outage. Now converted.
FINNHUB_TTL = int(os.environ.get("FINNHUB_TTL", "21600"))   # 6h -- analyst consensus and the
# Basic-Financials metric block both move on a daily-at-most cadence, and the free tier is
# 60 calls/min shared across the whole book, so re-pulling per brief run is pure waste.

# 2026-08-08: `stale_after` is a SEPARATE question from `ttl` (see store.cached_fetch's header).
# ttl = how often we re-ask. stale_after = how long the answer stays true if we cannot re-ask.
# Analyst consensus and valuation multiples drift over days, not hours -- a 6h TTL miss is not a
# reason to tell the user their brief is dated.
FINNHUB_STALE_AFTER = int(os.environ.get("FINNHUB_STALE_AFTER", "172800"))   # 2d
NEWS_STALE_AFTER = int(os.environ.get("NEWS_STALE_AFTER", "86400"))          # 1d -- news is the
# one input where age genuinely invalidates the thesis, so it keeps the tightest window.
# (All windows are measured in TRADING TIME -- see market_calendar. A Friday-evening pull read on
#  Saturday is 6h old, not 15h, because no fresher value can exist while the market is shut.)

# Bumped whenever the MEANING of a brief's cache_* flags changes. Briefs are persisted to
# analysis.json and re-rendered on every page load, so a rules change silently reinterprets
# months of stored output: right after the 2026-08-08 deploy, 28 pre-existing briefs carrying the
# old any-fallback-is-stale boolean rendered under the new UI as "28 of 28 used data past its
# freshness window", which was not what the new rules would have concluded at all. The frontend
# now labels briefs written under older rules instead of misreporting them.
CACHE_RULES_VERSION = 2


def _finnhub_json(path, sym, extra=None):
    """(payload, transport_ok). The second element distinguishes "Finnhub ANSWERED, and this
    symbol genuinely has no data" from "the call FAILED" (missing/garbled key, 429, 403 on a
    premium endpoint, timeout). cached_fetch needs that distinction: an empty-but-successful
    answer for a legitimately uncovered micro-cap should be CACHED (otherwise every run re-hammers
    the free tier for a result we know will be empty), while a real outage must fall back to the
    last known good instead of overwriting it with nothing.

    Note the explicit 200-with-an-error-body check: Finnhub returns HTTP 200 and
    {"error": "Please use an API key."} for auth problems rather than a 4xx, so a naive
    `status_code < 400` test reads an auth failure as success -- which would have poisoned this
    cache with empty values the moment it was introduced."""
    if not FINNHUB:
        return None, False
    params = {"symbol": sym, "token": FINNHUB}
    if extra:
        params.update(extra)
    try:
        r = requests.get(f"https://finnhub.io/api/v1/{path}", params=params, timeout=20)
        if r.status_code >= 400:
            return None, False
        try:
            data = r.json()
        except ValueError:
            return None, False
    except Exception:
        return None, False
    if isinstance(data, dict) and data.get("error"):
        return None, False
    return data, True


def _finnhub(path, sym):
    """Back-compat thin wrapper -- callers that only want the payload (quote, peers) keep the
    original None-on-any-failure contract."""
    return _finnhub_json(path, sym)[0]


_EMPTY_GROUNDED = {"rating": None, "analyst_total": 0, "counts": {}, "target_mean": None,
                   "target_high": None, "target_low": None, "period": None}


def _grounded(sym, with_meta=False):
    """Analyst consensus + price targets from Finnhub. Disk-persisted last-known-good cache
    (2026-08-06). Return shape is UNCHANGED for every existing caller -- the cache envelope's
    bookkeeping keys are stripped before returning, deliberately: this dict is fed verbatim into
    the brief prompt via `json.dumps(grounded)`, so a stray `_ok`/`_cache_*` key would leak into
    the model's context as if it were market data. `with_meta=True` returns the
    {"fetched_at", "stale"} dict alongside, for callers that surface "data as of" to the user."""
    def _compute():
        rec, rec_ok = _finnhub_json("stock/recommendation", sym)
        pt, pt_ok = _finnhub_json("stock/price-target", sym)
        rec = rec if isinstance(rec, list) else []
        pt = pt if isinstance(pt, dict) else {}
        latest = rec[0] if rec else {}
        buys = (latest.get("strongBuy") or 0) + (latest.get("buy") or 0)
        holds = latest.get("hold") or 0
        sells = (latest.get("sell") or 0) + (latest.get("strongSell") or 0)
        total = buys + holds + sells
        rating = None
        if total:
            rating = "Buy" if buys >= holds and buys >= sells else ("Sell" if sells > buys and sells > holds else "Hold")
        return {"_ok": bool(rec_ok or pt_ok),   # price-target is premium (403 on free) -- either
                                                # endpoint answering counts as "Finnhub is up"
                "data": {"rating": rating, "analyst_total": total,
                         "counts": {k: latest.get(k) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")},
                         "target_mean": pt.get("targetMean"), "target_high": pt.get("targetHigh"),
                         "target_low": pt.get("targetLow"), "period": latest.get("period")}}

    env, meta = store.cached_fetch("finnhub_grounded", sym, FINNHUB_TTL, _compute,
                                   is_ok=lambda e: bool(e.get("_ok")),
                                   stale_after=FINNHUB_STALE_AFTER)
    g = dict(env.get("data") or _EMPTY_GROUNDED)
    return (g, meta) if with_meta else g


# 2026-10-19: Tavily failures used to be INVISIBLE. mcp-tavily's unpinned `mcp` dependency resolved
# to 2.x (which renamed McpError -> MCPError), so mcpo's Tavily server died on import and mcpo
# registered no /tavily route: every call here got a 404 for ~6 weeks, _compute() swallowed it as
# []/"" and cached_fetch quietly served the Sept 3 copy. These helpers record WHY each call failed
# so /api/news-status, the discovery report and the cockpit can say so. In-memory (per process).
_NEWS_DIAG = {"last_ok": None, "last_fail": None, "last_fail_detail": None,
              "ok_count": 0, "fail_count": 0, "consecutive_failures": 0}
_NEWS_DIAG_LOCK = threading.Lock()
_NEWS_LOG_LAST = {"detail": None, "at": 0.0}


def _news_note(ok, detail=None, where=""):
    now = time.time()
    with _NEWS_DIAG_LOCK:
        if ok:
            _NEWS_DIAG["last_ok"] = now
            _NEWS_DIAG["ok_count"] += 1
            _NEWS_DIAG["consecutive_failures"] = 0
            return
        _NEWS_DIAG["last_fail"] = now
        _NEWS_DIAG["fail_count"] += 1
        _NEWS_DIAG["consecutive_failures"] += 1
        _NEWS_DIAG["last_fail_detail"] = detail
        # one log line per distinct reason per 10 minutes -- a dead feed makes dozens of calls a run
        if detail != _NEWS_LOG_LAST["detail"] or now - _NEWS_LOG_LAST["at"] > 600:
            _NEWS_LOG_LAST.update(detail=detail, at=now)
            print(f"[news] Tavily call failed ({where}): {detail}", flush=True)


def _news_http_detail(r):
    d = f"HTTP {r.status_code}"
    if r.status_code == 404:
        d += (" from mcpo /tavily/tavily_news_search -- the mcpo 'tavily' MCP server is not "
              "registered (it likely failed to start; see `docker logs fiduciary-mcpo` for a Traceback)")
    elif r.status_code in (401, 403):
        d += " -- mcpo rejected MCPO_API_KEY"
    else:
        d += ": " + (r.text or "")[:160].replace("\n", " ")
    return d


def _news(sym, name, with_meta=False):
    """Raw Tavily news blob (truncated) for the LLM's research-brief prompt context -- a SEPARATE
    direct call from _tavily_results()/_company_news() (different endpoint usage: raw text, not
    parsed articles). Disk-persisted last-known-good cache (2026-07-30) -- this path had NO
    caching at all before, the same gap the "last-known-good cache" backlog item flagged for
    _tavily_results(), just not separately named since this call site wasn't inventoried at the
    time. `with_meta=True` also returns {"fetched_at", "stale"} for a caller (get_research) that
    surfaces "data as of" to the user."""
    query = f"{sym} {name} stock"

    def _compute():
        try:
            r = requests.post(f"{MCPO}/tavily/tavily_news_search",
                              headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
                              json={"query": query, "days": 7, "max_results": 5}, timeout=45)
            if r.status_code >= 400:
                _news_note(False, _news_http_detail(r), "brief-news")
                return ""
            _news_note(True)
            return r.text[:2500]
        except Exception as e:
            _news_note(False, f"{type(e).__name__}: {str(e)[:160]}", "brief-news")
            return ""

    # NOTE the failure signal here is an EMPTY body, which only happens on an HTTP error or an
    # exception -- a 200 with no articles still returns a JSON envelope, so "quiet week" already
    # scores as the success it is. Kept as a bare string rather than an envelope dict on purpose:
    # changing the cached shape would orphan every existing disk entry for this source.
    text, meta = store.cached_fetch("tavily_news_raw", query, TAVILY_CACHE_TTL, _compute,
                                    is_ok=lambda r: bool(r), stale_after=NEWS_STALE_AFTER)
    return (text, meta) if with_meta else text


def _valuation(sym, with_meta=False):
    """Valuation multiples from Finnhub Basic Financials (/stock/metric). Read-only.
    Returns None if no data (e.g. foreign listing not in Finnhub, or no key) -- contract UNCHANGED
    for the ~12 existing callers, all of which do `_valuation(x) or {}`. Disk-persisted
    last-known-good cache (2026-08-06); same envelope-and-strip approach as _grounded() above and
    for the same reason (this dict is json.dumps'd straight into the brief prompt)."""
    def _compute():
        data, ok = _finnhub_json("stock/metric", sym, {"metric": "all"})
        if not ok:
            return {"_ok": False, "data": None}      # outage -> keep whatever we last had
        m = (data or {}).get("metric") or {}
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
        # A genuine "Finnhub has nothing for this symbol" (every field None -- normal for OTC
        # foreign listings) is a SUCCESS, cached as None so we stop re-asking every run.
        return {"_ok": True, "data": out if any(v is not None for v in out.values()) else None}

    env, meta = store.cached_fetch("finnhub_valuation", sym, FINNHUB_TTL, _compute,
                                   is_ok=lambda e: bool(e.get("_ok")),
                                   stale_after=FINNHUB_STALE_AFTER)
    v = env.get("data")
    v = dict(v) if isinstance(v, dict) else None
    return (v, meta) if with_meta else v


def _price(sym):
    """Current share price from Finnhub /quote (free)."""
    q = _finnhub("quote", sym)
    c = q.get("c") if isinstance(q, dict) else None
    return c if c else None


INSIDER_TTL = int(os.environ.get("INSIDER_TTL", "21600"))   # cache the insider signal ~6h
# 2026-07-30: valuation-percentile/insider-signal caching moved onto store.cached_fetch() --
# see _valuation_percentile()/_insider_signal() -- no more bare module-level dicts here.


def _valuation_percentile(sym):
    """Sector-relative valuation (Part-4): where the ticker's forward P/E sits vs its Finnhub
    peer set — a CHEAPNESS percentile (0 = cheapest of the group, 100 = richest). A relative-value
    read that own-multiple-in-a-vacuum misses; a rich reading is a candidate EXIT trigger. Cached
    ~1 day, disk-persisted via store.cached_fetch (2026-07-30 -- survives a restart). None if
    peers/valuation are unavailable (foreign, funds, no key)."""
    def _compute():
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
                return {"forward_pe": round(mine, 1), "peer_median_pe": round(med, 1),
                       "peer_count": len(pes), "cheapness_percentile": pct, "verdict": verdict}
        except Exception:
            pass
        return None

    res, meta = store.cached_fetch("valuation_percentile", sym, 86400, _compute,
                                   is_ok=lambda r: r is not None)
    if res is not None:
        res = dict(res)
        res["_cache_fetched_at"] = meta.get("fetched_at")
        res["_cache_stale"] = meta.get("stale", False)
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
    Context-only. Fetches the issuer's Form 4s (EDGAR), parses each, and aggregates. Cached ~6h,
    disk-persisted via store.cached_fetch (2026-07-30 -- survives a restart). None-ish
    {available:false} for foreign/funds/no filings."""
    t = (sym or "").upper().strip()
    if not t:
        return None
    days = int(_knob("insider_lookback_days"))

    def _compute():
        try:
            raw = edgar.recent_form4(t, days=days, limit=int(_knob("insider_max_filings")))
            parsed = [insider.parse_form4(f.get("xml")) for f in raw]
            parsed = [p for p in parsed if p]
            if parsed:
                r = insider.aggregate(parsed, t, days)
                r["available"] = True
                return r
            return {"ticker": t, "available": False,
                    "reason": "no Form 4 filings (foreign listing, fund/ETF, or none in window)"}
        except Exception as e:
            return {"ticker": t, "available": False, "error": True,
                    "reason": f"insider fetch error: {e}"}

    res, meta = store.cached_fetch(
        "insider_signal", t, INSIDER_TTL, _compute,
        is_ok=lambda r: bool(r.get("available")),
        # "this issuer filed no Form 4s in the window" is the single most common outcome and is a
        # real answer -- most of the book is funds and ADRs that never will. Not an outage.
        is_empty=lambda r: bool(isinstance(r, dict) and not r.get("available") and not r.get("error")),
        stale_after=int(os.environ.get("INSIDER_STALE_AFTER", "604800")))   # 7d
    res = dict(res)
    res["_cache_fetched_at"] = meta.get("fetched_at")
    res["_cache_stale"] = bool(meta.get("stale"))
    res["_cache_error"] = bool(meta.get("error"))
    return res


@app.get("/api/insider")
def get_insider(ticker: str):
    """Net open-market insider buy/sell signal for one ticker from recent SEC Form 4 filings (G15).
    CONTEXT ONLY — cluster buying is the higher-signal event; selling is noisy. Not advice.
    P1 update: FINRA consolidated short interest rides along when API-console credentials are
    configured (secrets FINRA_CLIENT_ID / FINRA_CLIENT_SECRET); 13F ownership stays with inst13f."""
    t = (ticker or "").upper().strip()
    if not t:
        return JSONResponse({"error": "ticker required"}, status_code=400)
    res = _insider_signal(t) or {"ticker": t, "available": False, "reason": "no data"}
    try:
        if shortint.configured():
            res["short_interest"] = shortint.short_interest(t)
        else:
            res["short_interest"] = {"configured": False,
                                     "note": "Free FINRA API-console credentials unlock short "
                                             "interest (secrets: FINRA_CLIENT_ID/SECRET)."}
    except Exception:
        pass
    return res


@app.get("/api/edgar-status")
def get_edgar_status():
    """Health of the SEC EDGAR entry point (2026-08-08).

    Added after a whole-book diagnosis was impossible from the outside: every EDGAR-backed
    endpoint reported 'no SEC CIK (foreign-listed, fund/ETF, or not SEC-registered)' for names
    like PLTR, because get_cik() swallowed the ticker-map fetch exception and returned None. That
    string is indistinguishable from the truth, so a total outage looked like 28 unremarkable
    classifications. This endpoint answers 'can we reach SEC at all' in one call, and probes a
    known-good large-cap so a stale/short map is visible too."""
    st = edgar.cik_map_status()
    probe = None
    if st.get("ok"):
        try:
            probe = {"ticker": "AAPL", "cik": edgar.get_cik("AAPL")}
        except Exception as e:
            probe = {"ticker": "AAPL", "error": f"{type(e).__name__}: {e}"}
    raw = os.environ.get("SEC_USER_AGENT")
    return {"cik_map": st, "probe": probe,
            "user_agent": edgar.SEC_UA,
            # The env var being PRESENT BUT EMPTY is its own failure mode and reads identically to
            # "unset" in most diagnostics -- call it out explicitly. This is what actually happened.
            "env_var": {"present": raw is not None, "blank": (raw is not None and not raw.strip())},
            "user_agent_looks_like_placeholder": edgar.ua_is_placeholder(),
            "hint": "SEC returns 403 for generic or empty User-Agents. Set SEC_USER_AGENT in .env "
                    "to a real contact string (e.g. 'Fiduciary yourname@yourdomain.com'), then "
                    "`docker compose up -d` to re-inject it. A blank value in .env is NOT the same "
                    "as omitting it: compose passes it through as an empty string."}


# ============ G15: institutional 13F forward whale-tracking (context-only) ============
# Of the managers you WATCH, which hold each of your names, and did they add/trim/exit last quarter.
# Free SEC EDGAR; long-only, ≤45-day-lagged — a conviction/turnover context lens, never a signal.
MANAGERS_FILE = os.environ.get("MANAGERS_FILE", "/data/managers.json")
INSTITUTIONAL_FILE = os.environ.get("INSTITUTIONAL_FILE", "/data/institutional.json")
INSTITUTIONAL_TTL = int(os.environ.get("INSTITUTIONAL_TTL", "259200"))   # 13F is quarterly → ~3-day cache
INST_MGR_TTL = int(os.environ.get("INST_MGR_TTL", "604800"))             # per-manager filings ~7 days
_inst_lock = threading.Lock()
# 2026-07-30: per-manager 13F caching moved onto store.cached_fetch() -- see _manager_13f().


def _read_managers():
    """Watched-manager registry {cik: label}. Seeds a small default set on first run (editable)."""
    try:
        m = store.read_json_strict(MANAGERS_FILE)
        if isinstance(m, dict) and m:
            return {str(k).zfill(10): v for k, v in m.items()}
    except Exception:
        pass
    return dict(inst13f.DEFAULT_MANAGERS)


def _write_managers(reg):
    store.write_json(MANAGERS_FILE, reg)


def _holdings_for_inst():
    """The user's single-name equity holdings [{ticker, name}] (funds/ETFs/cash excluded — 13F is a
    single-name long-equity report; funds have their own N-PORT look-through)."""
    out = []
    for c in _data().get("consolidated", []):
        if c.get("ticker") and (c.get("type") or "") not in ("Cash", "Mutual Fund", "ETF"):
            out.append({"ticker": c["ticker"], "name": c.get("name") or c["ticker"]})
    return out


def _manager_13f(cik):
    """Latest two 13F-HR information tables for a manager CIK (cached ~7d, disk-persisted via
    store.cached_fetch, 2026-07-30 -- survives a restart, unlike the plain in-process dict this
    used before). Returns (filings_list, meta) -- meta = {"fetched_at", "stale"}, see
    store.cached_fetch()."""
    return store.cached_fetch("edgar_manager_13f", cik, INST_MGR_TTL,
                              lambda: edgar.recent_13f(cik, n=2), is_ok=lambda r: bool(r))


def _institutional():
    holdings = _holdings_for_inst()
    mgrs = _read_managers()
    views = []
    oldest_fetch, any_stale = None, False
    for cik, label in mgrs.items():
        try:
            fils, meta = _manager_13f(cik)
        except Exception:
            fils, meta = [], {"fetched_at": None, "stale": True}
        fa = meta.get("fetched_at")
        if fa is not None and (oldest_fetch is None or fa < oldest_fetch):
            oldest_fetch = fa
        any_stale = any_stale or bool(meta.get("stale"))
        if not fils:
            views.append({"manager": label, "cik": cik, "as_of": None, "matches": [],
                          "unavailable": True})
            continue
        cur = fils[0].get("xml")
        prev = fils[1].get("xml") if len(fils) > 1 else None
        mv = inst13f.manager_holdings_of(cur, prev, holdings, manager=label)
        mv["cik"] = cik
        mv["as_of"] = fils[0].get("date")
        mv["prev_as_of"] = fils[1].get("date") if len(fils) > 1 else None
        views.append(mv)
    by_holding = inst13f.aggregate_holdings(views, holdings)
    return {"status": "ready", "generated_at": int(time.time()),
            # RESUME-TRACKER (2026-07-30, "last-known-good cache" Part 2): when this was actually
            # fetched from EDGAR (oldest across the watched managers, so ANY stale manager flags
            # the whole panel), as opposed to `generated_at` above, which is just when this
            # response was assembled -- the frontend's "Based on data retrieved ..." label reads
            # this pair, not generated_at.
            "cache_fetched_at": oldest_fetch, "cache_stale": any_stale,
            "managers": [{"cik": c, "label": l} for c, l in mgrs.items()],
            "manager_views": views, "by_holding": by_holding,
            "note": "Forward 13F whale-tracking: of the managers you watch, who holds each of your "
                    "names and did they add/trim/exit last quarter. 13F is long-only and filed up to "
                    "45 days after quarter-end — a conviction/turnover CONTEXT lens, not a live "
                    "signal, and never a standalone buy/sell decision."}


def _read_institutional():
    try:
        return store.read_json_strict(INSTITUTIONAL_FILE)
    except Exception:
        return None


def _do_institutional():
    if not _inst_lock.acquire(blocking=False):
        return
    try:
        res = _institutional()
        store.write_json(INSTITUTIONAL_FILE, res)
    except Exception:
        pass
    finally:
        _inst_lock.release()


def _inst_for(sym):
    """Per-holding smart-money slice from the cached institutional build (or None)."""
    d = _read_institutional() or {}
    return (d.get("by_holding") or {}).get((sym or "").upper())


@app.get("/api/institutional")
def get_institutional():
    """G15 forward 13F whale-tracking, CACHED (an EDGAR pull per watched manager runs in the
    background; 13F is quarterly so the cache is long-lived). Context-only, never advice."""
    cached = _read_institutional()
    fresh = bool(cached) and (time.time() - (cached.get("generated_at") or 0) < INSTITUTIONAL_TTL)
    if not fresh and not _inst_lock.locked():
        threading.Thread(target=_do_institutional, daemon=True).start()
    if cached:
        return {**cached, "cached": True, "refreshing": (not fresh)}
    return {"status": "computing", "computing": True,
            "note": "First 13F build is running (one EDGAR pull per watched manager) — refresh shortly."}


@app.post("/api/run-institutional")
def run_institutional():
    threading.Thread(target=_do_institutional, daemon=True).start()
    return {"status": "started"}


@app.get("/api/managers")
def get_managers():
    """The watched-manager registry {cik: label} used by the 13F tracker."""
    return {"managers": _read_managers(), "defaults": inst13f.DEFAULT_MANAGERS,
            "note": "13F filer CIKs of managers you want to track. Defaults are examples, not "
                    "endorsements — edit to the investors whose conviction is relevant to your names."}


@app.post("/api/managers")
async def post_managers(req: Request):
    """Add/rename a watched manager. Body: {cik, label}. cik = the manager's SEC 13F filer number."""
    body = await req.json()
    cik = re.sub(r"[^0-9]", "", str(body.get("cik") or ""))
    if not cik:
        return JSONResponse({"error": "numeric cik required"}, status_code=400)
    reg = _read_managers()
    reg[cik.zfill(10)] = str(body.get("label") or cik).strip()
    _write_managers(reg)
    return {"ok": True, "managers": reg}


@app.post("/api/managers/delete")
async def delete_manager(req: Request):
    body = await req.json()
    cik = str(body.get("cik") or "").zfill(10)
    reg = _read_managers()
    if cik in reg:
        reg.pop(cik)
        _write_managers(reg)
    return {"ok": True, "managers": reg}


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
    """Call the configured LLM backend, returning the raw text. Disables the reasoning channel so
    the JSON answer lands in `content` (gpt-oss & other reasoning models otherwise return nothing
    usable there with everything in `thinking`). Falls back gracefully for backends that reject
    the `think` field. RESUME-TRACKER 2026-07-24: routed through llm._llm_chat (LLM-agnostic
    adapter) — dialect-switchable via LLM_API_DIALECT — instead of a raw requests.post to Ollama's
    /api/generate; the retry-without-`think` behavior now lives in llm._ollama_chat."""
    # Qwen3 family: soft-switch off the <think> channel so the JSON isn't buried in reasoning
    # (unless ANALYSIS_THINK is enabled — then let it reason for deeper, better-calibrated briefs).
    if "qwen3" in ANALYSIS_MODEL.lower() and not ANALYSIS_THINK:
        prompt = prompt + "\n/no_think"
    try:
        with _gpu_slot():   # serialize qwen-27b generation across all features
            res = llm._llm_chat([{"role": "user", "content": prompt}], _text_model(),
                                format_json=True, think=ANALYSIS_THINK, temperature=0.3,
                                num_ctx=ANALYSIS_NUM_CTX, keep_alive=LLM_KEEP_ALIVE, timeout=LLM_TIMEOUT)
    except Exception as e:
        return None, f"request failed: {e}"
    if not res.ok:
        return None, res.error
    raw = (res.content.strip() or res.thinking.strip())
    return raw, None


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


_NAME_JUNK_RE = re.compile(r"\s+(isin|sedol|cusip|figi)\b.*$", re.I)
_NAME_STOP = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "llc",
              "lp", "the", "de", "nv", "sa", "ag", "holdings", "holding", "group", "new"}


def _clean_name(n):
    """Strip brokerage/aggregator identifier junk (' Isin CA92... Sedol BL9...') and SEC state
    suffixes ('/DE', '/NEW/') so a company name reads as a name."""
    n = _NAME_JUNK_RE.sub("", (n or "").strip())
    return re.sub(r"\s*/[A-Za-z]{1,5}/?$", "", n).strip()


def _name_tokens(n):
    return {w for w in re.findall(r"[a-z0-9]+", (n or "").lower()) if w not in _NAME_STOP}


def _company_identity(sym, name, fund):
    """(display_name, note) for the LLM prompt/news query. The holdings source's security name can
    be WRONG for a reused ticker (Monarch listed VOYG as 'Voyager Digital Ltd' -- the old Canadian
    crypto broker -- while the SEC registrant for VOYG is Voyager Technologies, a space company),
    and the on-demand path used to pass the bare ticker, leaving the model to guess from memory.
    The SEC entity name is keyed by the ticker->CIK lookup the fundamentals already use, so it is
    authoritative when present; the holdings name is only a fallback, and a disagreement is flagged
    to the model instead of silently picking a side."""
    held = _clean_name(name)
    if held.upper() == (sym or "").upper():
        held = ""
    sec = _clean_name((fund or {}).get("entity")) if (fund or {}).get("available") else ""
    if not sec:
        return (held or sym), ""
    a, b = _name_tokens(sec), _name_tokens(held)
    if held and a and b and not (a <= b or b <= a):
        return sec, (f"IDENTITY: {sym} is {sec} (the SEC registrant for this ticker). The holdings record "
                     f"labels it '{held}', which is a DIFFERENT company that may have shared or reused "
                     f"this symbol -- do NOT use that company's business, news or history; describe "
                     f"{sec} only, using the data below.\n")
    return sec, ""


def _llm(sym, name, weight, gain_pct, grounded, news, fundamentals=None, valuation=None):
    name, _identity_note = _company_identity(sym, name, fundamentals)
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
        f"{_identity_note}"
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
            lines = store.read_lines_strict(VERIFIER_LOG_FILE)
        except Exception:
            pass
        lines.append(json.dumps(row))
        lines = lines[-VERIFIER_LOG_MAX:]
        store.write_lines(VERIFIER_LOG_FILE, lines)
    except Exception:
        pass


def _verifier_summary(recent=10):
    """Aggregate judge behavior from the verdict log: volume, average groundedness, downgrade
    rate, failure rate — the 'is the judge itself sane?' dashboard (G13b)."""
    try:
        rows = [json.loads(x) for x in store.read_lines_strict(VERIFIER_LOG_FILE) if x.strip()]
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


def _cit_brief(c):
    """Static honest brief for a detected CIT (RESUME-TRACKER 2026-07-18k) — replaces the
    stock-style Buy/Sell/Hold pipeline entirely: no Finnhub grounded-rating/valuation calls, no
    EDGAR fundamentals lookup (would always fail — CITs aren't SEC filers), no Tavily news search
    (would just waste a call on a garbage "TICKER NAME stock" query). Reuses the EXACT same
    grounded/fundamentals/valuation/llm shapes the real pipeline produces, so the existing frontend
    rendering (badge() -> '—' on a None rating, fundLine() skipping when fundamentals.available is
    False) already displays it correctly; only a small CIT label was added to the brief row."""
    note = ("Collective Investment Trust — not publicly traded, no ticker/CUSIP, no SEC filings; "
            "NAV and performance are recordkeeper-reported only. Decision-support here is limited "
            "to what your plan provider reports (expense ratio, glidepath if target-date, category).")
    return {
        "name": c["name"],
        "grounded": {"rating": None, "analyst_total": 0, "counts": {}, "target_mean": None,
                     "target_high": None, "target_low": None, "period": None},
        "fundamentals": {"available": False, "cit": True},
        "valuation": None,
        "llm": {"rating": None, "confidence": None, "thesis": note, "cit": True},
        "is_cit": True,
        "updated_at": int(time.time()),
    }


def _do_analysis(limit, tickers=None):
    """`tickers`, if given, scopes this run to just that ticker subset (OPP-6 post-earnings
    refresh) instead of the whole book / a `limit`-count slice. IMPORTANT: the stale-brief prune
    below must still be based on ALL currently-held tickers, not just this run's scope — otherwise
    a 2-ticker earnings-triggered run would wipe out every other ticker's cached brief on write."""
    if not _run_lock.acquire(blocking=False):
        return
    try:
        all_secs = [c for c in _data()["consolidated"] if c["ticker"]]
        secs = all_secs
        if tickers:
            wanted = set(tickers)
            secs = [c for c in all_secs if c["ticker"] in wanted]
        elif limit:
            secs = secs[:limit]
        _run_state.update(status="running", analyzed=0, total=len(secs), started=int(time.time()))
        current = {c["ticker"] for c in all_secs}   # ALL held tickers — only drops briefs for
                                                      # tickers no longer held at all (e.g. sold),
                                                      # never for ones merely outside this run's scope
        briefs = {k: v for k, v in (_read_analysis().get("briefs") or {}).items() if k in current}
        for c in secs:
            if c.get("is_cit"):
                # Detected CIT (RESUME-TRACKER 2026-07-18k): skip the entire stock-analytics
                # pipeline (Finnhub/EDGAR/Tavily calls that would either fail or mislead) and
                # write a static honest note instead. Not counted in _log_rating's AI scorecard —
                # there is no rating to score.
                briefs[c["ticker"]] = _cit_brief(c)
                _run_state["analyzed"] += 1
                out = {"updated_at": int(time.time()), "briefs": briefs}
                store.write_json(ANALYSIS_FILE, out)
                continue
            g, g_meta = _grounded(c["ticker"], with_meta=True)
            fund = edgar.fundamentals(c["ticker"])
            nm, _ = _company_identity(c["ticker"], c["name"], fund)   # SEC registrant beats a wrong holdings label
            news, news_meta = _news(c["ticker"], nm, with_meta=True)
            val, val_meta = _valuation(c["ticker"], with_meta=True)
            llm = _llm(c["ticker"], c["name"], c.get("weight_pct"), c.get("gain_pct"), g, news, fund, val)
            llm = _verify_and_apply(c["ticker"], nm, llm, g, news, fund, val)  # Buy/Sell only (not Holds)
            # 2026-08-06: carry the same "data as of" provenance the on-demand /api/research path
            # has always recorded (see get_research) onto the DAILY briefs too. Until now
            # _do_analysis called _news() WITHOUT with_meta, so a stored brief had no way to say
            # its news was a month old -- which is precisely the blind spot the 2026-07-30
            # last-known-good work created: a Tavily outage stopped producing an empty "recent
            # news: NONE" prompt and started producing a confident thesis built on stale articles,
            # with nothing on screen to flag it. Oldest successful fetch across the cached inputs
            # actually used, plus a stale flag if ANY of them fell back.
            #
            # 2026-08-08: split into TWO flags, because collapsing them made the banner useless.
            #   cache_stale    -- an input fell back to a copy older than that source's own
            #                     freshness window (store.cached_fetch's stale_after). The thesis
            #                     rests on data that has actually gone out of date.
            #   cache_degraded -- an input could not be retrieved AT ALL and nothing was cached to
            #                     fall back on, so the thesis was built WITHOUT it. Strictly worse
            #                     than stale and previously indistinguishable from it.
            # `cache_missing` names the offending sources so the banner can say which one broke
            # instead of listing all four as suspects.
            _metas = [("analyst consensus", g_meta), ("news", news_meta), ("valuation", val_meta),
                      ("SEC fundamentals",
                       {"fetched_at": fund.get("_cache_fetched_at"), "stale": fund.get("_cache_stale"),
                        "error": fund.get("_cache_error")})]
            _times = [m.get("fetched_at") for _n, m in _metas if m.get("fetched_at") is not None]
            _missing = sorted({n for n, m in _metas if m.get("error")})
            _dated = sorted({n for n, m in _metas if m.get("stale")})
            # RESUME-TRACKER 2026-07-11 queued follow-up (1), built 2026-08-06: surface the 13F
            # WATCHED-HOLDERS line on the main RESEARCH -> BRIEFS tab. _inst_for() is a pure
            # dictionary lookup into the already-built institutional cache (_read_institutional) --
            # NO network call and NO EDGAR pull happens here, which is why this can ride along in
            # the nightly loop without slowing it down or spending an API budget. Returns None when
            # the 13F build hasn't run yet or no watched manager holds the name; the frontend just
            # renders nothing in that case, same as it already does on the on-demand research panel.
            try:
                _inst = _inst_for(c["ticker"])
            except Exception:
                _inst = None      # never let a cache-shape surprise kill the whole nightly run
            briefs[c["ticker"]] = {"name": nm, "grounded": g, "fundamentals": fund,
                                   "institutional": _inst,
                                   "cache_fetched_at": min(_times) if _times else None,
                                   "cache_stale": bool(_dated),
                                   "cache_dated": _dated,
                                   "cache_degraded": bool(_missing),
                                   "cache_missing": _missing,
                                   "cache_rules_version": CACHE_RULES_VERSION,
                                   "valuation": val, "llm": llm, "updated_at": int(time.time())}
            _log_rating(c["ticker"], llm)   # record rating+price for the AI scorecard
            _run_state["analyzed"] += 1
            out = {"updated_at": int(time.time()), "briefs": briefs}
            store.write_json(ANALYSIS_FILE, out)   # write incrementally
        _run_state["status"] = "idle"
    finally:
        _run_lock.release()


def _do_earnings_brief():
    """OPP-6 event-driven research: refresh ONLY the briefs for tickers whose earnings print is
    freshly out (see _earnings_due_tickers), instead of waiting for the next 18:00 daily cron —
    the desk reacts to the tape instead of the clock. No-ops if nothing's actually due, so a
    manual trigger (POST /api/run-earnings-brief) is always safe to call."""
    due = _earnings_due_tickers()
    if not due:
        return
    _do_analysis(0, tickers=due)
    _mark_earnings_triggered(due)


@app.post("/api/run-earnings-brief")
def run_earnings_brief():
    """Manually force the post-earnings brief check (normally event-driven — the scheduler checks
    once daily at 08:15 local and only enqueues when something's actually due)."""
    jid, created = _enqueue("earnings_brief", source="api")
    return {"ok": True, "status": "queued" if created else "already queued/running",
            "job_id": jid, "due_now": _earnings_due_tickers()}


@app.post("/api/run-analysis")
def run_analysis(limit: int = 0):
    """Trigger the daily research run. The internal scheduler enqueues this daily; you can
    also POST here on demand. Both funnel through the serial job queue (deduped)."""
    if not FINNHUB:
        return JSONResponse({"error": "FINNHUB_API_KEY not set"}, status_code=400)
    jid, created = _enqueue("analysis", source="api")   # serial queue + dedup (Increment B)
    return {"ok": True, "status": "queued" if created else "already queued/running",
            "job_id": jid, **_run_state}


@app.get("/api/run-analysis/status")
def run_status():
    return _run_state


# ---------------- Rebalancing / Drift Monitor (proactive, read-only, suggestions only) ----------------
def _read_policy():
    """Investment policy: target allocation by asset type, per-position cap, drift band.
    Set it via POST /api/policy. Missing keys fall back to defaults."""
    try:
        return {**DEFAULT_POLICY, **(store.read_json_strict(POLICY_FILE) or {})}
    except Exception:
        return dict(DEFAULT_POLICY)


@app.get("/api/policy")
def get_policy():
    return _read_policy()


@app.post("/api/policy")
async def post_policy(body: PolicyUpdate, request: Request):
    """Set the investment policy (local config, not a money action — editable from the dashboard).
    Body may include targets_by_type (e.g. {"Stock":60,"Mutual Fund":35,"Cash":5}), max_position_pct,
    drift_threshold_pct, and position_caps (per-ticker overrides, e.g. {"PLTR":40,"RKLB":40}).
    Pydantic-validated (2026-07-24): percentages must be 0-100, maps must have numeric values."""
    updates = body.model_dump(exclude_unset=True)
    old = _read_policy()
    pol = {**old, **{k: v for k, v in updates.items() if k in DEFAULT_POLICY}}
    store.write_json(POLICY_FILE, pol)
    # SEC-1 config.changed: record only the keys that actually changed (old->new), encrypted at rest.
    changed = {k: {"old": old.get(k), "new": pol.get(k)} for k in updates if old.get(k) != pol.get(k)}
    if changed:
        audit.emit("config.changed", request=request, actor=request.session.get("uid"),
                   resource="/api/policy", detail={"changed": changed})
    return {"ok": True, "policy": pol}


_BOND_KW = ("bond", "treasury", "fixed income", "aggregate bond", "govt", "municipal", " muni",
            "tips", "income fund", "core plus", "intermediate-term")
_INTL_KW = ("international", " intl", "ex-us", "ex us", "world ex", "developed markets", "eafe",
            "emerging market", "foreign", "global equity", "europe", "pacific", "japan", "china")
_BOND_TK = {"BND", "AGG", "BNDX", "VGIT", "VCIT", "TLT", "IEF", "MUB", "SCHZ", "FXNAX", "VBTLX", "VTEB"}
_INTL_TK = {"VXUS", "VEU", "VEA", "VWO", "IXUS", "EFA", "EEM", "SCHF", "SCHE", "VT", "ACWI", "VYMI", "VSS"}


def _sleeve(c):
    """Classify a holding into an IPS asset-CLASS sleeve (US Equity / Intl Equity / Bond / Cash / Other).
    Heuristic (type + ticker map + name keywords + HQ country) — a US-listed equity/fund defaults to
    US Equity unless flagged bond/international. Direct foreign stocks (e.g. FGPHF / First Graphene,
    an ASX-listed Australian company trading OTC in the US) don't match the fund-oriented ticker/name
    lists above, since a US OTC ticker and an English company name give no textual signal of the
    issuer's home country. Two additional checks catch those: (1) policy.intl_tickers, a manual
    override for names Finnhub covers poorly (thin-float OTC/ADR), and (2) the Finnhub company
    profile's `country` field (HQ country, reused from the /api/exposure sector lookup and cached
    ~1 day) — any direct stock whose HQ isn't the US is Intl Equity. Fund look-through could refine
    this further for fund-of-fund international exposure."""
    typ = c.get("type") or ""
    if typ == "Cash":
        return "Cash"
    tkr = (c.get("ticker") or "").upper()
    nm = " " + (c.get("name") or "").lower()
    if tkr in _BOND_TK or any(k in nm for k in _BOND_KW):
        return "Bond"
    if tkr in _INTL_TK or any(k in nm for k in _INTL_KW):
        return "Intl Equity"
    if typ == "Stock":
        manual_intl = (_read_policy() or {}).get("intl_tickers") or {}
        if manual_intl.get(tkr):
            return "Intl Equity"
        country = ((_profile(tkr)[0] or {}).get("country") or "").upper()
        if country and country != "US":
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
        store.write_json(INSIGHTS_FILE, out)
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


@app.post("/api/run-rebalance")
def run_rebalance_ep():
    """Manual trigger for the 'Rebalance / drift refresh' scheduled job (RESUME-TRACKER: Task
    Schedule RUN NOW buttons — this kind previously had no manual endpoint, only the internal
    cron scheduler could enqueue it via _enqueue(); GET /api/rebalance already recomputes fresh
    on every call, so this mainly gives it job-queue visibility (/api/jobs) + dedup parity with
    the other 5 scheduled kinds. Token-guarded like every other /api/run-* endpoint."""
    jid, created = _enqueue("rebalance", source="api")
    return {"ok": True, "job_id": jid, "created": created}


# ---------------- Retirement / FIRE Planner (proactive, read-only, informational) ----------------
def _read_retire():
    try:
        return {**DEFAULT_RETIRE, **(store.read_json_strict(RETIRE_FILE) or {})}
    except Exception:
        return dict(DEFAULT_RETIRE)


def _derive_age(birth_year, stored_age=None, ref_year=None):
    """Age as of the end of `ref_year` (defaults to the current year), derived from birth year.
    2026-07-23: replaces manually-entered current_age/spouse_age as the single source of truth,
    per Jason's ask — but only after confirming year-only precision is actually sufficient for
    every place this app uses age, not assumed. Checked directly against the code: every age-gated
    rule here (tax.py standard_deduction's 65+ addon; benefits.py's 401(k) 50+ catch-up, 60-63
    super-catch-up, HSA 55+ catch-up, IRA 50+ catch-up) is a plain `age >= N` integer comparison —
    which matches the IRS's OWN convention for all of these (each is defined by statute as
    "attained by the end of the year," not by exact birth date), so year-only math is not an
    approximation here, it's the correct test. SS claim age is unaffected (already a separate,
    directly-entered decimal field, never derived from birth year).
    Falls back to `stored_age` — the old manually-entered value, if an existing config file still
    has one — only when birth_year isn't set yet, so upgrading to this doesn't silently blank out
    a previously-working age for an install that hasn't entered a birth year yet."""
    if birth_year:
        try:
            return (ref_year or datetime.date.today().year) - int(birth_year)
        except (TypeError, ValueError):
            pass
    return stored_age


_STATE_ADDR_RE = re.compile(r"\b([A-Z]{2})\s+\d{5}(?:-\d{4})?\b")


def _infer_state_from_accounts(d):
    """Best-effort auto-detect (RESUME-TRACKER item 3c): scan real-estate account NAMES for a
    trailing '<City> <ST> <ZIP>' pattern (Monarch's Zillow-valued home account is named this
    way, e.g. '8660 7th St Frisco TX 75034') and take the largest such account's state as the
    default residence. Silent no-op (returns None) on anything unexpected — this is a
    convenience default, never the only path; state_of_residence in the config always wins."""
    try:
        re_accts = [a for a in (d or {}).get("accounts", [])
                    if (a.get("type") or "").strip().lower() in ("real estate", "real_estate", "realestate")
                    and (a.get("balance") or 0) > 0]
        re_accts.sort(key=lambda a: -(a.get("balance") or 0))
        for a in re_accts:
            m = _STATE_ADDR_RE.search(a.get("name") or "")
            if m and m.group(1) in states.STATE_NAMES:
                return m.group(1)
    except Exception:
        pass
    return None


def _state_of_residence_info(cfg=None, d=None):
    """Resolve the household's state of residence AND how confident we are in it: explicit
    config setting ("explicit") > auto-detected from the home address ("inferred") > silent
    TX fallback ("default_unconfirmed"). The distinction matters: a renter (or anyone whose
    home isn't tracked) with the setting still blank gets a GUESSED Texas, not a known one —
    RESUME-TRACKER item 8 (found while answering Jason's renter question) — so callers that
    care about correctness (the alerts digest, the state-aware endpoints) should surface
    `source`, not just silently use `code` as if it were always known."""
    cfg = cfg if cfg is not None else _read_retire()
    explicit = cfg.get("state_of_residence")
    if explicit:
        return {"code": states.norm_state(explicit), "source": "explicit"}
    inferred = _infer_state_from_accounts(d if d is not None else _data())
    if inferred:
        return {"code": states.norm_state(inferred), "source": "inferred"}
    return {"code": "TX", "source": "default_unconfirmed"}


def _state_of_residence(cfg=None, d=None):
    """Just the resolved 2-letter code — for callers that don't need the confidence source.
    Prefer `_state_of_residence_info()` in anything user-facing so an unconfirmed guess isn't
    presented as fact."""
    return _state_of_residence_info(cfg, d)["code"]


@app.get("/api/retirement-config")
def get_retire_config():
    return _read_retire()


@app.post("/api/retirement-config")
async def post_retire_config(body: RetirementConfigUpdate, request: Request):
    """Set retirement assumptions (local config). Keys: withdrawal_rate_pct, expected_real_return_pct,
    annual_expenses_override, annual_savings_override, current_age, target_age, exclude_account_types.
    Pydantic-validated (2026-07-24): ages 0-120, percentages/rates range-checked, SS claim age 62-70."""
    updates = body.model_dump(exclude_unset=True)
    old = _read_retire()   # independent read, captured before any mutation — see _emit_config_changed
    cfg = {**_read_retire(), **{k: v for k, v in updates.items() if k in DEFAULT_RETIRE}}
    store.write_json(RETIRE_FILE, cfg)
    _emit_config_changed(request, "/api/retirement-config", old, cfg)
    return {"ok": True, "config": cfg}


# ============ OPP-1: weekly "State of the Wealth" narrative brief ============
# The family-office quarterly letter, weekly and personal: ONE plain-language note composing
# every module's cached headline into "where you stand / what changed / one focus". Context
# comes from caches only (cheap); the single LLM pass rides the serial GPU queue.
NARRATIVE_FILE = os.environ.get("NARRATIVE_FILE", "/data/narrative.json")
NARRATIVE_KEEP = int(os.environ.get("NARRATIVE_KEEP", "26"))
# Separate from LLM_TIMEOUT (2026-07-19, Jason hit a 1200s timeout on a real run): narrative is a
# single one-shot generate call (not the per-ticker loop LLM_TIMEOUT was sized for), and on this
# hardware (dual Pascal 1080 Ti, no tensor cores, qwen3.6:27b spread across both cards) a cold
# model load + generation genuinely may need more headroom — mirrors STRAT_LLM_TIMEOUT's same
# reasoning ("reasoning ON is slow on Pascal", app.py ~8079). The owner's own estimate was ~40min.
NARR_LLM_TIMEOUT = int(os.environ.get("NARR_LLM_TIMEOUT", "2400"))


def _narrative_context():
    """Compact, numbers-only snapshot from the existing caches — no heavy recompute."""
    ctx = {}
    try:
        d = _data()
        ctx["net_worth"] = d.get("net_worth")
        ctx["invested"] = d.get("invested_value")
        hist = _read_value_history()
        if hist:
            wk = [h for h in hist if h.get("net_worth")][-8:]
            if len(wk) >= 2:
                ctx["net_worth_wk_change"] = round((wk[-1]["net_worth"] or 0) - (wk[0]["net_worth"] or 0), 2)
            fr = next((h.get("funded_ratio") for h in reversed(hist) if h.get("funded_ratio")), None)
            ctx["funded_ratio"] = fr
    except Exception:
        pass
    for name, fn in (("risk_metrics", lambda: _risk_metrics()),):
        try:
            rm = fn() or {}
            ctx["realized_vol_pct"] = rm.get("annualized_volatility_pct")
            ctx["max_drawdown_pct"] = rm.get("max_drawdown_pct")
            ctx["sharpe"] = rm.get("sharpe")
        except Exception:
            pass
    try:
        ex = _read_exp() or {}
        ctx["concentration_flags"] = ((ex.get("concentration") or {}).get("flags") or [])[:3]
        lt = ex.get("lookthrough_concentration") or {}
        ctx["true_top_name_pct"] = lt.get("top_position_pct")
    except Exception:
        pass
    try:
        ctx["attention"] = [a.get("message") for a in (_alerts_digest() or [])[:6]]
    except Exception:
        pass
    try:
        ta = _tax_audit() or {}
        tlh = ta.get("tax_loss_harvest") or {}
        ctx["tlh_candidates"] = tlh.get("count")
        ctx["tlh_total_loss"] = tlh.get("total_harvestable_loss")
    except Exception:
        pass
    try:
        bt = store.read_json_strict(BACKTEST_FILE) or {}
        h12 = ((bt.get("ratings") or {}).get("by_horizon") or {}).get("12m") or {}
        ctx["backtest_12m_hit_pct"] = h12.get("hit_rate_pct")
        ctx["paper_excess_vs_spy_pct"] = ((bt.get("paper") or {}).get("weighted_excess_pct"))
    except Exception:
        pass
    try:
        pf = store.read_json_strict(PERF_FILE) or {}
        ytd = (pf.get("periods") or {}).get("YTD") or {}
        ctx["ytd_portfolio_pct"] = ytd.get("portfolio_pct")
        ctx["ytd_excess_vs_policy_pct"] = ytd.get("excess_vs_policy_pct")
    except Exception:
        pass
    try:
        ctx["earnings_this_week"] = [f"{e['ticker']} {e.get('date')}" for e in _earnings_this_week()[:5]]
    except Exception:
        pass
    return {k: v for k, v in ctx.items() if v not in (None, [], {})}


def _write_narrative_error(msg):
    """Persist the reason the LAST attempt failed so /api/narrative (and the DASHBOARD box) can
    show something better than an eternally-empty 'No letter yet' — found 2026-07-18 while
    diagnosing Jason's 'ran it several times, it never appears': _do_narrative() used to swallow
    EVERY failure mode (timeout, non-2xx, empty response, any exception) into a bare `return`,
    so the job silently finished 'done' with nothing written and no trace anywhere. Doesn't
    touch the `letters` history — only this one status field."""
    try:
        nd = store.read_json_strict(NARRATIVE_FILE) or {}
    except Exception:
        nd = {}
    nd["last_error"] = msg
    nd["last_error_at"] = int(time.time())
    try:
        store.write_json(NARRATIVE_FILE, nd)
    except Exception:
        pass


def _do_narrative():
    ctx = _narrative_context()
    prompt = _sys(
        "You are the household's fiduciary analyst writing the weekly 'State of the Wealth' note "
        "to the family. Using ONLY the JSON numbers below, write PLAIN TEXT (no markdown headers, "
        "no bullet spam): three short paragraphs — (1) where the household stands (net worth, "
        "funded ratio, year-to-date vs its own policy); (2) what changed or needs attention this "
        "week (concentration, alerts, tax-loss opportunities, earnings ahead); (3) end with a "
        "single line beginning exactly 'THIS WEEK'S FOCUS:' naming the ONE most valuable action. "
        "Cite only numbers present in the data; round sensibly; warm but direct; under 220 words. "
        "Close with: 'Educational, not advice.'\n\nDATA:\n" + json.dumps(ctx))
    if "qwen3" in ANALYSIS_MODEL.lower() and not ANALYSIS_THINK:
        prompt += "\n/no_think"
    messages = [{"role": "user", "content": prompt}]
    # Every failure mode below used to collapse into `letter = ""` and a silent `return` — the
    # job queue then marked it "done" (no exception raised) with zero trace anywhere. Now each
    # branch records WHY, then re-raises so /api/jobs shows a real "error" status + message,
    # and /api/narrative's last_error survives even after the in-memory job history resets on
    # the next container restart.
    letter, err = "", None
    # RESUME-TRACKER 2026-07-19: LLM_TIMEOUT (now NARR_LLM_TIMEOUT here) only ever bounded the
    # requests.post() call itself — time spent BLOCKED waiting for _gpu_slot() (behind a live chat
    # generation or another job) was invisible, so a reported "timed out after Ns" could silently
    # include an unknown amount of queue-wait on top, making it impossible to tell GPU contention
    # apart from genuinely slow generation on this hardware. gpu_wait_s below closes that gap.
    t_submit = time.time()
    gpu_wait_s = None
    try:
        with _gpu_slot():
            gpu_wait_s = round(time.time() - t_submit, 1)
            res = llm._llm_chat(messages, _text_model(), temperature=0.4, num_ctx=ANALYSIS_NUM_CTX,
                                keep_alive=LLM_KEEP_ALIVE, timeout=NARR_LLM_TIMEOUT)
        if not res.ok:
            err = (f"Ollama returned HTTP {res.status_code}: {res.error}" if res.status_code
                   else (res.error or "unknown LLM backend error"))
        else:
            letter = res.content.strip()
            if not letter:
                err = "Ollama returned an empty response (model may have produced nothing usable)"
    except requests.exceptions.Timeout:
        wait_note = (f" (waited {gpu_wait_s}s for the GPU lock before generation even started)"
                     if gpu_wait_s is not None else "")
        err = (f"timed out after {NARR_LLM_TIMEOUT}s of generation{wait_note} — model may be "
               "cold-loading, GPU-starved, or the prompt/context too large")
    except requests.exceptions.ConnectionError as e:
        err = f"could not reach the LLM backend at {llm._base_url()}: {e}"[:300]
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:300]
    if err:
        _write_narrative_error(err)
        raise RuntimeError(err)   # _gpu_slot() still releases correctly on unwind
    try:
        nd = store.read_json_strict(NARRATIVE_FILE) or {}
    except Exception:
        nd = {}
    rows = (nd.get("letters") or [])
    # gpu_wait_s persists on SUCCESSFUL runs too (not just the error path above) so Jason can see
    # typical GPU-queue delay over time in /api/narrative's history — useful for judging whether
    # NARR_LLM_TIMEOUT is comfortably sized without needing to reproduce an actual failure.
    rows.append({"generated_at": int(time.time()), "letter": letter, "context": ctx, "gpu_wait_s": gpu_wait_s})
    nd = {"letters": rows[-NARRATIVE_KEEP:], "last_error": None, "last_error_at": None}
    store.write_json(NARRATIVE_FILE, nd)


@app.get("/api/narrative")
def get_narrative():
    try:
        nd = store.read_json_strict(NARRATIVE_FILE) or {}
    except Exception:
        nd = {}
    rows = nd.get("letters") or []
    return {"latest": rows[-1] if rows else None, "count": len(rows),
            "last_error": nd.get("last_error"), "last_error_at": nd.get("last_error_at"),
            "note": ("Weekly plain-language composition of every module's cached headline — the "
                     "cockpit as a story, with one recommended focus. Educational, not advice.")}


@app.post("/api/run-narrative")
def run_narrative_ep():
    jid, created = _enqueue("narrative", source="manual")
    return {"status": "queued" if created else "duplicate", "job_id": jid}


# ============ P1/G10: hedging analytics — covered calls & collars (options.py) ============
# Delayed Yahoo chains + locally-computed BS deltas for the concentrated names. Network-heavy
# only (no LLM/GPU) → risk-suite pattern: background build, cached, never blocks the UI.
HEDGING_FILE = os.environ.get("HEDGING_FILE", "/data/hedging.json")
HEDGING_TTL = int(os.environ.get("HEDGING_TTL", "86400"))
HEDGING_MAX_NAMES = int(os.environ.get("HEDGING_MAX_NAMES", "6"))
_hedging_lock = threading.Lock()


def _hedging_symbols():
    """Concentrated names worth hedging math: sizing over-budget first, then top direct weights."""
    syms, seen = [], set()
    try:
        for r in (((_read_sizing() or {}).get("loss_budget") or {}).get("positions") or []):
            if r.get("over_budget") and r.get("ticker") and r["ticker"] not in seen:
                syms.append(r["ticker"]); seen.add(r["ticker"])
    except Exception:
        pass
    try:
        for p in ((_read_exp() or {}).get("positions") or []):
            t = p.get("ticker")
            if t and t not in seen and (p.get("weight_pct") or 0) >= 3.0:
                syms.append(t); seen.add(t)
    except Exception:
        pass
    return syms[:HEDGING_MAX_NAMES]


def _spot_hint(sym):
    """Last cached daily close (the risk-suite price cache, `_stooq_daily`) as a same-day-ish
    spot price for options_mod.analyze() — CBOE's chain response has no underlying-price field
    at all (verified 2026-07-18), so this avoids either an inference-from-strike approximation
    or a redundant Yahoo quote call. None (analyze() falls back to its own inference) on any
    miss — never raises."""
    try:
        series = _stooq_daily(sym)
        return series[-1][1] if series else None
    except Exception:
        return None


def _do_hedging():
    if not _hedging_lock.acquire(blocking=False):
        return
    try:
        cards, skipped = [], []
        for s in _hedging_symbols():
            try:
                card = options_mod.analyze(s, spot_hint=_spot_hint(s))
            except Exception:
                card = None
            (cards.append(card) if card else skipped.append(s))
        res = {"generated_at": int(time.time()), "cards": cards, "no_chain": skipped,
               "note": ("Delayed chains; premiums are mid-quotes and thin markets widen fast — "
                        "treat numbers as sizing guides, verify live quotes before any order. "
                        "Educational; assignment/collars carry tax consequences on appreciated lots.")}
        store.write_json(HEDGING_FILE, res)
    except Exception:
        pass
    finally:
        _hedging_lock.release()


@app.get("/api/hedging")
def get_hedging():
    try:
        cached = store.read_json_strict(HEDGING_FILE)
    except Exception:
        cached = None
    fresh = bool(cached) and (time.time() - (cached.get("generated_at") or 0) < HEDGING_TTL)
    if not fresh and not _hedging_lock.locked():
        threading.Thread(target=_do_hedging, daemon=True).start()
    if cached:
        return {**cached, "cached": True, "refreshing": (not fresh)}
    return {"status": "computing", "computing": True,
            "note": "First hedging build is fetching option chains — refresh in a moment."}


@app.post("/api/run-hedging")
def run_hedging():
    threading.Thread(target=_do_hedging, daemon=True).start()
    return {"status": "started"}


# ============ P1: RentCast free-tier AVM (50 calls/mo — one home = one call/mo) ============
RENTCAST_FILE = os.environ.get("RENTCAST_FILE", "/data/rentcast.json")
RENTCAST_TTL = int(os.environ.get("RENTCAST_TTL", str(30 * 86400)))   # monthly refresh fits the free tier


# ============ Deed/property fraud alert guidance (2026-07-30) ============
# Option 2 of the 2026-07-27 backlog item (option 1, the informational-only tip, already shipped).
# Research pass (2026-07-30) confirmed the owner's own suspicion that there's no single national API
# or registry for county recorder "property fraud alert" signups -- BUT found more than "very
# limited options": a real semi-national aggregator (propertyfraudalert.com, run by Fidlar,
# spanning many counties across several states) and one genuinely complete, actively-maintained
# STATEWIDE list (Florida's Court Clerks & Comptrollers association, all ~65 counties with direct
# sign-up links). Texas and most other states remain pure per-county patchwork with no state list.
# Jason's direction: identify the county from the tracked address and point the user at the best
# available resource, as far as that's buildable without a giant hand-curated (and quickly stale)
# table of individual county URLs.
_STATE_FIPS_TO_ABBR = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE",
    "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA",
    "20": "KS", "21": "KY", "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM",
    "36": "NY", "37": "NC", "38": "ND", "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI",
    "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY", "72": "PR",
}
CENSUS_GEOCODE_TTL = 180 * 86400   # a county practically never changes for a given address

# Deliberately NOT a giant hand-curated table of individual county sign-up URLs -- those are
# per-office pages that rot silently and there are 3000+ counties. Only entries the research pass
# confirmed are a single page actively maintained by a state/industry body, not one county office.
DEED_FRAUD_STATE_RESOURCES = {
    "FL": {"label": "Florida Court Clerks & Comptrollers — statewide list (all counties, direct sign-up links)",
          "url": "https://www.flclerks.com/page/PropertyAlertServices"},
}
# California SB 255 (2025) mandates EVERY CA county stand up a recorder notification program by
# 2027-01-01 (LA County runs its own equivalent program instead); only a handful of CA counties
# have one live as of this research pass, so there's no complete list to link the way Florida's is
# -- this note is surfaced instead of a (currently incomplete, would-go-stale-fast) link table.
DEED_FRAUD_STATE_NOTES = {
    "CA": "California law (SB 255) requires every county to offer this notification service by "
          "January 1, 2027 — only a handful do today, so if your county isn't live yet, check "
          "back closer to that date or contact your county recorder directly.",
}


def _county_lookup(address):
    """Resolves a US mailing address to {"county": "<Name> County", "state": "<USPS abbr>"} via
    the Census Bureau's free, keyless Geocoder API -- no signup, no paid tier, same bar as the
    app's other free-government-data pulls (FRED T-bill, SEC EDGAR). Cached via store.cached_fetch
    with a long (~180d) TTL since a county is effectively permanent for a given address. Returns
    None on any failure (unparseable/partial address, API down, no match) -- callers fall back to
    the generic (non-personalized) tip rather than erroring."""
    addr = (address or "").strip()
    if not addr:
        return None

    def _compute():
        try:
            r = requests.get("https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress",
                             params={"address": addr, "benchmark": "Public_AR_Current",
                                     "vintage": "Current_Current", "format": "json"}, timeout=20)
            if r.status_code >= 400:
                return None
            matches = ((r.json() or {}).get("result") or {}).get("addressMatches") or []
            if not matches:
                return None
            geo = matches[0].get("geographies") or {}
            counties = geo.get("Counties") or next(
                (v for k, v in geo.items() if "count" in k.lower() and v), [])
            if not counties:
                return None
            c = counties[0]
            county_name = c.get("NAME")
            if not county_name:
                return None
            abbr = _STATE_FIPS_TO_ABBR.get(str(c.get("STATE") or "").zfill(2))
            return {"county": county_name, "state": abbr}
        except Exception:
            return None

    try:
        value, _meta = store.cached_fetch("census_geocode", addr, CENSUS_GEOCODE_TTL, _compute,
                                          is_ok=lambda v: v is not None)
        return value
    except Exception:
        return None


def _deed_fraud_guidance(address):
    """Best-effort 'where do I actually sign up' guidance: resolves the tracked home address to a
    county/state (see _county_lookup), then returns a small set of links -- a known-good statewide
    resource when one exists (currently just Florida), the semi-national propertyfraudalert.com
    aggregator (coverage varies by county, no guarantee), and a pre-filled web-search link that
    always resolves to something useful regardless of coverage. Returns None if the address can't
    be geocoded -- caller falls back to the generic, non-personalized tip."""
    loc = _county_lookup(address)
    if not loc or not loc.get("county"):
        return None
    import urllib.parse
    county, state = loc["county"], loc.get("state")
    q = f"{county} {state or ''} property fraud alert deed notification sign up".strip()
    links = []
    if state and state in DEED_FRAUD_STATE_RESOURCES:
        links.append(DEED_FRAUD_STATE_RESOURCES[state])
    links.append({"label": "propertyfraudalert.com — search for your county (multi-state aggregator, coverage varies)",
                 "url": "https://propertyfraudalert.com/"})
    links.append({"label": f"Web search: “{q}”", "url": "https://www.google.com/search?q=" + urllib.parse.quote(q)})
    return {"county": county, "state": state, "links": links,
           "note": DEED_FRAUD_STATE_NOTES.get(state)}


def _rentcast_avm(address):
    """Value + rent AVM for an address, cached ~30d. None when unkeyed/unavailable."""
    key = (os.environ.get("RENTCAST_API_KEY") or "").strip()
    if not key or not address:
        return None
    try:
        cache = store.read_json_strict(RENTCAST_FILE) or {}
    except Exception:
        cache = {}
    rec = cache.get(address)
    if rec and time.time() - (rec.get("ts") or 0) < RENTCAST_TTL:
        return rec
    out = {"ts": int(time.time()), "address": address}
    try:
        for field, path in (("value", "avm/value"), ("rent", "avm/rent/long-term")):
            r = requests.get(f"https://api.rentcast.io/v1/{path}",
                             params={"address": address},
                             headers={"X-Api-Key": key, "Accept": "application/json"}, timeout=25)
            if r.status_code >= 400:
                continue
            j = r.json() or {}
            out[field] = {"estimate": j.get("price") or j.get("rent"),
                          "low": j.get("priceRangeLow") or j.get("rentRangeLow"),
                          "high": j.get("priceRangeHigh") or j.get("rentRangeHigh")}
        if out.get("value") or out.get("rent"):
            cache[address] = out
            store.write_json(RENTCAST_FILE, cache)
            return out
    except Exception:
        pass
    return rec or None    # stale cache beats nothing


# ============ GAP-3: performance reporting — TWR + policy benchmark (performance.py) ============
# Composite returns fetch per-ticker + 4 proxy-ETF histories (network on a cold cache) →
# risk-suite pattern: background build, cached ~12h, POST to force.
PERF_FILE = os.environ.get("PERF_FILE", "/data/performance.json")
PERF_TTL = int(os.environ.get("PERF_TTL", "43200"))
_perf_lock = threading.Lock()


def _do_performance():
    if not _perf_lock.acquire(blocking=False):
        return
    try:
        d = _data()
        weights = {}
        for c in d.get("consolidated") or []:
            t = c.get("ticker")
            if t and (c.get("type") or "") != "Cash" and (c.get("value") or 0) > 0:
                weights[t] = weights.get(t, 0.0) + float(c["value"])
        res = performance.run(weights,
                              (_read_policy() or {}).get("targets_by_sleeve") or {},
                              _stooq_daily,
                              _read_value_history())
        res["generated_at"] = int(time.time())
        store.write_json(PERF_FILE, res)
    except Exception:
        pass
    finally:
        _perf_lock.release()


@app.get("/api/performance")
def get_performance():
    try:
        cached = store.read_json_strict(PERF_FILE)
    except Exception:
        cached = None
    fresh = bool(cached) and (time.time() - (cached.get("generated_at") or 0) < PERF_TTL)
    if not fresh and not _perf_lock.locked():
        threading.Thread(target=_do_performance, daemon=True).start()
    if cached:
        return {**cached, "cached": True, "refreshing": (not fresh)}
    return {"status": "computing", "computing": True,
            "note": "First performance build is fetching price history — refresh in a moment."}


@app.post("/api/run-performance")
def run_performance_ep():
    threading.Thread(target=_do_performance, daemon=True).start()
    return {"status": "started"}


# ============ GAP-5: benefits & contribution-room optimizer (benefits.py) ============
@app.get("/api/benefits")
def get_benefits():
    """401(k) match/headroom, HSA, Roth IRA eligibility, mega-backdoor capacity — from the
    household register + retirement-config ages. Cheap; 2026 limits in the G13 registry."""
    cfg = _read_insurance_cfg()
    ret = _read_retire()
    facts = {"salary": cfg.get("income_self"),
             "age": _derive_age(ret.get("birth_year"), ret.get("current_age")),
             "spouse_age": _derive_age(ret.get("spouse_birth_year"), ret.get("spouse_age")),
             "pretax_pct": cfg.get("k401_pretax_pct"),
             "aftertax_pct": cfg.get("k401_aftertax_pct"),
             "match_t1_pct": cfg.get("k401_match_t1_pct"),
             "match_t1_rate": cfg.get("k401_match_t1_rate"),
             "match_t2_pct": cfg.get("k401_match_t2_pct"),
             "match_t2_rate": cfg.get("k401_match_t2_rate"),
             "biweekly_total": cfg.get("k401_biweekly_total"),
             "pay_frequency": cfg.get("k401_pay_frequency"),
             "pay_periods_custom": cfg.get("k401_pay_periods_custom"),
             "plan_aftertax": cfg.get("plan_aftertax"),
             "inservice_rollover": cfg.get("inservice_rollover"),
             "hsa_coverage": cfg.get("hsa_coverage"),
             "hsa_contribution": cfg.get("hsa_contribution"),
             "hsa_invested": cfg.get("hsa_invested"),
             "magi": (cfg.get("income_self") or 0) + (cfg.get("income_spouse") or 0),
             "roth_contributed_self": cfg.get("roth_ira_contribution_self"),
             "roth_contributed_spouse": cfg.get("roth_ira_contribution_spouse"),
             "roth_monthly_self": cfg.get("roth_ira_monthly_self"),
             "roth_monthly_spouse": cfg.get("roth_ira_monthly_spouse")}
    res = benefits.run(facts)
    res["facts"] = facts
    res["generated_at"] = int(time.time())
    return res


# ============ GAP-2: cash / liquidity / debt desk (cashdebt.py) ============
TBILL_CACHE_FILE = os.environ.get("TBILL_CACHE_FILE", "/data/tbill_yield.json")
TBILL_SERIES = os.environ.get("TBILL_SERIES", "DTB3")


def _tbill_yield():
    """Latest 3-mo T-bill yield (%) from FRED's keyless fredgraph CSV, cached daily; falls back
    to the `tbill_fallback_pct` knob. Same pattern as _tips_real_yield."""
    today = datetime.date.today()
    try:
        c = store.read_json_strict(TBILL_CACHE_FILE) or {}
        if c.get("fetched") == today.isoformat() and c.get("yield_pct") is not None:
            return c["yield_pct"], c.get("source") or f"FRED {TBILL_SERIES}"
    except Exception:
        pass
    try:
        r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv",
                         params={"id": TBILL_SERIES,
                                 "cosd": (today - datetime.timedelta(days=45)).isoformat()},
                         timeout=20)
        if r.status_code < 400:
            rows = [ln.split(",") for ln in r.text.strip().splitlines()[1:] if "," in ln]
            vals = [(d, v) for d, v in rows if v not in (".", "", None)]
            if vals:
                y = round(float(vals[-1][1]), 2)
                store.write_json(TBILL_CACHE_FILE, {"fetched": today.isoformat(), "yield_pct": y,
                               "source": f"FRED {TBILL_SERIES}"})
                return y, f"FRED {TBILL_SERIES}"
    except Exception:
        pass
    return _knob("tbill_fallback_pct"), "fallback knob"


def _cash_total(d):
    """SPENDABLE household cash: non-investment cash accounts (checking/savings/money market)
    plus Cash-typed positions inside investment accounts.

    Tax-advantaged wrappers are excluded (fixed 2026-08-08). They weren't before, and the name
    heuristic below happily matched "Health Savings Account" on the word "savings" and
    "IRA BROKERAGE CASH" on "cash " — so ~$2.4k of HSA money was sitting inside the household's
    "cash" figure, inflating both the emergency-fund fallback and the idle-cash drag. An HSA or
    IRA sweep balance is not spendable emergency cash: reaching it costs ordinary income tax plus
    a penalty. It is still idle and still worth investing, but that's an asset-location question,
    not a household-liquidity one.

    KNOWN LIMITATION (unchanged): the `consolidated` loop below counts Cash-typed POSITIONS across
    all accounts without attribution, so cash held inside a 401(k)/IRA still slips in that way.
    Today that's ~$48 of the household's total; it needs per-account position attribution to fix
    properly and hasn't been worth the surgery yet."""
    tot = 0.0
    for a in d.get("accounts") or []:
        if a.get("is_asset") is False:
            continue
        if _account_tax_status(a.get("name"), a.get("type")) != "na":
            continue   # HSA / IRA / 401(k) / Roth / 529 wrapper — not spendable cash
        if _is_cash_account(a):
            tot += a.get("balance") or 0
    for c in d.get("consolidated") or []:
        if (c.get("type") or "") == "Cash":
            tot += c.get("value") or 0
    return round(tot, 2)


def _is_cash_account(a):
    """The same checking/savings/money-market recognizer _cash_total() uses, factored out so the
    account registry and the EF tiering below agree with the idle-cash figure by construction
    rather than by two copies of the same word list drifting apart."""
    t = ((a.get("type") or "") + " " + (a.get("name") or "")).lower()
    return (a.get("holdings_count") or 0) == 0 and any(
        w in t for w in ("checking", "savings", "money market", "cash management", "cash "))


def _ef_tier(a):
    """Which cashdebt.EF_TIERS liquidity tier an ASSET account belongs to, for emergency-fund
    purposes (2026-08-08). This is not the same question as _account_tax_status() — that one asks
    "where should securities be located for tax efficiency", this one asks "how fast can this
    become spendable money and what does it cost to get it out". They agree often enough to reuse
    the classifier and disagree in exactly the place that matters: a Roth is a poor asset-location
    answer for bonds but a legitimate LAST-RESORT emergency source, because contributions (never
    earnings) come out anytime tax- and penalty-free.

    Returns None for accounts that are not plausible EF sources at all — real estate, liabilities
    — so the registry can hide the toggle entirely instead of offering a checkbox that lies.

    ORDER MATTERS, and getting it wrong was a real bug (found 2026-08-08 by reading the live
    registry rather than the source): the tax-status check MUST run before the cash-name check.
    `_is_cash_account()` matches on words in the account NAME, and the household has accounts named
    "IRA BROKERAGE CASH" and "Health Savings Account" — the first contains "cash ", the second
    contains "savings". With the cash check first, three IRA sweep accounts and two HSAs were
    classified as tier `cash`, which offered them as fully eligible emergency-fund sources counted
    at par. They are the exact opposite: money that costs income tax plus a 10% penalty (20% for a
    non-medical HSA withdrawal) to reach. A wrapper's tax treatment always outranks what the
    balance inside it happens to be invested in."""
    if a.get("is_asset") is False:
        return None
    st = _account_tax_status(a.get("name"), a.get("type"))
    if st == "roth":
        return "roth_basis"
    if st in ("deferred", "hsa"):
        return "deferred"
    if st == "taxable":
        return "taxable"
    if _is_cash_account(a):
        return "cash"
    atype = (a.get("type") or "").lower()
    if "real estate" in atype or "vehicle" in atype or "loan" in atype or "credit" in atype:
        return None
    # An asset account we can't classify (a manual "Other asset", say) is treated as cash-like at
    # par. It is only ever counted if the household explicitly designates it, and the cap field is
    # there for the case where only part of it is really liquid.
    return "cash"


EF_STATES = ("unset", "yes", "no")


def _ef_state(ent):
    """Resolve one `ef_accounts` entry to its THREE-state designation (2026-08-08b, Jason).

    A two-state flag could not distinguish "you haven't told me yet" from "you told me no", so the
    panel nagged permanently about $8.5k of checking balances that are deliberately excluded —
    every one of them committed to that month's bills, which is a decision, not an omission. The
    third state makes an answered "no" durable while a genuinely NEW account still surfaces.

    Storage stays backward-compatible with the two-state shape already on disk:
      · absent / {}          -> "unset"  (never ruled on)
      · {"on": true, ...}    -> "yes"    (counted; this is the pre-existing shape, read as-is)
      · {"excluded": true}   -> "no"     (explicitly ruled out; stored with "on": false)
    Anything else falls back to "unset", so a malformed or half-written entry degrades to "ask
    again" rather than to a silent yes or a silent no."""
    if not isinstance(ent, dict):
        return "unset"
    if ent.get("on"):
        return "yes"
    if ent.get("excluded"):
        return "no"
    return "unset"


def _ef_source_rows(d, cfg):
    """The designated emergency-fund sources, in the shape cashdebt.ef_sources() consumes.

    Designation lives in insurance.json's `ef_accounts` — {account name: {"on": bool, "excluded":
    bool, "cap": float|None}} — keyed by account NAME, the same convention debt_rates /
    pay_in_full_overrides already use, so one editor can write all of them and a renamed account
    fails visibly (its designation stops matching) rather than silently attaching to the wrong
    balance. See _ef_state() for the three states.

    Ineligible tiers are still RETURNED rather than filtered out, carrying their tier: cashdebt
    counts them at $0 and raises an alert naming them. Dropping them here instead would make a
    mis-designated 401(k) simply vanish from the panel, which is how a household ends up believing
    it has a reserve it can't actually reach without a 10% penalty."""
    ef_cfg = cfg.get("ef_accounts") or {}
    if not isinstance(ef_cfg, dict):
        return []
    rows = []
    for a in d.get("accounts") or []:
        nm = a.get("name") or ""
        ent = ef_cfg.get(nm)
        if _ef_state(ent) != "yes":
            continue
        tier = _ef_tier(a)
        if tier is None:
            continue   # designation on a non-source account (real estate/liability): ignore
        try:
            cap = None if ent.get("cap") in (None, "") else float(ent["cap"])
        except (TypeError, ValueError):
            cap = None
        rows.append({"name": nm, "institution": a.get("institution"),
                     "balance": a.get("balance") or 0, "tier": tier, "cap": cap})
    return rows


def _unreviewed_cash(d, cfg):
    """Spendable cash sitting in accounts the household has NOT yet ruled on either way.

    This, not "all undesignated cash", is what the panel should ask about. An account marked NO is
    an answered question and must never be counted here again — that was the whole point of going
    three-state. Mirrors _cash_total()'s definition of spendable (tax-advantaged wrappers excluded)
    so the figure the panel nags about is the same money the panel calls cash."""
    ef_cfg = cfg.get("ef_accounts") or {}
    if not isinstance(ef_cfg, dict):
        ef_cfg = {}
    tot = 0.0
    for a in d.get("accounts") or []:
        if a.get("is_asset") is False:
            continue
        if _account_tax_status(a.get("name"), a.get("type")) != "na":
            continue
        if not _is_cash_account(a):
            continue
        if _ef_state(ef_cfg.get(a.get("name") or "")) == "unset":
            tot += a.get("balance") or 0
    return round(tot, 2)


def _is_mortgage_account(a, overrides):
    """Name-substring heuristic ("mortgage"/"home loan" in the account name or type), with an
    explicit per-account override taking precedence — added 2026-07-18 because a Rocket-branded
    loan (~$4.2k, nearly paid off) has neither word in its Monarch name and was silently landing
    in the non-mortgage avalanche + leaving payoff-vs-invest unavailable. `overrides` is
    insurance.json's `is_mortgage_overrides` dict, keyed by account name."""
    nm = a.get("name") or "?"
    ov = (overrides or {}).get(nm)
    if ov is not None:
        return bool(ov)
    low = (nm + " " + (a.get("type") or "")).lower()
    return any(w in low for w in ("mortgage", "home loan"))


@app.get("/api/cashdebt")
def get_cashdebt():
    """GAP-2 household-CFO desk: EF gauge, idle-cash drag, payoff-vs-invest, refi, avalanche.
    Cheap (one cached FRED read; no LLM)."""
    d = _data()
    ret = _read_retire()
    cfg = _read_insurance_cfg()
    tb, tb_src = _tbill_yield()
    exp = float(ret.get("annual_expenses_override") or 0) or None
    if not exp:
        try:
            _inc, exp = _cashflow_annual()
        except Exception:
            exp = None
    mort_overrides = cfg.get("is_mortgage_overrides") or {}
    mort_bal = sum((a.get("balance") or 0) for a in (d.get("accounts") or [])
                   if a.get("is_asset") is False and _is_mortgage_account(a, mort_overrides))
    debts = []
    rates = cfg.get("debt_rates") or {}
    min_payments = cfg.get("debt_min_payments") or {}
    pay_in_full = cfg.get("pay_in_full_overrides") or {}
    for a in d.get("accounts") or []:
        if a.get("is_asset") is False:
            if _is_mortgage_account(a, mort_overrides):
                continue
            nm = a.get("name") or "?"
            debts.append({"name": nm, "balance": abs(a.get("balance") or 0),
                          # a figure the user entered wins; otherwise the provider's (Plaid /liabilities/get)
                          "rate_pct": rates.get(nm) if rates.get(nm) not in (None, "") else a.get("apr_pct"),
                          "min_payment": min_payments.get(nm) if min_payments.get(nm) not in (None, "") else a.get("min_payment"),
                          "pay_in_full": bool(pay_in_full.get(nm))})
    inputs = {"cash_total": _cash_total(d),
              "annual_expenses": exp,
              "needs_pct": ret.get("needs_pct_of_expenses") or 65,
              "tbill_pct": tb,
              "deposit_yield_pct": cfg.get("deposit_yield_pct"),
              "expected_real_pct": ret.get("expected_real_return_pct"),
              "mortgage_balance": round(mort_bal, 2),
              "mortgage_rate_pct": cfg.get("mortgage_rate_pct"),
              "mortgage_payment_monthly": cfg.get("mortgage_payment_monthly"),
              "refi_rate_pct": cfg.get("refi_rate_pct"),
              "refi_closing_costs": cfg.get("refi_closing_costs"),
              "debt_extra_monthly": cfg.get("debt_extra_monthly"),
              # ef_sources (2026-08-08, Jason): the accounts the household explicitly designated as
              # emergency-fund resources. cash_total above stays exactly what it was and still
              # drives the idle-cash drag — a brokerage held AS the reserve isn't earning a deposit
              # yield, so it must never land in that figure. See cashdebt.ef_sources().
              "ef_sources": _ef_source_rows(d, cfg),
              # Only cash the household hasn't ruled on either way — an account marked NO is an
              # answered question, not an outstanding one. See _unreviewed_cash().
              "unreviewed_cash": _unreviewed_cash(d, cfg),
              "debts": debts}
    sinfo = _state_of_residence_info(ret, d)
    inputs["state"] = sinfo["code"]
    res = cashdebt.run(inputs, {"ef_target_months": _knob("ef_target_months"),
                                "ef_stress_drawdown_pct": _knob("ef_stress_drawdown_pct")})
    res["state_of_residence_source"] = sinfo["source"]
    res["inputs"] = inputs
    res["tbill_source"] = tb_src
    res["generated_at"] = int(time.time())
    return res


# ============ CALCULATORS tab (2026-10-19, Jason) — calculators.py ============
# The math lives in calculators.py (pure, unit-tested in test_calculators.py). These two routes are
# the only app-side glue: one generic compute endpoint, and the liability list the WHAT-IF panel
# prefills from. Nothing here writes; saving a missing APR/payment back goes through the existing
# /api/account-registry so the CASH·DEBT desk, SETTINGS→ACCOUNTS and WHAT-IF can never disagree.
@app.post("/api/calc")
async def post_calc(req: Request):
    """Body: {"calculator": name, "inputs": {...}, "schedule": bool (default true)}.
    Always 200 with either the result or {"error", "missing"?} — a half-filled form is normal
    while someone is typing, not a server error."""
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": "Body must be JSON."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Body must be a JSON object."}, status_code=400)
    return calculators.run(body.get("calculator"), body.get("inputs") or {},
                           include_schedule=body.get("schedule", True) is not False)


@app.get("/api/calc/catalog")
def get_calc_catalog():
    return {"calculators": [{"key": k, "title": t} for k, (_f, t) in calculators.CALCULATORS.items()]}


def _whatif_liabilities():
    """Every liability the household has (feed-synced AND manual), with the three facts the
    WHAT-IF needs — balance, APR, payment — and where each one came from. Mortgages are included
    here (unlike the avalanche): "what if I paid $200 more on the mortgage" is the single most
    common version of the question.

    APR/payment resolve per account first (debt_rates / debt_min_payments, the same keys the
    registry edits); a mortgage falls back to the household mortgage_rate_pct /
    mortgage_payment_monthly from PLAN→CASH·DEBT only when exactly ONE mortgage exists — with two,
    there's no way to know which one those facts describe, so they're reported missing instead of
    attached to the wrong loan. Nothing is guessed: a blank stays blank and is listed in `missing`.

    Paid-off accounts (balance KNOWN to be $0) are left out entirely (2026-10-19, Jason) — a feed
    keeps closed/zeroed cards and loans around indefinitely, and "what if I paid more on a $0
    balance" has no answer. An account whose balance is UNKNOWN (no figure at all) is kept, with
    "balance" in `missing`, since it may well be owed. The single-mortgage fallback below counts
    only mortgages that still carry a balance, so a paid-off old mortgage left in the feed no
    longer blocks the household mortgage facts from attaching to the live one.

    Also left out (2026-10-19, Jason): ANY account with a KNOWN 0% APR — there is no interest to
    save, and the tool exists to price paying a debt off faster (an earlier cut kept a 0% debt
    that had a payment; Jason wanted those gone too) — and any account flagged PAID IN FULL (a
    transactor card cleared every statement; its balance isn't real revolving debt). An UNKNOWN
    APR stays: blank is not zero."""
    d = _data()
    cfg = _read_insurance_cfg()
    rates = cfg.get("debt_rates") or {}
    mins = cfg.get("debt_min_payments") or {}
    pif = cfg.get("pay_in_full_overrides") or {}
    mort_ov = cfg.get("is_mortgage_overrides") or {}
    def _bal(a):
        try:
            return None if a.get("balance") in (None, "") else abs(float(a.get("balance")))
        except (TypeError, ValueError):
            return None
    accts = [a for a in (d.get("accounts") or []) if a.get("is_asset") is False]
    accts = [a for a in accts if _bal(a) is None or _bal(a) >= 0.005]   # drop known-$0 (paid off)
    n_mort = sum(1 for a in accts if _is_mortgage_account(a, mort_ov))
    out = []
    for a in accts:
        nm = a.get("name") or "?"
        is_m = _is_mortgage_account(a, mort_ov)
        rate, rate_src = rates.get(nm), "account"
        pay, pay_src = mins.get(nm), "account"
        if rate in (None, "") and a.get("apr_pct") is not None:      # provider default (Plaid), user entry wins
            rate, rate_src = a.get("apr_pct"), "plaid"
        if pay in (None, "", 0) and a.get("min_payment"):
            pay, pay_src = a.get("min_payment"), "plaid"
        if is_m and n_mort == 1:
            if rate is None and cfg.get("mortgage_rate_pct") is not None:
                rate, rate_src = cfg.get("mortgage_rate_pct"), "plan_cashdebt_mortgage"
            if pay in (None, 0) and cfg.get("mortgage_payment_monthly"):
                pay, pay_src = cfg.get("mortgage_payment_monthly"), "plan_cashdebt_mortgage"
        bal = _bal(a)
        missing = [k for k, v in (("balance", bal), ("apr", rate), ("payment", pay)) if v in (None, "")]
        if pay == 0:
            missing.append("payment")
        # Known 0% APR (with or without a payment) and PAID-IN-FULL accounts have nothing to
        # optimise here. An UNKNOWN apr (None/"") stays — blank is not zero.
        try:
            zero_apr = rate is not None and rate != "" and float(rate) == 0.0
        except (TypeError, ValueError):
            zero_apr = False
        if zero_apr or pif.get(nm):
            continue
        out.append({"name": nm, "institution": a.get("institution"), "type": a.get("type"),
                    "balance": round(bal, 2) if bal is not None else None, "apr": rate, "apr_source": rate_src if rate is not None else None,
                    "payment": pay, "payment_source": pay_src if pay else None,
                    "is_mortgage": is_m, "pay_in_full": bool(pif.get(nm)),
                    "manual": bool(a.get("manual")), "missing": sorted(set(missing))})
    out.sort(key=lambda x: (not x["is_mortgage"], -(x["balance"] or 0)))
    return out


# DRIP ticker lookup (2026-10-19, Jason): type a symbol, get the DRIP inputs (price, trailing
# yield, payment frequency, trailing dividend + price growth) instead of hunting them down by hand.
# Source: the same KEYLESS Yahoo chart API _fetch_yahoo() already falls back to — 10y of monthly
# closes plus split-adjusted dividend events in one call, and it covers ETFs/funds (Finnhub's free
# tier generally doesn't). Finnhub (price + indicated yield, no history) is the fallback.
# In-memory cache: dividend history changes quarterly at most, and the panel re-asks on every
# holding pick.
_DIV_LOOKUP_CACHE = {}
_DIV_LOOKUP_TTL = 6 * 3600


def _yahoo_dividend_history(sym):
    ysym = sym.replace(".", "-")          # BRK.B -> BRK-B (Yahoo's class-share spelling)
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            r = requests.get(f"https://{host}/v8/finance/chart/{ysym}",
                             params={"range": "10y", "interval": "1mo", "events": "div"},
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            if r.status_code >= 400:
                continue
            res = (((r.json() or {}).get("chart") or {}).get("result") or [None])[0]
            if not res:
                continue
            meta = res.get("meta") or {}
            ts = res.get("timestamp") or []
            closes = ((((res.get("indicators") or {}).get("quote") or [{}])[0]) or {}).get("close") or []
            divs = [(v.get("date"), v.get("amount"))
                    for v in ((res.get("events") or {}).get("dividends") or {}).values()
                    if v.get("date") and v.get("amount")]
            price = meta.get("regularMarketPrice")
            if not price:
                continue
            return {"price": float(price), "dividends": divs,
                    "closes": [(t, c) for t, c in zip(ts, closes) if c],
                    "name": meta.get("longName") or meta.get("shortName"),
                    "type": meta.get("instrumentType"), "currency": meta.get("currency")}
        except Exception:
            continue
    return None


@app.get("/api/calc/dividend-lookup")
def get_dividend_lookup(ticker: str = ""):
    sym = re.sub(r"[^A-Za-z0-9.\-]", "", ticker or "").upper()[:12]
    if not sym:
        return {"error": "Enter a ticker symbol."}
    hit = _DIV_LOOKUP_CACHE.get(sym)
    if hit and time.time() - hit[0] < _DIV_LOOKUP_TTL:
        return hit[1]
    raw = _yahoo_dividend_history(sym)
    if raw:
        out = calculators.dividend_profile(raw["price"], raw["dividends"], raw["closes"])
        out.update({"ticker": sym, "name": raw.get("name"), "type": raw.get("type"),
                    "currency": raw.get("currency"), "source": "Yahoo Finance (chart API)"})
        if raw.get("currency") and raw["currency"] != "USD":
            out["notes"].append(f"Prices and dividends are in {raw['currency']}, not USD.")
    else:
        price = None
        try:
            price = _price(sym)
        except Exception:
            price = None
        if not price:
            return {"error": f"Couldn't find price data for {sym}. Check the symbol, or enter the "
                             "share price and yield by hand."}
        v = _valuation(sym) or {}
        y = v.get("dividend_yield_pct")
        out = {"ticker": sym, "price": round(float(price), 2), "dividend_yield_pct": y,
               "pays_dividend": bool(y), "frequency": None, "source": "Finnhub (no history)",
               "notes": ["Dividend history unavailable — yield only; enter frequency and growth "
                         "assumptions yourself."]}
    out["as_of"] = int(time.time())
    _DIV_LOOKUP_CACHE[sym] = (time.time(), out)
    if len(_DIV_LOOKUP_CACHE) > 500:
        _DIV_LOOKUP_CACHE.pop(next(iter(_DIV_LOOKUP_CACHE)))
    return out


@app.get("/api/calc/liabilities")
def get_calc_liabilities():
    try:
        return {"liabilities": _whatif_liabilities()}
    except Exception as e:
        return {"liabilities": [], "error": f"Couldn't load liabilities: {e}"}


# ============ P0-3: insurance & risk-transfer (insurance.py) ============
# Household facts the feeds can't see (incomes, children, in-force policies) live in a
# local JSON register; the balance-sheet side (net worth, liabilities, home, mortgage)
# comes from the same _data() everything else uses. Read-only analysis, no products.
INSURANCE_FILE = os.environ.get("INSURANCE_FILE", "/data/insurance.json")
DEFAULT_INSURANCE = {"income_self": None, "income_spouse": None,
                     # income_sources_self/_spouse (2026-07-24, Jason: "implement multiple income
                     # sources"): OPTIONAL itemized breakdown per earner -- {label, type
                     # (w2/1099/self_employment/rental/pension/other), annual_amount, notes}.
                     # income_self/income_spouse above stay the two scalars every downstream
                     # calc (insurance.py's need-sizing, get_benefits()'s tax facts, the ACA/tax
                     # MAGI figure, the setup checklist) already reads -- nothing else in the app
                     # had to change. _read_insurance_cfg() below auto-derives the scalar as the
                     # SUM of that person's sources list once it's non-empty (a household with two
                     # side gigs and a W2 no longer has to mentally add them up and re-enter one
                     # lump number every time something changes); an empty list leaves the scalar
                     # exactly as manually entered, so a single-income-number household sees no
                     # behavior change at all.
                     "income_sources_self": [], "income_sources_spouse": [],
                     "children": 0,
                     # no_income: explicit household confirmation of "$0, on purpose" (unemployed /
                     # retired / no earned income) -- 2026-07-23, Jason. Without this flag, a $0 entry
                     # in income_self/income_spouse is NOT counted as answered by the setup checklist
                     # (see _setup_field_filled's "income" kind) and the INSURANCE page's setup_needed
                     # highlight still fires -- both would otherwise treat an accidental/placeholder 0
                     # the same as a deliberate "we have no income" answer.
                     "no_income": False,
                     "home_value_override": None, "home_address": None, "policies": [],
                     # GAP-2 cash/debt facts (same household register, edited in PLAN→CASH·DEBT):
                     "mortgage_rate_pct": None, "mortgage_payment_monthly": None,
                     "deposit_yield_pct": None, "refi_rate_pct": None,
                     "refi_closing_costs": None, "debt_rates": {},
                     # debt_min_payments{name: $/mo}, debt_extra_monthly ($): RESUME-TRACKER
                     # 2026-07-20 payoff-simulator inputs — mirrors debt_rates' shape (keyed by
                     # the same Monarch account name). No amortization/snowball simulation is
                     # possible without a minimum payment per debt (a guessed one could mislead a
                     # real payoff schedule); debt_extra_monthly is the household's chosen extra
                     # $/mo budget to throw at whichever debt the strategy targets.
                     "debt_min_payments": {}, "debt_extra_monthly": None,
                     # is_mortgage_overrides{name: bool}: the "mortgage"/"home loan" name-substring
                     # heuristic below misses accounts like a Rocket-branded loan that never says
                     # "mortgage" in its Monarch name — a per-account override, checked in the
                     # PLAN→CASH·DEBT avalanche table, forces correct classification (diagnosed
                     # 2026-07-18; see _is_mortgage_account()).
                     "is_mortgage_overrides": {},
                     # pay_in_full_overrides{name: bool}: 2026-07-23, Jason -- "my credit cards with
                     # small balances that get paid every month will always be on top because this
                     # seems to still be factoring APR only." The avalanche method IS correctly
                     # rate-based (see cashdebt.avalanche()'s docstring), but a card you pay off in
                     # full every statement isn't real revolving debt -- its point-in-time balance
                     # can't tell the app that. This per-account flag (same MERGE-not-replace shape as
                     # is_mortgage_overrides, checked in the same PLAN→CASH·DEBT table) excludes an
                     # account from 'pay-first' classification, avalanche alerts, and the payoff
                     # simulator regardless of its snapshot balance/APR.
                     "pay_in_full_overrides": {},
                     # ef_accounts{name: {"on": bool, "cap": float|None}}: 2026-08-08, Jason -- the
                     # emergency fund was sized from a name-substring guess at what counts as
                     # "cash" (checking/savings/money-market). That guess is structurally blind to a
                     # reserve deliberately parked somewhere that earns a real return -- Jason's
                     # Wells Fargo investment account, opened because savings/HYSA yields weren't
                     # worth it -- so the gauge read 1.8 months / "critical" for a household that
                     # actually has a funded reserve. No heuristic can fix this: only the household
                     # knows which balances are earmarked. `on` designates the account; `cap` is an
                     # optional "count at most $X of it", which is what makes a partial earmark
                     # honest and is the ONLY correct way to count a Roth (the withdrawable amount
                     # is the contribution basis, which no feed exposes and which is nothing like
                     # the balance). Same MERGE-not-replace save semantics as the two override
                     # dicts above. Tiering/eligibility lives in _ef_tier(); the math is in
                     # cashdebt.ef_sources().
                     "ef_accounts": {},
                     # GAP-5 benefits facts (edited in PLAN→BENEFITS):
                     "k401_pretax_pct": None, "k401_aftertax_pct": 0.0,
                     "k401_match_t1_pct": None, "k401_match_t1_rate": None,
                     "k401_match_t2_pct": None, "k401_match_t2_rate": None,
                     "k401_biweekly_total": None,
                     # Pay-frequency for the salary cross-check above (Jason, 2026-07-18): used to
                     # be hardcoded to biweekly. "biweekly" default preserves old behavior for
                     # anyone who already saved a per-paycheck total under that assumption.
                     "k401_pay_frequency": "biweekly", "k401_pay_periods_custom": None,
                     "plan_aftertax": False, "inservice_rollover": "unknown",
                     "hsa_coverage": "family", "hsa_contribution": None, "hsa_invested": True,
                     # roth_ira_contribution_self/_spouse: YTD $ already contributed this year
                     # (RESUME-TRACKER 2026-07-20) — mirrors hsa_contribution's pattern. Without
                     # this, benefits.py's roth_ira headroom always assumed $0 contributed and
                     # ranked the full annual limit x2, with no way to know if it was already maxed.
                     "roth_ira_contribution_self": None, "roth_ira_contribution_spouse": None,
                     # roth_ira_monthly_self/_spouse (2026-07-30, the "pacing/projection" stretch
                     # goal from the same 2026-07-20 backlog item): an OPTIONAL recurring $/mo
                     # figure, used only to project whether the YTD contribution above is on pace
                     # to reach the annual limit by December — see benefits._roth_pacing(). Leaving
                     # either blank just means no projection is shown for that spouse (matches the
                     # YTD field's own "blank = not tracked" behavior), not an error.
                     "roth_ira_monthly_self": None, "roth_ira_monthly_spouse": None,
                     # OPP-5 continuity binder (PLAN→CONTINUITY): key-contacts register — attorney,
                     # CPA, financial advisor, insurance agent, executor, guardian, etc. Nothing here
                     # is computed; it's just surfaced verbatim into the binder for the survivor.
                     "contacts": [], "executor_name": None, "will_location": None,
                     # GAP-8 estate hygiene audit (PLAN→CONTINUITY, DOCUMENT CURRENCY): per-document
                     # last-reviewed dates + a review-cadence knob, feeding a G13-style staleness
                     # tripwire in _alerts_digest() (section 8) rather than a one-time report.
                     "estate_docs": [], "estate_doc_review_years": 3}
_INS_POLICY_KEYS = ("kind", "owner", "name", "coverage", "premium_annual", "term_expires", "notes")
_INCOME_SOURCE_KEYS = ("label", "type", "annual_amount", "notes")
_INCOME_SOURCE_TYPES = ("w2", "1099", "self_employment", "rental", "pension", "other")
_CONTACT_KEYS = ("role", "name", "phone", "email", "notes")
_ESTATE_DOC_KEYS = ("doc_type", "last_reviewed", "notes")
# Common estate-hygiene documents to track currency on (GAP-8). Not exhaustive — "notes" lets a
# user track anything else (a trust, a specific beneficiary designation) under an open doc_type.
ESTATE_DOC_TYPES = ("will", "durable_poa", "healthcare_directive", "trust", "beneficiary_review")


def _income_sources_total(sources):
    """Sum of annual_amount across a person's income_sources list -- None-safe, ignores any
    row with a non-numeric amount rather than raising (a half-filled row shouldn't crash the
    whole config read)."""
    total = 0.0
    any_row = False
    for row in (sources or []):
        try:
            amt = row.get("annual_amount")
            if amt in (None, ""):
                continue
            total += float(amt)
            any_row = True
        except (TypeError, ValueError, AttributeError):
            continue
    return total if any_row else None


def _read_insurance_cfg():
    try:
        cfg = store.read_json_strict(INSURANCE_FILE) or {}
    except Exception:
        cfg = {}
    merged = {**DEFAULT_INSURANCE, **{k: cfg.get(k) for k in DEFAULT_INSURANCE if k in cfg}}
    # Auto-derive income_self/income_spouse from the itemized sources list, once it's non-empty --
    # the sources list becomes that person's source of truth; a still-empty list leaves whatever
    # was manually entered in income_self/income_spouse completely untouched (backward compatible
    # with every household that just enters one lump number and never uses the breakdown).
    for scalar_key, sources_key in (("income_self", "income_sources_self"),
                                     ("income_spouse", "income_sources_spouse")):
        derived = _income_sources_total(merged.get(sources_key))
        if derived is not None:
            merged[scalar_key] = derived
    return merged


@app.get("/api/insurance-config")
def get_insurance_config():
    return _read_insurance_cfg()


@app.post("/api/insurance-config")
async def post_insurance_config(req: Request):
    """Set household insurance facts (local config; not a money action).
    Body keys: income_self, income_spouse, children, home_value_override, and/or
    policies: FULL replacement list of {kind, owner, name, coverage, premium_annual,
    term_expires, notes} (kind: life|disability|umbrella|home|auto|ltc).
    income_sources_self / income_sources_spouse (2026-07-24): OPTIONAL FULL replacement lists of
    {label, type, annual_amount, notes} (type: w2|1099|self_employment|rental|pension|other) --
    itemized income breakdown per earner. Once either list is non-empty, _read_insurance_cfg()
    auto-derives that person's income_self/income_spouse scalar as the sum of their sources on
    every read, overriding whatever's separately saved under that scalar key -- a household that
    doesn't use this stays on the plain single-number entry with zero behavior change."""
    body = await req.json()
    old = _read_insurance_cfg()   # independent read, captured before any mutation below
    cfg = _read_insurance_cfg()
    for k in ("income_self", "income_spouse", "children", "home_value_override",
              "mortgage_rate_pct", "mortgage_payment_monthly", "deposit_yield_pct",
              "refi_rate_pct", "refi_closing_costs",
              "k401_pretax_pct", "k401_aftertax_pct", "k401_match_t1_pct", "k401_match_t1_rate",
              "k401_match_t2_pct", "k401_match_t2_rate", "k401_biweekly_total",
              "k401_pay_periods_custom", "hsa_contribution",
              "debt_extra_monthly", "roth_ira_contribution_self", "roth_ira_contribution_spouse",
              "roth_ira_monthly_self", "roth_ira_monthly_spouse"):
        if k in body:
            try:
                cfg[k] = None if body[k] in (None, "") else float(body[k])
            except (TypeError, ValueError):
                pass
    if isinstance(body.get("debt_rates"), dict):
        dr = {}
        for nm, v in list(body["debt_rates"].items())[:30]:
            try:
                dr[str(nm)[:120]] = float(v)
            except (TypeError, ValueError):
                pass
        cfg["debt_rates"] = dr
    if isinstance(body.get("debt_min_payments"), dict):
        dmp = {}
        for nm, v in list(body["debt_min_payments"].items())[:30]:
            try:
                dmp[str(nm)[:120]] = float(v)
            except (TypeError, ValueError):
                pass
        cfg["debt_min_payments"] = dmp
    if isinstance(body.get("is_mortgage_overrides"), dict):
        # MERGE (not replace) — an account correctly flagged mortgage no longer appears in the
        # avalanche table, so it can't be re-sent by a later save; a full-replace would silently
        # drop the flag and regress the very bug this fixes. Cap prevents unbounded growth.
        mo = dict(cfg.get("is_mortgage_overrides") or {})
        for nm, v in list(body["is_mortgage_overrides"].items())[:30]:
            mo[str(nm)[:120]] = bool(v)
        cfg["is_mortgage_overrides"] = mo
    if isinstance(body.get("pay_in_full_overrides"), dict):
        # MERGE, same reasoning as is_mortgage_overrides just above.
        pf = dict(cfg.get("pay_in_full_overrides") or {})
        for nm, v in list(body["pay_in_full_overrides"].items())[:30]:
            pf[str(nm)[:120]] = bool(v)
        cfg["pay_in_full_overrides"] = pf
    if isinstance(body.get("ef_accounts"), dict):
        # MERGE, same reasoning as the two override dicts above -- and load-bearing here for a
        # second reason: the EF editor and the CASH·DEBT panel can both post this key, and a
        # full-replace from either would silently drop designations the other one knows about.
        # Three states (2026-08-08b): a value of None removes the entry entirely (back to "never
        # ruled on"); {"state":"no"} stores an explicit exclusion that must SURVIVE, because that
        # is the answer the panel stops asking about; {"state":"yes"} / the legacy {"on":true}
        # shape counts the account. An explicit "no" is deliberately NOT popped — popping it is
        # what would resurrect the nag Jason asked us to silence.
        ea = dict(cfg.get("ef_accounts") or {})
        for nm, v in list(body["ef_accounts"].items())[:100]:
            key = str(nm)[:120]
            if v is None:
                ea.pop(key, None)          # explicit reset to unset
                continue
            if v is False:
                ea[key] = {"on": False, "excluded": True}
                continue
            if not isinstance(v, dict):
                continue
            state = v.get("state")
            if state not in EF_STATES:
                state = "yes" if v.get("on") else ("no" if v.get("excluded") else "unset")
            if state == "unset":
                ea.pop(key, None)
                continue
            if state == "no":
                ea[key] = {"on": False, "excluded": True}
                continue
            cap = v.get("cap")
            try:
                cap = None if cap in (None, "") else max(0.0, float(cap))
            except (TypeError, ValueError):
                cap = None
            ea[key] = {"on": True, "cap": cap}
        cfg["ef_accounts"] = ea
    if "home_address" in body:
        cfg["home_address"] = (str(body["home_address"]).strip()[:200] or None) \
            if body["home_address"] not in (None, "") else None
    for k in ("executor_name", "will_location"):
        if k in body:
            cfg[k] = (str(body[k]).strip()[:200] or None) if body[k] not in (None, "") else None
    if isinstance(body.get("contacts"), list):
        cfg["contacts"] = [{k: (str(c.get(k))[:200] if c.get(k) not in (None, "") else None)
                            for k in _CONTACT_KEYS}
                           for c in body["contacts"] if isinstance(c, dict) and c.get("role")][:20]
    if isinstance(body.get("estate_docs"), list):
        cfg["estate_docs"] = [{k: (str(d.get(k))[:200] if d.get(k) not in (None, "") else None)
                               for k in _ESTATE_DOC_KEYS}
                              for d in body["estate_docs"] if isinstance(d, dict) and d.get("doc_type")][:20]
    if "estate_doc_review_years" in body:
        try:
            cfg["estate_doc_review_years"] = max(1, float(body["estate_doc_review_years"]))
        except (TypeError, ValueError):
            pass
    if body.get("inservice_rollover") in ("yes", "no", "unknown"):
        cfg["inservice_rollover"] = body["inservice_rollover"]
    if body.get("hsa_coverage") in ("self", "family"):
        cfg["hsa_coverage"] = body["hsa_coverage"]
    if body.get("k401_pay_frequency") in ("weekly", "biweekly", "semimonthly", "monthly", "custom"):
        cfg["k401_pay_frequency"] = body["k401_pay_frequency"]
    for bk in ("plan_aftertax", "hsa_invested", "no_income"):
        if bk in body:
            cfg[bk] = bool(body[bk])
    if isinstance(body.get("policies"), list):
        cfg["policies"] = [{k: p.get(k) for k in _INS_POLICY_KEYS}
                           for p in body["policies"] if isinstance(p, dict) and p.get("kind")][:50]
    for sk in ("income_sources_self", "income_sources_spouse"):
        if isinstance(body.get(sk), list):
            rows = []
            for r in body[sk][:30]:
                if not isinstance(r, dict) or not str(r.get("label") or "").strip():
                    continue
                try:
                    amt = None if r.get("annual_amount") in (None, "") else max(0.0, float(r["annual_amount"]))
                except (TypeError, ValueError):
                    amt = None
                rows.append({
                    "label": str(r.get("label"))[:120],
                    "type": r.get("type") if r.get("type") in _INCOME_SOURCE_TYPES else "other",
                    "annual_amount": amt,
                    "notes": (str(r.get("notes"))[:300] if r.get("notes") not in (None, "") else None),
                })
            cfg[sk] = rows
    store.write_json(INSURANCE_FILE, cfg)
    # PII-bulk fields summarized as before/after counts, not full contents — see
    # _emit_config_changed's docstring for why (avoids duplicating every policy/contact/estate-doc
    # record into the audit log on a routine save).
    _emit_config_changed(req, "/api/insurance-config", old, cfg,
                         summarize_keys={"policies", "contacts", "estate_docs",
                                         "income_sources_self", "income_sources_spouse"})
    return {"ok": True, "config": cfg}


# ============ Manual liability accounts (2026-07-30) ============
# Gap: PORTFOLIO_PROVIDER=csv (and snaptrade) users had NO way to record a loan/credit card --
# _build_csv()/_build_snaptrade() hardcode is_asset:True on every account and total_liabilities
# stays 0.0, so CASH·DEBT, net worth, the estate binder's liabilities list, etc. all silently see
# a household as debt-free the moment they leave Monarch. Deliberately NOT an extension of the
# holdings.csv row shape (HOLDINGS_CSV_COLUMNS) -- that table requires a ticker + positive share
# quantity (built for tradable positions); a liability has neither, so bolting it on would mean
# awkward blank ticker/quantity cells with unclear validation. Instead: a small separate
# name-keyed register (manual_liabilities.json), applied as an OVERLAY inside build() itself
# (see _apply_manual_liabilities) rather than inside any one provider function -- so it works for
# EVERY provider with one code path, not three (a Monarch household could use it too, e.g. for a
# private family loan Monarch itself never syncs).
#
# rate_pct/min_payment/pay_in_full/mortgage-classification are deliberately NOT duplicated into
# manual_liabilities.json -- they're read from and written straight into insurance.json's existing
# debt_rates/debt_min_payments/pay_in_full_overrides/is_mortgage_overrides dicts, the exact same
# name-keyed fields Monarch-sourced debts already use (see get_cashdebt()). One source of truth:
# SETTINGS -> PLAN CASH·DEBT and this editor can never disagree about the same account's rate.
MANUAL_LIABILITIES_FILE = os.environ.get("MANUAL_LIABILITIES_FILE", "/data/manual_liabilities.json")
LIABILITY_TYPE_HINTS = ["Credit Card", "Auto Loan", "Student Loan", "Personal Loan", "HELOC",
                        "Mortgage", "Other Loan"]


def _read_manual_liability_rows():
    """Saved liability identities (name/institution/type/balance/notes) merged with their
    rate_pct/min_payment/pay_in_full/is_mortgage read back from insurance.json, for display in the
    editor. is_mortgage falls back to (type == "Mortgage") when no explicit override exists yet --
    matches what a fresh row would get stamped with on its first save (see
    _write_manual_liability_rows)."""
    try:
        rows = store.read_json_strict(MANUAL_LIABILITIES_FILE) or []
    except Exception:
        rows = []
    if not isinstance(rows, list):
        rows = []
    cfg = _read_insurance_cfg()
    rates = cfg.get("debt_rates") or {}
    mins = cfg.get("debt_min_payments") or {}
    pif = cfg.get("pay_in_full_overrides") or {}
    mort = cfg.get("is_mortgage_overrides") or {}
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        nm = r.get("name") or ""
        out.append({
            "name": nm,
            "institution": r.get("institution") or "",
            "type": r.get("type") or "Other Loan",
            "balance": r.get("balance"),
            "notes": r.get("notes") or "",
            "rate_pct": rates.get(nm),
            "min_payment": mins.get(nm),
            "pay_in_full": bool(pif.get(nm)),
            "is_mortgage": bool(mort[nm]) if nm in mort else (r.get("type") == "Mortgage"),
        })
    return out


def _validate_manual_liability_rows(rows):
    """Mirrors _validate_holdings_rows' all-or-nothing contract: a save fails loudly with every
    row's error rather than silently dropping or mis-saving one."""
    clean, errors = [], []
    seen = set()
    for i, r in enumerate(rows, start=1):
        if not isinstance(r, dict):
            errors.append({"row": i, "message": "Malformed row."})
            continue
        nm = (r.get("name") or "").strip()
        if not nm:
            errors.append({"row": i, "message": "Name is required."})
            continue
        if nm.lower() in seen:
            errors.append({"row": i, "message": f"{nm}: duplicate liability name (names must be unique)."})
            continue
        seen.add(nm.lower())
        try:
            bal = float(r.get("balance") or 0)
        except (TypeError, ValueError):
            errors.append({"row": i, "message": f"{nm}: balance must be a number."})
            continue
        if bal <= 0:
            errors.append({"row": i, "message": f"{nm}: balance must be greater than 0."})
            continue
        rate_pct = r.get("rate_pct")
        if rate_pct not in (None, ""):
            try:
                rate_pct = float(rate_pct)
            except (TypeError, ValueError):
                errors.append({"row": i, "message": f"{nm}: rate % must be a number."})
                continue
            if rate_pct < 0 or rate_pct > 100:
                errors.append({"row": i, "message": f"{nm}: rate % must be between 0 and 100."})
                continue
        else:
            rate_pct = None
        min_payment = r.get("min_payment")
        if min_payment not in (None, ""):
            try:
                min_payment = float(min_payment)
            except (TypeError, ValueError):
                errors.append({"row": i, "message": f"{nm}: min payment must be a number."})
                continue
            if min_payment < 0:
                errors.append({"row": i, "message": f"{nm}: min payment can't be negative."})
                continue
        else:
            min_payment = None
        clean.append({
            "name": nm,
            "institution": (r.get("institution") or "").strip(),
            "type": (r.get("type") or "").strip() or "Other Loan",
            "balance": round(bal, 2),
            "notes": (r.get("notes") or "").strip(),
            "rate_pct": rate_pct,
            "min_payment": min_payment,
            "pay_in_full": bool(r.get("pay_in_full")),
        })
    return clean, errors


def _write_manual_liability_rows(clean_rows):
    """Splits each clean row across the two stores described above, then invalidates the
    portfolio cache so the next /api/portfolio read reflects the change instead of serving a
    stale build for up to TTL seconds (same as _write_holdings_csv_rows)."""
    identity_rows = [{"name": r["name"], "institution": r["institution"], "type": r["type"],
                      "balance": r["balance"], "notes": r["notes"]} for r in clean_rows]
    store.write_json(MANUAL_LIABILITIES_FILE, identity_rows)

    cfg = _read_insurance_cfg()
    rates = dict(cfg.get("debt_rates") or {})
    mins = dict(cfg.get("debt_min_payments") or {})
    pif = dict(cfg.get("pay_in_full_overrides") or {})
    mort = dict(cfg.get("is_mortgage_overrides") or {})
    for r in clean_rows:
        nm = r["name"]
        if r["rate_pct"] is not None:
            rates[nm] = r["rate_pct"]
        if r["min_payment"] is not None:
            mins[nm] = r["min_payment"]
        pif[nm] = r["pay_in_full"]
        if r["type"] == "Mortgage":
            mort[nm] = True
    cfg["debt_rates"] = rates
    cfg["debt_min_payments"] = mins
    cfg["pay_in_full_overrides"] = pif
    cfg["is_mortgage_overrides"] = mort
    store.write_json(INSURANCE_FILE, cfg)
    _cache["data"] = None


def _apply_manual_liabilities(d):
    """Overlay manually-entered liability accounts onto whichever provider built `d` -- called once,
    at the very end of build(), so CSV/SnapTrade users (no native liability sync) and Monarch users
    (tracking something Monarch itself doesn't see) are all covered by one code path. Each manual
    row becomes an is_asset:False account in EXACTLY the shape _build_monarch() produces, so
    cashdebt.py/estate.py/tax-audit/the continuity binder -- every existing is_asset consumer --
    needs zero changes to recognize it."""
    rows = _read_manual_liability_rows()
    if not rows:
        return d
    accounts = d.get("accounts") or []
    existing_names = {(a.get("name") or "").strip().lower() for a in accounts}
    added_liab = 0.0
    for r in rows:
        bal = r.get("balance") or 0
        if not bal or bal <= 0:
            continue
        nm = r.get("name") or "Manual Liability"
        if nm.strip().lower() in existing_names:
            continue   # a same-named provider-synced account already covers it -- don't double-count
        accounts.append({
            "id": "manual:" + nm, "name": nm, "institution": r.get("institution") or None,
            "type": r.get("type") or "Other Loan", "balance": round(bal, 2),
            "holdings_count": 0, "is_asset": False, "include_nw": True, "manual": True,
        })
        added_liab += bal
    if added_liab:
        d["accounts"] = accounts
        d["account_count"] = len(accounts)
        d["total_liabilities"] = round((d.get("total_liabilities") or 0) + added_liab, 2)
        d["net_worth"] = round((d.get("net_worth") or 0) - added_liab, 2)
    return d


@app.get("/api/manual-liabilities")
def get_manual_liabilities():
    """Manually-entered liability accounts (loans/credit cards) -- most useful for csv/snaptrade
    users with no native liability sync, but available regardless of active provider (same as
    /api/holdings-csv)."""
    return {"rows": _read_manual_liability_rows(), "type_hints": LIABILITY_TYPE_HINTS}


@app.post("/api/manual-liabilities")
async def post_manual_liabilities(req: Request):
    """Replace the manual liability list wholesale from the in-app table editor. Body:
    {"rows": [{"name","institution","type","balance","rate_pct","min_payment","pay_in_full",
    "notes"}...]}. All-or-nothing (same contract as /api/holdings-csv): any row failing validation
    aborts the whole save with every error returned together."""
    body = await req.json()
    rows = body.get("rows")
    if not isinstance(rows, list):
        return JSONResponse({"error": "Body must be {\"rows\": [...]}."}, status_code=400)
    clean, errors = _validate_manual_liability_rows(rows)
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)
    old_cfg = _read_insurance_cfg()
    _write_manual_liability_rows(clean)
    new_cfg = _read_insurance_cfg()
    _emit_config_changed(req, "/api/manual-liabilities", old_cfg, new_cfg)
    return {"ok": True, "rows_written": len(clean)}


@app.get("/api/insurance")
def get_insurance():
    """Protection-desk gap analysis: needs computed from the live balance sheet + the
    policy register. Cheap (no LLM, no network beyond the cached TIPS yield)."""
    cfg = _read_insurance_cfg()
    d = _data()
    ret = _read_retire()
    accounts = d.get("accounts") or []
    home_val = cfg.get("home_value_override")
    if not home_val:
        re_accts = [a for a in accounts
                    if (a.get("type") or "").lower().find("real estate") >= 0
                    and a.get("is_asset") is not False and (a.get("balance") or 0) > 0]
        home_val = max((a.get("balance") or 0 for a in re_accts), default=0)
    mort = sum((a.get("balance") or 0) for a in accounts
               if a.get("is_asset") is False
               and any(w in ((a.get("name") or "") + " " + (a.get("type") or "")).lower()
                       for w in ("mortgage", "home loan")))
    try:
        rr, _src, _dt = _tips_real_yield()
    except Exception:
        rr = _knob("tips_fallback_pct")
    household = {"net_worth": d.get("net_worth"),
                 "liquid_assets": d.get("invested_value"),
                 "debts_total": d.get("total_liabilities"),
                 "mortgage_balance": round(mort, 2),
                 "income_self": cfg.get("income_self"),
                 "income_spouse": cfg.get("income_spouse"),
                 "no_income": bool(cfg.get("no_income")),
                 "age_self": _derive_age(ret.get("birth_year"), ret.get("current_age")),
                 "age_spouse": _derive_age(ret.get("spouse_birth_year"), ret.get("spouse_age")),
                 "retire_age": ret.get("target_age") or 60,
                 "children": cfg.get("children") or 0,
                 "home_value": home_val,
                 # Raw override value (2026-07-23, new UI field) — distinct from "home_value"
                 # above, which is the RESOLVED value (override if set, else auto-detected from
                 # the Monarch/Zillow real-estate account balance). Exposing the raw override
                 # separately lets the frontend input show blank when nothing was ever explicitly
                 # entered, instead of pre-filling with the auto-detected balance and making an
                 # untouched field look "set."
                 "home_value_override": cfg.get("home_value_override"),
                 "real_rate_pct": rr}
    sinfo = _state_of_residence_info(ret, d)
    household["state"] = sinfo["code"]
    res = insurance.run(household, cfg.get("policies"))
    res["household"] = household
    res["policies_echo"] = cfg.get("policies") or []
    res["income_sources_self"] = cfg.get("income_sources_self") or []
    res["income_sources_spouse"] = cfg.get("income_sources_spouse") or []
    res["state_of_residence_source"] = sinfo["source"]
    res["generated_at"] = int(time.time())
    if not cfg.get("income_self") and not cfg.get("income_spouse") and not cfg.get("no_income"):
        res["setup_needed"] = True
    return res


# ---- Tax engine wiring (G5) --------------------------------------------------
# One bracket model (tax.py) feeds every place that used to carry a flat tax knob.
# Env vars still override the derived value when explicitly set, so nothing breaks.

def _tax_ctx(cfg=None):
    """Filing status + ages for the tax engine, from retirement config / env."""
    cfg = cfg if cfg is not None else _read_retire()
    status = os.environ.get("TAX_FILING_STATUS") or cfg.get("filing_status") or "mfj"
    return {"status": tax.norm_status(status),
            "age": _derive_age(cfg.get("birth_year"), cfg.get("current_age")),
            "spouse_age": _derive_age(cfg.get("spouse_birth_year"), cfg.get("spouse_age")),
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
    Read-only / educational — not tax advice. Federal brackets are the same everywhere;
    a light state-tax estimate (states.py, item 3) rides along in `state_tax`."""
    cfg = _read_retire()
    ctx = _tax_ctx(cfg)
    sinfo = _state_of_residence_info(cfg)
    # default the income input to the household's modeled expenses if not supplied
    if not gross_ordinary:
        try:
            gross_ordinary = float(cfg.get("annual_expenses_override") or 0) or 0.0
        except Exception:
            gross_ordinary = 0.0
    try:
        res = tax.summary(gross_ordinary=gross_ordinary, ltcg=ltcg,
                          status=ctx["status"], age=ctx["age"], spouse_age=ctx["spouse_age"],
                          fill_to_rate=ctx["fill_to_rate"], magi=magi, state=sinfo["code"])
        res["state_of_residence_source"] = sinfo["source"]
        return res
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
    see whether it crosses the cliff. Only relevant pre-Medicare. Educational.
    Medicaid-expansion status (states.py, item 3) changes what a MAGI below 100% FPL
    means — a real coverage gap (non-expansion states) vs. likely Medicaid (expansion)."""
    cfg = _read_retire()
    ctx = _aca_ctx(cfg)
    sinfo = _state_of_residence_info(cfg)
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
        exp = float(cfg.get("annual_expenses_override") or 0) or 0.0
        components = {"ira_401k_withdrawals": exp}
    try:
        res = aca.summary(components=components, household_size=hh, slcsp_annual=slcsp,
                          planned_conversion=planned_conversion, planned_gains=planned_gains,
                          state=sinfo["code"])
        res["state_of_residence_source"] = sinfo["source"]
        return res
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
        c = store.read_json_strict(TIPS_CACHE_FILE) or {}
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
                store.write_json(TIPS_CACHE_FILE, {"fetched": today.isoformat(), "yield_pct": y,
                               "obs_date": dt, "source": f"FRED {TIPS_SERIES}"})
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
    so solvency accrues a history right beside net worth. Best-effort, never raises.
    Storage Stage B (2026-07-24): a plain UPDATE ... WHERE date=today, which -- same as the
    original's `if hist[-1].date==today` guard -- silently no-ops if today's value-snapshot row
    doesn't exist yet (this function only ever patches an existing row, never creates one)."""
    if fr is None:
        return
    try:
        today = datetime.date.today().isoformat()
        store.value_history_set_funded_ratio(today, fr)
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
                   "annual_savings": annual_savings,
                   # current_age/spouse_age (2026-07-23, superseding the same-day fix that added a
                   # manual "Spouse age" input): DERIVED from birth_year/spouse_birth_year via
                   # _derive_age() rather than manually entered — Jason caught, entering spouse
                   # birth year didn't satisfy the "Spouse age" checklist item, which exposed that
                   # this app was asking for two separately-maintained, redundant facts per person
                   # (current_age drifts stale every birthday; birth year never does) for BOTH
                   # people, not just the newly-added spouse field. Confirmed year-only precision
                   # is sufficient before making this the source of truth: every age-gated rule in
                   # this codebase (tax.py's 65+ standard-deduction addon; benefits.py's 401(k)
                   # 50+/60-63/HSA 55+/IRA 50+ catch-up thresholds) is a plain `age >= N` integer
                   # check, matching the IRS's OWN "attained by year-end" convention for every one
                   # of those rules — not an approximation. See _derive_age()'s docstring.
                   "current_age": _derive_age(cfg.get("birth_year"), cfg.get("current_age")),
                   "target_age": cfg.get("target_age"),
                   "spouse_age": _derive_age(cfg.get("spouse_birth_year"), cfg.get("spouse_age")),
                   # state_of_residence (2026-07-23): was never echoed here, so the ASSUMPTIONS
                   # dropdown always rendered as "Auto-detect" on reload even after a user
                   # explicitly picked a state — display-only (every real calculation reads the raw
                   # config directly via _state_of_residence_info(), so this never affected actual
                   # numbers), but confusing. Fixed by echoing it like every other field here.
                   "state_of_residence": cfg.get("state_of_residence"),
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
    age = _derive_age(cfg.get("birth_year"), cfg.get("current_age"))
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


@app.post("/api/run-retirement")
def run_retirement_ep():
    """Manual trigger for the 'Retirement projection' scheduled job — see run_rebalance_ep's
    docstring above for why this endpoint was missing and what it's for (Task Schedule RUN NOW)."""
    jid, created = _enqueue("retirement", source="api")
    return {"ok": True, "job_id": jid, "created": created}


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
    # NOTE: the literal percent MUST be escaped as %% in a %-format string — an unescaped "0%-LTCG"
    # makes Python read "%-L…" as a (bad) conversion spec and raise "unsupported format character 'T'",
    # which crashed the WHOLE /api/income-plan endpoint (502) on every call (found 2026-07-18 via the
    # golden_eval annual_dividends question skipping).
    gains_budget_src = "bracket-engine 0%%-LTCG room (%s)" % _tctx["status"]
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
        cfg = {**DEFAULT_SCANNER, **(store.read_json_strict(SCANNER_FILE) or {})}
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
    old = _read_scanner()   # independent read, captured before any mutation
    cfg = {**_read_scanner(), **{k: v for k, v in body.items() if k in DEFAULT_SCANNER}}
    if "criteria" in body and isinstance(body["criteria"], dict):
        cfg["criteria"] = {**DEFAULT_SCANNER["criteria"], **body["criteria"]}
    store.write_json(SCANNER_FILE, cfg)
    _emit_config_changed(req, "/api/scanner-config", old, cfg)
    return {"ok": True, "config": cfg}


def _read_accounts_cfg():
    """Per-account settings: {"defaults": {drip, fractional_ok}, "accounts": {name: {...}}}."""
    try:
        cfg = store.read_json_strict(ACCOUNTS_FILE) or {}
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
    old = _read_accounts_cfg()   # independent read, captured before any mutation
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
    store.write_json(ACCOUNTS_FILE, cfg)
    # "accounts" summarized as a count (per-account DRIP/fractional flags aren't sensitive, but a
    # full per-account diff is noisy for a household with many accounts); "defaults" logged in full.
    _emit_config_changed(req, "/api/accounts-config", old, cfg, summarize_keys={"accounts"})
    return {"ok": True, "config": cfg}


# ============ Unified account registry (2026-08-08, Jason) ============
# Per-account settings had accreted into three unrelated places: DRIP/fractional under
# AUDITS→ACCOUNTS (investment accounts only), APR/min-payment/paid-in-full/is-mortgage in the
# PLAN→CASH·DEBT table and the manual-liabilities editor, and nothing at all for cash accounts.
# There was no single screen that could answer "what does the app know about this account, and
# what can I tell it?" -- which is how the emergency-fund designation had nowhere obvious to live.
#
# This endpoint is that screen's backend: ONE list of every known account, each carrying the set
# of toggles that actually apply to it and nothing else (DRIP on a checking account is
# meaningless; an APR on a brokerage account is meaningless). It deliberately does NOT introduce
# a new store -- every field reads from and writes back to the exact same JSON the existing
# editors use, so SETTINGS→ACCOUNTS, AUDITS→ACCOUNTS and PLAN→CASH·DEBT can never disagree about
# the same account. That is the same one-source-of-truth rule _read_manual_liability_rows()
# already follows for the liability editor.
_REGISTRY_GROUPS = [
    ("cash",        "CASH & LIQUIDITY"),
    ("taxable",     "TAXABLE INVESTMENT"),
    ("advantaged",  "TAX-ADVANTAGED"),
    ("other",       "OTHER ASSETS"),
    ("liability",   "LIABILITIES"),
]


def _registry_group(a):
    """Which registry section an account is listed under.

    Tax status is checked BEFORE the cash-name heuristic, for the same reason _ef_tier() does it
    (see that docstring): "IRA BROKERAGE CASH" contains "cash " and "Health Savings Account"
    contains "savings", so the naive order files five tax-advantaged accounts under CASH &
    LIQUIDITY. Fixing only _ef_tier left this half-fixed and visibly wrong (2026-08-08, caught by
    reading the deployed UI): the EF checkbox correctly read "blocked", but the rows still sat in
    the cash section, and because they're investment accounts they dragged DRIP and FRACTIONAL
    columns into that section — leaving ten genuine cash accounts rendering two columns of "—".
    Any future account classifier in this file wants this same ordering."""
    if a.get("is_asset") is False:
        return "liability"
    st = _account_tax_status(a.get("name"), a.get("type"))
    if st == "taxable":
        return "taxable"
    if st in ("roth", "deferred", "hsa"):
        return "advantaged"
    if _is_cash_account(a):
        return "cash"
    return "other"


@app.get("/api/account-registry")
def get_account_registry():
    """Every known account with the per-account settings that apply to it, grouped by what the
    account IS. `applicable` names the toggles the UI should render for that row; anything absent
    from it is not a meaningful setting for that account type and should not be shown at all
    (rather than shown greyed-out, which reads as "broken"). The one exception is `ef` on a
    tax-deferred account: that IS rendered, disabled, with `ef_blocked_reason` -- the user needs to
    see WHY their 401(k) can't be an emergency fund, not just find the box missing."""
    d = _data()
    acct_cfg = _read_accounts_cfg()
    cfg = _read_insurance_cfg()
    rates = cfg.get("debt_rates") or {}
    mins = cfg.get("debt_min_payments") or {}
    pif = cfg.get("pay_in_full_overrides") or {}
    mort = cfg.get("is_mortgage_overrides") or {}
    ef_cfg = cfg.get("ef_accounts") or {}
    rows = []
    for a in d.get("accounts") or []:
        nm = a.get("name") or "?"
        grp = _registry_group(a)
        st = _account_tax_status(nm, a.get("type"))
        tier = _ef_tier(a)
        r = {"name": nm, "institution": a.get("institution"), "type": a.get("type"),
             "balance": a.get("balance"), "is_asset": a.get("is_asset") is not False,
             "group": grp, "tax_status": st, "manual": bool(a.get("manual")),
             "holdings_count": a.get("holdings_count") or 0, "applicable": []}
        if grp == "liability":
            r["applicable"] = ["rate_pct", "min_payment", "pay_in_full", "is_mortgage"]
            r["rate_pct"] = rates.get(nm)
            r["min_payment"] = mins.get(nm)
            r["pay_in_full"] = bool(pif.get(nm))
            r["is_mortgage"] = (bool(mort[nm]) if nm in mort
                                else _is_mortgage_account(a, mort))
        else:
            if st != "na":
                fl = _account_flags(nm, acct_cfg)
                r["applicable"] += ["drip", "fractional_ok"]
                r["drip"] = fl["drip"]
                r["fractional_ok"] = fl["fractional_ok"]
            if tier is not None:
                r["applicable"] += ["ef", "ef_cap"]
                ent = ef_cfg.get(nm) if isinstance(ef_cfg.get(nm), dict) else {}
                r["ef_state"] = _ef_state(ent)
                r["ef"] = (r["ef_state"] == "yes")   # kept for any older consumer of the boolean
                r["ef_cap"] = ent.get("cap")
                r["ef_tier"] = tier
                r["ef_tier_label"] = cashdebt.EF_TIERS[tier]["label"]
                r["ef_eligible"] = tier not in cashdebt.EF_INELIGIBLE_TIERS
                if not r["ef_eligible"]:
                    r["ef_blocked_reason"] = (
                        "Tax-deferred. An early withdrawal is taxed as ordinary income AND takes a "
                        "10% penalty (HSA: for non-medical spending; 529: on earnings), so it can't "
                        "function as emergency money — it would be counted at $0 anyway.")
                elif tier == "roth_basis":
                    r["ef_cap_hint"] = (
                        "Roth: only your CONTRIBUTION BASIS comes out tax- and penalty-free, and no "
                        "feed exposes it. Set the cap to your total lifetime contributions — leaving "
                        "it blank counts the whole balance and overstates your reserve.")
                elif tier == "taxable":
                    r["ef_cap_hint"] = (
                        "Optional: count only part of this account as reserve. Leave blank to count "
                        "the full balance. Market-risk sources are also shown after a drawdown "
                        "haircut on the EF gauge.")
        rows.append(r)
    rows.sort(key=lambda x: ([g for g, _ in _REGISTRY_GROUPS].index(x["group"]),
                             -abs(x.get("balance") or 0)))
    return {"groups": [{"key": g, "label": lbl} for g, lbl in _REGISTRY_GROUPS],
            "accounts": rows,
            "ef_stress_drawdown_pct": _knob("ef_stress_drawdown_pct"),
            "ef_target_months": _knob("ef_target_months")}


@app.post("/api/account-registry")
async def post_account_registry(req: Request):
    """Save one account's settings from the unified editor. Body:
    {"account": name, "drip": bool, "fractional_ok": bool, "ef": bool, "ef_cap": num|null,
     "rate_pct": num|null, "min_payment": num|null, "pay_in_full": bool, "is_mortgage": bool}
    -- only the keys present are touched. Fields are routed to whichever store already owns them
    (accounts.json for the investment flags, insurance.json for everything else); this endpoint
    adds no store of its own, so it cannot drift from the older editors that write the same keys.

    A tax-deferred account cannot be designated as an EF source: the request is accepted but the
    `ef` field is dropped and reported back in `rejected`, so the UI can say why instead of the
    save appearing to work and the number never moving."""
    body = await req.json()
    nm = (body.get("account") or "").strip()
    if not nm:
        return JSONResponse({"error": 'Body must include {"account": name}.'}, status_code=400)
    d = _data()
    acct = next((a for a in (d.get("accounts") or []) if (a.get("name") or "") == nm), None)
    if acct is None:
        return JSONResponse({"error": f"No account named {nm!r}."}, status_code=404)
    rejected, touched = [], []

    if any(k in body for k in ("drip", "fractional_ok")):
        old_ac = _read_accounts_cfg()
        ac = _read_accounts_cfg()
        cur = dict(ac["accounts"].get(nm) or {})
        for k in ("drip", "fractional_ok"):
            if k in body:
                if _account_tax_status(nm, acct.get("type")) == "na":
                    rejected.append({"field": k, "reason": "Not an investment account — DRIP and "
                                                           "fractional-share settings don't apply."})
                    continue
                cur[k] = bool(body[k])
                touched.append(k)
        ac["accounts"][nm] = cur
        store.write_json(ACCOUNTS_FILE, ac)
        _emit_config_changed(req, "/api/account-registry", old_ac, ac, summarize_keys={"accounts"})

    ins_keys = ("ef", "ef_cap", "rate_pct", "min_payment", "pay_in_full", "is_mortgage")
    if any(k in body for k in ins_keys):
        old_cfg = _read_insurance_cfg()
        cfg = _read_insurance_cfg()
        is_liab = acct.get("is_asset") is False
        if any(k in body for k in ("ef", "ef_state", "ef_cap")):
            tier = _ef_tier(acct)
            # A tax-deferred account may still be marked NO — that's the household answering the
            # question, and refusing it would leave the row permanently unanswered. Only YES is
            # blocked. (The blocked rows are rendered non-interactive anyway, so this is belt and
            # braces for a hand-rolled request.)
            want = body.get("ef_state")
            if want not in EF_STATES:
                want = None
            if want is None and "ef" in body:
                want = "yes" if body["ef"] else "no"
            if tier is None:
                rejected.append({"field": "ef", "reason": "This account can't serve as an emergency-"
                                                          "fund source."})
            elif tier in cashdebt.EF_INELIGIBLE_TIERS and want == "yes":
                rejected.append({"field": "ef", "reason": "Tax-deferred accounts can't be designated "
                                                          "as emergency-fund sources — an early "
                                                          "withdrawal costs income tax plus a 10% "
                                                          "penalty."})
            else:
                ea = dict(cfg.get("ef_accounts") or {})
                prev = ea.get(nm) if isinstance(ea.get(nm), dict) else {}
                state = want if want is not None else _ef_state(prev)
                # Typing a cap on an un-ruled account IS a designation -- there is no other reason
                # to say "count $25,000 of this one". Without this, the cap would be written and
                # then immediately discarded, and the user would watch the number they just
                # entered disappear. A cap typed on an account marked NO does NOT flip it to yes:
                # that would silently reverse an explicit decision.
                if want is None and "ef_cap" in body and body["ef_cap"] not in (None, "") \
                        and state == "unset":
                    state = "yes"
                if "ef_cap" in body:
                    try:
                        cap = None if body["ef_cap"] in (None, "") else max(0.0, float(body["ef_cap"]))
                    except (TypeError, ValueError):
                        cap = None
                else:
                    cap = prev.get("cap")
                if state == "yes":
                    ea[nm] = {"on": True, "cap": cap}
                elif state == "no":
                    ea[nm] = {"on": False, "excluded": True}   # an answered "no" must persist
                else:
                    ea.pop(nm, None)
                cfg["ef_accounts"] = ea
                touched += [k for k in ("ef", "ef_state", "ef_cap") if k in body]
        for key, store_key, caster in (("rate_pct", "debt_rates", float),
                                       ("min_payment", "debt_min_payments", float)):
            if key not in body:
                continue
            if not is_liab:
                rejected.append({"field": key, "reason": "Not a liability — APR and minimum payment "
                                                         "don't apply."})
                continue
            dct = dict(cfg.get(store_key) or {})
            if body[key] in (None, ""):
                dct.pop(nm, None)
            else:
                try:
                    dct[nm] = caster(body[key])
                except (TypeError, ValueError):
                    rejected.append({"field": key, "reason": "Must be a number."})
                    continue
            cfg[store_key] = dct
            touched.append(key)
        for key, store_key in (("pay_in_full", "pay_in_full_overrides"),
                               ("is_mortgage", "is_mortgage_overrides")):
            if key not in body:
                continue
            if not is_liab:
                rejected.append({"field": key, "reason": "Not a liability."})
                continue
            dct = dict(cfg.get(store_key) or {})
            dct[nm] = bool(body[key])
            cfg[store_key] = dct
            touched.append(key)
        store.write_json(INSURANCE_FILE, cfg)
        _emit_config_changed(req, "/api/account-registry", old_cfg, cfg,
                             summarize_keys={"ef_accounts", "debt_rates", "debt_min_payments",
                                             "pay_in_full_overrides", "is_mortgage_overrides"})
    _cache["data"] = None
    return {"ok": True, "account": nm, "saved": touched, "rejected": rejected}


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
        staged = stored.get(k) not in (None, "")
        if env_locked:
            val, source = os.environ.get(k), "env"
        elif staged:
            val, source = stored.get(k), "settings"
        else:
            val, source = spec["default"], "default"
        # 2026-07-24: editable no longer requires NOT env_locked (see post_settings) -- you can
        # stage a new value while .env still wins, so migrating off .env is one restart, not two.
        cfg_out.append({"key": k, "label": spec["label"], "group": spec["group"], "value": val,
                        "default": spec["default"], "source": source, "env_locked": env_locked,
                        "staged_behind_env": bool(env_locked and staged),
                        "editable": True, "applies": "restart", "options": spec.get("options")})
    return {"settings": out, "config": cfg_out,
            "note": "Analysis knobs apply at request time (no restart). Model config saves here and applies "
                    "the next time portfolio-api's container is RECREATED (`docker compose up -d` — a plain "
                    "`restart` reuses the existing container's environment and will NOT pick this up). A "
                    "real .env value always wins over a saved one while both exist — remove the .env line, "
                    "then `docker compose up -d`, to activate a value you've already saved here. API keys / "
                    "provider credentials live in the SECRETS tab instead (also DB-backed, encrypted)."}


@app.post("/api/settings")
async def post_settings(req: Request):
    """Set advanced knobs (local settings.json). ENV-set knobs are ignored (env wins) and
    reported as env_locked. Blank clears back to default. Config only — no money action."""
    body = await req.json()
    if str((body or {}).get("PORTFOLIO_PROVIDER") or "").lower() == "monarch" and _monarch_ack() is None:
        return JSONResponse({"error": MONARCH_LABEL + " can only be selected via the acknowledgment in "
                             "SETTINGS -> ACCOUNTS (POST /api/integrations/monarch/enable)."},
                            status_code=400)
    old = _read_settings()   # independent read, captured before any mutation
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
    # Non-secret model/infra config (stored under ENV name; applies on RESTART). 2026-07-24: a
    # key can now be SAVED here even while env_locked (staged) -- it just has no effect until the
    # real .env value is removed and portfolio-api is restarted (collapses what used to be a
    # restart-then-save-then-restart-again dance into one restart).
    cfg_keys = {s["key"] for s in CONFIG_SPECS}
    restart_needed, staged_behind_env = [], []
    for key, val in (body or {}).items():
        if key not in cfg_keys:
            continue
        if val in (None, ""):
            stored.pop(key, None)
        else:
            stored[key] = str(val)
        applied.append(key)
        restart_needed.append(key)
        if key in _ENV_SECRETS_AT_BOOT:
            staged_behind_env.append(key)
    store.write_json(SETTINGS_FILE, stored)
    # Settings/config knobs are non-secret operational values (timeouts, model names, etc.) — safe
    # to log in full; SETTING_SPECS/CONFIG_SPECS never overlap with SECRET_SPECS (secrets are a
    # separate encrypted store, see /api/secrets above, which already logs key-names-only).
    _emit_config_changed(req, "/api/settings", old, stored)
    return {"ok": True, "applied": applied, "ignored_env_locked": ignored,
            "staged_behind_env": staged_behind_env, "restart_required": restart_needed, "settings": stored}


# ---- Monarch opt-in (TASK-003): status / enable-with-acknowledgment / disable. Admin only. ----
# ---- Plaid (TASK-005) -- every endpoint is admin-only and checks for itself (the global gate lets
# viewers GET everything). Access tokens never leave the server.
def _plaid_admin(request):
    a = _require_admin(request)
    return a, (None if a else JSONResponse({"error": "admin only"}, status_code=403))


@app.get("/api/integrations/plaid")
def get_plaid_status(request: Request):
    admin, bad = _plaid_admin(request)
    if bad:
        return bad
    return {"configured": plaid_integration.configured(), "environment": plaid_integration.environment(),
            "selected": os.environ.get("PORTFOLIO_PROVIDER", "csv").lower() == "plaid",
            "kinds": [{"kind": k, "label": v} for k, v in plaid_integration.KIND_LABELS.items()],
            "items": plaid_integration.public_items(),
            "note": "Balances, investment holdings and liabilities only — transactions are never requested. "
                    "Sandbox (test data) is always free. For real banks, Plaid offers new US/Canada accounts "
                    "a free Trial plan limited to 10 connections for the life of the account (details: "
                    "plaid.com/docs/account/billing); otherwise it is Pay-as-you-go, where Investments and "
                    "Liabilities are billed per connected institution per month until you remove it. Check "
                    "your own Plaid Dashboard for the plan and prices that apply to you."}


def _plaid_err(e):
    return JSONResponse({"error": str(e), "code": getattr(e, "code", None)},
                        status_code=502 if getattr(e, "code", "") not in ("NOT_CONFIGURED", "BAD_KIND", "NO_SUCH_ITEM") else 400)


@app.post("/api/integrations/plaid/link")
async def post_plaid_link(request: Request):
    admin, bad = _plaid_admin(request)
    if bad:
        return bad
    body = await request.json()
    try:
        r = plaid_integration.start_link((body or {}).get("kind") or "bank", admin["id"], (body or {}).get("item_id"))
    except plaid_integration.PlaidError as e:
        return _plaid_err(e)
    audit.emit("integration.plaid.link_started", request=request, actor=admin["id"], actor_name=admin.get("username"),
               resource="/api/integrations/plaid/link", detail={"kind": r["kind"], "update": bool((body or {}).get("item_id"))})
    return {"ok": True, "session_id": r["session_id"], "hosted_link_url": r["hosted_link_url"], "kind": r["kind"]}


@app.post("/api/integrations/plaid/poll")
async def post_plaid_poll(request: Request):
    admin, bad = _plaid_admin(request)
    if bad:
        return bad
    body = await request.json()
    try:
        r = plaid_integration.poll_link((body or {}).get("session_id") or "")
    except plaid_integration.PlaidError as e:
        return _plaid_err(e)
    if r["status"] == "linked" and r["items"]:
        _cache["data"] = None          # next dashboard load re-reads the new accounts
        for it in r["items"]:
            audit.emit("integration.plaid.item_added", request=request, actor=admin["id"], actor_name=admin.get("username"),
                       resource="/api/integrations/plaid/poll",
                       detail={"institution": it.get("institution"), "kind": it.get("kind")})
    return {"ok": True, **r}


@app.post("/api/integrations/plaid/remove")
async def post_plaid_remove(request: Request):
    admin, bad = _plaid_admin(request)
    if bad:
        return bad
    body = await request.json()
    iid = (body or {}).get("item_id") or ""
    inst = next((i.get("institution") for i in plaid_integration.public_items() if i["item_id"] == iid), None)
    try:
        plaid_integration.remove_item(iid)
    except plaid_integration.PlaidError as e:
        return _plaid_err(e)
    _cache["data"] = None
    audit.emit("integration.plaid.item_removed", request=request, actor=admin["id"], actor_name=admin.get("username"),
               resource="/api/integrations/plaid/remove", detail={"institution": inst})
    return {"ok": True}


@app.get("/api/integrations/monarch")
def monarch_status(request: Request):
    ack = _monarch_ack()
    return {"label": MONARCH_LABEL, "enabled": _monarch_enabled(), "acknowledged": ack is not None,
            "acknowledgment": ({"at": ack.get("at"), "by": ack.get("by")} if ack else None),
            "provider": os.environ.get("PORTFOLIO_PROVIDER", "csv").lower(),
            "ack_text": MONARCH_ACK_TEXT,
            "cooldown_s": max(0, int(_MONARCH_COOLDOWN_S - (time.time() - _monarch_state["fail_ts"])))}


@app.post("/api/integrations/monarch/enable")
async def monarch_enable(req: Request):
    user = _require_admin(req)
    if not user:
        return JSONResponse({"error": "admin only"}, status_code=403)
    try:
        body = await req.json()
    except Exception:
        body = {}
    if (body or {}).get("acknowledge") is not True:
        return JSONResponse({"error": "You must accept the risk acknowledgment to enable " + MONARCH_LABEL},
                            status_code=400)
    stored = _read_settings()
    stored["MONARCH_RISK_ACK"] = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                                  "by": user.get("username"), "version": MONARCH_ACK_VERSION}
    stored["PORTFOLIO_PROVIDER"] = "monarch"
    store.write_json(SETTINGS_FILE, stored)
    env_locked = "PORTFOLIO_PROVIDER" in _ENV_SECRETS_AT_BOOT and \
        os.environ.get("PORTFOLIO_PROVIDER", "").lower() != "monarch"
    if not env_locked:
        os.environ["PORTFOLIO_PROVIDER"] = "monarch"
    _monarch_state.update(fail_ts=0.0, err="")
    _cache["data"], _cache["ts"] = None, 0
    audit.emit("integration.monarch.enabled", actor=user.get("id"), actor_name=user.get("username"),
               resource="/api/integrations/monarch/enable", request=req,
               detail={"ack_version": MONARCH_ACK_VERSION, "ack_text": MONARCH_ACK_TEXT,
                       "env_locked": env_locked})
    return {"ok": True, "enabled": _monarch_enabled(), "env_locked": env_locked}


@app.post("/api/integrations/monarch/disable")
async def monarch_disable(req: Request):
    user = _require_admin(req)
    if not user:
        return JSONResponse({"error": "admin only"}, status_code=403)
    stored = _read_settings()
    stored.pop("MONARCH_RISK_ACK", None)
    if str(stored.get("PORTFOLIO_PROVIDER") or "").lower() == "monarch":
        stored["PORTFOLIO_PROVIDER"] = "csv"
    store.write_json(SETTINGS_FILE, stored)
    if os.environ.get("PORTFOLIO_PROVIDER", "").lower() == "monarch":
        os.environ["PORTFOLIO_PROVIDER"] = "csv"
    _cache["data"], _cache["ts"] = None, 0
    audit.emit("integration.monarch.disabled", actor=user.get("id"), actor_name=user.get("username"),
               resource="/api/integrations/monarch/disable", request=req)
    return {"ok": True, "enabled": False}


# ---- Secrets editor (N6 Phase 2): write-only, masked. Values are NEVER returned. ----
# scope "app"  = portfolio-api reads it directly -> editable here, applies when portfolio-api's
#                container is next RECREATED (`docker compose up -d` -- NOT `restart`, which
#                reuses the container's existing environment and won't see this at all).
# scope "mcpo" = used by the mcpo container -> NOT editable here (set in .env; `docker compose
#                up -d` to apply a change there too).
SECRET_SPECS = [
    {"key": "FINNHUB_API_KEY",       "label": "Finnhub API key",                 "group": "Market data",            "scope": "app"},
    {"key": "SEC_USER_AGENT",        "label": "SEC EDGAR User-Agent (your email)", "group": "Market data",          "scope": "app"},
    # ANALYSIS_TOKEN entry removed 2026-07-24 -- the session auth gate is now the only guard on
    # /api/run-* (and every other route); the token stopped doing anything real once that gate
    # covered every endpoint, so surfacing it here would just mislead someone into thinking it
    # still matters.
    {"key": "SNAPTRADE_CLIENT_ID",   "label": "SnapTrade client ID",             "group": "Portfolio (SnapTrade)",  "scope": "app"},
    {"key": "SNAPTRADE_CONSUMER_KEY","label": "SnapTrade consumer key",          "group": "Portfolio (SnapTrade)",  "scope": "app"},
    {"key": "SNAPTRADE_USER_ID",     "label": "SnapTrade user ID",               "group": "Portfolio (SnapTrade)",  "scope": "app"},
    {"key": "SNAPTRADE_USER_SECRET", "label": "SnapTrade user secret",           "group": "Portfolio (SnapTrade)",  "scope": "app"},
    # 2026-07-24: these 4 moved from scope="mcpo" (env-only, no UI path at all) to scope="mcpo-file"
    # -- portfolio-api now writes them into ./mcpo-secrets/mcpo.env (a second env_file on the mcpo
    # compose service), so they're editable here like any other secret, just applying on the NEXT
    # MCPO restart (not portfolio-api's) instead of live. See _write_mcpo_secrets_file() below.
    {"key": "MONARCH_EMAIL",         "label": "Monarch email",                   "group": "Portfolio (Monarch)",    "scope": "mcpo-file"},
    {"key": "MONARCH_PASSWORD",      "label": "Monarch password",                "group": "Portfolio (Monarch)",    "scope": "mcpo-file"},
    {"key": "MONARCH_MFA_SECRET",    "label": "Monarch MFA / TOTP secret",       "group": "Portfolio (Monarch)",    "scope": "mcpo-file"},
    {"key": "TAVILY_API_KEY",        "label": "Tavily API key (news)",           "group": "Market data",            "scope": "mcpo-file"},
    # MCPO_API_KEY stays scope="mcpo" (env-only, no UI path) -- unlike the 4 above, it's passed to
    # mcpo via a `--api-key ${MCPO_API_KEY}` CLI flag in docker-compose.yml's `command:`, which
    # Compose resolves ONLY from the top-level .env at compose-parse time -- env_file entries
    # can't feed that substitution, so there's no equivalent write-a-file path for this one key.
    {"key": "MCPO_API_KEY",          "label": "mcpo API key",                    "group": "Internal",               "scope": "mcpo"},
    # P1 free-data adoptions (final review): all three are FREE-tier registrations.
    {"key": "FINRA_CLIENT_ID",       "label": "FINRA API client ID (short interest)",     "group": "Market data", "scope": "app"},
    {"key": "FINRA_CLIENT_SECRET",   "label": "FINRA API client secret (short interest)", "group": "Market data", "scope": "app"},
    {"key": "RENTCAST_API_KEY",      "label": "RentCast API key (home AVM, free 50/mo)",  "group": "Market data", "scope": "app"},
    # ---- AI / LLM provider (2026-07-24, Claude integration) ----
    # portfolio-api makes these calls directly (unlike Monarch/Tavily/mcpo below) — no separate
    # container involved, so this is a normal "app" scope secret: DB-backed, UI-editable, applies
    # the next time portfolio-api's container is recreated (`docker compose up -d`, not `restart`).
    # Anthropic Pro (claude.ai) subscriptions do NOT include
    # API access — this key comes from a separate console.anthropic.com account with its own
    # pay-per-token billing. See NEW-USER-SETUP-GUIDE.md.
    {"key": "LLM_API_KEY",   "label": "LLM provider API key (Anthropic/OpenAI-compatible — blank for local Ollama)",
     "group": "AI / LLM provider", "scope": "app"},
    {"key": "EMBED_API_KEY", "label": "Embeddings provider API key (blank = same as above, or local Ollama)",
     "group": "AI / LLM provider", "scope": "app"},
    # ---- Notifications / email (2026-07-26) -- generic SMTP, deliberately NOT tied to any one
    # provider (Gmail, Outlook, a self-hosted mail server, ...) and NOT a paid cloud API like AWS
    # SES -- Jason's call: no recurring cloud cost just to send an alert email, and per-installation
    # configurability (each self-hosted deployment brings its own mailbox). For Gmail specifically:
    # smtp.gmail.com / 587 / your address / a 16-character App Password (not your real password —
    # Google requires 2-Step Verification enabled first to generate one). See notifications.py.
    {"key": "PLAID_CLIENT_ID", "label": "Plaid client ID", "group": "Portfolio (Plaid)", "scope": "app", "plain": True},
    {"key": "PLAID_SECRET", "label": "Plaid secret (the one for the environment chosen under ADVANCED KNOBS)", "group": "Portfolio (Plaid)", "scope": "app"},
    {"key": "SMTP_HOST",         "label": "SMTP server (e.g. smtp.gmail.com)",         "group": "Notifications (Email)", "scope": "app", "plain": True},
    {"key": "SMTP_PORT",         "label": "SMTP port (587 = STARTTLS, blank defaults to 587)", "group": "Notifications (Email)", "scope": "app", "plain": True},
    {"key": "SMTP_USERNAME",     "label": "SMTP username (usually your full email address)", "group": "Notifications (Email)", "scope": "app", "plain": True},
    {"key": "SMTP_PASSWORD",     "label": "SMTP password (Gmail: an App Password, not your login password)", "group": "Notifications (Email)", "scope": "app"},
    {"key": "SMTP_FROM_ADDRESS", "label": "From address (blank = same as SMTP username)", "group": "Notifications (Email)", "scope": "app", "plain": True},
    {"key": "SMTP_TO_ADDRESS",   "label": "Send alert emails to this address",         "group": "Notifications (Email)", "scope": "app", "plain": True},
    {"key": "SMTP_USE_TLS",      "label": "Use STARTTLS (1 = yes, 0 = no; blank defaults to yes)", "group": "Notifications (Email)", "scope": "app", "plain": True},
    # 2026-07-27 (SEC-7a, UI-triggered backup): scope="app" like SMTP above -- read live via
    # _read_secrets() at the moment a backup actually runs, no container restart needed after
    # saving it. Deliberately the SAME passphrase concept scripts/backup.sh's BACKUP_PASSPHRASE
    # env var uses (see backup.py's module docstring) -- set them to the same value if you want a
    # UI-triggered backup and the systemd-timer-triggered one to be interchangeable, though nothing
    # requires that; restore only ever needs whichever passphrase actually encrypted a given file.
    # Label corrected 2026-07-27 (caught while auditing the setup checklist) -- this field itself
    # lives right here, in SETTINGS -> SECRETS & KEYS ("Backups" group); it was wrongly described
    # as living under "SETTINGS -> SECURITY -> Backups", which is actually where the RUN BACKUP
    # NOW / Stage restore buttons live (SETTINGS -> SECURITY POSTURE, not plain SECURITY) -- two
    # different panels, easy to conflate since they're both part of the same feature.
    {"key": "BACKUP_PASSPHRASE", "label": "Backup passphrase (set here; use it in SETTINGS -> SECURITY POSTURE -> Backups)", "group": "Backups", "scope": "app"},
]


def _read_secrets():
    """Decrypts every value on the way out (secrets_crypto.decrypt_or_legacy falls back to
    returning a value as-is if it doesn't decrypt as a Fernet token -- i.e. it predates
    2026-07-24's encryption-at-rest change -- so nothing already saved is silently lost;
    _write_secrets() below re-encrypts it properly the next time anything here is saved)."""
    try:
        raw = store.read_json_strict(SECRETS_FILE) or {}
    except Exception:
        return {}
    return {k: secrets_crypto.decrypt_or_legacy(v) for k, v in raw.items()}


def _write_secrets(stored):
    """Encrypts every value before it reaches store.write_json -- the ONLY place SECRETS_FILE is
    ever written. store.py itself stays unaware of encryption; it just sees longer strings."""
    enc = {k: secrets_crypto.encrypt(v) for k, v in stored.items() if v}
    store.write_json(SECRETS_FILE, enc)


_write_secrets(_read_secrets())   # one-time-per-boot upgrade pass: re-encrypts any legacy
                                  # plaintext values found (no-op, just a fresh ciphertext, for
                                  # anything already properly encrypted) -- see _read_secrets().


# ---- mcpo-scope secrets bridge (2026-07-24) -----------------------------------------------
# Writes the "mcpo-file" scope keys (Monarch email/password/MFA secret, Tavily key) out to a
# plain KEY=VALUE file on the volume shared with the mcpo container (docker-compose.yml's
# ./mcpo-secrets bind mount) -- mcpo's OWN compose entry reads that same host path as a second
# env_file. A key already set in portfolio-api's REAL environment (env_locked, i.e. a real .env
# value) is deliberately EXCLUDED here, not just skipped on the read side -- so mcpo's separate
# `env_file: .env` entry is always the one supplying it, and the two files are never actually
# defining the same key at once. Applies the next time mcpo's container is genuinely RECREATED.
# CONFIRMED 2026-07-26 (Jason, live incident): a plain `docker compose up -d mcpo` is NOT enough --
# Compose's container-recreate decision is driven by a config hash computed from the resolved
# docker-compose.yml service definition, which does NOT include the byte content of a file
# referenced via `env_file:`. Editing mcpo.env alone never changes that hash, so plain `up -d mcpo`
# sees "no config change" and leaves the existing container (and its stale environment) running --
# exactly what happened here: mcpo's cached Monarch session expired, it tried to re-auth using its
# own (stale) MONARCH_EMAIL/PASSWORD, and a plain `up -d mcpo` first attempt did nothing. The fix
# that actually worked: `docker compose up -d mcpo --force-recreate`. Also NOT
# `docker compose restart mcpo`, which reuses the existing container's environment and will never
# see an edited env_file either. When migrating a currently env-locked value off .env, recreate
# portfolio-api FIRST (its boot is what regenerates this file with the now-unlocked value) and mcpo
# SECOND, both with --force-recreate. There is no live push into an already-running mcpo container
# (that would need Docker-socket access from portfolio-api, deliberately not done here).
MCPO_SECRETS_FILE = os.environ.get("MCPO_SECRETS_FILE", "/mcpo-secrets/mcpo.env")


def _write_mcpo_secrets_file():
    stored = _read_secrets()
    lines = ["# Auto-generated by portfolio-api (SETTINGS -> SECRETS) -- do not edit by hand,",
             "# this file is OVERWRITTEN on every save. See mcpo-secrets/mcpo.env.example.\n"]
    for spec in SECRET_SPECS:
        if spec["scope"] != "mcpo-file" or spec["key"] in _ENV_SECRETS_AT_BOOT:
            continue
        v = stored.get(spec["key"])
        if v:
            lines.append(f"{spec['key']}={str(v).replace(chr(10), ' ').replace(chr(13), ' ')}")
    try:
        # mode 0644, NOT 0600 (2026-07-24 fix): this container runs as root (no USER in the
        # Dockerfile), so on the bind-mounted host path this file lands owned by root. At 0600
        # only root can read it -- but `docker compose` itself runs as whatever unprivileged host
        # user invokes it, and it reads EVERY service's env_file (including mcpo's) to compute
        # config hashes even when a command targets just one service. Result: any `docker compose
        # up`/`ps`/etc as that host user fails with "permission denied" reading this file the
        # instant portfolio-api regenerates it. 0644 keeps it out of other users' write access but
        # lets the host's own compose invocation read it -- no worse than the file already being
        # plaintext-by-necessity for Compose's env_file mechanism (see mcpo.env.example).
        store.atomic_write_text(MCPO_SECRETS_FILE, "\n".join(lines) + "\n", mode=0o644)
    except Exception:
        pass   # best-effort -- a write failure here shouldn't break the /api/secrets response


_write_mcpo_secrets_file()   # regenerate on every portfolio-api boot too, not just on save -- keeps
                             # the shared file in sync with the DB even if it was deleted/recreated
                             # (e.g. a fresh ./mcpo-secrets bind mount) between restarts.


@app.get("/api/secrets")
def get_secrets(request: Request):
    """Status ONLY — never returns secret VALUES (except the few spec-marked "plain" non-credential settings, to an admin). Per secret: whether it's set and from where
    (env / saved / unset), whether it's env-locked (real .env wins), and whether it's editable
    here. 2026-07-24: editable no longer requires NOT env_locked -- you can stage a replacement
    value into the DB while the old .env value is still active, so migrating off .env only takes
    ONE container recreation (`docker compose up -d`, NOT `restart` -- restart reuses the
    container's existing environment and never sees an edited .env at all). A staged value has
    zero effect until the .env entry is actually removed."""
    stored = _read_secrets()
    # 2026-08-06: the third of SEC-1's three declared-but-never-emitted event types. Worth being
    # precise about what this event does and does NOT mean, because "secret.read" reads like
    # "someone retrieved a credential" and that is NOT what happened: this endpoint is write-only
    # by design and never returns a VALUE. What it does return is the credential INVENTORY -- which
    # keys exist, which are configured, which are staged behind .env -- and that inventory is
    # exactly what reconnaissance looks like ahead of a targeted attempt, so viewing it is a
    # genuine access event under SOC 2 CC6.1 / NIST AC-6(9) (auditing privileged-function use).
    # Emitting per-VALUE-decrypt instead would be useless noise -- _read_secrets() runs constantly
    # in normal operation (every SMTP send, every backup) and would drown the log.
    # NOTE: `data.shared` remains declared-but-unemitted; unlike these three it has no current code
    # path at all (nothing in this app shares data outbound), so inventing a trigger would be worse
    # than leaving the vocabulary entry reserved.
    try:
        _uid = request.session.get("uid")
        _u = auth.get_user_by_id(_uid) if _uid else None
        audit.emit("secret.read", request=request, actor=_uid,
                   actor_name=(_u or {}).get("username"), resource="/api/secrets",
                   detail={"scope": "inventory_metadata_only_no_secret_values",
                           "plain_settings_returned_to_admin": sorted(sp["key"] for sp in SECRET_SPECS if sp.get("plain")) if _require_admin(request) else [],
                           "keys_configured": sorted(k for k in stored if stored.get(k)),
                           "spec_count": len(SECRET_SPECS)})
    except Exception:
        pass   # audit.emit is already fail-safe; this guard covers the session/user lookup too
    out = []
    # Non-credential settings (SMTP host/port/username/from/to/STARTTLS -- spec "plain": True) are
    # echoed back to an ADMIN so the panel can show them as ordinary text fields (Jason, 2026-10-19).
    # Everything else, including SMTP_PASSWORD, stays write-only. The global gate lets viewers do
    # every GET, so the value is gated on the caller's role HERE.
    is_admin = bool(_require_admin(request))
    for spec in SECRET_SPECS:
        k = spec["key"]
        env_locked = k in _ENV_SECRETS_AT_BOOT
        in_file = bool(stored.get(k))
        source = "env" if env_locked else ("saved" if in_file else "unset")
        out.append({"key": k, "label": spec["label"], "group": spec["group"], "scope": spec["scope"],
                    "set": bool(env_locked or in_file), "source": source, "env_locked": env_locked,
                    **({"plain": True, "value": (stored.get(k) if (is_admin and not env_locked) else None)}
                       if spec.get("plain") else {}),
                    "staged_behind_env": bool(env_locked and in_file),
                    "editable": spec["scope"] in ("app", "mcpo-file")})
    return {"secrets": out,
            "note": "Write-only — credential values are never shown (the SMTP server/port/username/addresses are plain text, the password is not), and everything is encrypted at rest (secrets_crypto.py). "
                    "'app' secrets you set here save to the DB and apply the next time portfolio-api's "
                    "container is recreated (`docker compose up -d`); Monarch/Tavily secrets save the "
                    "same way but apply on mcpo's next `docker compose up -d` instead — used by that "
                    "separate container. A plain `restart` does NOT pick up an edited .env/mcpo.env; "
                    "only `up -d` re-reads them and recreates the container. A real .env value always "
                    "wins over a saved one while both exist. The mcpo API key itself stays .env-only "
                    "(it's a startup CLI flag, not a runtime value)."}


@app.post("/api/secrets")
async def post_secrets(req: Request):
    """Set portfolio-api 'app' secrets and mcpo-bridged 'mcpo-file' secrets (write-only, encrypted
    at rest; mcpo-file additionally rewrites ./mcpo-secrets/mcpo.env for any key NOT currently
    env-locked). Ignores 'mcpo'-scope (env-only) keys and unknown keys. Blank clears. A key that's
    currently env_locked can still be SAVED here (staged) -- it just has no effect until the real
    .env value is removed and the relevant service's container is RECREATED via `docker compose
    up -d --force-recreate` (portfolio-api for 'app', mcpo for 'mcpo-file' -- recreate portfolio-api
    first when migrating a currently-locked value, since its boot regenerates the mcpo bridge
    file). `--force-recreate` is NOT optional here (confirmed 2026-07-26): Compose's recreate-
    detection hash doesn't cover env_file byte content, so a plain `up -d <service>` silently does
    nothing when only mcpo.env changed. A plain `restart` will NOT pick this up either. Values
    never echoed back."""
    body = await req.json()
    stored = _read_secrets()
    by_key = {s["key"]: s for s in SECRET_SPECS}
    saved, ignored, staged_behind_env = [], [], []
    mcpo_file_touched = False
    for k, v in (body or {}).items():
        spec = by_key.get(k)
        if not spec or spec["scope"] not in ("app", "mcpo-file"):
            ignored.append(k)
            continue
        if v in (None, ""):
            stored.pop(k, None)
        else:
            stored[k] = str(v)
        saved.append(k)
        if k in _ENV_SECRETS_AT_BOOT:
            staged_behind_env.append(k)
        if spec["scope"] == "mcpo-file":
            mcpo_file_touched = True
    _write_secrets(stored)   # encrypted at rest — see secrets_crypto.py
    if saved:
        # SEC-1 secret.changed: record WHICH secret keys were set/cleared — never the values.
        audit.emit("secret.changed", request=req, actor=req.session.get("uid"), resource="/api/secrets",
                   detail={"keys": saved, "staged_behind_env": staged_behind_env})
    if mcpo_file_touched:
        _write_mcpo_secrets_file()   # regenerate ./mcpo-secrets/mcpo.env from the just-saved DB state
                                      # (still correctly excludes any key that's env_locked)
    return {"ok": True, "saved": saved, "ignored": ignored, "staged_behind_env": staged_behind_env,
            "restart_required": bool(saved)}


# ============================================================================
# Notifications (2026-07-26) — SETTINGS -> NOTIFICATIONS. Self-service (each session manages its
# OWN push subscriptions/preferences — see _VIEWER_ALLOWED_WRITE_PATHS above), no admin gate needed
# beyond "you must be logged in" (the global auth gate already covers that). See notifications.py
# module docstring for the full design (VAPID web push + generic SMTP, self-hosted, no third party).
# ============================================================================
@app.get("/api/notifications/vapid-public-key")
def notifications_vapid_key(request: Request):
    """Public by necessity of what it is (a public key), but still behind the normal login gate —
    no reason to hand it to an unauthenticated caller. Used by the frontend's
    `PushManager.subscribe({applicationServerKey: ...})` call."""
    return {"key": notifications.vapid_public_key_b64()}


@app.post("/api/notifications/subscribe")
async def notifications_subscribe(request: Request):
    uid = request.session.get("uid")
    body = await request.json()
    sub = body.get("subscription")
    if not sub or not sub.get("endpoint"):
        return JSONResponse({"error": "missing subscription"}, status_code=400)
    count = notifications.add_subscription(uid, sub)
    return {"ok": True, "device_count": count}


@app.post("/api/notifications/unsubscribe")
async def notifications_unsubscribe(request: Request):
    uid = request.session.get("uid")
    body = await request.json()
    endpoint = body.get("endpoint")
    if not endpoint:
        return JSONResponse({"error": "missing endpoint"}, status_code=400)
    notifications.remove_subscription(uid, endpoint)
    return {"ok": True}


@app.get("/api/notifications/preferences")
def notifications_get_preferences(request: Request):
    uid = request.session.get("uid")
    user = auth.get_user_by_id(uid)
    prefs = notifications.get_prefs(uid)
    return {"preferences": prefs, "categories": notifications.CATEGORIES,
            "device_count": notifications.subscription_count(uid),
            "is_admin": auth.is_admin(user),
            "email_available": notifications.smtp_configured(),
            "email_note": None if notifications.smtp_configured() else
                "Email is off until SMTP is configured under SETTINGS -> SECRETS & KEYS "
                "(\"Notifications (Email)\" group).",
            "email_note_role": None if auth.is_admin(user) else
                "Email notifications are configured once for the whole household by the admin "
                "account and go to one shared address."}


@app.post("/api/notifications/preferences")
async def notifications_set_preferences(request: Request):
    uid = request.session.get("uid")
    body = await request.json()
    saved = notifications.set_prefs(uid, body or {})
    return {"ok": True, "preferences": saved}


@app.post("/api/notifications/test")
def notifications_test(request: Request):
    uid = request.session.get("uid")
    user = auth.get_user_by_id(uid)
    result = notifications.send_test(uid, auth.is_admin(user))
    return {"ok": True, **result}


_LAST_TEST_EMAIL = [0.0]


@app.post("/api/notifications/test-email")
def notifications_test_email(request: Request):
    """Admin-only: send a test message through the SAVED SMTP settings (ignores the email
    preference checkbox) and say exactly why it failed if it did. A success also counts as the
    'successful test notification' the emergency-access enable gate asks for."""
    admin = _require_admin(request)
    if not admin:
        return JSONResponse({"error": "admin only"}, status_code=403)
    if time.time() - _LAST_TEST_EMAIL[0] < 10:
        return JSONResponse({"ok": False, "error": "Wait a few seconds between tests."}, status_code=429)
    _LAST_TEST_EMAIL[0] = time.time()
    ok, err = notifications.send_email_checked(
        "SMTP test", "If you are reading this, Fiduciary can send email through your SMTP settings. "
                     f"Sent {time.strftime('%Y-%m-%d %H:%M:%S')}.")
    audit.emit("notification.test_email", outcome="success" if ok else "failure", request=request,
               actor=admin["id"], actor_name=admin.get("username"), resource="/api/notifications/test-email",
               detail={"ok": ok, "error": err})
    if ok:
        try:
            emergency.mark_test_ok()
        except Exception:
            pass
    return {"ok": ok, "error": err,
            "to": (_read_secrets().get("SMTP_TO_ADDRESS") if ok else None)}


@app.get("/api/secrets/key-status")
def get_secrets_key_status(request: Request):
    """SEC-3 (2026-07-25) — admin-only encryption-key-version status for the SETTINGS → SECRETS
    display (docstring corrected 2026-07-26 — this was never actually under SECURITY, see
    secretsContent() in static/index.html). A GET, so (like /api/auth/users) it needs its own
    explicit admin check rather than relying on the write-only viewer restriction in the global
    gate."""
    if not _require_admin(request):
        return JSONResponse({"error": "admin only"}, status_code=403)
    return secrets_crypto.key_status()


# TASK-001: encrypted document vault. Tables live in store.db; blobs under VAULT_DIR.
vault_api.register(app)       # (vault/emergency/kit tables are created right after store.init_db() above)


def _kit_binder():
    """Binder export for the Emergency Kit: contacts/executor/will location/estate-doc currency plus
    the continuity account map. Never includes secrets or vault content."""
    icfg = _read_insurance_cfg()
    out = {"contacts": icfg.get("contacts") or [], "executor_name": icfg.get("executor_name"),
           "will_location": icfg.get("will_location"), "estate_docs": icfg.get("estate_docs") or []}
    try:
        c = get_continuity()
        if isinstance(c, dict):
            out["accounts"] = c.get("account_map") or c.get("accounts") or []
            out["checklist"] = c.get("checklist") or []
    except Exception:
        pass
    return out


emergency_api.register(app, _kit_binder)
emergency.start_emergency_thread()   # TASK-002: 15-min state-machine tick (grant/expire/remind); inert unless enabled
if emergency.is_test_mode():
    print("[emergency] *** EMERGENCY_ALLOW_SHORT_WAIT=1 — TEST MODE: wait floor, cool-down and notification "
          "gate are BYPASSED. Never run a real deployment like this. ***", flush=True)
    audit.emit("emergency.test_mode_active", outcome="denied", resource="startup",
               detail={"warning": "EMERGENCY_ALLOW_SHORT_WAIT=1"})


@app.post("/api/secrets/rotate-key")
def post_secrets_rotate_key(request: Request):
    """SEC-3 (2026-07-25) — admin-only: generate a new encryption key and make it current for all
    FUTURE writes across every module that uses secrets_crypto (store.py's config/history tables,
    knowledge/RAG, chat.db, auth.py's totp_secret, audit.py's event detail). Does NOT re-encrypt
    existing data — old ciphertext keeps decrypting under its original key version forever (see
    secrets_crypto.py's module docstring for why re-encrypting everything in one sweep is a
    separate, larger, deliberately out-of-scope operation). Audited as `secret.rotated` — the one
    EVENT_TYPES entry SEC-1 defined back on 2026-07-24 but never had a real trigger for until now."""
    admin = _require_admin(request)
    if not admin:
        return JSONResponse({"error": "admin only"}, status_code=403)
    new_version = secrets_crypto.rotate_key()
    try:
        rewrapped = vault.rewrap_all()   # vault DEKs move to the new key; blobs are untouched
    except Exception as e:
        rewrapped = None
        print(f"[vault] DEK re-wrap after key rotation failed (old key still opens everything): {e}", flush=True)
    audit.emit("secret.rotated", request=request, actor=admin["id"], actor_name=admin["username"],
               resource="/api/secrets/rotate-key",
               detail={"new_version": new_version, "vault_deks_rewrapped": rewrapped})
    return {"ok": True, "status": secrets_crypto.key_status()}


# ============ Persistent setup-progress checklist card (RESUME-TRACKER "START HERE NEXT
# SESSION" item, 2026-07-19 -> built 2026-07-22) ============
# Unified completeness model across EVERY optional onboarding field described in
# NEW-USER-SETUP-GUIDE.md / the W('setup-guide', ...) wiki entry. Before this, only 2 fields
# (household income, feeding the BENEFITS/INSURANCE `setup_needed` flag) had any coded
# "is this filled in" check anywhere in the app -- everything else was invisible to the app
# itself, only documented in the static guide. This is the bigger, real-work alternative to
# the per-field spotlights (`setupSpotlight()`/`SETUP_DISMISSED`, 2026-07-19 option (b)):
# those only fire once you're already deep in a panel that needs one specific field; this
# gives a single at-a-glance "X of N done" view up front, on the Dashboard.
#
# Each spec: id, tier (1/2/3, mirrors the guide's tiers), group (display label matching the
# guide's row), cfg (which store the key lives in: retire/insurance/policy/secret), key (the
# actual config key), kind ("scalar" default -- None/"" = blank; "list"/"dict" -- empty = blank;
# "sentinel" -- a specific non-None placeholder value, e.g. inservice_rollover's "unknown",
# counts as blank), required_if ("mfj" -- only scored for a married household, e.g. spouse age/
# income/birth year never show as an open item for a single filer), scored (False for fields
# that ship with an already-reasonable default -- filing_status defaults to mfj, target_age to
# 60, household_size to 2, the IPS policy targets to a 70/10/15/5 draft -- so leaving them alone
# isn't "incomplete", just "not customized"; still SHOWN in the card for transparency per the
# guide, just never counted against the X-of-N and never rendered as a red/open item), jump
# (tab, subtab) for the card's "go there" click (None for the two API-key items, which live in
# .env, not a UI form), severity ("critical" -- default "optional" -- for a SCORED-and-UNFILLED
# item only: 2026-07-23, per Jason: household_size's muted dot got misread as "not set" when it
# was actually just "has a usable default." That's a different confusion from the same dot being
# used for genuinely-blank-but-situational fields (e.g. home_value_override for a renter -- blank
# is completely normal, nothing is wrong). "critical" = blank silently produces a WRONG or
# incomplete number for every household (not just a missing nice-to-have) -- state_of_residence
# (silently assumes Texas), birth_year/spouse_birth_year (every age-gated tax/benefit rule quietly
# never fires without it), income_self/income_spouse ("nothing on the INSURANCE page computes"),
# isr_status ("the one fact that unlocks or blocks this lane"). Everything else scored is
# "optional" by default: either it has a graceful fallback (annual_expenses/annual_savings read
# Monarch's own cashflow; ss_benefit just reports "unavailable" rather than a wrong number) or it's
# genuinely situational and normal to skip (home_value_override for a renter, mortgage_rate/
# refi_rate for someone without a mortgage, k401_*/hsa_contribution for someone without that
# benefit, aca_slcsp pre only relevant pre-Medicare, policies/debt_rates/position_caps/estate_*
# explicitly "optional" in their own notes already).
SETUP_FIELDS = [
    # ---- Tier 1: account security (auth.db, not a config file -- 2026-07-24 backlog) ----
    # MFA is deliberately CRITICAL here, not optional -- 2026-07-24, Jason: "mark the MFA
    # enrollment as a critical/core step in the new user setup checklist & guides." Placed first
    # in Tier 1 so it's the first thing both the checklist card and the guided tour surface, ahead
    # of the financial-config fields -- this app manages a household's full financial picture, so
    # account-takeover risk is treated as at least as foundational as any single config value.
    {"id": "mfa_enrolled", "tier": 1, "group": "Multi-factor authentication (MFA)", "cfg": "auth",
     # jump/copy repointed 2026-07-26: account/access items (password, MFA, recovery, household
     # accounts, reset) moved from SETTINGS -> SECURITY into their own SETTINGS -> IAM subtab --
     # see iamContent() in static/index.html. SECURITY now holds only Certificate/Audit Log/Privacy.
     "key": "mfa_enabled", "jump": ("settings", "iam"), "severity": "critical",
     "note": "A rotating code from any authenticator app (Google Authenticator, Authy, 1Password, "
             "Bitwarden, etc.) on top of your password. Set up in SETTINGS -> IAM -- or right "
             "after creating your account, if you ticked \"enroll now\" on that screen."},
    # Account Recovery Tier 2 (2026-07-24) -- without this, the ONLY way back in after forgetting
    # the password with no working MFA is host/container access alone (`docker exec` + delete
    # auth.db), which silently grants full access with zero second factor. New accounts get a key
    # automatically at creation; accounts migrated in from before this feature existed (including
    # the owner's own live account) start with NONE and need this flagged loudly, not left silently
    # unset -- hence critical, not optional, same tier as MFA itself.
    {"id": "recovery_key_set", "tier": 1, "group": "Host recovery key", "cfg": "auth",
     "key": "recovery_key_set", "jump": ("settings", "iam"), "severity": "critical",
     "note": "The only way back in via host/container access if you ever forget your password AND "
             "lose MFA -- without one, that scenario means a full data wipe, not just a login "
             "reset. Generate it in SETTINGS -> IAM; shown once, save it somewhere safe."},
    # Backup passphrase (2026-07-27, Jason caught this missing while reviewing the checklist after
    # SEC-7a shipped) -- same tier/severity as the two account-recovery items right above, on
    # purpose: MFA and the host recovery key protect against losing ACCESS to your account; this
    # protects against losing the DATA itself (a bad upgrade, a corrupted volume, deleting the
    # wrong thing). All three are "the thing you wish you'd set up BEFORE the day you needed it."
    # Setting the passphrase alone doesn't create a backup by itself -- it just unlocks the RUN
    # BACKUP NOW button (SETTINGS -> SECURITY POSTURE) and the scheduled/systemd paths; the note
    # says so explicitly rather than letting filling this in feel like the whole job is done.
    {"id": "backup_passphrase", "tier": 1, "group": "Backup passphrase", "cfg": "secret",
     "key": "BACKUP_PASSPHRASE", "jump": ("settings", "secrets"), "severity": "critical",
     "note": "Set this before you need it, not after. Unlocks one-click encrypted backups "
             "(SETTINGS -> SECURITY POSTURE -> Backups) and the scheduled/systemd backup paths -- "
             "setting the passphrase alone doesn't create a backup by itself, so also run (or "
             "schedule) one once this is set. Set in SETTINGS -> SECRETS & KEYS."},
    # ---- Tier 1: PLAN -> ASSUMPTIONS (retirement.json) ----
    {"id": "state_of_residence", "tier": 1, "group": "State of residence", "cfg": "retire",
     "key": "state_of_residence", "jump": ("plan", "assumptions"), "severity": "critical",
     "note": "Biggest single lever -- Estate, ACA, Tax and Cash·Debt all read it. Left blank, the app silently assumes Texas."},
    {"id": "filing_status", "tier": 1, "group": "Filing status", "cfg": "retire",
     "key": "filing_status", "scored": False, "jump": ("plan", "assumptions"),
     "note": "Defaults to Married Filing Jointly -- change it if that's not accurate."},
    # current_age/spouse_age REMOVED as separate checklist items 2026-07-23: both are now derived
    # from birth_year/spouse_birth_year (see _derive_age()) rather than separately entered, so
    # "is birth_year filled" already fully covers "is age known" — a dedicated current_age/
    # spouse_age entry here would just be a second, now-meaningless checklist line for the same
    # underlying fact (birth_year/spouse_birth_year below still gate the real completeness count).
    {"id": "target_age", "tier": 1, "group": "Target retirement age", "cfg": "retire",
     "key": "target_age", "scored": False, "jump": ("plan", "assumptions"),
     "note": "Defaults to 60 -- change it if you're aiming for a different age."},
    {"id": "annual_expenses", "tier": 1, "group": "Annual expenses", "cfg": "retire",
     "key": "annual_expenses_override", "jump": ("plan", "assumptions"),
     "note": "Leave blank to use Monarch's own cashflow read where available."},
    {"id": "annual_savings", "tier": 1, "group": "Annual savings", "cfg": "retire",
     "key": "annual_savings_override", "jump": ("plan", "assumptions")},
    {"id": "household_size", "tier": 1, "group": "Household size", "cfg": "retire",
     "key": "household_size", "scored": False, "jump": ("plan", "assumptions"),
     "note": "Defaults to 2 -- change it if your tax household is a different size."},
    {"id": "birth_year", "tier": 1, "group": "Birth year", "cfg": "retire",
     "key": "birth_year", "jump": ("plan", "assumptions"), "severity": "critical"},
    {"id": "spouse_birth_year", "tier": 1, "group": "Spouse birth year", "cfg": "retire",
     "key": "spouse_birth_year", "required_if": "mfj", "jump": ("plan", "assumptions"), "severity": "critical"},
    {"id": "ss_benefit", "tier": 1, "group": "SS estimated benefit (at FRA)", "cfg": "retire",
     "key": "ss_estimated_fra_benefit", "jump": ("plan", "assumptions"),
     "note": "Best source: your own ssa.gov/myaccount statement."},
    # jump fixed 2026-07-23 (found while wiring the tour): both inputs actually live in
    # estateContent() (PLAN -> ESTATE, ids est-donees/est-homebasis), not ASSUMPTIONS — the old
    # ("plan","assumptions") jump target has been silently wrong since this checklist was first
    # built, sending every "GO ->" click for these two items to the wrong sub-tab.
    {"id": "estate_donees", "tier": 1, "group": "Estate donees (# of people)", "cfg": "retire",
     "key": "estate_donees", "jump": ("plan", "estate")},
    {"id": "estate_home_basis", "tier": 1, "group": "Estate home cost basis", "cfg": "retire",
     "key": "estate_home_basis", "jump": ("plan", "estate"),
     "note": "Purchase price + capital improvements on your primary residence."},

    # ---- Tier 2: PLAN -> ASSUMPTIONS (still stored in insurance.json -- 2026-07-25: the UI fields
    # moved here from PLAN -> INSURANCE because Jason found them non-intuitive to hunt for on the
    # INSURANCE tab; storage/endpoint (_read_insurance_cfg()/POST /api/insurance-config) is
    # untouched, since insurance.py, get_benefits(), and this very checklist all read it from
    # there already -- only the "cfg"/data-source label below stays "insurance" for that reason,
    # even though the "jump" target is now the ASSUMPTIONS subtab. ----
    {"id": "income_self", "tier": 2, "group": "Your income", "cfg": "insurance",
     "key": "income_self", "kind": "income", "jump": ("plan", "assumptions"), "severity": "critical",
     "note": "Nothing on the INSURANCE page (or the ACA/tax MAGI calcs) computes without at least "
             "one income entered. $0/blank counts on its own if the other income is a real number "
             "-- tick \"no income (unemployed/retired)\" only if BOTH incomes are $0."},
    {"id": "income_spouse", "tier": 2, "group": "Spouse income", "cfg": "insurance",
     "key": "income_spouse", "kind": "income", "required_if": "mfj", "jump": ("plan", "assumptions"),
     "severity": "critical",
     "note": "$0/blank counts on its own if the other income is a real number -- tick \"no income "
             "(unemployed/retired)\" only if BOTH incomes are $0."},
    {"id": "policies", "tier": 2, "group": "Policies in force", "cfg": "insurance",
     "key": "policies", "kind": "list", "jump": ("plan", "insurance"),
     "note": "One row per policy -- life, disability, umbrella, home, auto, LTC."},
    {"id": "home_value_override", "tier": 2, "group": "Home value override", "cfg": "insurance",
     "key": "home_value_override", "jump": ("plan", "insurance"),
     "note": "Only needed if your home isn't tracked via Monarch/Zillow."},

    # ---- Tier 2: PLAN -> CASH·DEBT (same insurance.json register) ----
    {"id": "mortgage_rate", "tier": 2, "group": "Mortgage rate + monthly payment", "cfg": "insurance",
     "key": "mortgage_rate_pct", "jump": ("plan", "cashdebt")},
    {"id": "refi_rate", "tier": 2, "group": "Refi candidate rate + closing costs", "cfg": "insurance",
     "key": "refi_rate_pct", "jump": ("plan", "cashdebt")},
    {"id": "deposit_yield", "tier": 2, "group": "Deposit yield (checking/savings APY)", "cfg": "insurance",
     "key": "deposit_yield_pct", "jump": ("plan", "cashdebt")},
    {"id": "debt_rates", "tier": 2, "group": "Per-debt APRs", "cfg": "insurance",
     "key": "debt_rates", "kind": "dict", "jump": ("plan", "cashdebt"),
     "note": "Only debts missing a rate show as set-rate on the avalanche table."},
    {"id": "ef_accounts", "tier": 2, "group": "Emergency-fund source accounts", "cfg": "insurance",
     "key": "ef_accounts", "kind": "dict", "jump": ("plan", "cashdebt"),
     "note": "Until you designate them, the emergency-fund gauge guesses from account names — it "
             "counts checking/savings/money-market only and is blind to a reserve held anywhere "
             "that earns a real return."},

    # ---- Tier 2: PLAN -> BENEFITS (same insurance.json register) ----
    {"id": "k401_pretax", "tier": 2, "group": "401(k) % + match formula", "cfg": "insurance",
     "key": "k401_pretax_pct", "jump": ("plan", "benefits")},
    {"id": "k401_biweekly", "tier": 2, "group": "Per-paycheck 401(k) total", "cfg": "insurance",
     "key": "k401_biweekly_total", "jump": ("plan", "benefits")},
    {"id": "isr_status", "tier": 2, "group": "After-tax/ISR status (mega-backdoor Roth)", "cfg": "insurance",
     "key": "inservice_rollover", "kind": "sentinel", "sentinel": "unknown", "jump": ("plan", "benefits"),
     "severity": "critical",
     "note": "If unknown, ask your plan administrator -- it's the one fact that unlocks or blocks this lane."},
    {"id": "hsa_contribution", "tier": 2, "group": "HSA contribution", "cfg": "insurance",
     "key": "hsa_contribution", "jump": ("plan", "benefits")},

    # ---- Tier 3: everyone eventually, not everyone immediately ----
    {"id": "aca_slcsp", "tier": 3, "group": "ACA benchmark premium (SLCSP)", "cfg": "retire",
     "key": "aca_slcsp_annual", "jump": ("plan", "assumptions"),
     "note": "Only relevant pre-Medicare (before 65)."},
    {"id": "policy_targets", "tier": 3, "group": "IPS policy targets", "cfg": "policy",
     "key": "targets_by_sleeve", "scored": False, "jump": ("insights", "drift"),
     "note": "Ships with a reasonable default (70/10/15/5 US-equity/Intl/Bond/Cash) -- only edit if yours differs."},
    {"id": "position_caps", "tier": 3, "group": "Position caps per ticker", "cfg": "policy",
     "key": "position_caps", "kind": "dict", "jump": ("insights", "drift"),
     "note": "Optional -- only if you want a per-name concentration alert."},
    {"id": "api_finnhub", "tier": 3, "group": "Finnhub API key", "cfg": "secret",
     "key": "FINNHUB_API_KEY", "jump": None,
     "note": "Set in .env (see INSTALL.md) -- gates Research briefs and grounded valuation."},
    {"id": "api_tavily", "tier": 3, "group": "Tavily API key", "cfg": "secret",
     "key": "TAVILY_API_KEY", "jump": None,
     "note": "Set in .env (see INSTALL.md) -- gates news alerts and opportunity scanning."},
    # Notifications (2026-07-26) -- Tier 3 (optional, not blocking): "configured" means either a
    # push device is subscribed OR SMTP is set up, either one is enough to count as done. cfg="notify"
    # is a NEW branch (see _setup_field_filled below) since this data lives in notify_subs.json/
    # notify_prefs.json + the SECRET_SPECS SMTP_* keys, not any of the existing retire/insurance/
    # policy/secret/auth sources.
    {"id": "notifications_configured", "tier": 3, "group": "Security & outage notifications", "cfg": "notify",
     "key": "configured", "jump": ("settings", "notifications"),
     "note": "Push and/or email alerts for security events and portfolio-provider outages, instead "
             "of only showing up in SETTINGS -> SECURITY -> SECURITY AUDIT LOG. Optional but "
             "recommended -- set up in SETTINGS -> NOTIFICATIONS."},
    # HTTPS/TLS (2026-07-27, found the same pass as backup_passphrase above) -- Tier 3/optional on
    # purpose, not critical: this app is designed to run LAN-only by default (Jason confirmed his
    # own instance is), where plain HTTP carries little real exposure. It's genuinely worth doing
    # if this instance (or a future user's) is ever reachable beyond a trusted LAN -- self-signed,
    # Let's Encrypt/ACME, or a manual CSR, all in-app, no command line. cfg="tls" reads
    # tls.has_active_cert() directly (a plain file-existence check, not session/request state), so
    # unlike notify_flags/auth_flags this needs no extra flags dict threaded through
    # get_setup_progress() -- see _setup_field_filled's cfg=="tls" branch.
    {"id": "https_enabled", "tier": 3, "group": "HTTPS / TLS certificate", "cfg": "tls",
     "key": "active", "jump": ("settings", "security"),
     "note": "Optional if this instance is strictly LAN-only (no real exposure); worth doing if "
             "it's ever reachable beyond your own network. Self-signed, Let's Encrypt/ACME, or a "
             "manual CSR -- all set up in SETTINGS -> SECURITY -> Certificate, no command line "
             "required."},
]


def _setup_field_filled(spec, retire, insurance, policy, secrets_set, auth_flags=None, notify_flags=None):
    """True if `spec`'s value counts as provided. `secrets_set` is the set of secret keys that
    are set (env-locked OR saved) -- same definition /api/secrets itself uses. `auth_flags`
    (2026-07-24, extended same day to cover 2 cfg=="auth" entries: `mfa_enabled` and
    `recovery_key_set`) is a small dict keyed by spec["key"] -- both live in auth.db, not any of
    the JSON/SQLite config stores the other three cfg branches read. `notify_flags` (2026-07-26,
    same pattern) covers cfg=="notify" -- notifications.py's own subscription/SMTP state, not a
    config-file value either."""
    if spec["cfg"] == "secret":
        return spec["key"] in secrets_set
    if spec["cfg"] == "auth":
        return bool((auth_flags or {}).get(spec["key"]))
    if spec["cfg"] == "notify":
        return bool((notify_flags or {}).get(spec["key"]))
    if spec["cfg"] == "tls":
        # Plain file-existence check (tls.py's has_active_cert()) -- no request/session state
        # involved, unlike auth_flags/notify_flags above, so no extra param needs threading
        # through get_setup_progress() for this one.
        return tls.has_active_cert()
    cfg = {"retire": retire, "insurance": insurance, "policy": policy}.get(spec["cfg"]) or {}
    val = cfg.get(spec["key"])
    kind = spec.get("kind", "scalar")
    if kind in ("list", "dict"):
        return bool(val)
    if kind == "sentinel":
        return val is not None and val != spec.get("sentinel")
    if kind == "income":
        # A literal 0 (or still-blank field) is unambiguous on its own once the OTHER earner
        # already has a real nonzero income on file -- entering 0 for yourself when your spouse's
        # $85k is already sitting there is clearly deliberate, no extra confirmation needed. It's
        # only ambiguous when BOTH sides read as zero/blank at once (could be two untouched fields,
        # or a genuinely no-income household) -- that combination requires the explicit "no income"
        # checkbox before either side counts as answered. Jason, 2026-07-23: "I expect to be able to
        # enter 0 for either ONE. I only need to tick retired/unemployed if I'm going to put 0 for
        # both."
        if val in (None, "", 0):
            other_key = "income_spouse" if spec["key"] == "income_self" else "income_self"
            other_val = insurance.get(other_key)
            if other_val not in (None, "", 0):
                return True
            return bool(insurance.get("no_income"))
        return True
    return val not in (None, "")


def _setup_progress(auth_flags=None, notify_flags=None):
    """Compute the full onboarding completeness model: every SETUP_FIELDS entry checked against
    the LIVE config, grouped by tier, with an honest X-of-N that only counts fields where
    "blank" really means "not yet provided" for THIS household (married-only fields are excluded
    from a single filer's denominator; already-reasonable-default fields are shown but never
    counted -- see SETUP_FIELDS' docstring). `auth_flags` (2026-07-24) is read from the caller's
    session/auth.db, not any of the JSON config stores this function otherwise reads.
    `notify_flags` (2026-07-26) is the notifications.py equivalent -- see get_setup_progress()."""
    retire = _read_retire()
    insurance = _read_insurance_cfg()
    policy = _read_policy()
    married = tax.norm_status(retire.get("filing_status") or "mfj") == "mfj"
    secrets_stored = _read_secrets()
    secrets_set = {s["key"] for s in SECRET_SPECS
                   if s["key"] in _ENV_SECRETS_AT_BOOT or bool(secrets_stored.get(s["key"]))}

    tiers = {}
    for spec in SETUP_FIELDS:
        applicable = not (spec.get("required_if") == "mfj" and not married)
        filled = _setup_field_filled(spec, retire, insurance, policy, secrets_set, auth_flags, notify_flags)
        scored = applicable and spec.get("scored", True)
        severity = spec.get("severity", "optional")
        # "done" (drives the % bar / X-of-N counts) != "filled" (drives the ✓/△/! glyph). A blank
        # optional/situational field (△) already has a usable default, so it counts as progress; only
        # a blank CRITICAL field (!) should cost you percentage -- Jason, 2026-07-23: "consider usable
        # defaults as progress and only detract percentage by factoring the critical ! entries
        # unresolved." `filled` itself is untouched so the checklist icon still shows the true state.
        counts_as_done = filled or severity != "critical"
        item = {"id": spec["id"], "tier": spec["tier"], "group": spec["group"],
                "filled": bool(filled), "applicable": applicable, "scored": scored,
                "severity": severity, "note": spec.get("note"),
                "jump": {"tab": spec["jump"][0], "subtab": spec["jump"][1]} if spec.get("jump") else None}
        t = tiers.setdefault(spec["tier"], {"tier": spec["tier"], "done": 0, "total": 0, "items": []})
        t["items"].append(item)
        if scored:
            t["total"] += 1
            if counts_as_done:
                t["done"] += 1
    tier_list = [tiers[k] for k in sorted(tiers)]
    done = sum(t["done"] for t in tier_list)
    total = sum(t["total"] for t in tier_list)
    return {"done": done, "total": total,
            "pct": round(100.0 * done / total, 1) if total else 100.0,
            "married": married, "tiers": tier_list}


@app.get("/api/setup-progress")
def get_setup_progress(request: Request):
    uid = request.session.get("uid")
    user = auth.get_user_by_id(uid) if uid else None
    auth_flags = {"mfa_enabled": bool(user and user.get("totp_enabled")),
                  "recovery_key_set": bool(user and user.get("recovery_key_hash"))}
    # Notifications (2026-07-26) -- "configured" counts as done if EITHER this account has a push
    # device subscribed OR SMTP is set up (shared household email doesn't need every viewer to
    # also subscribe push for the checklist to consider notifications "handled").
    notify_flags = {"configured": bool(uid and (notifications.subscription_count(uid)
                                                 or notifications.smtp_configured()))}
    return _setup_progress(auth_flags=auth_flags, notify_flags=notify_flags)


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
    # RESUME-TRACKER 2026-07-11 queued follow-up (2), built 2026-08-06: a ticker the user TYPED
    # into the watchlist is an explicit instruction, not a screening candidate -- it must never be
    # silently dropped for failing the growth/price/PE filters the way an auto-discovered peer
    # should be. Captured HERE, before the peer expansion below appends to `tickers`, so
    # "user_added" means exactly "came from cfg['watchlist']" and can never leak onto a peer.
    user_added = set(tickers)
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
            ideas.append({**_screen_one(t, crit), "already_held": t in held,
                          "user_added": t in user_added})
        except Exception as e:
            ideas.append({"ticker": t, "available": False, "error": str(e),
                          "user_added": t in user_added})
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

    all_ideas = ideas
    screened = len(all_ideas)
    # The screen applies to auto-discovered peers exactly as before. A user-added watchlist name
    # that fails the price cap or the fit gate is no longer discarded -- it moves to a separate
    # `unscreened_watchlist` list instead, so it's always visible WITH its metrics and the reason
    # it wouldn't have qualified, rather than disappearing with no explanation. Two deliberate
    # limits on the bypass: (1) it does NOT exempt `available` -- a ticker that returned no data at
    # all has nothing to display, and is surfaced as `unavailable_watchlist` so a typo'd symbol is
    # still visible as a problem; (2) these stay OUT of `ideas`, so the ranked idea list keeps
    # meaning "passed your screen" and the OPP_MAX_IDEAS cap can't be crowded out by watchlist
    # entries. The alternative -- mixing them into `ideas` -- would have made "fit >= min_fit" no
    # longer true of everything in that list, quietly breaking every downstream consumer of it.
    ideas = [i for i in all_ideas if i.get("available") and _price_ok(i)
             and (i.get("fit_score") or 0) >= OPP_MIN_FIT][:OPP_MAX_IDEAS]
    kept = {i.get("ticker") for i in ideas}
    unscreened_watchlist, unavailable_watchlist = [], []
    for i in all_ideas:
        if not i.get("user_added") or i.get("ticker") in kept:
            continue
        if not i.get("available"):
            unavailable_watchlist.append({"ticker": i.get("ticker"),
                                          "reason": i.get("error") or "no data returned for this symbol"})
            continue
        why = []
        if not _price_ok(i):
            p = i.get("price")
            why.append(f"share price {('$' + format(p, '.2f')) if p is not None else 'unknown'} outside your "
                       f"{'$' + str(minp) if minp is not None else 'no min'}–"
                       f"{'$' + str(maxp) if maxp is not None else 'no max'} cap")
        if (i.get("fit_score") or 0) < OPP_MIN_FIT:
            why.append(f"fit {i.get('fit_score') or 0}/{i.get('max_score') or 4} below the {OPP_MIN_FIT} minimum")
        unscreened_watchlist.append({**i, "excluded_because": why or ["outside the top-ranked cap"]})
    return {"generated_at": int(time.time()), "criteria": crit, "strategy": cfg.get("strategy_description"),
            "watchlist": cfg.get("watchlist") or [], "include_holding_peers": bool(cfg.get("include_holding_peers")),
            "discovery_queries": cfg.get("discovery_queries") or [], "alert_holdings_limit": cfg.get("alert_holdings_limit"),
            "screened": screened, "count": len(ideas), "min_fit": OPP_MIN_FIT, "ideas": ideas,
            "unscreened_watchlist": unscreened_watchlist,
            "unavailable_watchlist": unavailable_watchlist,
            "note": f"Top {len(ideas)} of {screened} screened — only names scoring >= {OPP_MIN_FIT}/4 within your "
                    "price cap. Ranked by 'opportunity_score' = analyst-target-implied forward return WEIGHTED "
                    "by quality fit (a per-DOLLAR % return, never per share; a wild target on a low-quality name "
                    "won't top a solid one). Analyst targets are noisy and mean-reverting — research ideas, NOT "
                    "advice; ask the Fiduciary to deep-dive any name."
                    + (f" Plus {len(unscreened_watchlist)} watchlist name(s) shown unscreened below — you added "
                       "them explicitly, so they're never dropped for failing the screen."
                       if unscreened_watchlist else "")}


_opp_lock = threading.Lock()


def _read_opps():
    try:
        return store.read_json_strict(OPPORTUNITIES_FILE)
    except Exception:
        return None


def _do_opportunities():
    """Recompute the (slow) live scan and cache it to disk. Single-flight."""
    if not _opp_lock.acquire(blocking=False):
        return
    try:
        res = _opportunities()
        store.write_json(OPPORTUNITIES_FILE, res)
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
def run_opportunities():
    """Recompute + cache the opportunity scan (the weekly scheduled scan). Token-guarded."""
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
    _tlh_drip_tickers = set()   # taxable+DRIP tickers among TLH candidates (wash-sale caution)
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
                if drip_on:
                    _tlh_drip_tickers.add(tkr)
            if yld is not None and yld >= _knob("loc_min_yield") and val > 0:
                est = round(val * yld / 100, 2)
                location.append({"ticker": tkr, "account": name, "value": round(val, 2),
                                 "dividend_yield_pct": round(yld, 2), "est_annual_income": est,
                                 "drip": drip_on,
                                 "est_annual_taxable_reinvestment": est if drip_on else 0.0})
    harvest.sort(key=lambda x: x["loss"])                       # most negative first
    # ---- GAP-4: wash-sale verdicts + replacement pairs on every TLH candidate ----
    try:
        sectors = {p.get("ticker"): p.get("sector") for p in ((_read_exp() or {}).get("positions") or [])
                   if p.get("ticker") and p.get("sector")}
        wash_by = {w["ticker"]: w for w in washsale.assess(
            harvest, _read_sharelog(), {t: True for t in _tlh_drip_tickers}, sectors)}
        for hrow in harvest:
            hrow["wash"] = wash_by.get(hrow["ticker"])
    except Exception:
        pass
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
        return store.read_json_strict(FL_FILE)
    except Exception:
        return None


def _do_fl():
    if not _fl_lock.acquire(blocking=False):
        return
    try:
        res = _fee_liquidity()
        store.write_json(FL_FILE, res)
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
def run_fees_liquidity():
    """Recompute + cache the fees/liquidity audit (for a scheduled refresh). Token-guarded."""
    if _fl_lock.locked():
        return {"ok": True, "status": "already running"}
    threading.Thread(target=_do_fl, daemon=True).start()
    return {"ok": True, "status": "started"}


# ---------------- Exposure: sector allocation + portfolio beta + concentration ----------------
EXP_FILE = os.environ.get("EXP_FILE", "/data/exposure.json")
EXP_TTL = int(os.environ.get("EXP_TTL", "43200"))    # cache exposure ~12h
_exp_lock = threading.Lock()
# 2026-07-30: company-profile caching moved onto store.cached_fetch() -- see _profile().


def _profile(sym):
    """Finnhub company profile (sector via finnhubIndustry). Cached ~1 day, disk-persisted via
    store.cached_fetch (2026-07-30 -- survives a restart). Returns (profile_dict, meta) -- meta =
    {"fetched_at", "stale"}, see store.cached_fetch(). `profile_dict` is {} (no keys to read) if
    Finnhub isn't configured or nothing has ever been fetched successfully."""
    if not FINNHUB:
        return {}, {"fetched_at": None, "stale": True}

    def _compute():
        try:
            r = requests.get("https://finnhub.io/api/v1/stock/profile2",
                             params={"symbol": sym, "token": FINNHUB}, timeout=15)
            prof = r.json() if r.status_code < 400 else {}
        except Exception:
            prof = {}
        return prof if isinstance(prof, dict) else {}

    return store.cached_fetch("finnhub_profile", sym, 86400, _compute, is_ok=lambda r: bool(r))


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
    oldest_fetch, any_stale = None, False
    for c in cons:
        tkr, typ, val = c["ticker"], (c.get("type") or ""), (c.get("value") or 0)
        if typ == "Cash":
            sector, beta = "Cash", None
        elif typ in ("Mutual Fund", "ETF"):
            sector, beta = "Funds (no look-through)", (_valuation(tkr) or {}).get("beta")
        else:
            prof, pmeta = _profile(tkr)
            sector = ((prof.get("finnhubIndustry") or "").strip() or "Other")
            beta = (_valuation(tkr) or {}).get("beta")
            fa = pmeta.get("fetched_at")
            if fa is not None and (oldest_fetch is None or fa < oldest_fetch):
                oldest_fetch = fa
            any_stale = any_stale or bool(pmeta.get("stale"))
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
    return {"generated_at": int(time.time()),
            # RESUME-TRACKER (2026-07-30, "last-known-good cache" Part 2): when the sector
            # classification data underlying `by_sector` was actually fetched (oldest across every
            # ticker profiled, so any one stale profile flags the whole panel) -- distinct from
            # `generated_at` above, which is just when this response was assembled.
            "cache_fetched_at": oldest_fetch, "cache_stale": any_stale,
            "invested_value": invested,
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
        return store.read_json_strict(EXP_FILE)
    except Exception:
        return None


def _do_exp():
    if not _exp_lock.acquire(blocking=False):
        return
    try:
        res = _exposure()
        store.write_json(EXP_FILE, res)
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
def run_exposure():
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
        return store.read_json_strict(LT_FILE)
    except Exception:
        return None


def _do_lt():
    if not _lt_lock.acquire(blocking=False):
        return
    try:
        res = _lookthrough()
        store.write_json(LT_FILE, res)
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
def run_lookthrough():
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


TAVILY_CACHE_TTL = int(os.environ.get("TAVILY_CACHE_TTL", "7200"))   # ~2h -- news moves fast, but
# an outage should still serve the last successful pull for this exact query, not come up empty.


def _tavily_results(query, days=14, max_results=5, with_meta=False):
    """Tavily news search via mcpo -> list of article dicts. mcpo returns a TEXT blob (not JSON).
    Disk-persisted last-known-good cache (2026-07-30, RESUME-TRACKER "last-known-good cache" item
    -- Tavily previously had NO caching/fallback at all: any error (rate-limit, quota, non-200,
    timeout, exception) returned [] outright, meaning a Tavily outage lost access to PREVIOUSLY
    successful results too, not just new ones). Cache key is the exact (query, days, max_results)
    triple -- different phrasing/windows are deliberately different cache entries, not merged.
    `with_meta=True` also returns the {"fetched_at", "stale"} dict (see store.cached_fetch) for a
    caller that wants to surface "data as of" to the user; default False keeps the plain-list
    contract every existing caller (web_search tool, _company_news, discovery) already expects."""
    def _compute():
        try:
            r = requests.post(f"{MCPO}/tavily/tavily_news_search",
                              headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
                              json={"query": query, "days": days, "max_results": max_results}, timeout=45)
            if r.status_code >= 400:
                _news_note(False, _news_http_detail(r), "search")
                return []
            _news_note(True)
            try:
                data = r.json()
            except ValueError:
                data = r.text
        except Exception as e:
            _news_note(False, f"{type(e).__name__}: {str(e)[:160]}", "search")
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

    key = f"{query}|{days}|{max_results}"
    results, meta = store.cached_fetch("tavily", key, TAVILY_CACHE_TTL, _compute,
                                       is_ok=lambda r: bool(r), stale_after=NEWS_STALE_AFTER)
    return (results, meta) if with_meta else results


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


def _discover(d, crit, _tavily_meta_acc=None):
    """Tavily news + one LLM extraction -> candidate tickers, then screen them (excludes held
    names). `_tavily_meta_acc`, if given a list, gets each query's cache {fetched_at, stale} dict
    appended -- lets the caller (_do_discovery) surface an overall "data as of" for the panel
    without changing this function's own return shape."""
    cfg = _read_scanner()
    snippets = []
    for q in (cfg.get("discovery_queries") or [])[:4]:
        arts, meta = _tavily_results(q, days=10, max_results=5, with_meta=True)
        if _tavily_meta_acc is not None:
            _tavily_meta_acc.append(meta)
        for a in arts:
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
        tavily_meta = []
        _fail0 = _NEWS_DIAG["fail_count"]
        discovered = _discover(d, cfg.get("criteria") or {}, _tavily_meta_acc=tavily_meta)
        fetch_times = [m["fetched_at"] for m in tavily_meta if m.get("fetched_at") is not None]
        out = {"generated_at": int(time.time()),
               # RESUME-TRACKER (2026-07-30, "last-known-good cache" Part 2): when the underlying
               # Tavily news pulls actually succeeded (oldest across this run's discovery queries,
               # so any one stale/fallback query flags the whole panel) -- distinct from
               # generated_at above, which is just when this discovery pass ran.
               "cache_fetched_at": min(fetch_times) if fetch_times else None,
               "cache_stale": any(m.get("stale") for m in tavily_meta),
               "alerts": _holdings_alerts(d, cfg.get("alert_holdings_limit") or 15),
               "discovered": discovered,
               "note": "News-discovered ideas + material-news alerts on holdings. Read-only research, not advice."}
        # 2026-10-19: surface fetch failures IN the report (see _news_note) so the UI can say why
        # news is stale instead of only that it is.
        _fails = _NEWS_DIAG["fail_count"] - _fail0
        out["fetch_failures"] = _fails
        out["fetch_error"] = _NEWS_DIAG["last_fail_detail"] if _fails else None
        store.write_json(DISCOVERY_FILE, out)
        _disc_state["status"] = "idle"
    finally:
        _disc_lock.release()


@app.get("/api/discovery")
def get_discovery():
    """Stored news-discovery report: ideas found from the newsfeed + material-news alerts on holdings."""
    try:
        return store.read_json_strict(DISCOVERY_FILE)
    except Exception:
        return {"generated_at": None, "discovered": [], "alerts": [],
                "note": "No discovery run yet — POST /api/run-discovery (or wait for the scheduled job)."}


@app.get("/api/news-status")
def get_news_status(probe: int = 0):
    """Health of the Tavily news path (2026-10-19; sibling of /api/edgar-status). Counters are
    since this process started. `?probe=1` makes ONE live, uncached call through mcpo (uses a
    Tavily credit) and reports exactly what came back -- the fastest way to tell 'mcpo's Tavily
    server is down' (HTTP 404) from 'key rejected' from 'working'."""
    out = {}
    if probe:
        t0 = time.time()
        try:
            r = requests.post(f"{MCPO}/tavily/tavily_news_search",
                              headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
                              json={"query": "SPY stock news", "days": 3, "max_results": 1}, timeout=45)
            if r.status_code >= 400:
                _news_note(False, _news_http_detail(r), "probe")
                out["probe"] = {"ok": False, "detail": _news_http_detail(r)}
            else:
                _news_note(True)
                out["probe"] = {"ok": True, "http": r.status_code, "bytes": len(r.text or "")}
        except Exception as e:
            _news_note(False, f"{type(e).__name__}: {str(e)[:160]}", "probe")
            out["probe"] = {"ok": False, "detail": f"{type(e).__name__}: {str(e)[:160]}"}
        out["probe"]["ms"] = int((time.time() - t0) * 1000)
    with _NEWS_DIAG_LOCK:
        diag = dict(_NEWS_DIAG)
    return {**diag, **out, "mcpo": MCPO, "cache_ttl_s": TAVILY_CACHE_TTL, "stale_after_s": NEWS_STALE_AFTER}


@app.post("/api/run-discovery")
def run_discovery():
    """Trigger the background news discovery + holdings-alerts run (the internal scheduler runs this daily)."""
    jid, created = _enqueue("discovery", source="api")   # serial queue + dedup (Increment B)
    return {"ok": True, "status": "queued" if created else "already queued/running",
            "job_id": jid, **_disc_state}


@app.get("/api/run-discovery/status")
def run_discovery_status():
    return _disc_state


# ---------------- Paper-trading ledger for the What-If / Strategist agent (HYPOTHETICAL, sandboxed) ----------------
def _apply_recurring(p, scenario_id):
    """Lazily apply any due recurring contributions — a self-contained 'cron' that runs on read,
    so recurring deposits work without an external scheduler. Adds cash, tracks contributed capital,
    logs a deposit transaction per period, and advances the next date. Storage Stage B (2026-07-24):
    each deposit is now a direct SQL insert (store.paper_tx_append), not a list append batched into
    a later _write_paper -- `scenario_id` is a new required param so the insert can be scoped."""
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
        store.paper_tx_append(scenario_id, rec["next_ts"], "deposit", "CASH", 0, 0, round(amt, 2), recurring=True)
        rec["next_ts"] += int(rec.get("cadence_days") or 30) * 86400
        n += 1
        changed = True
    return changed


# ---------------------------------------------------------------------------
# MULTI-SCENARIO PAPER LEDGERS (2026-07-23, Jason — Phase 1 of the 2026-07-19 backlog item).
# What used to be ONE global hypothetical paper-trading ledger is now a REGISTRY of named
# scenarios (start from scratch, or seeded once from the real portfolio and left to diverge).
# Capped at MAX_ACTIVE_SCENARIOS "active" at a time -- Jason's explicit resource-constraint call:
# each active scenario gets its own full weekly Strategist ReAct pass (a real LLM run on the
# dual-Pascal box), so uncapped scenario count directly multiplies GPU load. Archiving a scenario
# frees a slot without deleting its history (still viewable/comparable), so the cap limits live
# GPU work, not how many scenarios you can keep around for comparison.
#
# Legacy single-ledger installs migrate IN PLACE the first time this loads: the old flat
# paper.json becomes scenario id "default"/name "Default", status active, every field carried
# over verbatim. Every call site that doesn't care which scenario (the IRR calc, the backtest
# reader, a plain `/api/paper` hit with no scenario_id) keeps working unchanged against
# `_default_scenario_id()` -- the first active scenario by creation order -- so this is additive,
# not a breaking change for anything that predates it.
#
# 2026-07-23b (Jason, same day): every scenario now carries a "kind" -- "ai" (Strategist-managed,
# weekly autonomous ReAct pass, up to MAX_ACTIVE_SCENARIOS active) or "user" (a SEPARATE ledger the
# user trades BY HAND via manual buy/sell -- the Strategist never touches it, capped at
# MAX_USER_SCENARIOS since Jason wants exactly one). AI scenarios also carry a "risk_profile"
# (conservative/moderate/aggressive) so two AI scenarios have a PRACTICAL reason to coexist --
# they're given different mandates, not just duplicate runs of the same strategy (see
# _RISK_PROFILE_GUIDANCE / STRATEGIST_PROMPT). Legacy/pre-existing scenarios migrate as
# kind="ai", risk_profile="moderate" -- identical behavior to before this field existed.
# ---------------------------------------------------------------------------
MAX_ACTIVE_SCENARIOS = int(os.environ.get("PAPER_MAX_ACTIVE_SCENARIOS", "2"))
MAX_USER_SCENARIOS = int(os.environ.get("PAPER_MAX_USER_SCENARIOS", "1"))
RISK_PROFILES = ("conservative", "moderate", "aggressive")

# ---------------------------------------------------------------------------
# Strategist GOALS (2026-07-30, Jason -- see RESUME-TRACKER's "user-visible/settable Strategist
# GOALS" item + its follow-up design-analysis entry). Full structured knobs (option 2 from that
# analysis, chosen over free-text or hybrid): every AI scenario gets an explicit, editable stance
# on time horizon, benchmark/target, cash buffer, position-size ceiling, turnover, and sector
# tilt -- not just the risk-mandate preset. Each knob is its own field (not one free-text blob) so
# it can be validated, rendered compactly on the scenario card, and summarized in plain language
# (see _goal_summary_text()) without asking the model to interpret prose.
# ---------------------------------------------------------------------------
GOAL_SECTORS = ("Technology", "Healthcare", "Financials", "Energy", "Industrials",
                "Consumer Discretionary", "Consumer Staples", "Utilities", "Real Estate",
                "Materials", "Communication Services")
TURNOVER_LEVELS = ("low", "medium", "high")
TARGET_RETURN_MODES = ("benchmark", "absolute")
GOAL_BOUNDS = {"time_horizon_years": (1, 40), "cash_buffer_pct": (0, 50),
               "max_position_pct": (2, 60), "target_return_pct": (-50, 100)}

# Risk-profile-conditioned defaults for a scenario's goal, used both for brand-new scenarios
# (nothing chosen yet) and to migrate scenarios created BEFORE this feature existed -- a scenario
# with no "goal" key on disk gets these on next read (see _scenario_goal()), rather than a
# migration script. This is a DISCLOSED behavior change for pre-existing scenarios (their next
# weekly run gains explicit numeric constraints they didn't have before, e.g. a position-size
# ceiling) -- not silent, and roughly matches what each risk_profile's existing prose guidance
# already implied loosely, so it isn't a sharp discontinuity in practice.
_GOAL_DEFAULTS_BY_RISK = {
    "conservative": {"cash_buffer_pct": 10, "max_position_pct": 10, "turnover_tolerance": "low"},
    "moderate":     {"cash_buffer_pct": 5,  "max_position_pct": 15, "turnover_tolerance": "medium"},
    "aggressive":   {"cash_buffer_pct": 2,  "max_position_pct": 25, "turnover_tolerance": "high"},
}


def _default_goal_fields(risk_profile="moderate"):
    base = dict(_GOAL_DEFAULTS_BY_RISK.get(risk_profile, _GOAL_DEFAULTS_BY_RISK["moderate"]))
    base.update({"time_horizon_years": 10, "target_return_mode": "benchmark",
                 "target_return_pct": None, "favor_sectors": [], "avoid_sectors": []})
    return base


def _clamp(v, lo, hi):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, v))


def _normalize_goal(body, risk_profile, base=None):
    """Validates/coerces a (possibly partial) goal dict from a request body, layered onto `base`
    (an existing scenario's current goal, or risk-profile defaults for a new one) -- so a PATCH-
    style update only touching one field (e.g. just turnover_tolerance) can't blank out the rest.
    Unknown sector names and out-of-range numbers are dropped/clamped rather than rejecting the
    whole request -- keeps the endpoint forgiving of a slightly-stale frontend."""
    g = dict(base) if base else _default_goal_fields(risk_profile)
    if not isinstance(body, dict):
        return g
    if "time_horizon_years" in body:
        lo, hi = GOAL_BOUNDS["time_horizon_years"]
        v = _clamp(body["time_horizon_years"], lo, hi)
        if v is not None:
            g["time_horizon_years"] = int(round(v))
    if "cash_buffer_pct" in body:
        lo, hi = GOAL_BOUNDS["cash_buffer_pct"]
        v = _clamp(body["cash_buffer_pct"], lo, hi)
        if v is not None:
            g["cash_buffer_pct"] = round(v, 1)
    if "max_position_pct" in body:
        lo, hi = GOAL_BOUNDS["max_position_pct"]
        v = _clamp(body["max_position_pct"], lo, hi)
        if v is not None:
            g["max_position_pct"] = round(v, 1)
    if "turnover_tolerance" in body:
        t = str(body["turnover_tolerance"] or "").lower()
        if t in TURNOVER_LEVELS:
            g["turnover_tolerance"] = t
    if "target_return_mode" in body:
        m = str(body["target_return_mode"] or "").lower()
        if m in TARGET_RETURN_MODES:
            g["target_return_mode"] = m
    if "target_return_pct" in body:
        raw = body["target_return_pct"]
        if raw in (None, ""):
            g["target_return_pct"] = None
        else:
            lo, hi = GOAL_BOUNDS["target_return_pct"]
            v = _clamp(raw, lo, hi)
            if v is not None:
                g["target_return_pct"] = round(v, 1)
    if g.get("target_return_mode", "benchmark") != "absolute":
        g["target_return_pct"] = None   # no dangling target when mode isn't absolute
    if "favor_sectors" in body and isinstance(body["favor_sectors"], list):
        g["favor_sectors"] = [s for s in body["favor_sectors"] if s in GOAL_SECTORS]
    if "avoid_sectors" in body and isinstance(body["avoid_sectors"], list):
        g["avoid_sectors"] = [s for s in body["avoid_sectors"] if s in GOAL_SECTORS]
    # a sector can't be both favored and avoided -- avoid wins (more conservative to honor a
    # restriction than a preference if the frontend ever sends a contradictory pair)
    g["favor_sectors"] = [s for s in g.get("favor_sectors", []) if s not in g.get("avoid_sectors", [])]
    return g


def _scenario_goal(s):
    """The fully-populated goal dict for a scenario -- whatever's actually stored under s["goal"],
    with any missing keys backfilled from that scenario's risk-profile defaults (covers scenarios
    created before this feature existed, and any partial dict from an older frontend build)."""
    defaults = _default_goal_fields(s.get("risk_profile") or "moderate")
    stored = s.get("goal") if isinstance(s.get("goal"), dict) else {}
    out = dict(defaults)
    out.update({k: v for k, v in stored.items() if k in defaults})
    return out


def _goal_summary_text(goal):
    """Plain-language one-liner for the scenario card / prompt header -- the direct answer to
    'what is this scenario trying to do' that the tracker item flagged as missing entirely."""
    bench = (f"an absolute ~{goal['target_return_pct']}%/yr return" if goal.get("target_return_mode") == "absolute"
             and goal.get("target_return_pct") is not None else "beating the S&P 500")
    parts = [f"{goal.get('time_horizon_years', 10)}-year horizon, aiming for {bench}",
             f"~{goal.get('cash_buffer_pct', 5)}% cash buffer",
             f"positions capped at {goal.get('max_position_pct', 15)}%",
             f"{goal.get('turnover_tolerance', 'medium')} turnover"]
    s = ", ".join(parts) + "."
    if goal.get("favor_sectors"):
        s += " Favors " + ", ".join(goal["favor_sectors"]) + "."
    if goal.get("avoid_sectors"):
        s += " Avoids " + ", ".join(goal["avoid_sectors"]) + "."
    return s


def _goal_guidance_text(goal):
    """Rendered into STRATEGIST_PROMPT's {goal_guidance} placeholder -- the structured knobs as
    directive instructions, layered alongside (not replacing) the risk-mandate guidance."""
    bench = (f"an absolute annual return of roughly {goal['target_return_pct']}%"
             if goal.get("target_return_mode") == "absolute" and goal.get("target_return_pct") is not None
             else "beating the S&P 500 over the horizon below")
    turnover_note = {"low": "favor buy-and-hold — avoid trading just to stay active; only trade on a real thesis change",
                     "medium": "balanced turnover — trade when your thesis meaningfully changes, don't churn",
                     "high": "you may trade more actively when your read on a name changes, but every trade still needs a thesis"
                     }.get(goal.get("turnover_tolerance", "medium"), "")
    lines = [f"GOAL PARAMETERS for this scenario (in addition to the risk mandate above):",
             f"- Time horizon: roughly {goal.get('time_horizon_years', 10)} years. Target: {bench}.",
             f"- Cash buffer: keep roughly {goal.get('cash_buffer_pct', 5)}% of total value in cash as a "
             f"buffer rather than fully deploying every dollar.",
             f"- Position-size ceiling: keep any single position under about {goal.get('max_position_pct', 15)}% "
             f"of total value — a concentration limit, not a target.",
             f"- Turnover: {turnover_note}."]
    if goal.get("favor_sectors"):
        lines.append(f"- Favor sectors when you have a comparably strong idea: {', '.join(goal['favor_sectors'])}.")
    if goal.get("avoid_sectors"):
        lines.append(f"- Avoid entirely — do not initiate new positions in: {', '.join(goal['avoid_sectors'])}.")
    return "\n".join(lines)


def _new_scenario_id():
    return uuid.uuid4().hex[:12]


def _empty_scenario_fields(cash):
    """The ledger-only fields (no id/name/status/created) for a from-scratch scenario.
    Storage Stage B (2026-07-24): no `transactions`/`reports` keys anymore -- those live in real
    SQL tables now (see store.py), hydrated fresh by scenario id wherever they're needed
    (_read_paper, _paper_marked) rather than carried as part of this dict."""
    return {"starting_cash": round(cash, 2), "cash": round(cash, 2), "contributed": 0.0,
            "seed_basis": 0.0, "seeded_from": None, "positions": {}, "recurring": None}


def _seeded_scenario_fields(sel, cash, basis_mode, scenario_id):
    """The ledger-only fields for a scenario seeded from the owner's REAL holdings (real share
    counts). `sel`: list of tickers, or None/'all' for every tradable holding. `basis_mode`:
    'current' (seed at today's price, return starts ~0 — measures forward decisions from here)
    or 'real' (carry actual cost basis, return reflects embedded gains already baked in).
    Mutual funds and cash are skipped (no live tradable price). Storage Stage B (2026-07-24):
    `scenario_id` is a new required param -- seed transactions are inserted directly into SQL
    (store.paper_tx_append) as they're computed, rather than collected into a list and returned
    as part of the fields dict; the CALLER must generate the scenario id before calling this
    (previously `_new_scenario_id()` ran after this function — see post_paper_scenario)."""
    d = _data()
    by_ticker = {c.get("ticker"): c for c in d.get("consolidated", []) if c.get("ticker")}
    if sel in (None, "all", "ALL", "*"):
        want = [t for t, c in by_ticker.items() if c.get("type") not in ("Cash", "Mutual Fund")]
    else:
        want = [str(t).upper().strip() for t in sel if t]
    positions, seed_cost, seed_mkt, skipped = {}, 0.0, 0.0, []
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
        store.paper_tx_append(scenario_id, ts, "seed", t, round(qty, 6), round(px, 4), round(qty * px, 2))
    fields = {"starting_cash": round(cash, 2), "cash": round(cash, 2), "contributed": 0.0,
              "seed_basis": round(seed_cost, 2), "positions": positions,
              "recurring": None, "seeded_from": f"real_holdings ({basis_mode} basis)"}
    return fields, {"seeded": len(positions), "skipped": skipped, "seed_market_value": round(seed_mkt, 2),
                    "seed_cost_basis": round(seed_cost, 2)}


def _read_registry():
    try:
        raw = store.read_json_strict(PAPER_FILE)
    except Exception:
        raw = None
    if isinstance(raw, dict) and isinstance(raw.get("scenarios"), dict) and raw["scenarios"]:
        return raw
    # Migrate a legacy flat ledger (or start fresh on a brand-new install) into one scenario.
    # Storage Stage B (2026-07-24): no transactions/reports keys here anymore -- if this really
    # was a pre-multi-scenario flat ledger, store.ensure_paper_migrated() (called once at app
    # startup, before any request reaches this function) already imported its transactions/
    # reports into SQL under scenario id "default", matching the id assigned here.
    legacy = raw if isinstance(raw, dict) else {}
    scenario = {"id": "default", "name": "Default", "status": "active",
                "kind": legacy.get("kind", "ai"), "risk_profile": legacy.get("risk_profile", "moderate"),
                "starting_cash": legacy.get("starting_cash", DEFAULT_PAPER_CASH),
                "cash": legacy.get("cash", DEFAULT_PAPER_CASH),
                "contributed": legacy.get("contributed", 0.0),
                "seed_basis": legacy.get("seed_basis", 0.0),
                "seeded_from": legacy.get("seeded_from"),
                "positions": legacy.get("positions", {}), "recurring": legacy.get("recurring"),
                "created": legacy.get("created", int(time.time()))}
    reg = {"scenarios": {"default": scenario}}
    _write_registry(reg)
    return reg


def _write_registry(reg):
    store.write_json(PAPER_FILE, reg)


def _active_scenarios(reg, kind=None):
    """Active scenarios, oldest first. `kind` filters to 'ai' or 'user'; None (default) returns
    both kinds together, which only the scenario-switcher listing needs -- every cap check and
    the Strategist loop always pass an explicit kind so the two pools never bleed into each other."""
    return sorted((s for s in (reg.get("scenarios") or {}).values()
                   if s.get("status") == "active" and (kind is None or s.get("kind", "ai") == kind)),
                  key=lambda s: s.get("created") or 0)


def _default_scenario_id(reg=None, kind="ai"):
    """Which scenario an implicit (no scenario_id given) call operates on: the oldest ACTIVE
    scenario of `kind` (default 'ai' -- every pre-existing call site predates the user-ledger
    concept and means the AI-managed book), or failing that, the oldest scenario of that kind
    period, or failing THAT, the oldest scenario of any kind (defensive only)."""
    reg = reg or _read_registry()
    active = _active_scenarios(reg, kind=kind)
    if active:
        return active[0]["id"]
    same_kind = sorted((s for s in (reg.get("scenarios") or {}).values() if s.get("kind", "ai") == kind),
                       key=lambda s: s.get("created") or 0)
    if same_kind:
        return same_kind[0]["id"]
    any_ = sorted((reg.get("scenarios") or {}).values(), key=lambda s: s.get("created") or 0)
    return any_[0]["id"] if any_ else "default"


def _read_paper(scenario_id=None):
    """Storage Stage B (2026-07-24): `transactions`/`reports` are hydrated from SQL (the FULL
    history, not display-truncated -- _paper_irr and backtest.paper_backtest both need the
    complete timeline) after any due recurring contributions are applied, so a caller reading
    `p["transactions"]`/`p["reports"]` right after this returns sees everything, including
    whatever _apply_recurring just inserted this call."""
    reg = _read_registry()
    scens = reg.setdefault("scenarios", {})
    sid = scenario_id or _default_scenario_id(reg)
    p = scens.get(sid)
    if p is None:      # unknown/stale id — fall back to the default rather than erroring
        sid = _default_scenario_id(reg)
        p = scens.get(sid)
    if p is None:       # registry genuinely empty (defensive; migration should prevent this)
        p = dict(_empty_scenario_fields(DEFAULT_PAPER_CASH), id="default", name="Default",
                 status="active", created=int(time.time()))
        sid = p["id"]
        scens[sid] = p
        _write_registry(reg)
    if _apply_recurring(p, sid):
        _write_paper(sid, p)
    p["transactions"] = store.paper_tx_all(sid)
    p["reports"] = store.paper_reports_all(sid)
    return p


def _write_paper(scenario_id, p):
    """Storage Stage B (2026-07-24): transactions/reports are never persisted into the registry
    blob -- they live in SQL (see _read_paper's hydration, _paper_marked's own fetch) -- so they're
    stripped here before the write, rather than round-tripping a (possibly large, definitely
    redundant) copy back into app_state on every trade/contribution/recurring-application."""
    reg = _read_registry()
    stored = {k: v for k, v in p.items() if k not in ("transactions", "reports")}
    reg.setdefault("scenarios", {})[scenario_id] = stored
    _write_registry(reg)


def _paper_marked(p):
    """Mark the hypothetical ledger to current market prices. Storage Stage B (2026-07-24):
    transactions/reports are fetched fresh from SQL by scenario id HERE, rather than trusting
    whatever (if anything) is already sitting on `p["transactions"]`/`p["reports"]` -- this
    function is called both on a _read_paper()-hydrated dict (already carries the FULL history)
    and directly on a raw registry scenario dict (get_paper_scenarios/archive/activate/
    risk_profile all do this — that raw dict no longer carries these fields post-migration), so
    self-hydrating here is what makes it correct regardless of which kind of `p` it's given."""
    sid = p.get("id")
    recent_tx = store.paper_tx_recent(sid, 40) if sid else []
    recent_reports = store.paper_reports_recent(sid, 12) if sid else []
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
    goal = _scenario_goal(p) if p.get("kind", "ai") == "ai" else None
    return {"id": p.get("id"), "name": p.get("name"), "status": p.get("status"),
            "kind": p.get("kind", "ai"), "risk_profile": p.get("risk_profile"),
            "goal": goal, "goal_summary": _goal_summary_text(goal) if goal else None,
            "starting_cash": start, "contributed": contributed, "seed_basis": seed_basis,
            "seeded_from": p.get("seeded_from"), "invested_capital": round(basis, 2),
            "cash": cash, "holdings_value": round(holdings_val, 2),
            "total_value": total, "total_return": round(total - basis, 2),
            "return_pct": round((total - basis) / basis * 100, 2) if basis else None,
            "recurring": p.get("recurring"),
            "positions": positions, "transactions": recent_tx,
            "reports": recent_reports, "created": p.get("created"),
            "note": "HYPOTHETICAL paper-trading portfolio — no real money, no real orders. Return is "
                    "measured against invested capital (starting cash + contributions)."}


@app.get("/api/paper")
def get_paper(scenario_id: str = None):
    """A hypothetical paper-trading scenario, marked to current prices. Omit scenario_id for the
    default (oldest active) scenario — old integrations keep working unchanged."""
    return _paper_marked(_read_paper(scenario_id))


@app.get("/api/paper/scenarios")
def get_paper_scenarios():
    """List every scenario (active + archived, both kinds) with a lightweight marked summary
    each, for the scenario switcher + YOUR LEDGER panel UI."""
    reg = _read_registry()
    scens = sorted((reg.get("scenarios") or {}).values(), key=lambda s: s.get("created") or 0)
    out = []
    for s in scens:
        m = _paper_marked(s)
        out.append({"id": m["id"], "name": m["name"], "status": m["status"], "kind": m["kind"],
                    "risk_profile": m["risk_profile"], "goal": m["goal"], "goal_summary": m["goal_summary"],
                    "total_value": m["total_value"], "return_pct": m["return_pct"],
                    "invested_capital": m["invested_capital"], "created": m["created"],
                    "seeded_from": m["seeded_from"], "position_count": len(m["positions"])})
    default_id = _default_scenario_id(reg, kind="ai")
    return {"scenarios": out, "default_scenario_id": default_id,
            "max_active": MAX_ACTIVE_SCENARIOS, "active_count": len(_active_scenarios(reg, kind="ai")),
            "max_user_ledgers": MAX_USER_SCENARIOS,
            "user_ledger_count": len(_active_scenarios(reg, kind="user")),
            "risk_profiles": list(RISK_PROFILES),
            "goal_sectors": list(GOAL_SECTORS), "turnover_levels": list(TURNOVER_LEVELS),
            "goal_bounds": GOAL_BOUNDS,
            "note": f"Up to {MAX_ACTIVE_SCENARIOS} AI scenarios can be active (Strategist-managed) "
                    "at once — each active scenario gets its own weekly LLM review, so the cap "
                    "bounds GPU load. Archive one to free a slot without losing its history; "
                    f"archived scenarios are frozen (no more trades/reviews) but stay viewable for "
                    f"comparison. Separately, up to {MAX_USER_SCENARIOS} ledger(s) can be YOURS to "
                    "trade by hand — the Strategist never touches those."}


@app.post("/api/paper/scenarios")
async def post_paper_scenario(req: Request):
    """Create a new scenario. Body: {name, cash (from-scratch starting cash, ignored if seed given),
    seed: {tickers: [...]|'all', cash, basis: 'current'|'real'} | omitted for from-scratch,
    kind: 'ai'|'user' (default 'ai'), risk_profile: 'conservative'|'moderate'|'aggressive'
    (kind='ai' only, default 'moderate'), goal: {...} (kind='ai' only, optional -- see
    _normalize_goal(); any field left out gets that risk profile's default)}. Enforces the cap for
    whichever kind is being created — MAX_ACTIVE_SCENARIOS for 'ai', MAX_USER_SCENARIOS for
    'user' — separately from each other, so creating your one manual ledger never competes with
    the AI scenario slots or vice versa."""
    body = await req.json()
    kind = str(body.get("kind") or "ai").lower()
    if kind not in ("ai", "user"):
        kind = "ai"
    reg = _read_registry()
    cap = MAX_ACTIVE_SCENARIOS if kind == "ai" else MAX_USER_SCENARIOS
    label = "AI scenario" if kind == "ai" else "your ledger"
    if len(_active_scenarios(reg, kind=kind)) >= cap:
        return JSONResponse({"error": f"{label} cap reached ({cap} active max) — "
                                       f"archive or delete an existing {'scenario' if kind=='ai' else 'ledger'} first"},
                            status_code=400)
    name = str(body.get("name") or "").strip()[:80] or (f"Scenario {len(reg.get('scenarios') or {}) + 1}"
                                                          if kind == "ai" else "My Ledger")
    # sid generated BEFORE the seed branch (2026-07-24, Storage Stage B) -- _seeded_scenario_fields
    # now inserts seed transactions directly into SQL as it computes them, so it needs the
    # scenario's id up front rather than the caller assigning one afterward.
    sid = _new_scenario_id()
    seed = body.get("seed")
    seed_meta = None
    if isinstance(seed, dict):
        try:
            seed_cash = float(seed.get("cash") or 0)
        except (TypeError, ValueError):
            seed_cash = 0.0
        basis_mode = str(seed.get("basis") or "current").lower()
        fields, seed_meta = _seeded_scenario_fields(seed.get("tickers"), seed_cash, basis_mode, sid)
    else:
        try:
            cash = float(body.get("cash") or DEFAULT_PAPER_CASH)
        except (TypeError, ValueError):
            cash = DEFAULT_PAPER_CASH
        fields = _empty_scenario_fields(cash)
    ts = int(time.time())
    risk_profile = None
    goal = None
    if kind == "ai":
        risk_profile = str(body.get("risk_profile") or "moderate").lower()
        if risk_profile not in RISK_PROFILES:
            risk_profile = "moderate"
        goal = _normalize_goal(body.get("goal"), risk_profile)
    scenario = dict(fields, id=sid, name=name, status="active", created=ts, kind=kind,
                    risk_profile=risk_profile, goal=goal)
    reg.setdefault("scenarios", {})[sid] = scenario
    _write_registry(reg)
    out = {"ok": True, "scenario": _paper_marked(scenario)}
    if seed_meta:
        out["seed_result"] = seed_meta
    return out


@app.post("/api/paper/scenarios/{scenario_id}/archive")
def post_paper_archive(scenario_id: str):
    """Freeze a scenario (no more trades — AI reviews for an 'ai' scenario, manual trades for a
    'user' ledger) and free its active slot. History stays intact and viewable — this is not a
    delete. For a 'user' ledger this is the "disable to save compute" action (it also stops the
    valuation/mark-to-market work `/api/paper/scenarios` does on every load)."""
    reg = _read_registry()
    s = (reg.get("scenarios") or {}).get(scenario_id)
    if not s:
        return JSONResponse({"error": "scenario not found"}, status_code=404)
    s["status"] = "archived"
    _write_registry(reg)
    return {"ok": True, "scenario": _paper_marked(s)}


@app.post("/api/paper/scenarios/{scenario_id}/activate")
def post_paper_activate(scenario_id: str):
    """Reactivate an archived scenario — subject to the same per-kind cap as creation."""
    reg = _read_registry()
    s = (reg.get("scenarios") or {}).get(scenario_id)
    if not s:
        return JSONResponse({"error": "scenario not found"}, status_code=404)
    kind = s.get("kind", "ai")
    cap = MAX_ACTIVE_SCENARIOS if kind == "ai" else MAX_USER_SCENARIOS
    if s["status"] != "active" and len(_active_scenarios(reg, kind=kind)) >= cap:
        label = "AI scenario" if kind == "ai" else "ledger"
        return JSONResponse({"error": f"{label} cap reached ({cap} active max) — "
                                       f"archive another active {label} first"}, status_code=400)
    s["status"] = "active"
    _write_registry(reg)
    return {"ok": True, "scenario": _paper_marked(s)}


@app.post("/api/paper/scenarios/{scenario_id}/delete")
def post_paper_delete(scenario_id: str):
    """Permanently remove a scenario. Blocked only if it's the LAST remaining AI-kind scenario —
    the registry always keeps at least one AI scenario so implicit (no scenario_id) calls have
    somewhere to land. A 'user' ledger has no such floor: delete it freely, down to zero, since
    nothing implicitly depends on it existing (Jason, 2026-07-23: "should be able to delete... if
    they want to save the compute demands")."""
    reg = _read_registry()
    scens = reg.get("scenarios") or {}
    target = scens.get(scenario_id)
    if not target:
        return JSONResponse({"error": "scenario not found"}, status_code=404)
    if target.get("kind", "ai") == "ai":
        ai_count = sum(1 for s in scens.values() if s.get("kind", "ai") == "ai")
        if ai_count <= 1:
            return JSONResponse({"error": "can't delete the only remaining AI scenario"}, status_code=400)
    scens.pop(scenario_id, None)
    _write_registry(reg)
    store.paper_scenario_wipe(scenario_id)   # Storage Stage B (2026-07-24) -- SQL rows don't vanish for free like a dict-pop did; clean them up explicitly
    return {"ok": True, "remaining": len(scens)}


@app.post("/api/paper/scenarios/{scenario_id}/risk_profile")
async def post_paper_risk_profile(scenario_id: str, req: Request):
    """Change an AI scenario's risk mandate (conservative/moderate/aggressive) without resetting
    its ledger — takes effect on its NEXT weekly review. This is the practical lever that
    differentiates two AI scenarios from each other (Jason, 2026-07-23)."""
    body = await req.json()
    rp = str(body.get("risk_profile") or "").lower()
    if rp not in RISK_PROFILES:
        return JSONResponse({"error": f"risk_profile must be one of {', '.join(RISK_PROFILES)}"}, status_code=400)
    reg = _read_registry()
    s = (reg.get("scenarios") or {}).get(scenario_id)
    if not s:
        return JSONResponse({"error": "scenario not found"}, status_code=404)
    if s.get("kind", "ai") != "ai":
        return JSONResponse({"error": "risk profile only applies to AI-managed scenarios"}, status_code=400)
    s["risk_profile"] = rp
    _write_registry(reg)
    return {"ok": True, "scenario": _paper_marked(s)}


@app.post("/api/paper/scenarios/{scenario_id}/goal")
async def post_paper_goal(scenario_id: str, req: Request):
    """Update an AI scenario's structured GOAL parameters (2026-07-30) — time horizon, target
    return/benchmark, cash buffer, position-size ceiling, turnover tolerance, sector tilt — without
    resetting its ledger, same in-place/no-reset/next-run-applies contract as risk_profile above.
    Body is a (possibly partial) goal dict — see _normalize_goal(); any field omitted keeps its
    current value, so the frontend can PATCH a single slider without resending the whole form."""
    body = await req.json()
    reg = _read_registry()
    s = (reg.get("scenarios") or {}).get(scenario_id)
    if not s:
        return JSONResponse({"error": "scenario not found"}, status_code=404)
    if s.get("kind", "ai") != "ai":
        return JSONResponse({"error": "goal parameters only apply to AI-managed scenarios"}, status_code=400)
    current = _scenario_goal(s)
    s["goal"] = _normalize_goal(body, s.get("risk_profile") or "moderate", base=current)
    _write_registry(reg)
    return {"ok": True, "scenario": _paper_marked(s)}


def _exec_trade(action, ticker, dollars=None, shares=None, scenario_id=None):
    """Core paper-trade logic at the current price. Returns (result_dict, error_str)."""
    action = str(action or "").lower()
    t = str(ticker or "").upper().strip()
    if action not in ("buy", "sell") or not t:
        return None, "need action (buy|sell) and ticker"
    px = _price(t)
    if not px:
        return None, f"no price available for {t}"
    p = _read_paper(scenario_id)
    sid = p["id"]
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
    store.paper_tx_append(sid, int(time.time()), action, t, round(traded, 6), round(px, 4),
                          round(traded * px, 2))
    _write_paper(sid, p)
    return {"ok": True, "action": action, "ticker": t, "shares": round(traded, 6),
            "price": px, "cash": p["cash"]}, None


@app.post("/api/paper/trade")
async def paper_trade(req: Request):
    """Execute a HYPOTHETICAL MANUAL trade — this is the user's own hand on the ticket, not the
    Strategist. Body: {action: buy|sell, ticker, dollars|shares, scenario_id}. Omit scenario_id
    for the default 'ai' scenario, BUT that will then be rejected (see below) — manual trades are
    only allowed on a 'user'-kind ledger, so the two trading sources never mix in one scenario's
    history. Paper only."""
    body = await req.json()
    scenario_id = body.get("scenario_id")
    reg = _read_registry()
    sid_resolved = scenario_id or _default_scenario_id(reg, kind="user")
    target = (reg.get("scenarios") or {}).get(sid_resolved)
    if target and target.get("kind", "ai") != "user":
        return JSONResponse({"error": "manual trades aren't allowed on AI-managed scenarios — "
                                       "trade in your own ledger instead"}, status_code=400)
    res, err = _exec_trade(body.get("action"), body.get("ticker"), body.get("dollars"), body.get("shares"),
                           scenario_id=sid_resolved)
    return res if res else JSONResponse({"error": err}, status_code=400)


@app.post("/api/paper/reset")
async def paper_reset(req: Request):
    """Reset ONE scenario's ledger to a starting cash balance (positions/history/recurring wiped
    for that scenario only — other scenarios are untouched). Body: {starting_cash, scenario_id}."""
    body = await req.json()
    cash = float(body.get("starting_cash") or DEFAULT_PAPER_CASH)
    reg = _read_registry()
    sid = body.get("scenario_id") or _default_scenario_id(reg)
    existing = (reg.get("scenarios") or {}).get(sid) or {}
    fields = _empty_scenario_fields(cash)
    scenario = dict(fields, id=sid, name=existing.get("name", "Default"),
                    status=existing.get("status", "active"), created=int(time.time()),
                    kind=existing.get("kind", "ai"), risk_profile=existing.get("risk_profile"))
    reg.setdefault("scenarios", {})[sid] = scenario
    _write_registry(reg)
    store.paper_scenario_wipe(sid)
    return {"ok": True, "starting_cash": cash, "scenario_id": sid}


@app.post("/api/paper/seed")
async def paper_seed(req: Request):
    """RESET one scenario's ledger, pre-seeded with a selection of the owner's REAL holdings (real
    share counts). Body: {tickers: [...] | "all", cash: 0, basis: "current"|"real", scenario_id}.
    basis 'current' seeds each position at today's price (return starts ~0 — measures the
    Strategist's forward decisions); 'real' carries your actual per-share cost basis (return then
    reflects embedded gains). Mutual funds and cash are skipped (no live price to trade).
    HYPOTHETICAL — no real orders."""
    body = await req.json()
    try:
        cash = float(body.get("cash") or 0)
    except (TypeError, ValueError):
        cash = 0
    basis_mode = str(body.get("basis") or "current").lower()
    reg = _read_registry()
    sid = body.get("scenario_id") or _default_scenario_id(reg)
    existing = (reg.get("scenarios") or {}).get(sid) or {}
    store.paper_scenario_wipe(sid)
    fields, seed_meta = _seeded_scenario_fields(body.get("tickers"), cash, basis_mode, sid)
    scenario = dict(fields, id=sid, name=existing.get("name", "Default"),
                    status=existing.get("status", "active"), created=int(time.time()),
                    kind=existing.get("kind", "ai"), risk_profile=existing.get("risk_profile"))
    reg.setdefault("scenarios", {})[sid] = scenario
    _write_registry(reg)
    return {"ok": True, "scenario_id": sid, "basis_mode": basis_mode,
            "cash": round(cash, 2), "invested_capital": round(seed_meta["seed_cost_basis"] + cash, 2),
            **seed_meta}


def _log_report(summary, scenario_id=None, goal_snapshot=None):
    """Append a weekly report + snapshot total value to one scenario. `goal_snapshot` (2026-07-30,
    Strategist GOALS): the goal parameters active for the run that produced this report, stamped
    in so history stays interpretable even after the scenario's goal is later edited. Returns
    {ok, report_count}."""
    p = _read_paper(scenario_id)
    sid = p["id"]
    marked = _paper_marked(p)
    ts = int(time.time())
    store.paper_report_append(sid, ts, marked["total_value"], marked["return_pct"],
                              str(summary or "")[:4000], goal_snapshot=goal_snapshot)
    return {"ok": True, "report_count": store.paper_reports_count(sid), "scenario_id": sid}


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
    p = _read_paper(body.get("scenario_id"))
    sid = p["id"]
    p["cash"] = round((p.get("cash") or 0) + amt, 2)
    p["contributed"] = round((p.get("contributed") or 0) + amt, 2)
    store.paper_tx_append(sid, int(time.time()), "deposit", "CASH", 0, 0, round(amt, 2))
    _write_paper(sid, p)
    return {"ok": True, "added": round(amt, 2), "cash": p["cash"], "contributed": p["contributed"],
            "scenario_id": sid}


@app.post("/api/paper/recurring")
async def paper_recurring(req: Request):
    """Set or clear a RECURRING cash contribution on one scenario. Body: {amount,
    cadence: weekly|biweekly|monthly, scenario_id}. amount<=0 clears it. Applied lazily on read
    (no external scheduler needed)."""
    body = await req.json()
    try:
        amt = float(body.get("amount") or 0)
    except (TypeError, ValueError):
        amt = 0
    p = _read_paper(body.get("scenario_id"))
    sid = p["id"]
    if amt <= 0:
        p["recurring"] = None
        _write_paper(sid, p)
        return {"ok": True, "recurring": None, "scenario_id": sid}
    cadence = str(body.get("cadence") or "monthly").lower()
    days = {"weekly": 7, "biweekly": 14, "monthly": 30}.get(cadence, 30)
    p["recurring"] = {"amount": round(amt, 2), "cadence": cadence, "cadence_days": days,
                      "next_ts": int(time.time()) + days * 86400}
    _write_paper(sid, p)
    return {"ok": True, "recurring": p["recurring"], "scenario_id": sid}


@app.post("/api/paper/run")
def paper_run():
    """Dashboard trigger for the Strategist's weekly review — covers EVERY active scenario in one
    job (sequentially, one LLM pass at a time; see `_do_strategist`), local/unguarded, single-
    flighted by the same lock as /api/run-strategist so it can't pile up concurrent runs."""
    jid, created = _enqueue("strategist", source="dashboard")   # serial queue + dedup (Increment B)
    return {"ok": True, "status": "queued" if created else "already queued/running",
            "job_id": jid, **_strat_state}


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
    """System prompt with the current date grounded in front of it. Also substitutes the {user}
    token (how the assistants refer to the user — see _user_ref / SETTINGS → PERSONA PROMPTS) so any
    prompt can carry {user} and have it resolved centrally; a no-op for prompts without the token."""
    try:
        prompt = prompt.replace("{user}", _user_ref())
    except Exception:
        pass
    return _today_line() + "\n\n" + prompt


# ---------------------------------------------------------------------------
# STRATEGIST — server-side autonomous tool loop (replaces the OWUI/n8n path).
# Drives Ollama /api/chat directly; every tool is an in-process function, so
# the loop runs to completion under OUR iteration cap (no OWUI pass-through,
# no per-turn tool-call limit). HYPOTHETICAL paper account only — no real money.
# ---------------------------------------------------------------------------

# Per-scenario risk mandate injected into STRATEGIST_PROMPT via {risk_guidance} -- this is what
# gives two AI scenarios a PRACTICAL reason to coexist (Jason, 2026-07-23): they run under
# genuinely different marching orders, not the same strategy twice. Kept short/directive since
# it's competing for attention with a long prompt already.
_RISK_PROFILE_GUIDANCE = {
    "conservative": "Your mandate for THIS scenario is CONSERVATIVE (capital-preservation-first): "
        "favor larger, established, lower-volatility names and quality/dividend compounders; keep a "
        "meaningful cash buffer rather than staying fully invested; avoid speculative or highly "
        "volatile names; size positions smaller. Prioritize not losing money over chasing upside.",
    "moderate": "Your mandate for THIS scenario is MODERATE / balanced growth: blend established "
        "compounders with a modest allocation to higher-conviction growth ideas; diversify across "
        "sectors; avoid both extreme concentration and excessive cash-hoarding.",
    "aggressive": "Your mandate for THIS scenario is AGGRESSIVE / growth-maximizing: seek "
        "higher-conviction, higher-upside names (including smaller-cap or higher-volatility growth "
        "stories) and size them meaningfully; you may run a more concentrated book and hold less "
        "idle cash than a conservative mandate would. Still require a real thesis per position — "
        "aggressive means higher risk tolerance, not speculation for its own sake.",
}

STRATEGIST_PROMPT = (
    "You are the Strategist — an autonomous portfolio manager running a HYPOTHETICAL paper-trading "
    "account (a SIMULATION; no real money, no real orders are ever placed). Your job is to grow a small "
    "starting balance through disciplined, well-reasoned buy/sell decisions and to report honestly.\n"
    "You are managing the '{scenario_name}' scenario specifically — {user} may have more than one AI "
    "scenario running in parallel, each with a DIFFERENT risk mandate and different starting conditions "
    "on purpose (that's the point of running more than one); every tool call in this run reads and writes "
    "ONLY this one scenario's ledger, so decide and report as if it's the only book that exists for this "
    "session.\n"
    "{risk_guidance}\n"
    "{goal_guidance}\n"
    "ACCOUNT TOOLS: get_paper_portfolio (your cash, positions, P/L, return vs. start — call this FIRST); "
    "paper_buy(ticker, dollars); paper_sell(ticker, shares — omit shares to sell the whole position); "
    "log_report(summary — call this LAST, exactly once).\n"
    "RESEARCH TOOLS: get_opportunities and get_discovery (idea sources); get_fundamentals(ticker) and "
    "get_valuation(ticker); get_research(ticker) (a Buy/Sell/Hold brief); get_holdings ({user}'s REAL "
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
    {"type": "function", "function": {"name": "get_fees_liquidity",
        "description": "Fee drag (fund/ETF expense ratios) and liquidity flags on The user's REAL holdings — "
                       "context only, e.g. when weighing a paper idea against what he already pays elsewhere.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_benefits",
        "description": "The user's REAL 401(k) employer match/headroom, HSA room, Roth IRA eligibility, and "
                       "mega-backdoor Roth capacity — context only (RESUME-TRACKER 2026-07-19: added so a "
                       "401k/HSA/contribution-room question doesn't require Jason to type figures the app "
                       "already tracks).",
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
_strat_state = {"status": "idle", "iterations": 0, "started": 0, "finished": 0,
                "scenario": None, "scenario_id": None, "scenarios_done": 0, "scenarios_total": 0}

# --- Strategist run telemetry (N13 candidate 3): a durable, capped JSONL audit log of every
#     autonomous weekly run so the ReAct loop is OBSERVABLE, not a black box. One record per run:
#     iterations used, how it ended (report / no_tool_calls / cap / error), whether it converged,
#     repeated ("wandering") tool calls, trades placed, search-budget burn, per-tool histogram,
#     duration. Best-effort — never raises, never influences the run. ---
STRAT_RUN_LOG_FILE = os.environ.get("STRAT_RUN_LOG_FILE", "/data/strategist_runs.jsonl")
# STRAT_RUN_LOG_MAX (2026-07-24, Storage Stage B): no longer a storage-trimming cap -- the
# `strategist_runs` table keeps every record forever now. Kept only as the /api/strategist-runs
# `limit` query param's upper clamp (a display sanity cap, not a data-retention one), so that
# endpoint's contract is unchanged for existing callers.
STRAT_RUN_LOG_MAX = int(os.environ.get("STRAT_RUN_LOG_MAX", "200"))
store.ensure_strategist_runs_migrated(STRAT_RUN_LOG_FILE)   # one-time import from the old .jsonl blob, no-ops after


def _log_strategist_run(rec):
    try:
        store.strategist_run_log(rec)
    except Exception:
        pass


def _strategist_dispatch(name, args, scenario_id=None, goal=None):
    """Run one tool call in-process and return a JSON-serializable result. `scenario_id` scopes
    the paper-ledger tools to ONE scenario for the duration of a run — the weekly loop below fixes
    it per pass (never exposed as a model-choosable tool argument, so the model can't accidentally
    trade the wrong scenario); advisory/read-only chat callers resolve it to the default scenario.
    `goal` (2026-07-30, Strategist GOALS): the scenario's active goal dict, stamped onto log_report
    only — never model-choosable, same reasoning as scenario_id above."""
    args = args or {}
    try:
        if name == "get_paper_portfolio":
            return _paper_marked(_read_paper(scenario_id))
        if name == "paper_buy":
            res, err = _exec_trade("buy", args.get("ticker"), dollars=args.get("dollars"),
                                   shares=args.get("shares"), scenario_id=scenario_id)
            return res or {"error": err}
        if name == "paper_sell":
            res, err = _exec_trade("sell", args.get("ticker"), shares=args.get("shares"),
                                   scenario_id=scenario_id)
            return res or {"error": err}
        if name == "log_report":
            return _log_report(args.get("summary"), scenario_id=scenario_id, goal_snapshot=goal)
        if name == "get_opportunities":
            return _opportunities()
        if name == "get_discovery":
            try:
                return store.read_json_strict(DISCOVERY_FILE)
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
        if name == "get_fees_liquidity":
            return get_fees_liquidity()
        if name == "get_benefits":
            return get_benefits()
        if name == "web_search":
            return {"results": _tavily_results(str(args.get("query") or ""),
                                               days=int(args.get("days") or 14),
                                               max_results=int(args.get("max_results") or 5))}
        return {"error": f"unknown tool {name}"}
    except Exception as e:
        return {"error": f"{name} failed: {e}"}


def _do_strategist_one(scenario_id, scenario_name, max_iter, risk_profile="moderate", goal=None):
    """One full ReAct pass against ONE AI-kind scenario. Returns the run's telemetry dict. Caller
    (below) holds `_strat_lock` for the whole multi-scenario job, only ever iterates 'ai'-kind
    ACTIVE scenarios (a 'user' ledger is never passed here — see `_do_strategist`), and drives one
    of these per scenario, sequentially — never in parallel, so this never adds concurrent GPU
    load beyond the single existing `_gpu_slot()` serialization point. `goal` (2026-07-30,
    Strategist GOALS): the scenario's structured goal dict (see _scenario_goal()) -- defaults to
    that risk profile's goal defaults if the caller doesn't have one handy."""
    goal = goal or _default_goal_fields(risk_profile)
    _t0 = time.time()
    telem = {"ts": int(_t0), "scenario_id": scenario_id, "scenario_name": scenario_name,
             "risk_profile": risk_profile, "goal_snapshot": goal, "max_iter": max_iter, "iterations": 0,
             "tool_calls": {}, "repeat_calls": 0, "trades": 0, "search_calls": 0, "search_budget": None,
             "nudged": False, "converged": False, "ended": "unknown", "error": None}
    _seen_sigs = set()
    try:
        risk_guidance = _RISK_PROFILE_GUIDANCE.get(risk_profile, _RISK_PROFILE_GUIDANCE["moderate"])
        goal_guidance = _goal_guidance_text(goal)
        prompt = (STRATEGIST_PROMPT.replace("{scenario_name}", scenario_name)
                  .replace("{risk_guidance}", risk_guidance).replace("{goal_guidance}", goal_guidance))
        messages = [
            {"role": "system", "content": _sys(prompt)},
            {"role": "user", "content": f"Perform your weekly review of the '{scenario_name}' scenario now: "
             "call get_paper_portfolio, research a few opportunities, make any buy/sell trades you judge "
             "worthwhile, then call log_report with your summary. Be efficient — don't deep-dive every name."},
        ]
        reported = False
        # Per-run web-search budget (settings knob, default 20) — PER SCENARIO PASS, same as before
        # multi-scenario support existed. Caps ONLY this unattended weekly run so an agentic loop
        # can't spray Tavily calls / crowd the free-tier pool it shares with the daily pipelines.
        # The interactive chat + on-demand advisory runs are deliberately NOT capped here (user-paced).
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
                with _gpu_slot():   # serialize qwen-27b generation across all features
                    res = llm._llm_chat(messages, _text_model(strategist=True), tools=STRATEGIST_TOOLS,
                                        temperature=0.4, num_ctx=STRAT_NUM_CTX,
                                        keep_alive=LLM_KEEP_ALIVE, timeout=STRAT_LLM_TIMEOUT)
                if not res.ok:
                    telem["ended"] = "ollama_error"
                    telem["error"] = res.error
                    _strat_state.update(status=f"error: {res.error}", finished=int(time.time()))
                    return telem
            except Exception as e:
                telem["ended"] = "exception"
                telem["error"] = str(e)[:200]
                _strat_state.update(status=f"error: {e}", finished=int(time.time()))
                return telem
            tcs = res.tool_calls
            asst = {"role": "assistant", "content": res.content}
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
                        result = _strategist_dispatch(nm, a, scenario_id=scenario_id, goal=goal)
                else:
                    result = _strategist_dispatch(nm, a, scenario_id=scenario_id, goal=goal)
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
            _log_report(final or "Weekly run ended without an explicit report (may have hit the step cap).",
                       scenario_id=scenario_id, goal_snapshot=goal)
        telem["converged"] = reported
    finally:
        telem["duration_s"] = round(time.time() - _t0, 1)
    return telem


def _do_strategist(max_iter=20):
    """Weekly Strategist review — runs once per ACTIVE 'ai'-kind scenario, SEQUENTIALLY (never in
    parallel; each pass is a separate full ReAct loop under the same single `_gpu_slot()`
    serialization everything else on the box already respects). Jason, 2026-07-23: capped at
    MAX_ACTIVE_SCENARIOS active AI scenarios specifically so this job's total GPU time scales with
    a small, bounded number, not with however many scenarios a user creates. A brand-new/legacy
    install always has exactly one AI scenario (post-migration), so this reduces to the original
    single-ledger behavior. 2026-07-23b: explicitly filtered to kind='ai' — a 'user'-kind ledger
    (traded by hand via /api/paper/trade) is NEVER included here, by construction, not just by
    convention; that's what makes it safe for the user to trade it themselves without the
    Strategist fighting them over it."""
    if not _strat_lock.acquire(blocking=False):
        return
    try:
        reg = _read_registry()
        active = _active_scenarios(reg, kind="ai")
        if not active:      # defensive — migration guarantees at least one AI scenario exists
            active = [{"id": _default_scenario_id(reg, kind="ai"), "name": "Default", "risk_profile": "moderate"}]
        _strat_state.update(status="running", iterations=0, started=int(time.time()), finished=0,
                            scenarios_total=len(active), scenarios_done=0, scenario=None)
        for idx, s in enumerate(active):
            _strat_state.update(scenario=s.get("name"), scenario_id=s.get("id"), iterations=0)
            telem = _do_strategist_one(s["id"], s.get("name") or s["id"], max_iter,
                                       risk_profile=s.get("risk_profile") or "moderate",
                                       goal=_scenario_goal(s))
            _log_strategist_run(telem)
            _strat_state["scenarios_done"] = idx + 1
        _strat_state.update(status="idle", finished=int(time.time()), scenario=None)
    finally:
        _strat_lock.release()


@app.post("/api/run-strategist")
def run_strategist():
    """Kick off the autonomous weekly Strategist run (server-side tool loop). Token-guarded; returns
    immediately and runs in the background. Poll /api/run-strategist/status or /api/paper for results."""
    jid, created = _enqueue("strategist", source="api")   # serial queue + dedup (Increment B)
    return {"ok": True, "status": "queued" if created else "already queued/running",
            "job_id": jid, **_strat_state}


@app.get("/api/run-strategist/status")
def run_strategist_status():
    return _strat_state


@app.get("/api/strategist-runs")
def strategist_runs(limit: int = 50):
    """Telemetry for recent autonomous Strategist runs — observability for the ReAct loop (is it
    converging, wandering, or hitting the step cap?). Read-only. `summary` aggregates the window so
    'does it wander?' is answerable at a glance; `runs` is the newest-first per-run detail."""
    try:
        rows = store.strategist_runs_read(limit=max(1, min(limit, STRAT_RUN_LOG_MAX)))
    except Exception:
        rows = []
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
    "record nothing — no buy, sell, or report tools exist in this mode. {user} is asking what you would do "
    "with a specific amount of NEW cash. Reason it through with your research and data tools.\n"
    "TOOLS: get_holdings ({user}'s REAL portfolio — the positions your plan MUST consider); "
    "get_fees_liquidity and get_benefits ({user}'s REAL fund expense ratios and 401k/HSA/Roth "
    "contribution room — use these for any question involving fees or contribution/match headroom "
    "instead of asking {user} to supply figures the app already tracks); "
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
                with _gpu_slot():   # serialize qwen-27b generation across all features
                    res = llm._llm_chat(messages, _text_model(strategist=True), tools=STRATEGIST_ADVICE_TOOLS,
                                        temperature=0.4, num_ctx=STRAT_NUM_CTX,
                                        keep_alive=LLM_KEEP_ALIVE, timeout=STRAT_LLM_TIMEOUT)
                if not res.ok:
                    _advice_state.update(status=f"error: {res.error}", finished=int(time.time()))
                    return
            except Exception as e:
                _advice_state.update(status=f"error: {e}", finished=int(time.time()))
                return
            tcs = res.tool_calls
            asst = {"role": "assistant", "content": res.content}
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


def _chat_enc(v):
    """Encrypts a chat.db text field before it hits SQLite -- message content/tool-call JSON and
    session titles are arguably the single most sensitive table in the app (a verbatim transcript
    of financial discussions). None passes through; session_id/role/persona/ts/created/updated
    stay plain (sort/lookup keys, low sensitivity). 2026-07-24, "All sensitive information stored
    needs to be encrypted"."""
    if v is None:
        return None
    return secrets_crypto.encrypt(v)


def _chat_dec(v):
    """Decrypts a chat.db text field, with legacy-plaintext fallback for messages/titles written
    before this module started encrypting (decrypt_or_legacy() returns non-token strings as-is).
    Never raises."""
    if v is None:
        return None
    return secrets_crypto.decrypt_or_legacy(v)


def _chat_create_session(persona, title):
    sid = uuid.uuid4().hex[:16]
    now = int(time.time())
    with _chat_conn() as c:
        c.execute("INSERT INTO sessions(id, persona, title, created, updated) VALUES(?,?,?,?,?)",
                  (sid, persona, _chat_enc(title), now, now))
    return sid


def _chat_add_message(sid, role, content, tool_json=None):
    with _chat_conn() as c:
        c.execute("INSERT INTO messages(session_id, role, content, tool_json, ts) VALUES(?,?,?,?,?)",
                  (sid, role, _chat_enc(content), _chat_enc(tool_json), int(time.time())))


def _chat_touch_session(sid, title=None):
    with _chat_conn() as c:
        if title:
            c.execute("UPDATE sessions SET updated=?, title=? WHERE id=?", (int(time.time()), _chat_enc(title), sid))
        else:
            c.execute("UPDATE sessions SET updated=? WHERE id=?", (int(time.time()), sid))


def _chat_sessions():
    with _chat_conn() as c:
        rows = c.execute("SELECT id, persona, title, created, updated FROM sessions "
                         "ORDER BY updated DESC LIMIT 200").fetchall()
    out = [dict(r) for r in rows]
    for d in out:
        d["title"] = _chat_dec(d["title"])
    return out


def _chat_messages(sid):
    with _chat_conn() as c:
        rows = c.execute("SELECT role, content, tool_json, ts FROM messages WHERE session_id=? "
                         "ORDER BY id ASC", (sid,)).fetchall()
    out = [dict(r) for r in rows]
    for d in out:
        d["content"] = _chat_dec(d["content"])
        d["tool_json"] = _chat_dec(d["tool_json"])
    return out


def _chat_delete_session(sid):
    with _chat_conn() as c:
        c.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        c.execute("DELETE FROM sessions WHERE id=?", (sid,))


_chat_init()


# ---- Fiduciary persona: full read-only analytics + web, no mutation ----
# Default persona system prompts. `{user}` is substituted at request time with the configured
# user reference (SETTINGS → PERSONA PROMPTS, default "the user") — see _resolve_persona_prompt.
# Users can override either prompt entirely and reset it back to these defaults.
FIDUCIARY_PROMPT = (
    "You are the Fiduciary — {user}'s read-only, holistic financial advisor. You analyze their REAL "
    "portfolio and financial plan; you place no trades and change nothing. Ground every answer in your "
    "tools rather than assumption, and cite the figures you used.\n"
    "START by calling get_holdings and get_policy ({user}'s Investment Policy Statement) so your advice fits "
    "their actual positions and targets. Then reach for the tool that fits the question: get_exposure "
    "(sector/beta/concentration), get_rebalance (IPS sleeve drift), get_risk_metrics (realized vol / "
    "drawdown / Sharpe), get_retirement and get_income_plan (long-range plan, Monte Carlo, glide path), "
    "get_tax_audit (tax-loss-harvesting + asset-location), get_aca (ACA/MAGI subsidy + cliff), "
    "get_social_security, get_benefits (401k match / HSA / Roth contribution room), get_fees_liquidity, "
    "get_lookthrough (fund holdings), get_scorecard, get_opportunities and get_discovery (ideas), "
    "get_fundamentals / get_valuation / get_research for a specific ticker, search_knowledge for tax rules / "
    "IRA-Roth mechanics / investing-policy guidance / estate-planning mechanics from {user}'s local reference "
    "library (cite the source doc), and web_search for current news or context.\n"
    "Estate planning — answer BOTH kinds of question, not just the numbers: {user}'s ESTATE module (via "
    "get_retirement/search_knowledge) already quantifies the tax/basis side of their actual plan (step-up "
    "ledger, gift-vs-hold, gifting runway, titling audit, and — once an estate nears the exemption — the "
    "SLAT/IDGT/GRAT/dynasty-trust tier). But {user} may also ask HOW to actually do something: what a "
    "revocable vs. irrevocable trust means in plain terms, whether they need a will, a trust, or both, what "
    "funding a trust actually involves (retitling deeds/accounts, what stays on beneficiary designations "
    "instead), the difference between a durable financial POA and a healthcare directive, roughly what "
    "probate involves and how long it takes, Texas-specific shortcuts (independent administration, muniment "
    "of title, small estate affidavit), and honest DIY-vs-attorney tradeoffs. search_knowledge over the "
    "estate-planning-mechanics reference doc for this — it's the foundational layer beneath the tax-strategy "
    "one. Always frame this as preparation for a licensed estate-planning attorney (what to ask for, what to "
    "expect), never as a substitute for drafting the actual documents.\n"
    "Security/compliance questions about the APP ITSELF (not {user}'s investments) — 'what happens if "
    "there's a breach', 'what vulnerabilities are known and accepted', 'how long is my data kept', 'is "
    "this ISO 27001 compliant', 'what's the incident response plan' — are answered from "
    "list_compliance_docs / get_compliance_doc (the Risk Register, Statement of Applicability, IR "
    "Runbook, Privacy Notice, Retention Schedule, ROPA, DPIA), NOT search_knowledge (that's external "
    "tax/investing/estate reference material, a different corpus entirely). Cite the specific document.\n"
    "ARITHMETIC: never compute loan payments, payoff timelines, interest saved, mortgage total cost, "
    "future/present values, implied rates, DRIP projections, rent-vs-buy, or NPV/IRR in your head — call "
    "run_calculator and quote its figures (it is the same engine as the CALCULATORS tab, so you can point "
    "{user} there to explore further). For 'what if I paid $X extra on <my debt>', call get_liabilities "
    "first for the real balance/APR/payment; if a fact is missing, ask for it rather than assuming one.\n"
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
    {"type": "function", "function": {"name": "get_benefits",
        "description": "401(k) employer match and headroom, HSA contribution room, Roth IRA eligibility, "
                       "and mega-backdoor Roth capacity — from the user's actual plan/salary configuration and "
                       "the 2026 IRS contribution limits. Use this for any 401k/HSA/Roth contribution-room or "
                       "match-optimization question (RESUME-TRACKER 2026-07-19: this data existed only on a "
                       "plain REST endpoint before, invisible to chat).",
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
                       "IRS/SEC/Vanguard tax & investing reference docs, plus estate-planning reference docs) "
                       "for relevant passages. Use this for tax rules, IRA/Roth mechanics, investing-principle "
                       "/ policy guidance, AND estate-planning questions — both the advanced tax-strategy tier "
                       "(SLAT/IDGT/GRAT, basis/step-up, GST) and the foundational mechanics tier (revocable "
                       "vs. irrevocable trusts, funding a trust, will-vs-trust, durable POA vs. healthcare "
                       "directive, probate basics, DIY-vs-attorney). Returns the top passages with their "
                       "source document — cite the source and verify against current official guidance.",
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
    # 2026-07-27, Jason's call: chat previously had NO way to answer questions about the app's OWN
    # security/GRC program (Risk Register, incident response, ISO 27001 control status, privacy
    # program) — search_knowledge only covers external tax/investing/estate reference docs, a
    # completely separate corpus from compliance/. These two tools close that gap.
    {"type": "function", "function": {"name": "list_compliance_docs",
        "description": "List the available security/compliance (GRC) documents: Risk Register, "
                       "Statement of Applicability (ISO 27001 Annex A control status), Incident "
                       "Response Runbook, Privacy Notice, Data Retention Schedule, ROPA, DPIA. Use "
                       "this when {user} asks what security/compliance documentation exists, then "
                       "call get_compliance_doc for the one that's actually relevant.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_compliance_doc",
        "description": "Full text of one named security/compliance document (see "
                       "list_compliance_docs for valid names). Use this for questions about the "
                       "app's own security posture, known/risk-accepted vulnerabilities, incident "
                       "response process, data retention, or privacy program — e.g. 'what happens "
                       "if there's a breach', 'what CVEs are currently accepted', 'how long is my "
                       "data kept', 'is this ISO 27001 compliant'. This describes the APP's own "
                       "security program, not investing/tax/estate guidance (use search_knowledge "
                       "for that instead).",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Exact filename from list_compliance_docs, "
                                                        "e.g. \"RISK-REGISTER.md\"."}},
            "required": ["name"]}}},
    # CALCULATORS (2026-10-19): deterministic financial math, so the model never does
    # amortization/TVM arithmetic in its head. Same engine as the CALCULATORS tab.
    {"type": "function", "function": {"name": "run_calculator",
        "description": "Run one of the app's deterministic financial calculators and get exact "
                       "figures — ALWAYS use this instead of doing loan/mortgage/savings/return math "
                       "yourself. Rates are annual % (6.5 = 6.5%). calculator + its main inputs:\n"
                       "mortgage: price, down_payment|down_pct, apr, term_years, property_tax_pct|"
                       "property_tax_annual, insurance_annual, pmi_pct, hoa_monthly, maintenance_pct, "
                       "closing_costs, extra, appreciation_pct, horizon_years (true total cost of ownership)\n"
                       "loan: amount (or price/down_payment/trade_in/sales_tax_pct), apr, term_months|"
                       "term_years, extra, origination_fee\n"
                       "land_loan: price, down_pct, apr, amort_years, balloon_years, io_years, land_type "
                       "(raw|unimproved|improved), property_tax_annual\n"
                       "drip: initial_investment, share_price, dividend_per_share (annual $/share; preferred over "
                       "dividend_yield_pct, which is only a fallback), price_growth_pct, "
                       "dividend_growth_pct, years, monthly_contribution, frequency, tax_rate_pct\n"
                       "tvm: give 4 of n, rate, pv, pmt, fv (omit the one to solve; signs: received +, "
                       "paid −), periods_per_year, timing (end|begin)\n"
                       "whatif: balance, apr, payment, extra, lump_sum, target_months\n"
                       "rent_vs_buy: price, apr, rent_monthly, horizon_years, appreciation_pct, "
                       "invest_return_pct\n"
                       "affordability: annual_income, monthly_debts, down_payment, apr, property_tax_pct, "
                       "insurance_annual\n"
                       "points: loan_amount, apr_base, apr_points, points, hold_years\n"
                       "yields: fed_rate_pct, state_rate_pct, taxable_yield, treasury_yield, "
                       "muni_instate_yield, muni_outstate_yield, apr_pct|apy_pct\n"
                       "inflation: amount, years, inflation_pct, nominal_return_pct\n"
                       "lump_vs_annuity: lump_sum, payment, years, cola_pct, discount_rate_pct\n"
                       "npv_irr: initial_investment, cash_flows (comma list), discount_rate_pct",
        "parameters": {"type": "object", "properties": {
            "calculator": {"type": "string", "enum": list(calculators.CALCULATORS)},
            "inputs": {"type": "object", "description": "Field: value pairs for that calculator."}},
            "required": ["calculator", "inputs"]}}},
    {"type": "function", "function": {"name": "get_liabilities",
        "description": "The user's REAL debts (including mortgages) with balance, APR and payment, and "
                       "which of those facts are missing. Call this before a what-if payoff question "
                       "about one of their actual debts, then pass the figures to run_calculator "
                       "(calculator='whatif').",
        "parameters": {"type": "object", "properties": {}}}},
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
        if name == "get_benefits":        return get_benefits()
        if name == "get_lookthrough":     return get_lookthrough()
        if name == "get_scorecard":       return get_scorecard()
        if name == "get_opportunities":   return _opportunities()
        if name == "get_discovery":
            try:
                return store.read_json_strict(DISCOVERY_FILE)
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
        if name == "list_compliance_docs":     return list_compliance_docs()
        if name == "get_compliance_doc":       return _compliance_doc_data(str(args.get("name") or ""))
        if name == "run_calculator":
            # Schedules can be hundreds of rows; the model needs the summary, not every period.
            return calculators.run(args.get("calculator"), args.get("inputs") or {},
                                   include_schedule=False)
        if name == "get_liabilities":          return {"liabilities": _whatif_liabilities()}
        return {"error": f"unknown tool {name}"}
    except Exception as e:
        return {"error": f"{name} failed: {e}"}


# ---- Strategist persona for chat: advisory, conversational, no trading ----
STRATEGIST_CHAT_PROMPT = (
    "You are the Strategist in conversational ADVISORY mode. You discuss ideas, opportunities and "
    "portfolio moves, but you place NO trades and record nothing — no buy/sell/report tools exist here. "
    "There are TWO separate books — do not conflate them: get_holdings is {user}'s REAL portfolio (their "
    "actual money, actual positions, actual cost basis) and get_paper_portfolio is YOUR OWN separate "
    "hypothetical simulation book that only tracks the paper trades you have logged — it does NOT reflect "
    "what {user} actually owns or how much. Whenever {user} asks about a position they hold, adding to an "
    "existing holding, sizing a real purchase, or 'my' portfolio/holding, that means the REAL book: call "
    "get_holdings FIRST (not get_paper_portfolio) to see their actual size, cost basis and weight in that "
    "name before saying anything about it. Only reach for get_paper_portfolio when {user} is explicitly "
    "asking about your paper-trading track record or hypothetical strategy performance.\n"
    "Also available: get_fees_liquidity for their REAL fund expense ratios/liquidity flags and get_benefits "
    "for their REAL 401k/HSA/Roth contribution room (use these instead of asking {user} to type figures the "
    "app already tracks), get_opportunities and get_discovery for ideas, get_fundamentals / get_valuation "
    "/ get_research to dig into a name, and web_search(query) for LIVE web/news via Tavily — use it to "
    "uncover fresh opportunities and to answer 'what's driving this stock right now / why is it up or "
    "down today' questions. Prefer trailing-twelve-month figures; weigh fees and liquidity on small "
    "orders using get_fees_liquidity's real numbers, not a guess. Give specific, reasoned takes and "
    "acknowledge uncertainty.\n"
    "TOOL HONESTY: your capabilities are exactly the tools listed above. If asked to do something no "
    "tool supports, say so plainly in ONE turn and offer the closest thing you CAN do — do NOT loop or "
    "deliberate at length about a capability you lack. Hypothetical and educational — NOT financial advice."
)

PERSONAS = {
    "fiduciary": {"prompt": FIDUCIARY_PROMPT, "tools": FIDUCIARY_TOOLS,
                  "dispatch": _fiduciary_dispatch, "model": _text_model(), "temp": 0.3},
    "strategist": {"prompt": STRATEGIST_CHAT_PROMPT, "tools": STRATEGIST_ADVICE_TOOLS,
                   "dispatch": _advice_dispatch, "model": _text_model(), "temp": 0.4},
}

# --- Customizable persona prompts + user reference (2026-07-22) ----------------------------------
# The two chat persona prompts above are DEFAULTS. A user can (a) set how the assistants refer to
# them (the `{user}` token — default "the user", e.g. set to their name) and (b) override either
# system prompt entirely, with a one-click reset back to the default. Overrides live in a gitignored
# JSON store (personal / may embed a name), resolved at request time so no restart is needed.
PROMPTS_FILE = os.environ.get("PROMPTS_FILE", "/data/prompts.json")
DEFAULT_USER_REF = "the user"
DEFAULT_PERSONA_PROMPTS = {"fiduciary": FIDUCIARY_PROMPT, "strategist": STRATEGIST_CHAT_PROMPT}
PERSONA_LABELS = {"fiduciary": "Fiduciary", "strategist": "Strategist"}

def _read_prompts_store():
    try:
        d = store.read_json_strict(PROMPTS_FILE) or {}
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    if not isinstance(d.get("prompts"), dict):
        d["prompts"] = {}
    return d

def _write_prompts_store(d):
    store.write_json(PROMPTS_FILE, d)   # store.py's DB is chmod 0600 at init — this may embed
                                         # the user's name, kept private the same as before

def _user_ref():
    v = (_read_prompts_store().get("user_ref") or "").strip()
    return v or DEFAULT_USER_REF

def _resolve_persona_prompt(key):
    """The system prompt actually sent for persona `key`: the user's override if set, else the coded
    default, with the `{user}` token filled in from the configured user reference."""
    pd = _read_prompts_store()
    tmpl = pd["prompts"].get(key) or DEFAULT_PERSONA_PROMPTS.get(key, "")
    return tmpl.replace("{user}", _user_ref())


@app.get("/api/prompts")
def get_prompts():
    """Current persona-prompt configuration for the SETTINGS editor: the user reference plus, per
    persona, the coded default, the effective current text (override or default), whether it's
    customized, and a preview with `{user}` already substituted."""
    pd = _read_prompts_store()
    overrides = pd["prompts"]
    ref = _user_ref()
    personas = {}
    for key, default in DEFAULT_PERSONA_PROMPTS.items():
        cur = overrides.get(key) or default
        personas[key] = {
            "label": PERSONA_LABELS.get(key, key.capitalize()),
            "default": default,
            "current": cur,
            "is_custom": bool(overrides.get(key)),
            "resolved_preview": cur.replace("{user}", ref),
        }
    return {"user_ref": ref, "user_ref_default": DEFAULT_USER_REF,
            "user_ref_is_custom": bool((pd.get("user_ref") or "").strip()),
            "token": "{user}", "personas": personas}


@app.post("/api/prompts")
async def post_prompts(req: Request):
    """Update the user reference and/or a persona's system-prompt override.
    Body: {user_ref?: str, persona?: 'fiduciary'|'strategist', prompt?: str, reset?: bool}.
    reset=true (or an empty prompt) clears that persona's override back to the coded default;
    an empty user_ref resets the reference to the default. Applies immediately (no restart)."""
    try:
        body = await req.json()
    except Exception:
        body = {}
    # Independent read for the audit diff (see /api/schedule's comment above for why — this
    # handler also mutates the nested "prompts" dict in place via .pop(), so it needs a fully
    # separate read rather than a shallow copy of `pd`).
    old = _read_prompts_store()
    pd = _read_prompts_store()
    if "user_ref" in body:
        pd["user_ref"] = str(body.get("user_ref") or "").strip()
    persona = body.get("persona")
    if persona in DEFAULT_PERSONA_PROMPTS:
        if body.get("reset"):
            pd["prompts"].pop(persona, None)
        elif "prompt" in body:
            txt = str(body.get("prompt") or "").strip()
            if txt and txt != DEFAULT_PERSONA_PROMPTS[persona]:
                pd["prompts"][persona] = txt
            else:
                pd["prompts"].pop(persona, None)   # identical to default or blank → treat as reset
    try:
        _write_prompts_store(pd)
    except Exception as e:
        return JSONResponse({"error": f"could not save prompts: {e}"}, status_code=500)
    # Logged in FULL (not summarized) — a persona system-prompt override changes AI behavior, and
    # being able to see exactly what changed is the point of auditing this endpoint (AI-governance/
    # model-risk angle, not just generic config hygiene).
    _emit_config_changed(req, "/api/prompts", old, pd)
    return get_prompts()


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


def _vision_identify_tickers(images, user_msg):
    """RESUME-TRACKER 2026-07-18j: a single non-streaming multimodal call against VISION_MODEL
    (a distinct small vision-capable model — ANALYSIS_MODEL/persona models are text-only) that
    reads a pasted screenshot and extracts ticker symbols. This function's ONLY job is OCR-style
    ticker extraction, not analysis — the actual RESEARCH still goes through the persona's own
    existing tool-calling loop (get_fundamentals/get_news/etc, already implemented in _chat_stream)
    once tickers are identified. That's the reuse win: this is a new front door onto machinery
    that already exists, not a second research pipeline.
    `images` is a list of base64-encoded image strings (data-URL prefix, if any, stripped by the
    caller). Returns {"tickers": [...], "note": str, "error": bool}, or None if `images` is falsy.
    Best-effort: any Ollama/parse failure degrades to an empty-tickers result with the failure
    reason in `note`, never raises — a bad screenshot shouldn't break the chat turn."""
    if not images:
        return None
    prompt = (
        "Look at the attached image(s) carefully. Identify any stock/fund ticker symbols visible "
        "(e.g. in a screenshot of a brokerage app, watchlist, portfolio, or news headline). "
        "Reply with ONLY a JSON object, no other text: "
        '{"tickers": ["AAPL", ...], "note": "short description of what you saw"}. '
        "If no tickers are clearly identifiable, return an empty tickers list and use `note` to "
        "describe what you saw instead so the user can clarify."
    )
    try:
        res = llm._llm_chat([{"role": "user", "content": prompt}], VISION_MODEL, images=images,
                            temperature=0.1, keep_alive=LLM_KEEP_ALIVE, timeout=VISION_LLM_TIMEOUT)
        if not res.ok:
            return {"tickers": [], "note": f"vision model error {res.status_code}: {res.error}", "error": True}
        content = res.content
        parsed = _parse_json_obj(content) or {}
        # `t and str(t).strip()` — checking truthiness of `t` FIRST matters: a JSON `null` in the
        # list parses to Python None, and str(None)=="None" is non-empty, so filtering on the
        # stringified form alone (a bug caught by a reconstructed-function test during this build)
        # would let a null slip through as the literal ticker "NONE".
        tickers = [str(t).upper().strip() for t in (parsed.get("tickers") or []) if t and str(t).strip()][:10]
        return {"tickers": tickers, "note": (parsed.get("note") or "").strip(), "error": False}
    except Exception as e:
        return {"tickers": [], "note": f"vision call failed: {e}", "error": True}


# Cooperative cancellation for /api/chat: keyed by a client-generated req_id so the Stop button
# can reach the SPECIFIC in-flight generation. Client-side abort() only closes the browser's own
# fetch — it does nothing to the server, which used to keep running to completion in the background
# (still holding _strat_lock / the GPU) with no visibility. Registered only while a generation is
# actually in flight; always popped in _chat_stream's finally.
_CHAT_CANCEL = {}
_CHAT_CANCEL_LOCK = threading.Lock()


def _chat_stream(persona_key, session_id, user_msg, req_id=None, images=None):
    """SSE generator: streams assistant tokens and tool events for one user message, persisting the
    turn to SQLite. Holds _strat_lock for the duration so a chat never competes with the weekly run
    or an advisory run for the GPUs. Cancellable via req_id + POST /api/chat/cancel: checked while
    queued for _strat_lock, while queued for _GPU (RESUME-TRACKER 2026-07-18l bug #2 — a job-queue
    run holding _GPU used to block here with zero visibility; now yields a `status` SSE event naming
    the job + ETA, same as the _strat_lock wait), and on every streamed line/tool-call, so Stop
    actually frees the GPU right away instead of letting the generation finish unattended.
    `images` (RESUME-TRACKER 2026-07-18j), if given, runs a VISION_MODEL pre-pass to identify
    ticker symbols in the attached screenshot(s) before the normal turn — see
    _vision_identify_tickers's docstring for why this is a pre-pass, not a separate pipeline."""
    persona = PERSONAS.get(persona_key)
    if not persona:
        yield _sse("error", {"error": f"unknown persona '{persona_key}'"})
        return
    cancel_event = threading.Event()
    if req_id:
        with _CHAT_CANCEL_LOCK:
            _CHAT_CANCEL[req_id] = cancel_event

    def _forget():
        if req_id:
            with _CHAT_CANCEL_LOCK:
                _CHAT_CANCEL.pop(req_id, None)

    if not _strat_lock.acquire(blocking=False):
        # Queue behind the running strategist/advisory job instead of forcing a retry.
        # Single-flight on the GPU by design — DON'T parallelize (two big generations would
        # contend on the Pascal cards). Tell the client we're waiting, then poll for the lock
        # (instead of one long blocking acquire) so a Stop while queued takes effect immediately
        # rather than after the full CHAT_QUEUE_TIMEOUT.
        yield _sse("status", {"state": "waiting",
                              "message": "A strategist/advisory run is using the model — your message is "
                                         "queued and will start automatically when it finishes."})
        acquired = False
        deadline = time.time() + CHAT_QUEUE_TIMEOUT
        while time.time() < deadline:
            if cancel_event.is_set():
                yield _sse("cancelled", {"reason": "stopped while queued"})
                _forget()
                return
            if _strat_lock.acquire(timeout=1):
                acquired = True
                break
        if not acquired:
            yield _sse("error", {"error": "still busy after waiting a while — please try again shortly"})
            _forget()
            return
    try:
        # Image pre-pass (RESUME-TRACKER 2026-07-18j): identify tickers via VISION_MODEL BEFORE
        # the normal turn, under the same visible/cancellable GPU-wait treatment bug #2 added
        # above (a vision call is still a GPU op and must serialize with everything else, and a
        # job-queue run holding _GPU shouldn't leave THIS wait silent either). The note gets
        # appended only to what the LLM SEES this turn (below, after `messages` is built) — the
        # chat history stored to SQLite keeps the user's original message text, so re-opening this
        # conversation later doesn't show a synthetic "[Image analysis...]" tag baked in.
        vision_note = None
        if images:
            yield _sse("status", {"state": "vision", "message": "reading the attached image…"})
            if _GPU.locked():
                yield _sse("status", {"state": "waiting", "message": _gpu_wait_message()})
            v_deadline = time.time() + CHAT_QUEUE_TIMEOUT
            if not _gpu_acquire_visible(cancel_event, v_deadline):
                if cancel_event.is_set():
                    yield _sse("cancelled", {"reason": "stopped while queued for image analysis"})
                else:
                    yield _sse("error", {"error": "GPU still busy after waiting a while — please try again shortly"})
                return
            try:
                vres = _vision_identify_tickers(images, user_msg)
            finally:
                _gpu_release_visible()
            if vres and vres.get("tickers"):
                vision_note = (f"[Image analysis identified possible ticker(s): {', '.join(vres['tickers'])}."
                                + (f" {vres['note']}" if vres.get("note") else "")
                                + " Use your research tools to look these up before answering.]")
            elif vres:
                vision_note = f"[Image analysis: {vres.get('note') or 'no tickers clearly identified in the image.'}]"
            yield _sse("vision", {"tickers": (vres or {}).get("tickers") or [], "note": (vres or {}).get("note"),
                                  "error": bool((vres or {}).get("error"))})

        if not session_id:
            title = (user_msg or "New chat").strip().replace("\n", " ")[:60] or "New chat"
            session_id = _chat_create_session(persona_key, title)
        _chat_add_message(session_id, "user", user_msg)
        yield _sse("session", {"session_id": session_id, "persona": persona_key})

        history = _chat_messages(session_id)[-CHAT_MAX_HISTORY:]
        # Resolve the system prompt at request time: the user's override (if any) with the {user}
        # reference substituted, falling back to the coded default. See _resolve_persona_prompt.
        messages = [{"role": "system", "content": _sys(_resolve_persona_prompt(persona_key))}]
        for m in history:
            if m["role"] in ("user", "assistant"):
                messages.append({"role": m["role"], "content": m["content"] or ""})
        if vision_note and messages and messages[-1]["role"] == "user":
            messages[-1]["content"] = (messages[-1]["content"] or "") + "\n\n" + vision_note

        final_content = ""
        cancelled = False
        content_buf = ""
        for i in range(CHAT_MAX_ITER):
            content_buf = ""
            tool_calls = []
            # Serialize on the GPU — but make the WAIT itself visible + cancellable (RESUME-TRACKER
            # 2026-07-18l bug #2): a job-queue run (analysis/discovery/narrative/etc, some observed
            # at 19-29+ minutes) can hold _GPU here, and _gpu_slot()'s plain blocking acquire() used
            # to sit inside this generator with ZERO SSE events for the whole remaining duration —
            # indistinguishable from a dead connection to the client. Same wait-then-poll pattern as
            # the _strat_lock queue above, so Stop works while queued for the GPU too, not just for
            # _strat_lock.
            if _GPU.locked():
                yield _sse("status", {"state": "waiting", "message": _gpu_wait_message()})
            gpu_deadline = time.time() + CHAT_QUEUE_TIMEOUT
            if not _gpu_acquire_visible(cancel_event, gpu_deadline):
                if cancel_event.is_set():
                    cancelled = True
                    break
                yield _sse("error", {"error": "GPU still busy after waiting a while — please try again shortly"})
                return
            try:
                for chunk in llm._llm_chat(messages, persona["model"], tools=persona["tools"],
                                           stream=True, temperature=persona.get("temp", 0.3),
                                           num_ctx=STRAT_NUM_CTX, keep_alive=LLM_KEEP_ALIVE,
                                           timeout=STRAT_LLM_TIMEOUT, cancel_check=cancel_event.is_set):
                    if chunk.get("error"):
                        yield _sse("error", {"error": chunk["error"]})
                        return
                    if chunk.get("cancelled"):
                        cancelled = True
                        break
                    if chunk.get("thinking"):     # qwen3 reasoning phase — stream as an activity indicator
                        yield _sse("thinking", {"t": chunk["thinking"]})
                    if chunk.get("content"):
                        content_buf += chunk["content"]
                        yield _sse("token", {"t": chunk["content"]})
                    if chunk.get("tool_calls"):
                        tool_calls.extend(chunk["tool_calls"])
                    if chunk.get("done"):
                        break
            except Exception as e:
                yield _sse("error", {"error": f"generation failed: {e}"})
                return
            finally:
                _gpu_release_visible()

            if cancelled:
                break

            asst = {"role": "assistant", "content": content_buf}
            if tool_calls:
                asst["tool_calls"] = tool_calls
            messages.append(asst)

            if not tool_calls:
                final_content = content_buf
                break

            for tc in tool_calls:
                if cancel_event.is_set():
                    cancelled = True
                    break
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
            if cancelled:
                break
        else:
            final_content = final_content or "(reached the step limit before finishing — ask me to continue.)"

        if cancelled:
            partial = (content_buf or "").strip()
            if partial:
                _chat_add_message(session_id, "assistant", partial + "\n\n_(stopped)_")
                _chat_touch_session(session_id)
            yield _sse("cancelled", {"session_id": session_id})
            return

        _chat_add_message(session_id, "assistant", final_content)
        _chat_touch_session(session_id)
        yield _sse("done", {"session_id": session_id})
    finally:
        _strat_lock.release()
        _forget()


CHAT_IMAGE_MAX = int(os.environ.get("CHAT_IMAGE_MAX", "3"))            # cap images per message
CHAT_IMAGE_MAX_BYTES = int(os.environ.get("CHAT_IMAGE_MAX_BYTES", "8000000"))  # ~8MB base64/image, before Ollama sees it


def _sanitize_chat_images(raw):
    """Validate + normalize the optional `images` field (RESUME-TRACKER 2026-07-18j): accepts a
    list of base64 strings, stripping a `data:image/...;base64,` prefix if the browser's
    FileReader/clipboard API included one (Ollama's multimodal `images` field wants raw base64,
    no data-URL wrapper). Caps count and per-image size so a pasted image can't balloon the
    request or the eventual Ollama call unbounded. Silently drops anything that isn't a
    non-empty string (before OR after prefix-stripping — an empty `data:image/jpeg;base64,` with
    nothing after the comma must not become an empty-string "image" sent to Ollama) rather than
    erroring the whole chat turn over one bad image.
    IMPORTANT: filters junk BEFORE capping to CHAT_IMAGE_MAX, not after — capping the raw list
    first would let a few leading junk entries (empty strings, wrong types) silently push a real
    trailing image out of the slice even though far fewer than CHAT_IMAGE_MAX valid images were
    actually sent (caught by a reconstructed-function test during this build)."""
    if not raw or not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            continue
        s = item.strip()
        if s.startswith("data:"):
            comma = s.find(",")
            if comma != -1:
                s = s[comma + 1:]
        if not s or len(s) > CHAT_IMAGE_MAX_BYTES:
            continue
        out.append(s)
        if len(out) >= CHAT_IMAGE_MAX:
            break
    return out


# --- Resumable chat generation (2026-07-22) -----------------------------------------------------
# Previously /api/chat was a single StreamingResponse tied 1:1 to one HTTP connection: the open
# reader, the partial tokens, and the "thinking" state lived only in that page load's JS + that one
# request. A refresh or navigation destroyed the display, and (because generation ran inside the
# request generator) could tear down the generation itself. Fix, mirroring the job-queue pattern
# already proven for narrative/strategist/reindex: run _chat_stream in a BACKGROUND THREAD that
# appends every SSE frame it yields to an in-memory buffer keyed by session_id; both the initial
# POST and a resume GET (/api/chat/stream) simply TAIL that buffer. So generation no longer depends
# on any single connection — a reload reconnects from the buffer and continues live, and the final
# message still persists to SQLite exactly as before. Buffers linger briefly after completion so a
# late reconnect can still replay the finished answer, then are reaped.
_CHAT_ACTIVE = {}                      # session_id -> _ChatGen (most-recent generation for that session)
_CHAT_ACTIVE_LOCK = threading.Lock()
CHAT_BUFFER_LINGER = int(os.environ.get("CHAT_BUFFER_LINGER", "180"))   # keep a finished buffer this long (s) for late reconnects


class _ChatGen:
    """Holds the live SSE-frame buffer for one background chat generation. tail(cursor) yields
    frames from `cursor` onward, blocking (with a heartbeat) until new frames arrive or the
    generation completes — usable by many concurrent readers (the original POST + any reconnects)."""
    def __init__(self, session_id):
        self.session_id = session_id
        self.chunks = []               # raw SSE strings exactly as _chat_stream yielded them
        self.done = False
        self.finished_at = None
        self.cond = threading.Condition()

    def emit(self, s):
        with self.cond:
            self.chunks.append(s)
            self.cond.notify_all()

    def finish(self):
        with self.cond:
            self.done = True
            self.finished_at = time.time()
            self.cond.notify_all()

    def tail(self, cursor):
        cursor = max(0, int(cursor or 0))
        while True:
            batch = []
            with self.cond:
                if cursor >= len(self.chunks) and not self.done:
                    self.cond.wait(timeout=15)   # wake on new frame, completion, or heartbeat tick
                while cursor < len(self.chunks):
                    batch.append(self.chunks[cursor]); cursor += 1
                done_now = self.done and cursor >= len(self.chunks)
            for s in batch:
                yield s
            if not batch and not done_now:
                yield ": ping\n\n"               # SSE comment heartbeat — keeps idle proxies/clients from closing
            if done_now:
                return


def _chat_run_bg(gen, persona, session_id, message, req_id, images):
    """Drive _chat_stream to completion in a background thread, buffering every yielded SSE frame.
    Generation lifetime is now independent of any client connection."""
    try:
        for chunk in _chat_stream(persona, session_id, message, req_id, images=images):
            gen.emit(chunk)
    except Exception as e:
        try:
            gen.emit(_sse("error", {"error": f"generation failed: {e}"}))
        except Exception:
            pass
    finally:
        gen.finish()

        def _reap():
            time.sleep(CHAT_BUFFER_LINGER)
            with _CHAT_ACTIVE_LOCK:
                cur = _CHAT_ACTIVE.get(session_id)
                if cur is gen and cur.done:
                    _CHAT_ACTIVE.pop(session_id, None)
        threading.Thread(target=_reap, daemon=True).start()


@app.post("/api/chat")
async def chat(req: Request):
    """Streaming multi-turn chat with a persona (fiduciary|strategist).
    Body: {persona, message, session_id?, req_id?, images?}. req_id is a client-generated token
    the caller can later pass to POST /api/chat/cancel to actually stop this generation
    server-side (not just close the browser stream) — pass one if you want Stop to work.
    `images` (RESUME-TRACKER 2026-07-18j), if given, is a list of up to CHAT_IMAGE_MAX base64
    image strings (data-URL prefix optional) — e.g. a pasted screenshot of a watchlist. Message
    may be empty when images are attached (defaults to a generic "identify tickers" prompt).
    Generation runs in a background thread (see _chat_run_bg) so it survives a client refresh /
    navigation; reconnect with GET /api/chat/stream?session_id=…&cursor=N.
    Returns text/event-stream: events session, status, vision, token, thinking, tool, tool_done,
    done, cancelled, error. READ-ONLY."""
    try:
        body = await req.json()
    except Exception:
        body = {}
    persona = str((body or {}).get("persona") or "fiduciary").strip()
    session_id = (body or {}).get("session_id")
    message = str((body or {}).get("message") or "").strip()
    req_id = str((body or {}).get("req_id") or "").strip() or None
    images = _sanitize_chat_images((body or {}).get("images"))
    if not message and images:
        message = "Identify any ticker symbols in the attached image and research them."
    if not message:
        return JSONResponse({"error": "empty message"}, status_code=400)
    # Resolve the session id up front so the buffer is keyed by a real id the client can reconnect
    # to (previously _chat_stream created it internally, invisible until the first frame arrived).
    if not session_id:
        title = (message or "New chat").strip().replace("\n", " ")[:60] or "New chat"
        session_id = _chat_create_session(persona, title)
    gen = _ChatGen(session_id)
    with _CHAT_ACTIVE_LOCK:
        _CHAT_ACTIVE[session_id] = gen   # a new send supersedes any prior buffer for this session
    threading.Thread(target=_chat_run_bg, args=(gen, persona, session_id, message, req_id, images),
                     daemon=True).start()
    return StreamingResponse(gen.tail(0), media_type="text/event-stream")


@app.get("/api/chat/stream")
def chat_stream_resume(session_id: str, cursor: int = 0):
    """Reconnect to an in-progress (or just-finished, within CHAT_BUFFER_LINGER) generation and tail
    its buffer from `cursor`. If nothing is buffered for this session, emits a single `idle` event so
    the client knows to fall back to loading the saved transcript instead."""
    with _CHAT_ACTIVE_LOCK:
        gen = _CHAT_ACTIVE.get(session_id)
    if not gen:
        return StreamingResponse(iter([_sse("idle", {"session_id": session_id})]),
                                  media_type="text/event-stream")
    return StreamingResponse(gen.tail(cursor), media_type="text/event-stream")


@app.get("/api/chat/active")
def chat_active(session_id: str = ""):
    """Which chat sessions currently have a live (or lingering-but-recent) background generation.
    With ?session_id=… returns {active:bool} for that one; otherwise the list of active session ids —
    used by the UI to mark generating conversations and offer to reconnect after a reload."""
    with _CHAT_ACTIVE_LOCK:
        if session_id:
            g = _CHAT_ACTIVE.get(session_id)
            return {"session_id": session_id, "active": bool(g and not g.done)}
        return {"active_sessions": [sid for sid, g in _CHAT_ACTIVE.items() if not g.done]}


@app.post("/api/chat/cancel")
async def chat_cancel(req: Request):
    """Best-effort cancel of an in-flight /api/chat generation by its req_id. Sets a threading.Event
    that _chat_stream polls at every queue-wait tick and every streamed line/tool-call — the
    generation stops within ~1s and releases _strat_lock immediately, rather than continuing to run
    (and hold the GPU) after the caller has walked away. Unknown/already-finished req_id is a no-op."""
    try:
        body = await req.json()
    except Exception:
        body = {}
    req_id = str((body or {}).get("req_id") or "").strip()
    with _CHAT_CANCEL_LOCK:
        ev = _CHAT_CANCEL.get(req_id)
    if ev:
        ev.set()
        return {"ok": True}
    return {"ok": False, "note": "unknown or already-finished req_id"}


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
            "finnhub": bool(FINNHUB), "analysis_model": ANALYSIS_MODEL,
            "llm_dialect": llm._dialect(), "llm_model": _text_model(),
            "llm_strategist_model": _text_model(strategist=True), "embed_dialect": llm._embed_dialect()}


@app.get("/")
def index():
    """Serves the dashboard. 2026-07-24: no longer injects an ANALYSIS_TOKEN into the page --
    that whole mechanism (a shared secret handed to the client by the very page it was meant to
    protect) was retired as redundant now that the session auth gate above covers every route,
    including the /api/run-*/knowledge-reindex endpoints this used to patch around. See
    RESUME-TRACKER.md for the retirement writeup."""
    path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    with open(path, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/manifest.json")
def manifest():
    """PWA manifest (2026-07-26 notifications work) -- lets a browser offer "Add to Home Screen" /
    "Install". Required on iOS specifically: the Push API is only available to a Home-Screen-
    installed web app there (Safari 16.4+), never to an ordinary open tab."""
    path = os.path.join(os.path.dirname(__file__), "static", "manifest.json")
    with open(path, encoding="utf-8") as f:
        return Response(content=f.read(), media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    """Service worker (2026-07-26 notifications work) -- served from the ROOT path (not
    /static/sw.js) so its default registration scope covers the whole origin, not just a
    subfolder. Cache-Control: no-cache so an update to this file is picked up on the next visit
    rather than being stuck behind a stale cached copy indefinitely."""
    path = os.path.join(os.path.dirname(__file__), "static", "sw.js")
    with open(path, encoding="utf-8") as f:
        return Response(content=f.read(), media_type="application/javascript",
                         headers={"Cache-Control": "no-cache"})
