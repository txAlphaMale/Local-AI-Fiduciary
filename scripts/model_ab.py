#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""
Fiduciary model A/B harness — quality-first.

Runs the SAME research-brief prompt (rubric + data-coverage, faithful to portfolio-api `_llm`)
and a tool-calling probe against several Ollama models, and writes full outputs SIDE BY SIDE so
you can judge ANALYSIS QUALITY directly. Latency is captured but reported last and labeled
tiebreaker-only, per the project's decision criterion (quality is paramount; speed only breaks a
tie when quality is defensibly equal).

Usage (on the box that runs Ollama):
    python3 scripts/model_ab.py                       # uses default model list
    python3 scripts/model_ab.py qwen3.5:27b qwen3:32b gpt-oss:20b
    OLLAMA_BASE_URL=http://localhost:11434 python3 scripts/model_ab.py

Output: docs/model_ab_results.md
Requires: `pip install requests` and the models pulled in Ollama (`ollama pull qwen3:32b`, etc.).
"""
import os, sys, json, time, datetime, requests

OLLAMA = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
MODELS = sys.argv[1:] or ["qwen3.5:27b", "qwen3.6:27b", "gpt-oss:20b"]
NUM_CTX = int(os.environ.get("ANALYSIS_NUM_CTX", "16384"))
TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "1200"))
OUT = os.path.join(os.path.dirname(__file__), "..", "docs", "model_ab_results.md")

# ---- faithful copy of the Fiduciary research-brief prompt (rubric + coverage) ----
BRIEF_HEAD = (
    "You are a research analyst. Using ONLY the data provided below, return a JSON object with keys: "
    "rating (one of Buy, Hold, Sell), confidence (low, med, high), thesis, bull_case, bear_case, key_risks. "
    "This is informational only and NOT financial advice.\n"
    "CONFIDENCE RUBRIC — rate how well-supported YOUR call is by the data actually provided (see the DATA "
    "COVERAGE line), NOT how certain you are about the future:\n"
    "  - high: fundamentals, valuation, AND analyst coverage are all present and their signals broadly AGREE.\n"
    "  - med: most inputs present but signals MIXED, or one key input missing.\n"
    "  - low: fundamentals/valuation/analyst coverage thin or unavailable, or signals CONFLICT sharply.\n"
    "Do NOT default to low when the data is rich — EARN 'high' when evidence lines up.\n"
    "Analyze fundamentals with a GAAP lens (margins, ROE, growth + trend, leverage, quality of earnings via "
    "OCF/FCF vs net income), prefer TTM over a single annual print, and assess valuation (P/E, P/B, P/S, "
    "earnings yield vs ~4% Treasury); flag value traps and price outrunning fundamentals. Weigh fundamentals "
    "WITH analyst consensus and news, not in isolation.\n"
)

# representative fed-in data (PLTR-like: rich coverage) so the comparison is reproducible
BRIEF_CASES = [
    {"sym": "PLTR", "name": "Palantir Technologies",
     "coverage": "SEC GAAP fundamentals: PRESENT; valuation multiples: PRESENT; analyst consensus: 24 analysts; recent news: PRESENT (4 of 4 input sources available)",
     "body": (
        "Ticker: PLTR (Palantir Technologies). Portfolio weight: 35%. Holder's unrealized total return: +120% since purchase.\n"
        "Analyst consensus data: {\"rating\": \"Hold\", \"analyst_total\": 24, \"counts\": {\"strongBuy\": 4, \"buy\": 5, \"hold\": 10, \"sell\": 3, \"strongSell\": 2}, \"target_mean\": 42, \"target_high\": 60, \"target_low\": 25}.\n"
        "SEC GAAP fundamentals (latest annual 10-K): {\"metrics\": {\"fiscal_year_end\": \"2025-12-31\", \"revenue_growth_pct\": 29, \"ttm\": {\"net_margin_pct\": 16, \"revenue\": 2900000000}}, \"ratios\": {\"net_margin_pct\": 9, \"return_on_equity_pct\": 11, \"fcf_margin_pct\": 33, \"debt_to_equity\": 0.05, \"current_ratio\": 5.8}}.\n"
        "Valuation multiples: {\"pe_ttm\": 210, \"pe_forward\": 160, \"pb\": 45, \"ps_ttm\": 75, \"earnings_yield_pct\": 0.48, \"beta\": 2.6}.\n"
        "Recent news: Palantir raised full-year guidance on accelerating U.S. commercial AIP adoption; multiple new government contracts; bulls cite durable growth, bears cite extreme valuation.")},
    {"sym": "LUNR", "name": "Intuitive Machines",
     "coverage": "SEC GAAP fundamentals: PRESENT; valuation multiples: PRESENT; analyst consensus: 6 analysts; recent news: PRESENT (4 of 4 input sources available)",
     "body": (
        "Ticker: LUNR (Intuitive Machines). Portfolio weight: 2%. Holder's unrealized total return: -15% since purchase.\n"
        "Analyst consensus data: {\"rating\": \"Buy\", \"analyst_total\": 6, \"counts\": {\"strongBuy\": 3, \"buy\": 2, \"hold\": 1, \"sell\": 0, \"strongSell\": 0}, \"target_mean\": 14, \"target_high\": 20, \"target_low\": 9}.\n"
        "SEC GAAP fundamentals (latest annual 10-K): {\"metrics\": {\"fiscal_year_end\": \"2025-12-31\", \"revenue_growth_pct\": 40}, \"ratios\": {\"net_margin_pct\": -35, \"return_on_equity_pct\": null, \"fcf_margin_pct\": -50, \"debt_to_equity\": 0.2, \"current_ratio\": 1.4}}.\n"
        "Valuation multiples: {\"pe_ttm\": null, \"pe_forward\": null, \"pb\": 6.1, \"ps_ttm\": 8.0, \"earnings_yield_pct\": null, \"beta\": 2.9}.\n"
        "Recent news: New NASA lunar delivery award; second landing mission slated; cash-burn and dilution concerns persist.")},
]

# tool-calling probe (Strategist-style): does the model emit a valid tool_call?
TOOL_SYS = ("You are a portfolio strategist. Decide what to look at first, then CALL a tool. "
            "You must use the provided tools rather than answering directly.")
TOOLS = [{"type": "function", "function": {
    "name": "get_holdings",
    "description": "Get the current portfolio holdings (tickers, values, weights).",
    "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_valuation",
        "description": "Get valuation multiples for a ticker.",
        "parameters": {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]}}}]


def _no_think(model):
    return "\n/no_think" if "qwen3" in model.lower() else ""


def run_brief(model, case):
    prompt = BRIEF_HEAD + "DATA COVERAGE: " + case["coverage"] + ".\n" + case["body"] + _no_think(model)
    body = {"model": model, "prompt": prompt, "format": "json", "stream": False,
            "keep_alive": "5m", "options": {"temperature": 0.3, "num_ctx": NUM_CTX}}
    t0 = time.time()
    try:
        r = requests.post(f"{OLLAMA}/api/generate", json={**body, "think": False}, timeout=TIMEOUT)
        if r.status_code >= 400 and "think" in r.text.lower():
            r = requests.post(f"{OLLAMA}/api/generate", json=body, timeout=TIMEOUT)
        d = r.json()
        raw = (d.get("response") or "").strip() or (d.get("thinking") or "").strip()
    except Exception as e:
        return {"latency": time.time() - t0, "raw": f"ERROR: {e}", "json_ok": False, "parsed": None}
    parsed = None
    try:
        parsed = json.loads(raw)
    except Exception:
        s, e = raw.find("{"), raw.rfind("}")
        if s != -1 and e > s:
            try:
                parsed = json.loads(raw[s:e + 1])
            except Exception:
                pass
    return {"latency": round(time.time() - t0, 1), "raw": raw, "json_ok": parsed is not None,
            "parsed": parsed}


def run_tool(model):
    body = {"model": model, "stream": False, "tools": TOOLS,
            "messages": [{"role": "system", "content": TOOL_SYS},
                         {"role": "user", "content": "Review my portfolio and start your analysis."}],
            "options": {"temperature": 0.4, "num_ctx": NUM_CTX}}
    t0 = time.time()
    try:
        r = requests.post(f"{OLLAMA}/api/chat", json=body, timeout=TIMEOUT)
        d = r.json()
        msg = (d.get("message") or {})
        tcs = msg.get("tool_calls") or []
        names = [ (tc.get("function") or {}).get("name") for tc in tcs ]
        valid = all(n in ("get_holdings", "get_valuation") for n in names) and bool(names)
        return {"latency": round(time.time() - t0, 1), "tool_calls": names,
                "valid": valid, "content": (msg.get("content") or "")[:400]}
    except Exception as e:
        return {"latency": round(time.time() - t0, 1), "tool_calls": [], "valid": False, "content": f"ERROR: {e}"}


def main():
    print(f"Ollama: {OLLAMA} | models: {MODELS}")
    lines = ["# Fiduciary model A/B — results",
             f"_Run {datetime.datetime.now().isoformat(timespec='seconds')} · Ollama {OLLAMA} · "
             "QUALITY is the deciding factor; latency is tiebreaker-only._\n",
             "> Read the full brief outputs below and judge: correct GAAP/valuation reasoning? "
             "sensible rating? EARNED confidence (not defaulted)? coherent bull/bear/risks? "
             "Then check the tool-calling probe. Only if two models are quality-equal does latency matter.\n"]
    results = {}
    for m in MODELS:
        print(f"\n=== {m} ===")
        results[m] = {"briefs": [], "tool": None}
        for case in BRIEF_CASES:
            print(f"  brief {case['sym']} …", flush=True)
            results[m]["briefs"].append((case, run_brief(m, case)))
        print("  tool probe …", flush=True)
        results[m]["tool"] = run_tool(m)

    # scoreboard
    lines.append("## Scoreboard (objective guardrails)\n")
    lines.append("| model | JSON-valid briefs | tool-call valid | brief latency (s) | tool latency (s) |")
    lines.append("|---|---|---|---|---|")
    for m in MODELS:
        jok = sum(1 for _, b in results[m]["briefs"] if b["json_ok"])
        lat = ", ".join(str(b["latency"]) for _, b in results[m]["briefs"])
        t = results[m]["tool"]
        lines.append(f"| `{m}` | {jok}/{len(BRIEF_CASES)} | {'yes' if t['valid'] else 'NO'} ({', '.join(t['tool_calls']) or '—'}) | {lat} | {t['latency']} |")
    lines.append("\n_Objective metrics are a floor, not the decision. The quality judgment is yours, below._\n")

    # side-by-side full outputs, per case
    for ci, case in enumerate(BRIEF_CASES):
        lines.append(f"\n## Brief — {case['sym']} ({case['name']})\n")
        for m in MODELS:
            b = results[m]["briefs"][ci][1]
            lines.append(f"### `{m}`  ·  JSON-valid: {b['json_ok']}  ·  {b['latency']}s")
            p = b["parsed"] or {}
            if b["json_ok"]:
                lines.append(f"- **rating:** {p.get('rating')}  ·  **confidence:** {p.get('confidence')}")
                for k in ("thesis", "bull_case", "bear_case", "key_risks"):
                    v = p.get(k)
                    if isinstance(v, list):
                        v = "; ".join(str(x) for x in v)
                    lines.append(f"- **{k}:** {v}")
            else:
                lines.append("```\n" + (b["raw"][:1500] or "(empty)") + "\n```")
            lines.append("")
    # tool outputs
    lines.append("\n## Tool-calling probe (Strategist-style)\n")
    for m in MODELS:
        t = results[m]["tool"]
        lines.append(f"### `{m}` — valid: {t['valid']}  ·  calls: {t['tool_calls'] or '—'}  ·  {t['latency']}s")
        if t["content"]:
            lines.append("```\n" + t["content"] + "\n```")
        lines.append("")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\nWrote {os.path.abspath(OUT)}")


if __name__ == "__main__":
    main()
