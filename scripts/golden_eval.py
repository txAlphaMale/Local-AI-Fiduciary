#!/usr/bin/env python3
"""
golden_eval.py — P0-2 golden regression set: catch capability regressions BEFORE deploy.

~30 fixed questions whose ground truth is computed live from the portfolio-api's own
endpoints (so the answers are always true for THIS deployment, no hand-maintained
fixtures). Each question is asked of the model with the SAME JSON context the chat
personas see; the model must return a number, scored within a stated tolerance.

Run it on every model or prompt change (pairs with scripts/model_ab.py, which judges
open-ended quality; this one judges grounded numeric extraction — the failure mode that
silently corrupts advice):

    python3 scripts/golden_eval.py                     # incumbent model
    python3 scripts/golden_eval.py qwen3.6:27b qwen3-coder:30b
    PORTFOLIO_API_URL=http://localhost:8097 OLLAMA_BASE_URL=http://localhost:11434 \
        python3 scripts/golden_eval.py

Output: docs/golden_eval_results.md (PASS/FAIL/SKIP per question + accuracy per model).
SKIP = the endpoint data isn't populated on this deployment (not a model failure).
Quality bar: a model change that drops accuracy is a regression — do not deploy it.
"""
import os, sys, json, time, datetime, re
import requests

API = os.environ.get("PORTFOLIO_API_URL", "http://localhost:8097").rstrip("/")
OLLAMA = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
MODELS = sys.argv[1:] or [os.environ.get("ANALYSIS_MODEL", "qwen3.6:27b")]
NUM_CTX = int(os.environ.get("ANALYSIS_NUM_CTX", "16384"))
TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "600"))
OUT = os.path.join(os.path.dirname(__file__), "..", "docs", "golden_eval_results.md")
TOKEN = os.environ.get("ANALYSIS_TOKEN", "")


def get(path):
    try:
        r = requests.get(API + path, headers={"X-Analysis-Token": TOKEN} if TOKEN else {}, timeout=60)
        j = r.json()
        return j if isinstance(j, (dict, list)) and not (isinstance(j, dict) and j.get("error")) else None
    except Exception:
        return None


def dig(obj, *keys):
    """Nested get; returns None on any miss."""
    for k in keys:
        if obj is None:
            return None
        obj = obj.get(k) if isinstance(obj, dict) else None
    return obj


# ---- the golden set -------------------------------------------------------------------
# Each spec: (id, endpoint, question, truth_fn(json)->float|None, tolerance)
# tolerance: ("pct", x) = within x% relative; ("abs", x) = within x absolute.
def _top_pos_pct(exp):
    ps = (exp or {}).get("positions") or []
    ws = [p.get("weight_pct") or p.get("pct") for p in ps if (p.get("weight_pct") or p.get("pct"))]
    return max(ws) if ws else None


