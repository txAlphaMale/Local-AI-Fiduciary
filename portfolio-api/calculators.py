"""
calculators.py — CALCULATORS tab (2026-10-19, Jason): the household-fiduciary calculator bench.

Every calculator is a pure function of its inputs (no I/O, no network, no app state), so it is
unit-testable offline (test_calculators.py) and callable three ways with identical results:
the CALCULATORS tab (POST /api/calc), the WHAT-IF liability panel (same endpoint, prefilled from
the household's own liabilities), and the Fiduciary chat persona (run_calculator tool) — an LLM
doing amortization arithmetic in its head is exactly the failure this module exists to prevent.

Conventions (shared by every calculator):
  · Rates are ANNUAL PERCENTAGES (6.5 means 6.5%). Money is plain dollars. Periods are integers.
  · Every function returns a dict. Bad/insufficient input returns {"error": str, "missing": [...]}
    instead of raising — a panel that 500s because one field is blank is worse than one that
    names the field to fill in. (Same rule cashdebt.remaining_payments() follows.)
  · Nothing is silently defaulted that would change the answer materially. A blank APR is NOT
    0% (that understates a 19.49% card by years); a blank PMI rate on a >80% LTV purchase is
    reported as "not included" rather than guessed. Assumption-type inputs (growth rates) that
    DO have defaults are echoed back in `assumptions` so the UI can show what was used.
  · Payment timing is END of period unless a calculator says otherwise (standard for loans).

Not advice: these are arithmetic models. Notes on each result say what the model ignores.
"""

import datetime
import math

FREQS = {"weekly": 52, "biweekly": 26, "semimonthly": 24, "monthly": 12,
         "quarterly": 4, "semiannual": 2, "annual": 1}
MAX_PERIODS = 1200          # 100 years of monthly payments — "never pays off" beyond this
_EPS = 0.005                # half a cent: balances below this are paid


# ----------------------------------------------------------------------------- input helpers
class _Missing(Exception):
    pass


def _num(inp, key, default=None, required=False, lo=None, hi=None, label=None):
    """Read a number from the inputs dict. Blank / None / non-numeric -> default (or a
    _Missing when required). Values outside [lo, hi] are a user error, reported by name."""
    v = (inp or {}).get(key)
    if v in (None, "") or (isinstance(v, str) and not v.strip()):
        if required:
            raise _Missing(key)
        return default
    try:
        x = float(str(v).replace(",", "").replace("$", "").replace("%", "").strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label or key} must be a number (got {v!r})")
    if math.isnan(x) or math.isinf(x):
        raise ValueError(f"{label or key} must be a finite number")
    if lo is not None and x < lo:
        raise ValueError(f"{label or key} must be ≥ {lo:g}")
    if hi is not None and x > hi:
        raise ValueError(f"{label or key} must be ≤ {hi:g}")
    return x


def _freq(inp, key="frequency", default="monthly"):
    f = (inp or {}).get(key) or default
    if isinstance(f, (int, float)) and f > 0:
        return int(f)
    f = str(f).lower().replace("-", "").replace(" ", "")
    if f.isdigit() and int(f) > 0:
        return int(f)
    if f not in FREQS:
        raise ValueError(f"{key} must be one of {', '.join(FREQS)}")
    return FREQS[f]


def _r2(x):
    return None if x is None else round(float(x), 2) + 0.0   # + 0.0 turns -0.0 into 0.0


def periodic_rate(apr_pct, periods_per_year=12, compounds_per_year=None):
    """Nominal annual rate -> effective rate per PAYMENT period. When compounding frequency
    differs from payment frequency (e.g. Canadian mortgages: semiannual compounding, monthly
    payments) the per-period rate is (1 + r/C)^(C/P) − 1, the standard TVM convention."""
    r = (apr_pct or 0.0) / 100.0
    p = periods_per_year
    c = compounds_per_year or p
    if r == 0:
        return 0.0
    return (1 + r / c) ** (c / p) - 1


def amort_payment(principal, i, n, balloon=0.0):
    """Level payment that takes `principal` to `balloon` in n periods at periodic rate i."""
    if n <= 0:
        return principal
    if i == 0:
        return (principal - balloon) / n
    v = (1 + i) ** (-n)
    return (principal - balloon * v) * i / (1 - v)


def amortize(principal, i, n_amort, term_periods=None, io_periods=0, extra=0.0,
             lumps=None, payment=None, max_periods=MAX_PERIODS):
    """The one amortization engine every loan-shaped calculator uses.

    principal     starting balance
    i             periodic rate
    n_amort       amortization length in periods (INCLUDING any interest-only periods — a
                  30-yr loan with 10 IO years amortizes over the remaining 20 once IO ends)
    term_periods  if set and shorter than n_amort: BALLOON — whatever is left is due then
    io_periods    interest-only periods at the start
    extra         extra principal paid every period (after IO, and during IO too if > 0)
    lumps         {period: amount} one-time principal prepayments
    payment       fixed scheduled payment override (What-If: the household's actual payment,
                  which may differ from the textbook amortized amount)

    Returns {"rows": [...], "periods", "total_interest", "total_paid", "balloon",
             "scheduled_payment", "resolved"}. resolved=False means the payment never
    retires the balance (payment ≤ interest) within max_periods."""
    lumps = lumps or {}
    bal = float(principal)
    rows = []
    sched = payment
    io_periods = int(io_periods or 0)
    if sched is None and io_periods == 0:
        sched = amort_payment(bal, i, n_amort)
    first_amort_payment = sched
    tot_int = tot_paid = 0.0
    balloon = 0.0
    k = 0
    limit = min(int(term_periods), max_periods) if term_periods else max_periods
    while bal > _EPS and k < limit:
        k += 1
        interest = bal * i
        if k <= io_periods and payment is None:
            pay = interest
        else:
            if sched is None:
                sched = amort_payment(bal, i, max(1, n_amort - io_periods))
                first_amort_payment = sched
            pay = sched
        princ = pay - interest
        if princ > bal:                       # final, short payment
            princ = bal
            pay = bal + interest
        ext = min(max(0.0, extra) + max(0.0, lumps.get(k, 0.0)), max(0.0, bal - max(princ, 0.0)))
        bal = bal - princ - ext
        if abs(bal) < _EPS:
            bal = 0.0
        tot_int += interest
        tot_paid += pay + ext
        row = {"period": k, "payment": pay, "interest": interest, "principal": princ,
               "extra": ext, "balance": bal}
        if term_periods and k == int(term_periods) and bal > _EPS:
            balloon = bal
            row["balloon"] = bal
            tot_paid += bal
            bal = 0.0
            row["balance"] = 0.0
        rows.append(row)
        if (k > io_periods and princ + ext <= 0 and row["balance"] > 0
                and not any(p > k for p in lumps)):
            # Payment doesn't even cover the interest and no future prepayment is scheduled:
            # the balance can only stay flat or grow from here, so it never resolves.
            return {"rows": rows, "periods": None, "total_interest": None, "total_paid": None,
                    "balloon": 0.0, "scheduled_payment": first_amort_payment, "resolved": False}
    resolved = bal <= _EPS
    return {"rows": rows, "periods": k if resolved else None,
            "total_interest": tot_int if resolved else None,
            "total_paid": tot_paid if resolved else None, "balloon": balloon,
            "scheduled_payment": first_amort_payment, "resolved": resolved}


def _yearly(rows, ppy, extra_cols=None):
    """Roll a period schedule up to one row per year (year 1 = periods 1..ppy)."""
    out = []
    for r in rows:
        y = (r["period"] - 1) // ppy + 1
        if not out or out[-1]["year"] != y:
            out.append({"year": y, "payment": 0.0, "interest": 0.0, "principal": 0.0,
                        "extra": 0.0, "balance": 0.0, **{c: 0.0 for c in (extra_cols or [])}})
        o = out[-1]
        for c in ("payment", "interest", "principal", "extra", *(extra_cols or [])):
            o[c] += r.get(c, 0.0) or 0.0
        o["balance"] = r["balance"]
        if r.get("balloon"):
            o["balloon"] = r["balloon"]
    return [{k: (_r2(v) if isinstance(v, float) else v) for k, v in o.items()} for o in out]


def _round_rows(rows):
    return [{k: (_r2(v) if isinstance(v, float) else v) for k, v in r.items()} for r in rows]


def _periods_label(n, ppy):
    """'7 yr 3 mo' style label for a count of payment periods."""
    if n is None:
        return None
    months = n * 12.0 / ppy
    y, m = divmod(int(round(months)), 12)
    if y and m:
        return f"{y} yr {m} mo"
    return f"{y} yr" if y else f"{m} mo"


# ============================================================================ LOAN (general)
def _effective_apr(net_proceeds, payment, n, ppy, balloon=0.0):
    """APR (annual %, nominal at payment frequency) that equates what the borrower actually
    NETS after up-front fees with the payment stream they owe — the Reg Z idea behind 'APR'
    being higher than the note rate when fees are charged. Solved numerically."""
    if net_proceeds <= 0 or n <= 0 or payment <= 0:
        return None

    def f(i):
        if abs(i) < 1e-12:
            return payment * n + balloon - net_proceeds
        v = (1 + i) ** (-n)
        return payment * (1 - v) / i + balloon * v - net_proceeds
    i = _solve_root(f, 0.0, 1.0)
    return None if i is None else i * ppy * 100.0


def _solve_root(f, lo, hi, iters=200):
    """Bisection on [lo, hi] after scanning for a sign change. Robust (never diverges) and
    plenty fast for these one-dimensional problems. Returns None if no bracket is found."""
    grid = [lo + (hi - lo) * (k / 400.0) ** 2 for k in range(401)]
    prev_x, prev_f = None, None
    for x in grid:
        try:
            fx = f(x)
        except (OverflowError, ZeroDivisionError, ValueError):
            continue
        if fx == 0:
            return x
        if prev_f is not None and (fx > 0) != (prev_f > 0):
            a, b, fa = prev_x, x, prev_f
            for _ in range(iters):
                m = (a + b) / 2
                fm = f(m)
                if (fm > 0) == (fa > 0):
                    a, fa = m, fm
                else:
                    b = m
                if b - a < 1e-14:
                    break
            return (a + b) / 2
        prev_x, prev_f = x, fx
    return None


def loan(inp):
    """General amortizing loan (personal, auto, student, boat, HELOC-fixed, etc.).

    Either `amount` directly, or purchase details that build it: price − down_payment − trade_in
    + trade_in_owed + sales tax (+ fees when fees_financed). Sales tax is applied to the price
    AFTER the trade-in credit when tax_after_trade is true (most US states) — before it otherwise.
    `origination_fee` (or origination_fee_pct) is a fee withheld up front: it doesn't change the
    payment but raises the effective APR, which is reported alongside the note rate."""
    try:
        apr = _num(inp, "apr", required=True, lo=0, hi=100, label="APR")
        ppy = _freq(inp)
        tm = _num(inp, "term_months", lo=1, hi=1200)
        ty = _num(inp, "term_years", lo=0.0834, hi=100)
        if tm is None and ty is None:
            raise _Missing("term_months")
        n = int(round(tm * ppy / 12.0)) if tm is not None else int(round(ty * ppy))
        amount = _num(inp, "amount", lo=0)
        build = None
        if amount is None:
            price = _num(inp, "price", required=True, lo=0, label="amount or price")
            down = _num(inp, "down_payment", 0.0, lo=0)
            trade = _num(inp, "trade_in", 0.0, lo=0)
            owed = _num(inp, "trade_in_owed", 0.0, lo=0)
            tax_pct = _num(inp, "sales_tax_pct", 0.0, lo=0, hi=25)
            fees = _num(inp, "fees", 0.0, lo=0)
            after = str(inp.get("tax_after_trade", True)).lower() not in ("false", "0", "no")
            taxable = max(0.0, price - trade) if after else price
            sales_tax = taxable * tax_pct / 100.0
            financed_fees = fees if str(inp.get("fees_financed", True)).lower() not in ("false", "0", "no") else 0.0
            amount = price - down - trade + owed + sales_tax + financed_fees
            build = {"price": _r2(price), "down_payment": _r2(down), "trade_in": _r2(trade),
                     "trade_in_owed": _r2(owed), "sales_tax": _r2(sales_tax),
                     "fees_financed": _r2(financed_fees), "fees_upfront": _r2(fees - financed_fees),
                     "negative_equity_rolled_in": _r2(max(0.0, owed - trade))}
        if amount <= 0:
            return {"error": "Nothing to finance — the down payment/trade-in covers the price."}
        extra = _num(inp, "extra", 0.0, lo=0)
        fee = _num(inp, "origination_fee", None, lo=0)
        fee_pct = _num(inp, "origination_fee_pct", None, lo=0, hi=50)
        if fee is None and fee_pct:
            fee = amount * fee_pct / 100.0
        cpy = _num(inp, "compounds_per_year", None, lo=1, hi=365)
    except _Missing as m:
        return {"error": f"Missing required input: {m}", "missing": [str(m)]}
    except ValueError as e:
        return {"error": str(e)}

    i = periodic_rate(apr, ppy, cpy)
    base = amortize(amount, i, n)
    res = amortize(amount, i, n, extra=extra) if extra else base
    pmt = base["scheduled_payment"]
    out = {"calculator": "loan", "amount_financed": _r2(amount), "payment": _r2(pmt),
           "periods_per_year": ppy, "scheduled_periods": n,
           "periods": res["periods"], "payoff_label": _periods_label(res["periods"], ppy),
           "total_paid": _r2(res["total_paid"]), "total_interest": _r2(res["total_interest"]),
           "interest_pct_of_principal": _r2(100.0 * res["total_interest"] / amount) if amount else None,
           "yearly": _yearly(res["rows"], ppy), "schedule": _round_rows(res["rows"])}
    if build:
        out["purchase"] = build
    if extra:
        out["without_extra"] = {"periods": base["periods"], "total_interest": _r2(base["total_interest"])}
        out["periods_saved"] = base["periods"] - res["periods"]
        out["time_saved_label"] = _periods_label(out["periods_saved"], ppy)
        out["interest_saved"] = _r2(base["total_interest"] - res["total_interest"])
    if fee:
        out["origination_fee"] = _r2(fee)
        out["effective_apr"] = _r2(_effective_apr(amount - fee, pmt, n, ppy))
        out["total_cost_incl_fee"] = _r2(res["total_paid"] + fee)
    out["notes"] = ["Fixed rate, level payments, end-of-period. Credit cards and daily-simple-"
                    "interest loans accrue slightly differently; results are within cents-to-"
                    "dollars for typical balances."]
    return out


# ============================================================================ LAND LOAN
# Typical MINIMUM down payments by land type (lender practice varies; these are the commonly
# cited floors — e.g. ConsumerAffairs "What is a land loan", 2025: improved ≥15%, unimproved
# ≥25%, raw ≥35%, i.e. max ~65% LTV on raw land). Used only to WARN, never to block.
LAND_TYPES = {
    "improved":   {"label": "Improved (road, utilities in place)", "min_down_pct": 15},
    "unimproved": {"label": "Unimproved (some utilities, not fully connected)", "min_down_pct": 25},
    "raw":        {"label": "Raw (no road/utility access)", "min_down_pct": 35},
}


def land_loan(inp):
    """Land / lot loan. What actually differs from a home loan, and is modeled here:
      · BALLOON terms are the norm: payments amortize over e.g. 20-30 yrs, but the whole
        remaining balance is due after 3-10 (`balloon_years`). The balloon is the risk —
        it has to be refinanced or paid at whatever rates exist then.
      · INTEREST-ONLY periods are common (`io_years`).
      · Much larger down payments by land type (warned against LAND_TYPES).
      · Carrying costs are property tax (+ optional liability insurance), not PITI —
        there's no dwelling to insure, and no PMI market for land."""
    try:
        price = _num(inp, "price", required=True, lo=0)
        down = _num(inp, "down_payment", None, lo=0)
        dpct = _num(inp, "down_pct", None, lo=0, hi=100)
        if down is None:
            down = price * (dpct or 0.0) / 100.0
        apr = _num(inp, "apr", required=True, lo=0, hi=100, label="APR")
        amort_y = _num(inp, "amort_years", required=True, lo=1, hi=40)
        balloon_y = _num(inp, "balloon_years", None, lo=0.25, hi=40)
        io_y = _num(inp, "io_years", 0.0, lo=0, hi=40)
        tax = _num(inp, "property_tax_annual", 0.0, lo=0)
        ins = _num(inp, "insurance_annual", 0.0, lo=0)
        closing = _num(inp, "closing_costs", 0.0, lo=0)
        extra = _num(inp, "extra", 0.0, lo=0)
        ltype = str(inp.get("land_type") or "").lower() or None
    except _Missing as m:
        return {"error": f"Missing required input: {m}", "missing": [str(m)]}
    except ValueError as e:
        return {"error": str(e)}
    amount = price - down
    if amount <= 0:
        return {"error": "Down payment covers the full price — nothing to finance."}
    n = int(round(amort_y * 12))
    io = int(round(io_y * 12))
    if io >= n:
        return {"error": "Interest-only period must be shorter than the amortization period."}
    term = int(round(balloon_y * 12)) if balloon_y and balloon_y < amort_y else None
    i = periodic_rate(apr, 12)
    res = amortize(amount, i, n, term_periods=term, io_periods=io, extra=extra)
    amort_pmt = res["scheduled_payment"] if not io else amort_payment(
        amount, i, n - io)   # after IO the full balance amortizes over the remainder
    warnings = []
    down_pct = 100.0 * down / price if price else 0.0
    if ltype in LAND_TYPES and down_pct + 1e-9 < LAND_TYPES[ltype]["min_down_pct"]:
        warnings.append(f"{down_pct:.0f}% down is below the typical {LAND_TYPES[ltype]['min_down_pct']}% "
                        f"minimum lenders ask for on {ltype} land — expect a higher rate, a smaller "
                        "loan, or seller financing.")
    if term and res["balloon"]:
        warnings.append(f"Balloon of ${res['balloon']:,.0f} due at month {term} "
                        f"({_periods_label(term, 12)}). Plan now for how it gets paid — a refinance at "
                        "future rates, a sale, or cash. If you build first, a construction-to-perm "
                        "loan often rolls the land loan in.")
    if io:
        warnings.append(f"Interest-only for {io} months: no equity is built in that window, and the "
                        f"payment then jumps to ${amort_pmt:,.0f}/mo.")
    months = res["periods"] or 0
    carry0 = (i * amount if io else amort_pmt) + (tax + ins) / 12.0
    out = {"calculator": "land_loan", "loan_amount": _r2(amount), "down_payment": _r2(down),
           "down_pct": _r2(down_pct), "ltv_pct": _r2(100 - down_pct),
           "io_payment": _r2(i * amount) if io else None, "payment": _r2(amort_pmt),
           "monthly_carry_first": _r2(carry0 + extra),
           "balloon": _r2(res["balloon"]) if res["balloon"] else None,
           "balloon_month": term if res["balloon"] else None,
           "periods": res["periods"], "payoff_label": _periods_label(res["periods"], 12),
           "total_interest": _r2(res["total_interest"]), "total_loan_paid": _r2(res["total_paid"]),
           "total_property_tax": _r2(tax * months / 12.0), "total_insurance": _r2(ins * months / 12.0),
           "closing_costs": _r2(closing),
           "total_cost": _r2(down + closing + (res["total_paid"] or 0) + (tax + ins) * months / 12.0),
           "land_type": LAND_TYPES.get(ltype, {}).get("label"),
           "land_types": LAND_TYPES, "warnings": warnings,
           "yearly": _yearly(res["rows"], 12), "schedule": _round_rows(res["rows"]),
           "notes": ["Tax and insurance are held flat. Land generally can't carry PMI and isn't "
                     "covered by a homeowners policy; liability coverage is optional but common.",
                     "Land doesn't depreciate for tax purposes and produces no rent unless leased "
                     "(e.g. ag/hunting) — the carrying cost is the full cost until you build or sell."]}
    if extra:
        b = amortize(amount, i, n, term_periods=term, io_periods=io)
        out["interest_saved"] = _r2((b["total_interest"] or 0) - (res["total_interest"] or 0))
        if res["balloon"] or b["balloon"]:
            out["balloon_reduction"] = _r2((b["balloon"] or 0) - (res["balloon"] or 0))
    return out