GOLDEN = [
    # -- plan / decumulation --
    ("funded_ratio", "/api/retirement", "What is the household's OVERALL funded ratio (the funded_ratio value)?",
     lambda j: dig(j, "funded_ratio", "funded_ratio"), ("pct", 1.0)),
    ("funded_essential", "/api/retirement", "What is the ESSENTIALS-ONLY funded ratio (the essential_funded_ratio value)?",
     lambda j: dig(j, "funded_ratio", "essential_funded_ratio"), ("pct", 1.0)),
    ("mc_static", "/api/retirement", "What is the Monte Carlo success probability of the STATIC withdrawal policy, in percent?",
     lambda j: dig(j, "monte_carlo", "success_pct"), ("abs", 1.0)),
    ("mc_riskbased", "/api/retirement", "What is the Monte Carlo success probability of the RISK-BASED guardrails policy, in percent?",
     lambda j: dig(j, "monte_carlo", "risk_based", "success_pct"), ("abs", 1.0)),
    ("ss_monthly", "/api/ss", "What is the primary earner's projected Social Security monthly benefit at the planned claim age, in dollars?",
     lambda j: dig(j, "self", "monthly_at_claim") or dig(j, "self", "monthly") or dig(j, "monthly_at_claim") or dig(j, "pia_monthly"), ("pct", 2.0)),
    # -- exposure / risk --
    ("top_position", "/api/exposure", "What percent of the portfolio is the single largest DIRECT position (top weight_pct)?",
     _top_pos_pct, ("abs", 0.6)),
    ("hhi_direct", "/api/exposure", "What is the portfolio's direct single-name HHI (concentration index)?",
     lambda j: dig(j, "concentration", "hhi"), ("pct", 3.0)),
    ("true_top", "/api/exposure", "After fund look-through, what percent is the TRUE largest single-name exposure?",
     lambda j: dig(j, "lookthrough_concentration", "top_position_pct"), ("abs", 0.6)),
    ("sharpe", "/api/risk-metrics", "What is the portfolio's realized Sharpe ratio?",
     lambda j: dig(j, "sharpe"), ("abs", 0.05)),
    ("var95", "/api/risk-suite", "What is the portfolio's 1-day 95% Value-at-Risk, in percent?",
     lambda j: dig(j, "var", "var_95_1d_pct") or dig(j, "var_table", "95.0", "1"), ("pct", 5.0)),
    ("cvar95", "/api/risk-suite", "What is the portfolio's 1-day 95% CVaR (expected shortfall), in percent?",
     lambda j: dig(j, "var", "cvar_95_1d_pct") or dig(j, "cvar_95_1d_pct"), ("pct", 5.0)),
    ("eff_bets", "/api/risk-suite", "How many effective independent bets does the portfolio hold?",
     lambda j: dig(j, "effective_bets") or dig(j, "diversification", "effective_bets"), ("abs", 0.3)),
    ("realized_vol", "/api/risk-metrics", "What is the portfolio's realized annualized volatility, in percent?",
     lambda j: dig(j, "annualized_volatility_pct"), ("pct", 4.0)),
    ("max_dd", "/api/risk-metrics", "What is the portfolio's maximum drawdown over the tracked period, in percent?",
     lambda j: dig(j, "max_drawdown_pct"), ("abs", 0.8)),
    # -- tax / roth / aca / estate --
    ("marginal_rate", "/api/tax", "What is the household's federal marginal ordinary-income tax rate, in percent (marginal_ordinary_rate_pct)?",
     lambda j: dig(j, "marginal_ordinary_rate_pct"), ("abs", 0.5)),
    ("ltcg_headroom", "/api/tax", "How many dollars of long-term gain-harvest headroom remain (the gain_harvest headroom)?",
     lambda j: dig(j, "gain_harvest", "headroom"), ("pct", 2.0)),
    ("roth_headroom", "/api/tax", "How many dollars of Roth-conversion headroom remain to the target bracket (roth_conversion_headroom)?",
     lambda j: dig(j, "roth_conversion_headroom", "headroom") or dig(j, "roth_conversion_headroom"), ("pct", 2.0)),
    ("roth_rung", "/api/roth", "What conversion amount does the multi-year Roth ladder recommend for the FIRST year, in dollars?",
     lambda j: (lambda L: L[0].get("convert") if L else None)(dig(j, "ladder", "years") or dig(j, "ladder") or []), ("pct", 2.0)),
    ("aca_cliff_room", "/api/aca", "How many dollars of MAGI headroom remain below the 400% FPL ACA cliff?",
     lambda j: dig(j, "cliff_headroom") or dig(j, "magi", "headroom_to_cliff"), ("pct", 2.0)),
    ("estate_headroom", "/api/estate", "How many dollars of federal estate-exemption headroom does the household have?",
     lambda j: dig(j, "snapshot", "headroom") or dig(j, "headroom"), ("pct", 2.0)),
    ("stepup_wiped", "/api/estate", "How many dollars of embedded gain would the §1014 step-up wipe today (taxable + real estate)?",
     lambda j: dig(j, "stepup", "total_gain_wiped") or dig(j, "stepup_ledger", "total_gain_wiped"), ("pct", 2.0)),
    # -- income / fees / leverage / avenues --
    ("annual_dividends", "/api/income-plan", "What is the portfolio's projected annual dividend income across all accounts, in dollars?",
     lambda j: dig(j, "annual_income_total") or dig(j, "total_annual_income"), ("pct", 2.0)),
    ("max_ltv", "/api/leverage", "What is the maximum prudent loan-to-value the ruin model recommends, in percent?",
     lambda j: dig(j, "max_prudent_ltv_pct") or dig(j, "ruin", "max_prudent_ltv_pct"), ("abs", 1.0)),
    ("top_avenue_ret", "/api/avenues", "What is the after-tax expected return of the top-ranked avenue for the next marginal dollar, in percent?",
     lambda j: (lambda L: L[0].get("after_tax_return_pct") if L else None)(dig(j, "ranked") or dig(j, "avenues") or []), ("abs", 0.5)),
    # -- ai self-knowledge --
    ("brier", "/api/scorecard", "What is the Brier score of the model's stated confidence on its directional calls?",
     lambda j: dig(j, "calibration", "brier"), ("abs", 0.02)),
    ("hit_rate", "/api/scorecard", "What percent of the model's directional calls were hits (Buys rose, Sells fell)?",
     lambda j: dig(j, "directional_hit_rate_pct"), ("abs", 1.0)),
    ("bt_hit_12m", "/api/backtest", "At the 12-month horizon, what percent of directional calls beat SPY (the backtest hit rate)?",
     lambda j: dig(j, "ratings", "by_horizon", "12m", "hit_rate_pct"), ("abs", 1.0)),
    ("bt_paper_excess", "/api/backtest", "What is the paper book's dollar-weighted excess return vs the SPY counterfactual, in percent?",
     lambda j: dig(j, "paper", "weighted_excess_pct"), ("abs", 0.5)),
    ("net_worth", "/api/holdings", "What is the household's total net worth, in dollars?",
     lambda j: dig(j, "net_worth") or dig(j, "summary", "net_worth"), ("pct", 1.0)),
    ("invested_total", "/api/holdings", "What is the total invested (securities) value across all accounts, in dollars?",
     lambda j: dig(j, "total_value") or dig(j, "summary", "total_value"), ("pct", 1.0)),
]