# ============================================================================ MORTGAGE (TCO)
def mortgage(inp):
    """Mortgage with the TRUE cost of ownership — Jason's framing: 'despite having an
    appreciating asset, too many people don't see what that property really costs them.'

    Monthly cash out = P&I + property tax + homeowners insurance + PMI + HOA (+ maintenance, if
    a % is given). Tax/insurance/HOA escalate yearly at their growth rates (echoed in
    `assumptions`); maintenance is a % of the (appreciating) home value.

    PMI: annual premium as % of the ORIGINAL loan, charged while LTV > 80%. Removal rule:
      'auto78'    (default, conservative) — the Homeowners Protection Act's automatic
                  termination: the month the ORIGINAL schedule reaches 78% of original value,
                  regardless of extra payments.
      'request80' — borrower requests cancellation once the ACTUAL balance hits 80% of original
                  value (extra payments pull this earlier). Requires a good payment history.
    A >80% LTV with no PMI rate entered is reported as a warning, not guessed.

    If appreciation_pct is given, the result also shows the NET picture at the horizon: sell at
    the appreciated value less selling costs, repay the balance, and net that against everything
    paid. That is the honest 'what did living here cost' number."""
    try:
        price = _num(inp, "price", required=True, lo=1)
        down = _num(inp, "down_payment", None, lo=0)
        dpct = _num(inp, "down_pct", None, lo=0, hi=100)
        if down is None:
            down = price * (dpct or 0.0) / 100.0
        apr = _num(inp, "apr", required=True, lo=0, hi=30, label="APR")
        ty = _num(inp, "term_years", 30, lo=1, hi=50)
        tax_amt = _num(inp, "property_tax_annual", None, lo=0)
        tax_pct = _num(inp, "property_tax_pct", None, lo=0, hi=10)
        ins = _num(inp, "insurance_annual", 0.0, lo=0)
        pmi_pct = _num(inp, "pmi_pct", None, lo=0, hi=5)
        pmi_rule = str(inp.get("pmi_rule") or "auto78").lower()
        hoa = _num(inp, "hoa_monthly", 0.0, lo=0)
        maint_pct = _num(inp, "maintenance_pct", 0.0, lo=0, hi=10)
        closing = _num(inp, "closing_costs", 0.0, lo=0)
        points = _num(inp, "points", 0.0, lo=0, hi=10)
        extra = _num(inp, "extra", 0.0, lo=0)
        g_tax = _num(inp, "tax_growth_pct", 2.0, lo=-10, hi=20)
        g_ins = _num(inp, "insurance_growth_pct", 2.0, lo=-10, hi=30)
        g_hoa = _num(inp, "hoa_growth_pct", 2.0, lo=-10, hi=30)
        app_pct = _num(inp, "appreciation_pct", None, lo=-20, hi=30)
        sell_pct = _num(inp, "selling_cost_pct", 6.0, lo=0, hi=20)
        horizon_y = _num(inp, "horizon_years", None, lo=1, hi=60)
    except _Missing as m:
        return {"error": f"Missing required input: {m}", "missing": [str(m)]}
    except ValueError as e:
        return {"error": str(e)}
    loan_amt = price - down
    if loan_amt < 0:
        return {"error": "Down payment exceeds the price."}
    n = int(round(ty * 12))
    i = periodic_rate(apr, 12)
    tax0 = tax_amt if tax_amt is not None else price * (tax_pct or 0.0) / 100.0
    ltv = 100.0 * loan_amt / price
    warnings = []
    pmi_monthly = 0.0
    if ltv > 80 + 1e-9:
        if pmi_pct is None:
            warnings.append(f"LTV is {ltv:.1f}% (> 80%) — conventional loans charge PMI until it "
                            "drops to 78–80%. Enter a PMI rate (quotes typically run ~0.3–1.5% of the "
                            "loan per year, credit-score dependent) to include it; it is NOT in these totals.")
        else:
            pmi_monthly = loan_amt * pmi_pct / 100.0 / 12.0
    sched = amortize(loan_amt, i, n, extra=extra) if loan_amt > 0 else {
        "rows": [], "periods": 0, "total_interest": 0.0, "total_paid": 0.0, "scheduled_payment": 0.0}
    base = amortize(loan_amt, i, n) if (extra and loan_amt > 0) else sched
    pi = sched["scheduled_payment"] or 0.0
    # PMI end month
    pmi_end = 0
    if pmi_monthly:
        if pmi_rule == "request80":
            pmi_end = next((r["period"] for r in sched["rows"] if r["balance"] <= 0.80 * price), n)
        else:
            pmi_end = next((r["period"] for r in base["rows"] if r["balance"] <= 0.78 * price), n)
    horizon = int(round(horizon_y * 12)) if horizon_y else n
    rows_by_k = {r["period"]: r for r in sched["rows"]}
    tot = {k: 0.0 for k in ("principal", "interest", "property_tax", "insurance", "pmi", "hoa",
                            "maintenance")}
    yearly = []
    value = price
    for k in range(1, horizon + 1):
        y = (k - 1) // 12
        if (k - 1) % 12 == 0:
            if y > 0 and app_pct is not None:
                value *= 1 + app_pct / 100.0
            yearly.append({"year": y + 1, "principal": 0.0, "interest": 0.0, "property_tax": 0.0,
                           "insurance": 0.0, "pmi": 0.0, "hoa": 0.0, "maintenance": 0.0,
                           "balance": 0.0, "home_value": None})
        r = rows_by_k.get(k)
        comp = {"principal": (r["principal"] + r["extra"]) if r else 0.0,
                "interest": r["interest"] if r else 0.0,
                "property_tax": tax0 * (1 + g_tax / 100.0) ** y / 12.0,
                "insurance": ins * (1 + g_ins / 100.0) ** y / 12.0,
                "pmi": pmi_monthly if k <= pmi_end and r else 0.0,
                "hoa": hoa * (1 + g_hoa / 100.0) ** y,
                "maintenance": value * maint_pct / 100.0 / 12.0}
        for c, v in comp.items():
            tot[c] += v
            yearly[-1][c] += v
        bal = r["balance"] if r else (0.0 if k > (sched["periods"] or 0) else loan_amt)
        yearly[-1]["balance"] = bal
        if app_pct is not None:
            yearly[-1]["home_value"] = price * (1 + app_pct / 100.0) ** (k / 12.0)   # end of month k
    yearly = [{k: (_r2(v) if isinstance(v, float) else v) for k, v in row.items()} for row in yearly]
    points_cost = loan_amt * points / 100.0
    monthly_total_outlay = sum(tot.values())
    total_cost = down + closing + points_cost + monthly_total_outlay
    first = {"principal_interest": _r2(pi), "property_tax": _r2(tax0 / 12.0),
             "insurance": _r2(ins / 12.0), "pmi": _r2(pmi_monthly), "hoa": _r2(hoa),
             "maintenance": _r2(price * maint_pct / 100.0 / 12.0), "extra_principal": _r2(extra)}
    first["total"] = _r2(sum(v or 0 for v in first.values()))
    out = {"calculator": "mortgage", "loan_amount": _r2(loan_amt), "down_payment": _r2(down),
           "down_pct": _r2(100.0 * down / price), "ltv_pct": _r2(ltv),
           "monthly_first": first, "piti": _r2(pi + (tax0 + ins) / 12.0 + pmi_monthly),
           "payoff_periods": sched["periods"], "payoff_label": _periods_label(sched["periods"], 12),
           "pmi_months": pmi_end if pmi_monthly else 0, "pmi_rule": pmi_rule,
           "horizon_months": horizon,
           "totals": {k: _r2(v) for k, v in tot.items()},
           "upfront": {"down_payment": _r2(down), "closing_costs": _r2(closing),
                       "points": _r2(points_cost)},
           "total_cost": _r2(total_cost),
           "cost_multiple_of_price": _r2(total_cost / price),
           "interest_plus_carry": _r2(tot["interest"] + tot["property_tax"] + tot["insurance"] +
                                      tot["pmi"] + tot["hoa"] + tot["maintenance"] + closing + points_cost),
           "assumptions": {"tax_growth_pct": g_tax, "insurance_growth_pct": g_ins,
                           "hoa_growth_pct": g_hoa, "maintenance_pct": maint_pct,
                           "selling_cost_pct": sell_pct, "appreciation_pct": app_pct},
           "yearly": yearly, "schedule": _round_rows(sched["rows"]), "warnings": warnings}
    if extra:
        out["interest_saved_by_extra"] = _r2(base["total_interest"] - sched["total_interest"])
        out["months_saved_by_extra"] = base["periods"] - sched["periods"]
    if app_pct is not None:
        end_val = price * (1 + app_pct / 100.0) ** (horizon / 12.0)
        end_bal = yearly[-1]["balance"] if yearly else loan_amt
        sell = end_val * sell_pct / 100.0
        proceeds = end_val - end_bal - sell
        out["at_horizon"] = {"home_value": _r2(end_val), "loan_balance": _r2(end_bal),
                             "selling_costs": _r2(sell), "net_proceeds": _r2(proceeds),
                             "net_cost_of_ownership": _r2(total_cost - proceeds),
                             "net_cost_per_month": _r2((total_cost - proceeds) / horizon)}
    out["notes"] = [
        "Mortgage-interest and property-tax deductions are ignored (most households take the "
        "standard deduction; itemizers would see a somewhat lower net cost).",
        "Appreciation is a single constant rate; real prices are lumpy and can fall."]
    if not maint_pct:
        out["notes"].insert(1, "Maintenance is 0% here — the common planning rule of thumb is ~1% of "
                               "home value per year (more for older homes). Add it for a realistic total.")
    return out


# ============================================================================ TVM SOLVER
TVM_FIELDS = ("n", "rate", "pv", "pmt", "fv")
TVM_LABELS = {"n": "Number of payments (N)", "rate": "Annual rate (I/Y %)",
              "pv": "Present value (PV)", "pmt": "Payment (PMT)", "fv": "Future value (FV)"}


def _tvm_eq(n, i, pv, pmt, fv, t):
    """The time-value-of-money identity every financial calculator (HP-12C, TI BA II+) solves:
         PV·(1+i)^n + PMT·(1+i·t)·((1+i)^n − 1)/i + FV = 0
    with cash-flow signs: money you RECEIVE is +, money you PAY OUT is −. t = 1 for payments
    at the beginning of each period (annuity due), 0 for the end (ordinary annuity)."""
    if abs(i) < 1e-12:
        return pv + pmt * n + fv
    a = (1 + i) ** n
    return pv * a + pmt * (1 + i * t) * (a - 1) / i + fv


def _tvm_solve(unknown, n, i, pv, pmt, fv, t):
    if unknown == "fv":
        return -(_tvm_eq(n, i, pv, pmt, 0.0, t))
    if unknown == "pv":
        a = (1 + i) ** n
        return -(_tvm_eq(n, i, 0.0, pmt, fv, t)) / a
    if unknown == "pmt":
        if abs(i) < 1e-12:
            return -(pv + fv) / n
        a = (1 + i) ** n
        return -(fv + pv * a) / ((1 + i * t) * (a - 1) / i)
    if unknown == "n":
        if abs(i) < 1e-12:
            if pmt == 0:
                return None
            n_ = -(pv + fv) / pmt
            return n_ if n_ > 0 else None
        k = pmt * (1 + i * t) / i
        if pv + k == 0:
            return None
        a = (k - fv) / (pv + k)
        if a <= 0:
            return None
        n_ = math.log(a) / math.log(1 + i)
        return n_ if n_ > 0 else None
    if unknown == "i":
        f = lambda x: _tvm_eq(n, x, pv, pmt, fv, t)
        # positive rates first (the overwhelmingly common case), then negative
        r = _solve_root(f, 0.0, 1.0)
        if r is None:
            r = _solve_root(lambda x: f(-x), 0.0, 0.99)
            r = -r if r is not None else None
        return r
    raise ValueError(unknown)


def tvm(inp):
    """One calculator for the whole family of 'all but one is known' problems: enter any four
    of N, I/Y, PV, PMT, FV (leave exactly one blank) and it solves the fifth, then shows the
    breakdown. Covers loan payment, max affordable loan, time to a savings goal, required
    savings rate, implied rate of return on a deal, and future value of a savings plan.

    Cash-flow signs follow the calculator convention (received +, paid −). A common slip —
    PV and PMT entered with the SAME sign when solving for N or I/Y, which has no solution —
    is auto-corrected by flipping PMT's sign, with a note saying so."""
    try:
        ppy = int(_num(inp, "periods_per_year", 12, lo=1, hi=365))
        cpy = _num(inp, "compounds_per_year", None, lo=1, hi=365)
        t = 1 if str(inp.get("timing") or "end").lower().startswith("b") else 0
        vals = {}
        for f in TVM_FIELDS:
            vals[f] = _num(inp, f, None, label=TVM_LABELS[f])
    except ValueError as e:
        return {"error": str(e)}
    blank = [f for f in TVM_FIELDS if vals[f] is None]
    if len(blank) != 1:
        return {"error": ("Leave exactly ONE field blank — it's the one that gets solved. "
                          + (f"Blank now: {', '.join(TVM_LABELS[b] for b in blank)}." if blank
                             else "All five are filled in.")),
                "blank": blank}
    unknown = blank[0]
    notes = []
    if unknown != "n" and vals["n"] is not None and vals["n"] <= 0:
        return {"error": "N must be greater than 0."}
    if unknown != "rate" and vals["rate"] is not None and vals["rate"] <= -100:
        return {"error": "I/Y must be greater than −100%."}
    i = periodic_rate(vals["rate"], ppy, cpy) if unknown != "rate" else None
    n = vals["n"]
    pv, pmt, fv = vals["pv"], vals["pmt"], vals["fv"]

    def attempt(pmt_):
        return _tvm_solve("i" if unknown == "rate" else unknown, n, i, pv, pmt_, fv, t)

    sol = attempt(pmt)
    if sol is None and unknown in ("n", "rate") and pmt:
        alt = attempt(-pmt)
        if alt is not None:
            notes.append("No solution with PMT's sign as entered — PV and PMT can't both be money "
                         "received. Solved with PMT as a payment OUT (sign flipped).")
            pmt, sol = -pmt, alt
    if sol is None:
        return {"error": ("No solution for these inputs. Check the signs: at least one of PV, PMT, "
                          "FV must be money paid out (−) and at least one money received (+)."),
                "unknown": unknown}
    if unknown == "rate":
        i = sol
        c = cpy or ppy
        rate = (c * ((1 + i) ** (ppy / c) - 1)) * 100.0
        vals["rate"] = rate
    else:
        vals[unknown] = sol
    n = vals["n"]
    pv, pmt, fv = vals["pv"], vals["pmt"], vals["fv"]
    total_pmts = pmt * n
    net = pv + total_pmts + fv     # >0: you came out ahead (interest earned); <0: interest paid
    # Balance path (from the holder of PV's perspective) by year — sign-normalized for display
    sched = []
    whole = int(math.ceil(n - 1e-9))
    if whole <= MAX_PERIODS:
        prev = pv
        acc_int = acc_pmt = 0.0
        # Display sign: a loan (PV received) shows the balance OWED and interest CHARGED as
        # positive; a savings/withdrawal plan (PV or first PMT paid in) shows the ACCOUNT VALUE
        # and interest EARNED as positive. Payments are shown as magnitudes.
        sgn = -1.0 if (pv < 0 or (pv == 0 and pmt < 0)) else 1.0
        for k in range(1, whole + 1):
            kk = min(k, n)
            bal = _tvm_eq(kk, i, pv, pmt, 0.0, t)   # PV and the PMTs so far, carried to period kk
            frac = kk - (k - 1)
            acc_pmt += pmt * frac
            acc_int += bal - prev - pmt * frac
            prev = bal
            if k % ppy == 0 or k == whole:
                sched.append({"period": k, "year": round(k / ppy, 2), "payments": _r2(abs(acc_pmt)),
                              "interest": _r2(sgn * acc_int), "balance": _r2(sgn * bal)})
                acc_int = acc_pmt = 0.0
        balance_meaning = "amount owed" if sgn > 0 else "account value"
    else:
        balance_meaning = None
    eff_annual = ((1 + i) ** ppy - 1) * 100.0
    out = {"calculator": "tvm", "solved_for": unknown, "solved_label": TVM_LABELS[unknown],
           "value": round(vals[unknown], 6 if unknown == "rate" else 4),
           "values": {k: (round(v, 6) if k == "rate" else _r2(v)) for k, v in vals.items()},
           "periods_per_year": ppy, "compounds_per_year": cpy or ppy,
           "timing": "begin" if t else "end",
           "periodic_rate_pct": round(i * 100.0, 6), "effective_annual_rate_pct": round(eff_annual, 4),
           "years": round(n / ppy, 3), "total_payments": _r2(total_pmts),
           "net_interest": _r2(net),
           "net_interest_meaning": ("interest earned (you received more than you paid in)" if net > 0.005
                                    else ("interest paid (you paid out more than you received)"
                                          if net < -0.005 else "no net interest")),
           "schedule": sched, "balance_meaning": balance_meaning, "notes": notes}
    if unknown == "n" and abs(n - round(n)) > 1e-6:
        out["notes"].append(f"N = {n:.2f}: {math.floor(n)} full payments plus a smaller final one "
                            f"({math.ceil(n)} payments in total).")
    return out


# ============================================================================ WHAT-IF PAYOFF
def whatif(inp):
    """'What if I paid more?' for one liability. Compares the CURRENT path (balance, APR, the
    actual payment) against a scenario with a one-time lump sum and/or extra $ every payment.

    Returns the delta in payments (and in time), interest saved (when APR > 0), and the new
    payoff date offset. Optional `target_months` solves the reverse question: the payment needed
    to be done in that many months. When a required fact is missing, `missing` names it so the UI
    can render just those fields."""
    missing = []
    try:
        bal = _num(inp, "balance", None, lo=0)
        apr = _num(inp, "apr", None, lo=0, hi=100, label="APR")
        pmt = _num(inp, "payment", None, lo=0)
        ppy = _freq(inp)
        lump = _num(inp, "lump_sum", 0.0, lo=0)
        lump_at = int(_num(inp, "lump_at_period", 1, lo=1, hi=MAX_PERIODS))
        extra = _num(inp, "extra", 0.0, lo=0)
        target = _num(inp, "target_months", None, lo=1, hi=MAX_PERIODS)
    except ValueError as e:
        return {"error": str(e)}
    if bal is None:
        missing.append("balance")
    if apr is None:
        missing.append("apr")
    if not pmt:
        missing.append("payment")
    if missing:
        return {"error": "Need " + ", ".join(missing) + " to run the comparison.",
                "missing": missing}
    if bal <= 0:
        return {"calculator": "whatif", "status": "paid", "note": "Balance is $0 — nothing to pay off."}
    i = periodic_rate(apr, ppy)
    base = amortize(bal, i, 1, payment=pmt)
    lumps = {lump_at: lump} if lump else {}
    scen = amortize(bal, i, 1, payment=pmt, extra=extra, lumps=lumps)

    def path(r):
        return {"resolved": r["resolved"], "periods": r["periods"],
                "label": _periods_label(r["periods"], ppy),
                "total_interest": _r2(r["total_interest"]), "total_paid": _r2(r["total_paid"])}
    out = {"calculator": "whatif", "periods_per_year": ppy, "balance": _r2(bal), "apr": apr,
           "payment": _r2(pmt), "first_period_interest": _r2(bal * i),
           "baseline": path(base), "scenario": path(scen),
           "scenario_inputs": {"lump_sum": _r2(lump), "lump_at_period": lump_at, "extra": _r2(extra)},
           "notes": []}
    if not base["resolved"]:
        out["notes"].append(f"At ${pmt:,.2f} the payment {'equals' if abs(pmt - bal * i) < 0.01 else 'is below'} "
                            f"the first period's interest (${bal * i:,.2f}) — on the current path this "
                            "balance never pays off.")
    if base["resolved"] and scen["resolved"]:
        out["periods_saved"] = base["periods"] - scen["periods"]
        out["time_saved_label"] = _periods_label(out["periods_saved"], ppy)
        out["months_saved"] = round(out["periods_saved"] * 12.0 / ppy, 1)
        if apr > 0:
            out["interest_saved"] = _r2(base["total_interest"] - scen["total_interest"])
            outlay = lump + extra * scen["periods"]
            if outlay > 0:
                out["return_on_extra_note"] = (f"Every extra dollar here earns a guaranteed {apr:g}% "
                                               "(less if this interest is tax-deductible for you, e.g. an "
                                               "itemized mortgage or student-loan interest) — the payoff "
                                               "'return' to weigh against investing it.")
    elif not base["resolved"] and scen["resolved"]:
        out["notes"].append("The extra payment turns a never-ending balance into one that pays off.")
    if apr == 0:
        out["notes"].append("0% APR: paying early saves no interest — it only frees the payment sooner. "
                            "Check when any promotional rate ends; deferred-interest promos charge "
                            "back-interest if not paid in full by the deadline.")
    if target:
        tn = int(round(target * ppy / 12.0))
        req = amort_payment(bal, i, tn)
        out["target"] = {"months": target, "periods": tn, "required_payment": _r2(req),
                         "extra_needed": _r2(max(0.0, req - pmt))}
    out["notes"].append("Assumes the APR and payment stay fixed and nothing new is charged. Card "
                        "interest accrues daily, so real figures differ by small amounts.")
    out["yearly_baseline"] = _yearly(base["rows"], ppy) if base["resolved"] else []
    out["yearly_scenario"] = _yearly(scen["rows"], ppy) if scen["resolved"] else []
    return out