PROMPT = (
    "You are the Fiduciary analyst. Using ONLY the JSON data below, answer the question with a single "
    "number. Return STRICT JSON: {\"answer\": <number>, \"unit\": \"<unit>\"} and nothing else. "
    "Do not round beyond what the data shows; do not estimate from outside knowledge; if the value is "
    "present in the data, extract it faithfully.\n\nQUESTION: %s\n\nDATA:\n%s\n"
)


def ask(model, question, context):
    body = {"model": model, "stream": False,
            "options": {"num_ctx": NUM_CTX, "temperature": 0},
            "messages": [{"role": "user", "content": PROMPT % (question, json.dumps(context)[:60000])}]}
    t0 = time.time()
    r = requests.post(OLLAMA + "/api/chat", json=body, timeout=TIMEOUT)
    txt = (r.json().get("message") or {}).get("content") or ""
    m = re.search(r'\{[^{}]*"answer"[^{}]*\}', txt.replace("\n", " "))
    val = None
    if m:
        try:
            val = float(json.loads(m.group(0)).get("answer"))
        except Exception:
            val = None
    return val, round(time.time() - t0, 1), txt


def within(truth, got, tol):
    if truth is None or got is None:
        return False
    kind, x = tol
    if kind == "abs":
        return abs(got - truth) <= x
    return abs(got - truth) <= abs(truth) * x / 100.0 + 1e-9


def main():
    lines = ["# Golden regression results — %s" % datetime.date.today().isoformat(),
             "", "_Ground truth computed live from %s at run time. SKIP = endpoint not populated "
             "(not a model failure). A model change that lowers accuracy is a REGRESSION — don't deploy it._" % API, ""]
    # pre-fetch endpoints once
    cache = {}
    for _, ep, *_ in GOLDEN:
        if ep not in cache:
            cache[ep] = get(ep)
    summary = {}
    for model in MODELS:
        lines += ["## %s" % model, "", "| # | question | truth | model | verdict | s |", "|---|---|---|---|---|---|"]
        n_pass = n_fail = n_skip = 0
        for qid, ep, question, truth_fn, tol in GOLDEN:
            data = cache.get(ep)
            truth = None
            try:
                truth = truth_fn(data) if data else None
            except Exception:
                truth = None
            if truth is None:
                n_skip += 1
                lines.append("| %s | %s | — | — | SKIP | — |" % (qid, question))
                continue
            got, secs, _raw = ask(model, question, data)
            ok = within(float(truth), got, tol)
            n_pass += int(ok)
            n_fail += int(not ok)
            lines.append("| %s | %s | %s | %s | %s | %s |"
                         % (qid, question, truth, got if got is not None else "∅", "PASS" if ok else "**FAIL**", secs))
            print("[%s] %-16s truth=%-14s got=%-14s %s" % (model, qid, truth, got, "PASS" if ok else "FAIL"))
        acc = round(n_pass / max(1, n_pass + n_fail) * 100, 1)
        summary[model] = (n_pass, n_fail, n_skip, acc)
        lines += ["", "**%s: %d pass / %d fail / %d skip → accuracy %s%% (of answerable)**" % (model, n_pass, n_fail, n_skip, acc), ""]
    lines += ["## Summary", "", "| model | pass | fail | skip | accuracy |", "|---|---|---|---|---|"]
    for m, (p, f, s, a) in summary.items():
        lines.append("| %s | %d | %d | %d | %s%% |" % (m, p, f, s, a))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\nWrote", os.path.normpath(OUT))


if __name__ == "__main__":
    main()