# ============================================================================ DRIP
def drip(inp):
    """Dividend-reinvestment projection, month by month.

    Model (each step explicit, since DRIP calculators online mostly don't say):
      · price grows continuously at price_growth_pct/yr (monthly compounding of the annual rate)
      · the ANCHOR is the annual dividend PER SHARE (a stated dollar amount, far steadier than a
        yield that moves with the share price). It steps up once a year by dividend_growth_pct
        (default 0% = flat). Yield is only DERIVED: DPS ÷ price. If you give just a yield, the
        starting DPS is price × yield and everything after that is DPS-driven.
      · a dividend can't exceed what the stock can pay: if DPS ÷ price would pass the yield
        ceiling (max_yield_pct, default max(1.25 × today's yield, 8%)) the dividend is CUT to the
        ceiling and growth resumes from the cut level (a ratchet — it never snaps back)
      · dividends pay `frequency` times a year (12/4/2/1); each payment = shares × DPS/frequency
      · tax_rate_pct comes out of each dividend BEFORE reinvesting — in a taxable account,
        reinvested dividends are still taxed the year they're paid. Use 0 inside an IRA/401(k).
      · reinvestment buys at price × (1 − drip_discount_pct) (some company plans discount 1–5%)
      · monthly_contribution buys shares at that month's price; it can grow yearly
    The same path is re-run with dividends taken as cash (not reinvested, held uninvested) so
    the DRIP advantage is apples-to-apples."""
    pre_notes = []
    try:
        price0 = _num(inp, "share_price", None, lo=0.0001)
        init = _num(inp, "initial_investment", None, lo=0)
        sh0 = _num(inp, "shares", None, lo=0)
        y0 = _num(inp, "dividend_yield_pct", None, lo=0, hi=50, label="dividend yield")
        dps_in = _num(inp, "dividend_per_share", None, lo=0, label="annual dividend per share")
        # Any two of (starting value, shares, price) pin down the third — a user who knows "1,000
        # shares worth $34,185" shouldn't also have to type the price (2026-10-19, Jason).
        missing = []
        if price0 is None:
            if init and sh0:
                price0 = init / sh0
                pre_notes.append(f"Share price derived from starting investment ÷ shares = ${price0:,.2f}.")
            else:
                missing.append("share_price")
        if init is None and sh0 is None:
            missing.append("initial_investment")
        if dps_in is not None and price0:
            # DPS wins over a yield typed alongside it: dollars per share are the stable figure.
            if y0 is not None and abs(y0 - 100.0 * dps_in / price0) > 0.5:
                pre_notes.append("Yield was recomputed from the dividend per share ÷ price "
                                 f"({100.0 * dps_in / price0:.2f}%); the dividend per share was used.")
            y0 = 100.0 * dps_in / price0
        elif y0 is None:
            missing.append("dividend_per_share")
        if missing:
            return {"error": "Missing required input: " + ", ".join(missing), "missing": missing}
        if sh0 is not None and init is not None and inp.get("share_price") not in (None, "") \
                and abs(sh0 * price0 - init) > max(1.0, 0.01 * init):
            pre_notes.append(f"Shares × price = ${sh0 * price0:,.0f}, not the ${init:,.0f} starting "
                             "investment entered — the share count was used.")
        gp = _num(inp, "price_growth_pct", 0.0, lo=-50, hi=50)
        gd = _num(inp, "dividend_growth_pct", 0.0, lo=-50, hi=50)
        years = _num(inp, "years", required=True, lo=1, hi=80)
        contrib = _num(inp, "monthly_contribution", 0.0, lo=0)
        gc = _num(inp, "contribution_growth_pct", 0.0, lo=0, hi=50)
        fq = int(_num(inp, "frequency", 4, lo=1, hi=12))
        tax = _num(inp, "tax_rate_pct", 0.0, lo=0, hi=60)
        cap_in = _num(inp, "max_yield_pct", None, lo=1, hi=100, label="yield ceiling")
        disc = _num(inp, "drip_discount_pct", 0.0, lo=0, hi=20)
        infl = _num(inp, "inflation_pct", None, lo=-5, hi=30)
    except _Missing as m:
        return {"error": f"Missing required input: {m}", "missing": [str(m)]}
    except ValueError as e:
        return {"error": str(e)}
    if 12 % fq:
        return {"error": "Dividend frequency must be 12, 4, 2 or 1 payments per year."}
    shares0 = sh0 if sh0 is not None else init / price0
    invested0 = shares0 * price0
    months = int(round(years * 12))
    step = 12 // fq
    dps0 = dps_in if dps_in is not None else price0 * y0 / 100.0   # annual dividend per share today
    ycap = cap_in if cap_in is not None else max(1.25 * y0, 8.0)

    def sim(reinvest):
        sh, cash = shares0, 0.0
        tot_gross = tot_tax = tot_contrib = 0.0
        yearly = []
        dps_cur, cut_year, cut_total = dps0, None, 0.0
        for m in range(1, months + 1):
            yi = (m - 1) // 12
            price = price0 * (1 + gp / 100.0) ** (m / 12.0)
            c = contrib * (1 + gc / 100.0) ** yi
            if c:
                sh += c / price
                tot_contrib += c
            if m > 1 and (m - 1) % 12 == 0:
                dps_cur *= 1 + gd / 100.0            # the annual raise (or trim) lands in month 1
            lim = ycap / 100.0 * price               # most the market lets this stock pay
            if dps_cur > lim:
                cut_total += dps_cur - lim
                dps_cur = lim
                if cut_year is None:
                    cut_year = yi + 1
            dps = dps_cur
            div_g = div_n = 0.0
            if m % step == 0:
                div_g = sh * dps / fq
                div_n = div_g * (1 - tax / 100.0)
                tot_gross += div_g
                tot_tax += div_g - div_n
                if reinvest:
                    sh += div_n / (price * (1 - disc / 100.0))
                else:
                    cash += div_n
            if (m - 1) % 12 == 0:
                yearly.append({"year": yi + 1, "contributions": 0.0, "dividends_net": 0.0})
            yearly[-1]["contributions"] += c
            yearly[-1]["dividends_net"] += div_n
            if m % 12 == 0 or m == months:
                yearly[-1].update({"shares": round(sh, 4), "price": _r2(price),
                                   "value": _r2(sh * price + cash), "cash": _r2(cash),
                                   "dps": round(dps_cur, 4), "annual_income": _r2(sh * dps_cur)})
        price_end = price0 * (1 + gp / 100.0) ** (months / 12.0)
        dps_end = dps_cur
        for y in yearly:
            y["contributions"] = _r2(y["contributions"])
            y["dividends_net"] = _r2(y["dividends_net"])
        return {"shares": sh, "cash": cash, "value": sh * price_end + cash, "price_end": price_end,
                "dps_end": dps_end, "cut_year": cut_year, "gross": tot_gross, "tax": tot_tax, "contrib": tot_contrib,
                "yearly": yearly}
    d, c = sim(True), sim(False)
    total_in = invested0 + d["contrib"]
    # Chart series (2026-10-19): per year, the dividends-as-cash path and cumulative money put in,
    # alongside the DRIP value already on each row — one axis, all dollars.
    cum = invested0
    for i, row in enumerate(d["yearly"]):
        cum += row["contributions"] or 0.0
        row["invested_cum"] = _r2(cum)
        row["cash_path_value"] = c["yearly"][i]["value"] if i < len(c["yearly"]) else None
    # Sanity guard (2026-10-19, found by Jason's MFA test): with dividend growth well above price
    # growth, the implied yield compounds without limit — a price falling 15%/yr against a
    # dividend falling 4%/yr reaches a 200%+ yield and DRIP "buys" billions of dollars. That is
    # arithmetic, not a forecast: in reality the dividend gets cut. Say so loudly.
    warnings = []
    end_yield = 100.0 * d["dps_end"] / d["price_end"] if d["price_end"] else None
    if d["cut_year"]:
        warnings.append(f"Dividend cut applied in year {d['cut_year']}: with the share price falling, "
                        f"the fixed ${dps0:,.2f}/share would have pushed the yield past the "
                        f"{ycap:.1f}% ceiling, which real payers don't sustain. The dividend was "
                        "reduced to the ceiling and grows from there. Raise the ceiling only if you "
                        "have a reason to believe the payout is covered.")
    if y0 >= 10:
        warnings.append(f"A {y0:.1f}% yield usually means the market expects a cut or the payout "
                        "includes return of capital. Check the payout's coverage (earnings or "
                        "distributable income vs. the dividend) before trusting a steady-dividend "
                        "projection.")
    # Implied annual return (money-weighted) — the reality check on the whole projection. A
    # DRIP path that compounds far above what any market has delivered is the inputs talking,
    # not a forecast, whatever the arithmetic says.
    def _npv(rate):
        mr = (1 + rate) ** (1 / 12.0)
        v = -invested0 * mr ** months
        for k in range(1, months + 1):
            v -= contrib * (1 + gc / 100.0) ** ((k - 1) // 12) * mr ** (months - k)
        return v + d["value"]
    lo_r, hi_r = -0.95, 5.0
    implied = None
    if _npv(lo_r) > 0 > _npv(hi_r):
        for _ in range(80):
            mid = (lo_r + hi_r) / 2
            lo_r, hi_r = (mid, hi_r) if _npv(mid) > 0 else (lo_r, mid)
        implied = 100.0 * (lo_r + hi_r) / 2
    if implied is not None and implied > 12.0:
        tag = "Implausible" if implied > 20.0 else "Optimistic"
        warnings.append(f"{tag} projection: these inputs imply about {implied:.0f}%/yr growth for "
                        f"{years:g} years (the broad US stock market has returned roughly 10%/yr "
                        "nominal over the long run). A very high yield normally comes with a falling "
                        "price or cuts to the dividend — enter a negative price growth and/or "
                        "dividend growth from the stock's own history, and treat this result as a "
                        "ceiling, not an expectation.")
    income_end = d["shares"] * d["dps_end"]
    out = {"calculator": "drip", "starting_shares": round(shares0, 4), "starting_value": _r2(invested0),
           "final_value": _r2(d["value"]), "final_shares": round(d["shares"], 4),
           "final_price": _r2(d["price_end"]), "total_contributions": _r2(d["contrib"]),
           "total_invested": _r2(total_in), "total_dividends_gross": _r2(d["gross"]),
           "total_dividend_tax": _r2(d["tax"]), "total_dividends_net": _r2(d["gross"] - d["tax"]),
           "annual_income_at_end": _r2(income_end),
           "monthly_income_at_end": _r2(income_end / 12.0),
           "yield_on_cost_pct": _r2(100.0 * income_end / total_in) if total_in else None,
           "without_drip": {"final_value": _r2(c["value"]), "shares": round(c["shares"], 4),
                            "cash_dividends_collected": _r2(c["cash"]),
                            "annual_income_at_end": _r2(c["shares"] * c["dps_end"])},
           "drip_advantage": _r2(d["value"] - c["value"]),
           "extra_shares_from_drip": round(d["shares"] - c["shares"], 4),
           "yearly": d["yearly"],
           "share_price_used": _r2(price0), "dividend_yield_used_pct": round(y0, 4),
           "dividend_per_share_used": round(dps0, 4), "dividend_per_share_end": round(d["dps_end"], 4),
           "max_yield_pct_used": round(ycap, 2), "dividend_cut_year": d["cut_year"],
           "implied_return_pct": round(implied, 2) if implied is not None else None,
           "end_yield_pct": round(end_yield, 2) if end_yield is not None else None,
           "warnings": warnings,
           "assumptions": {"price_growth_pct": gp, "dividend_growth_pct": gd, "frequency": fq,
                           "tax_rate_pct": tax, "drip_discount_pct": disc},
           "notes": pre_notes + ["Steady growth rates are a simplification — real dividends get cut and prices "
                     "swing; a falling price actually buys MORE shares per reinvested dollar.",
                     "Without DRIP, cash dividends are assumed held as uninvested cash (no interest)."]}
    if total_in and years:
        cagr = ((d["value"] / total_in) ** (1 / years) - 1) * 100.0 if not d["contrib"] else None
        out["cagr_pct"] = _r2(cagr) if cagr is not None else None
    if infl is not None:
        out["final_value_real"] = _r2(d["value"] / (1 + infl / 100.0) ** years)
        out["annual_income_at_end_real"] = _r2(income_end / (1 + infl / 100.0) ** years)
    return out


def dividend_profile(price, dividends, closes=None, now=None):
    """Summarize a ticker's dividend history into DRIP-calculator inputs. Pure: the app fetches
    the raw series (Yahoo chart API: split-adjusted dividend events + monthly closes) and hands
    them here, so the arithmetic is unit-tested offline.

    price      current share price
    dividends  [(epoch_seconds, amount_per_share), ...]
    closes     [(epoch_seconds, close), ...] monthly, for trailing price CAGRs (optional)

    frequency          payments in the last 24 months ÷ 2, snapped to 12/4/2/1
    ttm_dividend       sum of payments in the last 365 days
    dividend_yield_pct TTM ÷ price (what the market quotes as trailing yield)
    forward_yield_pct  latest payment × frequency ÷ price (catches a recent raise/cut)
    dividend_growth_Ny CAGR of CALENDAR-YEAR totals, last full year vs N years earlier, each
                       year normalized to `frequency` payments so a December payment that slips
                       into January can't fake a jump or a cut.
    price_cagr_Ny      trailing price-only CAGR (dividends excluded — the DRIP model adds them)"""
    now = now or datetime.datetime.now(datetime.timezone.utc).timestamp()
    yr = 365.25 * 86400
    divs = sorted((int(t), float(a)) for t, a in (dividends or []) if a and float(a) > 0)
    out = {"price": _r2(price), "payments_on_record": len(divs), "notes": []}
    if not price or price <= 0:
        out["error"] = "No current price."
        return out
    n24 = sum(1 for t, _ in divs if now - t <= 2 * yr)
    ttm = [a for t, a in divs if now - t <= yr]
    if not ttm:
        out.update({"pays_dividend": False, "dividend_yield_pct": 0.0, "ttm_dividend": 0.0,
                    "frequency": None})
        out["notes"].append("No dividend paid in the last 12 months.")
    else:
        per = n24 / 2.0 if n24 >= len(ttm) else float(len(ttm))
        freq = min((12, 4, 2, 1), key=lambda f: abs(f - per))
        ttm_sum = sum(ttm)
        out.update({"pays_dividend": True, "frequency": freq, "ttm_dividend": round(ttm_sum, 4),
                    "last_payment": round(divs[-1][1], 4),
                    "last_payment_date": datetime.datetime.fromtimestamp(divs[-1][0], datetime.timezone.utc).strftime("%Y-%m-%d"),
                    "dividend_yield_pct": round(100.0 * ttm_sum / price, 3),
                    "forward_yield_pct": round(100.0 * divs[-1][1] * freq / price, 3)})
        by_year = {}
        for t, a in divs:
            y = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).year
            tot = by_year.setdefault(y, [0.0, 0])
            tot[0] += a
            tot[1] += 1
        last_full = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).year - 1

        def norm(y):
            v = by_year.get(y)
            if not v or v[1] < max(1, freq - 1):     # partial year (fund launched mid-year): skip
                return None
            return v[0] * freq / v[1]
        for n in (3, 5, 10):
            a, b = norm(last_full), norm(last_full - n)
            if a and b:
                out[f"dividend_growth_{n}y_pct"] = round(((a / b) ** (1.0 / n) - 1) * 100.0, 2)
    cl = sorted((int(t), float(c)) for t, c in (closes or []) if c)
    for n in (5, 10):
        target = now - n * yr
        if cl and cl[0][0] <= target + 45 * 86400:
            past = min(cl, key=lambda tc: abs(tc[0] - target))[1]
            if past > 0:
                out[f"price_cagr_{n}y_pct"] = round(((price / past) ** (1.0 / n) - 1) * 100.0, 2)
    out["notes"].append("Trailing figures, split-adjusted. Past dividend and price growth are a "
                        "starting point for the assumptions, not a forecast.")
    return out


# ============================================================================ RENT vs BUY
def rent_vs_buy(inp):
    """Wealth at the horizon under each choice, with the money each side does NOT spend invested.

    Buyer: pays down payment + closing up front, then P&I, property tax (as % of the current
    value), insurance, HOA, maintenance (% of value), PMI while LTV > 80%. At the horizon, sells:
    value − balance − selling costs.
    Renter: invests what the buyer spent up front, pays rent (+ renter's insurance), and each
    month invests the difference whenever owning costs more than renting (the buyer invests the
    difference in the months renting costs more). Both portfolios earn invest_return_pct.
    The answer is the year in which buying first comes out ahead, if it does."""
    try:
        price = _num(inp, "price", required=True, lo=1)
        down = _num(inp, "down_payment", None, lo=0)
        dpct = _num(inp, "down_pct", None, lo=0, hi=100)
        if down is None:
            down = price * (dpct if dpct is not None else 20.0) / 100.0
        apr = _num(inp, "apr", required=True, lo=0, hi=30, label="APR")
        ty = _num(inp, "term_years", 30, lo=1, hi=50)
        tax_pct = _num(inp, "property_tax_pct", 1.0, lo=0, hi=10)
        ins = _num(inp, "insurance_annual", 0.0, lo=0)
        g_ins = _num(inp, "insurance_growth_pct", 3.0, lo=-10, hi=30)
        hoa = _num(inp, "hoa_monthly", 0.0, lo=0)
        maint = _num(inp, "maintenance_pct", 1.0, lo=0, hi=10)
        pmi_pct = _num(inp, "pmi_pct", 0.0, lo=0, hi=5)
        closing_pct = _num(inp, "closing_cost_pct", 3.0, lo=0, hi=15)
        sell_pct = _num(inp, "selling_cost_pct", 6.0, lo=0, hi=20)
        app = _num(inp, "appreciation_pct", 3.0, lo=-20, hi=30)
        rent = _num(inp, "rent_monthly", required=True, lo=0)
        g_rent = _num(inp, "rent_growth_pct", 3.0, lo=-10, hi=30)
        rins = _num(inp, "renters_insurance_annual", 0.0, lo=0)
        ret = _num(inp, "invest_return_pct", 6.0, lo=-20, hi=30)
        horizon = _num(inp, "horizon_years", 10, lo=1, hi=50)
    except _Missing as m:
        return {"error": f"Missing required input: {m}", "missing": [str(m)]}
    except ValueError as e:
        return {"error": str(e)}
    loan_amt = max(0.0, price - down)
    i = periodic_rate(apr, 12)
    n = int(round(ty * 12))
    pi = amort_payment(loan_amt, i, n) if loan_amt else 0.0
    closing = price * closing_pct / 100.0
    rm = (1 + ret / 100.0) ** (1 / 12.0) - 1
    bal, val = loan_amt, price
    renter_port, owner_port = down + closing, 0.0
    tot_own = tot_rent = 0.0
    rows, breakeven = [], None
    H = int(round(horizon * 12))
    for k in range(1, H + 1):
        y = (k - 1) // 12
        interest = bal * i
        pay = min(pi, bal + interest) if bal > 0 else 0.0
        bal = max(0.0, bal + interest - pay)
        pmi = loan_amt * pmi_pct / 100.0 / 12.0 if (bal / price > 0.8 and pmi_pct) else 0.0
        own = pay + pmi + val * tax_pct / 100.0 / 12.0 + ins * (1 + g_ins / 100.0) ** y / 12.0 \
            + hoa + val * maint / 100.0 / 12.0
        rnt = rent * (1 + g_rent / 100.0) ** y + rins / 12.0
        tot_own += own
        tot_rent += rnt
        renter_port *= 1 + rm
        owner_port *= 1 + rm
        if own > rnt:
            renter_port += own - rnt
        else:
            owner_port += rnt - own
        val *= (1 + app / 100.0) ** (1 / 12.0)
        if k % 12 == 0 or k == H:
            ow = val - bal - val * sell_pct / 100.0 + owner_port
            rows.append({"year": round(k / 12.0, 2), "owner_cost_cum": _r2(tot_own),
                         "renter_cost_cum": _r2(tot_rent), "home_value": _r2(val),
                         "loan_balance": _r2(bal), "owner_wealth": _r2(ow),
                         "renter_wealth": _r2(renter_port), "buy_advantage": _r2(ow - renter_port)})
            if breakeven is None and ow >= renter_port:
                breakeven = round(k / 12.0, 2)
    last = rows[-1]
    out = {"calculator": "rent_vs_buy", "loan_amount": _r2(loan_amt), "monthly_pi": _r2(pi),
           "first_month_owner_cost": None, "closing_costs": _r2(closing),
           "horizon_years": horizon, "owner_wealth": last["owner_wealth"],
           "renter_wealth": last["renter_wealth"], "buy_advantage": last["buy_advantage"],
           "verdict": ("BUY comes out ahead" if last["buy_advantage"] > 0 else "RENT comes out ahead")
           + f" by ${abs(last['buy_advantage']):,.0f} after {horizon:g} years",
           "breakeven_year": breakeven, "yearly": rows,
           "assumptions": {"appreciation_pct": app, "rent_growth_pct": g_rent,
                           "invest_return_pct": ret, "maintenance_pct": maint,
                           "closing_cost_pct": closing_pct, "selling_cost_pct": sell_pct,
                           "property_tax_pct": tax_pct, "insurance_growth_pct": g_ins},
           "notes": ["Taxes are ignored on both sides: the home-sale gain exclusion ($250k single / "
                     "$500k MFJ) usually shields the buyer, while the renter's portfolio gains would be "
                     "taxable outside retirement accounts — so this leans slightly toward renting.",
                     "Most sensitive inputs: appreciation, investment return, and horizon. Short "
                     "horizons favor renting because buying and selling costs ~9% round-trip."]}
    out["first_month_owner_cost"] = _r2(pi + (loan_amt * pmi_pct / 1200.0 if loan_amt / price > 0.8 else 0)
                                        + price * tax_pct / 1200.0 + ins / 12.0 + hoa + price * maint / 1200.0)
    return out


# ============================================================================ AFFORDABILITY
def affordability(inp):
    """How much house the 28/36 debt-to-income rule supports. Front-end: housing (PITI + PMI +
    HOA) ≤ front_pct of gross monthly income. Back-end: housing + other monthly debt payments ≤
    back_pct. The lower of the two binds. Lenders may approve higher DTIs (often to ~43–50%) —
    approval is not the same thing as comfortable, which is why the ratios are editable."""
    try:
        income = _num(inp, "annual_income", required=True, lo=1, label="gross annual income")
        debts = _num(inp, "monthly_debts", 0.0, lo=0)
        down = _num(inp, "down_payment", required=True, lo=0)
        apr = _num(inp, "apr", required=True, lo=0, hi=30, label="APR")
        ty = _num(inp, "term_years", 30, lo=1, hi=50)
        tax_pct = _num(inp, "property_tax_pct", None, lo=0, hi=10)
        ins = _num(inp, "insurance_annual", 0.0, lo=0)
        hoa = _num(inp, "hoa_monthly", 0.0, lo=0)
        pmi_pct = _num(inp, "pmi_pct", 0.0, lo=0, hi=5)
        front = _num(inp, "front_pct", 28.0, lo=1, hi=80)
        back = _num(inp, "back_pct", 36.0, lo=1, hi=80)
    except _Missing as m:
        return {"error": f"Missing required input: {m}", "missing": [str(m)]}
    except ValueError as e:
        return {"error": str(e)}
    gm = income / 12.0
    cap_front = gm * front / 100.0
    cap_back = gm * back / 100.0 - debts
    cap = min(cap_front, cap_back)
    binding = "front-end (housing)" if cap_front <= cap_back else "back-end (total debt)"
    i = periodic_rate(apr, 12)
    n = int(round(ty * 12))
    tp = (tax_pct or 0.0) / 100.0

    def cost(P):
        L = max(0.0, P - down)
        pmi = L * pmi_pct / 1200.0 if (P > 0 and L / P > 0.8) else 0.0
        return amort_payment(L, i, n) + P * tp / 12.0 + ins / 12.0 + hoa + pmi, pmi
    warnings = []
    if tax_pct is None:
        warnings.append("No property-tax rate entered — taxes are NOT in the payment, so the price "
                        "below is overstated. Add your county's effective rate.")
    if cap <= cost(down)[0]:
        return {"calculator": "affordability", "max_price": None, "max_housing_payment": _r2(cap),
                "binding_constraint": binding, "warnings": warnings,
                "error": "At these ratios, existing debts and fixed housing costs leave no room for a mortgage."}
    lo, hi = down, down + income * 50
    for _ in range(200):
        mid = (lo + hi) / 2
        if cost(mid)[0] > cap:
            hi = mid
        else:
            lo = mid
    P = lo
    tot, pmi = cost(P)
    L = P - down
    return {"calculator": "affordability", "max_price": _r2(P), "loan_amount": _r2(L),
            "down_pct": _r2(100.0 * down / P) if P else None,
            "max_housing_payment": _r2(cap), "binding_constraint": binding,
            "caps": {"front_end": _r2(cap_front), "back_end_less_debts": _r2(cap_back)},
            "payment": {"principal_interest": _r2(amort_payment(L, i, n)), "property_tax": _r2(P * tp / 12.0),
                        "insurance": _r2(ins / 12.0), "hoa": _r2(hoa), "pmi": _r2(pmi), "total": _r2(tot)},
            "dti": {"front_pct": _r2(100.0 * tot / gm), "back_pct": _r2(100.0 * (tot + debts) / gm)},
            "warnings": warnings + ([f"Down payment is {100.0 * down / P:.1f}% (< 20%) and no PMI rate "
                                     "entered — PMI is not included."] if (L / P > 0.8 and not pmi_pct) else []),
            "notes": ["28/36 is a conventional guideline, not a lender's decision. Leave room for "
                      "maintenance (~1%/yr of value), which no DTI ratio counts."]}


# ============================================================================ POINTS
def points(inp):
    """Should I buy discount points? One point = 1% of the loan, paid up front for a lower rate.
    Two break-evens: the simple one (cost ÷ monthly payment savings) and the true one, which
    also credits the lower rate's faster principal paydown — the month where cumulative payment
    savings + the balance difference first cover the cost. If you'll sell or refinance before
    that month, the points lose money."""
    try:
        L = _num(inp, "loan_amount", required=True, lo=1)
        ty = _num(inp, "term_years", 30, lo=1, hi=50)
        r0 = _num(inp, "apr_base", required=True, lo=0, hi=30, label="rate without points")
        r1 = _num(inp, "apr_points", required=True, lo=0, hi=30, label="rate with points")
        pts = _num(inp, "points", None, lo=0, hi=10)
        cost = _num(inp, "points_cost", None, lo=0)
        hold = _num(inp, "hold_years", None, lo=0.1, hi=50)
    except _Missing as m:
        return {"error": f"Missing required input: {m}", "missing": [str(m)]}
    except ValueError as e:
        return {"error": str(e)}
    if cost is None:
        if pts is None:
            return {"error": "Enter the number of points or their dollar cost.", "missing": ["points"]}
        cost = L * pts / 100.0
    if r1 >= r0:
        return {"error": "The rate with points must be lower than the rate without."}
    n = int(round(ty * 12))
    a = amortize(L, periodic_rate(r0, 12), n)
    b = amortize(L, periodic_rate(r1, 12), n)
    p0, p1 = a["scheduled_payment"], b["scheduled_payment"]
    save = p0 - p1
    true_be = None
    cum = 0.0
    for k in range(1, n + 1):
        cum += save
        if cum + (a["rows"][k - 1]["balance"] - b["rows"][k - 1]["balance"]) >= cost:
            true_be = k
            break
    out = {"calculator": "points", "points_cost": _r2(cost), "payment_without": _r2(p0),
           "payment_with": _r2(p1), "monthly_savings": _r2(save),
           "simple_breakeven_months": round(cost / save, 1) if save > 0 else None,
           "true_breakeven_months": true_be,
           "interest_saved_full_term": _r2(a["total_interest"] - b["total_interest"]),
           "net_saved_full_term": _r2(a["total_interest"] - b["total_interest"] - cost),
           "notes": ["Points may be tax-deductible as mortgage interest for itemizers on a purchase "
                     "(refi points generally amortize over the loan) — ignored here.",
                     "The money spent on points could instead go to a larger down payment or be "
                     "invested; that opportunity cost is not modeled."]}
    if hold:
        h = min(n, int(round(hold * 12)))
        out["at_hold"] = {"months": h,
                          "net_benefit": _r2(save * h + a["rows"][h - 1]["balance"] - b["rows"][h - 1]["balance"] - cost)}
    return out


# ============================================================================ YIELDS / TAX-EQUIVALENT
def yields(inp):
    """After-tax yield comparison across how interest is taxed:
      fully taxable (CDs, savings, corporates) · Treasuries (state-exempt) ·
      in-state munis (exempt both) · out-of-state munis (federal-exempt only).
    Plus each one's TAXABLE-EQUIVALENT yield — what a fully taxable bond must pay to match it.
    Also converts APR ↔ APY at a given compounding frequency."""
    try:
        f = _num(inp, "fed_rate_pct", None, lo=0, hi=60, label="federal marginal rate")
        s = _num(inp, "state_rate_pct", 0.0, lo=0, hi=20, label="state marginal rate")
        niit = 3.8 if str(inp.get("niit", False)).lower() in ("true", "1", "yes", "on") else 0.0
        ys = {k: _num(inp, k, None, lo=0, hi=50) for k in
              ("taxable_yield", "treasury_yield", "muni_instate_yield", "muni_outstate_yield")}
        apr = _num(inp, "apr_pct", None, lo=0, hi=200)
        apy = _num(inp, "apy_pct", None, lo=0, hi=1000)
        cpy = _num(inp, "compounds_per_year", 12, lo=1, hi=365)
    except ValueError as e:
        return {"error": str(e)}
    out = {"calculator": "yields", "rows": [], "notes": []}
    if any(v is not None for v in ys.values()):
        if f is None:
            return {"error": "Enter your federal marginal rate to compare after-tax yields.",
                    "missing": ["fed_rate_pct"]}
        full = (f + s + niit) / 100.0
        spec = [("taxable_yield", "Fully taxable (CD, savings, corporate)", f + s + niit, False),
                ("treasury_yield", "Treasury (state-exempt)", f + niit, False),
                ("muni_instate_yield", "In-state municipal (fed + state exempt)", 0.0, True),
                ("muni_outstate_yield", "Out-of-state municipal (fed-exempt)", s, True)]
        for key, label, rate, muni in spec:
            y = ys[key]
            if y is None:
                continue
            at = y * (1 - rate / 100.0)
            out["rows"].append({"key": key, "label": label, "yield_pct": y,
                                "tax_rate_pct": round(rate, 2), "after_tax_pct": round(at, 3),
                                "taxable_equivalent_pct": round(at / (1 - full), 3) if full < 1 else None})
        out["rows"].sort(key=lambda r: -r["after_tax_pct"])
        if out["rows"]:
            out["best"] = out["rows"][0]["label"]
        out["combined_rate_pct"] = round(full * 100, 2)
        out["notes"].append("Rates are added (federal + state + NIIT). Itemizers who deduct state tax "
                            "federally face slightly less; the SALT cap makes that rare. Muni AMT "
                            "exposure (private-activity bonds) and de minimis discount rules are ignored.")
    if apr is not None:
        out["apy_from_apr"] = round(((1 + apr / 100.0 / cpy) ** cpy - 1) * 100.0, 4)
    if apy is not None:
        out["apr_from_apy"] = round(cpy * ((1 + apy / 100.0) ** (1 / cpy) - 1) * 100.0, 4)
    out["compounds_per_year"] = cpy
    if not out["rows"] and apr is None and apy is None:
        return {"error": "Enter at least one yield to compare, or an APR/APY to convert."}
    return out


# ============================================================================ INFLATION
def inflation(inp):
    """Purchasing power over time, and nominal → real returns (Fisher: (1+n)/(1+i) − 1, not the
    n − i shortcut, which overstates real returns at higher rates)."""
    try:
        amt = _num(inp, "amount", required=True, lo=0)
        yrs = _num(inp, "years", required=True, lo=0, hi=100)
        inf = _num(inp, "inflation_pct", required=True, lo=-10, hi=50, label="inflation rate")
        nom = _num(inp, "nominal_return_pct", None, lo=-50, hi=100)
    except _Missing as m:
        return {"error": f"Missing required input: {m}", "missing": [str(m)]}
    except ValueError as e:
        return {"error": str(e)}
    g = (1 + inf / 100.0) ** yrs
    out = {"calculator": "inflation", "future_cost": _r2(amt * g),
           "purchasing_power_of_amount": _r2(amt / g),
           "cumulative_inflation_pct": _r2((g - 1) * 100.0),
           "halving_years": round(math.log(2) / math.log(1 + inf / 100.0), 1) if inf > 0 else None,
           "yearly": [{"year": y, "future_cost": _r2(amt * (1 + inf / 100.0) ** y),
                       "purchasing_power": _r2(amt / (1 + inf / 100.0) ** y)}
                      for y in range(1, int(yrs) + 1)]}
    if nom is not None:
        real = ((1 + nom / 100.0) / (1 + inf / 100.0) - 1) * 100.0
        out["real_return_pct"] = round(real, 3)
        out["nominal_future_value"] = _r2(amt * (1 + nom / 100.0) ** yrs)
        out["real_future_value"] = _r2(amt * (1 + real / 100.0) ** yrs)
    return out


# ============================================================================ LUMP SUM vs ANNUITY
def lump_vs_annuity(inp):
    """Pension / lottery / structured-settlement choice: a lump sum today vs a stream of payments.
    Reports the stream's present value at your discount rate (what you'd expect to earn investing
    the lump sum), the annuity's IMPLIED return (the rate at which the two are equal — if you
    can't reliably beat it investing, the annuity wins on math), and the nominal break-even year.
    Longevity is the hidden variable: run it at your life expectancy AND at +10 years."""
    try:
        lump = _num(inp, "lump_sum", required=True, lo=1)
        pay = _num(inp, "payment", required=True, lo=0.01)
        ppy = _freq(inp)
        years = _num(inp, "years", required=True, lo=0.5, hi=80, label="years of payments")
        cola = _num(inp, "cola_pct", 0.0, lo=-5, hi=15)
        delay = _num(inp, "start_delay_years", 0.0, lo=0, hi=40)
        disc = _num(inp, "discount_rate_pct", None, lo=-5, hi=30)
    except _Missing as m:
        return {"error": f"Missing required input: {m}", "missing": [str(m)]}
    except ValueError as e:
        return {"error": str(e)}
    N = int(round(years * ppy))
    d0 = int(round(delay * ppy))
    flows = [(d0 + k, pay * (1 + cola / 100.0) ** ((k - 1) // ppy)) for k in range(1, N + 1)]
    total = sum(c for _, c in flows)

    def pv(annual_pct):
        i = (1 + annual_pct / 100.0) ** (1.0 / ppy) - 1
        return sum(c / (1 + i) ** t for t, c in flows)
    implied = _solve_root(lambda r: pv(r * 100.0) - lump, -0.5, 1.0)
    cum, be = 0.0, None
    for t, c in flows:
        cum += c
        if cum >= lump:
            be = round(t / ppy, 2)
            break
    out = {"calculator": "lump_vs_annuity", "total_payments": _r2(total), "payments": N,
           "implied_return_pct": round(implied * 100.0, 3) if implied is not None else None,
           "nominal_breakeven_years": be,
           "notes": ["Taxes ignored: a lump sum can usually be rolled to an IRA tax-deferred; the "
                     "annuity is taxed as received. A pension annuity also carries the plan/insurer's "
                     "credit risk (PBGC guarantees are capped), while a lump sum carries market and "
                     "longevity risk.",
                     "Implied return uses annual compounding converted to the payment frequency."]}
    if disc is not None:
        v = pv(disc)
        out["annuity_present_value"] = _r2(v)
        out["discount_rate_pct"] = disc
        out["better"] = "annuity" if v > lump else "lump sum"
        out["advantage"] = _r2(abs(v - lump))
    return out


# ============================================================================ NPV / IRR
def _parse_flows(v):
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple)):
        return [float(str(x).replace(",", "").replace("$", "")) for x in v if str(x).strip() != ""]
    parts = [p.strip().replace("$", "") for p in str(v).replace("\n", ",").replace(";", ",").split(",")]
    return [float(p) for p in parts if p]


def npv_irr(inp):
    """Net present value and internal rate of return of a cash-flow series: an initial cost now,
    then one cash flow per period (year by default). Use for a rental, solar panels, a business
    purchase, a renovation — anything that costs now and pays later. IRR is solved numerically;
    a series that changes sign more than once can have several IRRs (flagged)."""
    try:
        init = _num(inp, "initial_investment", required=True, lo=0)
        rate = _num(inp, "discount_rate_pct", None, lo=-50, hi=100)
        flows = _parse_flows(inp.get("cash_flows"))
    except _Missing as m:
        return {"error": f"Missing required input: {m}", "missing": [str(m)]}
    except (ValueError, TypeError) as e:
        return {"error": f"Cash flows must be numbers separated by commas ({e})"}
    if not flows:
        return {"error": "Enter at least one future cash flow.", "missing": ["cash_flows"]}
    cf = [-init] + flows

    def npv(r):
        return sum(c / (1 + r) ** t for t, c in enumerate(cf))
    irr = _solve_root(npv, -0.99, 10.0)
    if irr is not None and abs(npv(irr)) > 1e-4 * max(1.0, init):
        irr = None
    sign_changes = sum(1 for a, b in zip(cf, cf[1:]) if a and b and (a > 0) != (b > 0))
    cum, payback = 0.0, None
    for t, c in enumerate(cf):
        prev = cum
        cum += c
        if t and payback is None and cum >= 0 and c > 0:
            payback = round(t - 1 + (-prev / c), 2)
    out = {"calculator": "npv_irr", "cash_flows": cf, "total_return": _r2(sum(cf)),
           "irr_pct": round(irr * 100.0, 3) if irr is not None else None,
           "payback_periods": payback, "multiple_irr_possible": sign_changes > 1,
           "notes": ["Cash flows are end-of-period; period 0 is the initial cost."]}
    if rate is not None:
        r = rate / 100.0
        out["npv"] = _r2(npv(r))
        out["discount_rate_pct"] = rate
        dc, dpb = 0.0, None
        for t, c in enumerate(cf):
            prev = dc
            dc += c / (1 + r) ** t
            if t and dpb is None and dc >= 0:
                pv_c = c / (1 + r) ** t
                dpb = round(t - 1 + (-prev / pv_c), 2) if pv_c > 0 else t
        out["discounted_payback_periods"] = dpb
        out["verdict"] = ("Positive NPV — beats the discount rate" if out["npv"] > 0
                          else "Negative NPV — earns less than the discount rate")
    return out


# ============================================================================ REGISTRY
CALCULATORS = {
    "mortgage":        (mortgage, "Mortgage · true cost of ownership"),
    "loan":            (loan, "Loan (auto, personal, student…)"),
    "land_loan":       (land_loan, "Land loan (balloon / interest-only)"),
    "drip":            (drip, "Dividend reinvestment (DRIP)"),
    "tvm":             (tvm, "Time-value-of-money solver (N, I/Y, PV, PMT, FV)"),
    "whatif":          (whatif, "What-if payoff (extra / lump-sum payments)"),
    "rent_vs_buy":     (rent_vs_buy, "Rent vs buy"),
    "affordability":   (affordability, "Home affordability (28/36)"),
    "points":          (points, "Mortgage points break-even"),
    "yields":          (yields, "After-tax / tax-equivalent yield, APR↔APY"),
    "inflation":       (inflation, "Inflation & real return"),
    "lump_vs_annuity": (lump_vs_annuity, "Lump sum vs annuity (pension)"),
    "npv_irr":         (npv_irr, "NPV / IRR of a cash-flow series"),
}


def run(name, inputs, include_schedule=True):
    """Dispatch by name. Never raises: an unknown name or an unexpected failure comes back as
    {"error": ...} so neither the API nor the chat tool loop can be taken down by a bad input."""
    ent = CALCULATORS.get(str(name or "").strip().lower())
    if not ent:
        return {"error": f"Unknown calculator {name!r}. Available: {', '.join(CALCULATORS)}"}
    if not isinstance(inputs, dict):
        return {"error": "inputs must be an object of field: value"}
    try:
        res = ent[0](inputs)
    except Exception as e:      # defensive: a math edge case must not 500 the panel
        return {"error": f"{name} failed: {type(e).__name__}: {e}"}
    if not include_schedule and isinstance(res, dict):
        res.pop("schedule", None)
    return res
