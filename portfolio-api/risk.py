"""
risk.py — tail & co-movement risk suite for the Fiduciary (Fable G4).

The existing realized-risk panel (vol / Sharpe / Sortino / max-drawdown in app.py's
_risk_metrics) sees a concentrated growth book's DISPERSION but is blind to the two
things that actually sink such a book: fat left tails and co-movement. This module
adds the missing lenses, all computed from REAL daily price series (the "risk ramp":
reconstruct history from current holdings x their historical dailies rather than
waiting ~20 days of live snapshots to accrue):

  1. Historical-simulation VaR / CVaR at 95 / 97.5 — the post-FRTB standard. VaR is the
     loss you exceed (1-level)% of days; CVaR (expected shortfall) is the AVERAGE loss
     on the days you breach VaR — the number that actually describes the tail. Purely
     empirical (order the realized returns), so it captures skew/fat-tails that a
     Normal-sigma multiple would miss.
  2. Correlation structure -> effective bets. A 12-name book that all trades on the same
     AI/rates factor is really ~2 bets. We report Meucci's PCA Effective Number of Bets
     (weight-aware), a correlation-only diversification ratio, and the largest pairwise
     correlations so the hidden single-factor risk is visible.
  3. Factor exposure — OLS of the reconstructed portfolio's excess return on the
     Fama-French 5 factors + momentum (Mkt-RF, SMB, HML, RMW, CMA, Mom) from Ken
     French's free daily library. Answers "what is this book actually a bet ON" beyond
     a single market beta.
  4. Calmar & Treynor — return per unit of max-drawdown pain, and return per unit of
     market (systematic) risk. Round out the ratio panel.

Pure arithmetic — NO network, NO numpy, NO GPU (small matrices: # holdings x # holdings,
solved with a Jacobi eigensolver and Gaussian-elimination OLS). app.py owns all the
data fetching (Stooq dailies, Ken French factors) and passes clean series in here, so
this file is fully unit-testable offline. Informational, not advice.
"""

import math

TRADING_DAYS = 252


# ----------------------------- series helpers -----------------------------

def pct_returns(prices):
    """Simple daily returns from a price series (oldest->newest). Skips zero/None denominators."""
    out = []
    for i in range(1, len(prices)):
        p0, p1 = prices[i - 1], prices[i]
        if p0 in (None, 0) or p1 is None:
            continue
        out.append(p1 / p0 - 1.0)
    return out


def align_returns(series_by_ticker):
    """Given {ticker: [(date, close), ...]} (each ascending by date), intersect on the
    common trading dates and return (dates, {ticker: [ret,...]}) of DAILY RETURNS aligned
    one-to-one. Returns are computed only across consecutive kept dates. A ticker with no
    overlap is dropped. Needs >=2 common dates to yield any return."""
    if not series_by_ticker:
        return [], {}
    # map each ticker to {date: close}
    maps = {t: {d: c for d, c in s if c} for t, s in series_by_ticker.items() if s}
    if not maps:
        return [], {}
    common = None
    for m in maps.values():
        ks = set(m.keys())
        common = ks if common is None else (common & ks)
    dates = sorted(common or [])
    if len(dates) < 2:
        return dates, {}
    rets = {}
    for t, m in maps.items():
        closes = [m[d] for d in dates]
        rets[t] = pct_returns(closes)
    # all return-lists now have len(dates)-1 entries aligned to dates[1:]
    return dates, rets


def portfolio_returns(weights, rets_by_ticker):
    """Daily portfolio return series from current weights (assumes DAILY REBALANCING back
    to the current weights — a standard, clearly-stated approximation for the risk ramp).
    weights: {ticker: w} (need not sum to 1; renormalized over tickers we HAVE returns for).
    Returns [] if nothing usable."""
    tickers = [t for t in weights if t in rets_by_ticker and rets_by_ticker[t]]
    if not tickers:
        return []
    n = min(len(rets_by_ticker[t]) for t in tickers)
    if n <= 0:
        return []
    wsum = sum(weights[t] for t in tickers) or 1.0
    w = {t: weights[t] / wsum for t in tickers}
    out = []
    for i in range(n):
        out.append(sum(w[t] * rets_by_ticker[t][i] for t in tickers))
    return out


# ----------------------------- tail risk -----------------------------

def hist_var_cvar(returns, level_pct):
    """Historical-simulation 1-day VaR and CVaR (expected shortfall) at a confidence level
    (e.g. 95 or 97.5), reported as POSITIVE loss fractions. VaR = the loss you exceed on the
    worst (100-level)% of days; CVaR = the mean loss ON those worst days. Empirical (no
    distributional assumption), so it reflects the actual left tail. None if too few points."""
    r = sorted(x for x in returns if x is not None)
    n = len(r)
    if n < 20:
        return None
    alpha = 1.0 - level_pct / 100.0          # tail mass, e.g. 0.05
    # index into the sorted (ascending) losses; floor gives a conservative (deeper) quantile
    k = int(math.floor(alpha * n))
    k = min(max(k, 0), n - 1)
    var_ret = r[k]                            # a (usually negative) return at the tail edge
    tail = r[:k + 1]                          # the worst k+1 observations (the breach set)
    cvar_ret = sum(tail) / len(tail) if tail else var_ret
    return {"level_pct": level_pct, "n": n,
            "var_pct": round(-var_ret * 100, 2),      # positive = a loss
            "cvar_pct": round(-cvar_ret * 100, 2),
            "var_frac": -var_ret, "cvar_frac": -cvar_ret}


def var_table(returns, levels=(95.0, 97.5), horizons=(1, 10)):
    """VaR/CVaR at several confidence levels, each scaled to a few horizons by the
    sqrt-of-time rule (states the sqrt-t assumption; fine for short horizons)."""
    out = []
    for lv in levels:
        base = hist_var_cvar(returns, lv)
        if not base:
            continue
        row = {"level_pct": lv, "n": base["n"], "by_horizon": []}
        for h in horizons:
            s = math.sqrt(h)
            row["by_horizon"].append({
                "days": h,
                "var_pct": round(base["var_frac"] * s * 100, 2),
                "cvar_pct": round(base["cvar_frac"] * s * 100, 2)})
        out.append(row)
    return out


# ----------------------------- linear algebra (small, pure) -----------------------------

def mean(v):
    return sum(v) / len(v) if v else 0.0


def _cov(a, b):
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    ma, mb = mean(a[:n]), mean(b[:n])
    return sum((a[i] - ma) * (b[i] - mb) for i in range(n)) / (n - 1)


def pearson_corr(a, b):
    n = min(len(a), len(b))
    if n < 2:
        return None
    va, vb = _cov(a[:n], a[:n]), _cov(b[:n], b[:n])
    if va <= 0 or vb <= 0:
        return None
    return _cov(a[:n], b[:n]) / math.sqrt(va * vb)


def corr_matrix(rets_by_ticker, order):
    """Full Pearson correlation matrix over `order` (list of tickers). Missing pairs -> None."""
    m = []
    for ti in order:
        row = []
        for tj in order:
            row.append(1.0 if ti == tj else pearson_corr(rets_by_ticker[ti], rets_by_ticker[tj]))
        m.append(row)
    return m


def cov_matrix(rets_by_ticker, order):
    n = len(order)
    m = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i, n):
            c = _cov(rets_by_ticker[order[i]], rets_by_ticker[order[j]])
            m[i][j] = m[j][i] = c
    return m


def jacobi_eigen(a, iters=100, tol=1e-12):
    """Eigenvalues/vectors of a small REAL SYMMETRIC matrix by cyclic Jacobi rotation.
    Returns (eigenvalues[list], eigenvectors[list-of-columns]) sorted DESCENDING by
    eigenvalue. Adequate and stable for the small (n<=~40) covariance matrices here."""
    n = len(a)
    A = [row[:] for row in a]
    V = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for _ in range(iters):
        # largest off-diagonal magnitude
        p, q, off = 0, 1, 0.0
        for i in range(n):
            for j in range(i + 1, n):
                if abs(A[i][j]) > off:
                    off, p, q = abs(A[i][j]), i, j
        if off < tol:
            break
        app, aqq, apq = A[p][p], A[q][q], A[p][q]
        if abs(apq) < tol:
            break
        phi = 0.5 * math.atan2(2 * apq, (aqq - app)) if (aqq - app) != 0 else math.pi / 4
        c, s = math.cos(phi), math.sin(phi)
        for k in range(n):
            akp, akq = A[k][p], A[k][q]
            A[k][p] = c * akp - s * akq
            A[k][q] = s * akp + c * akq
        for k in range(n):
            apk, aqk = A[p][k], A[q][k]
            A[p][k] = c * apk - s * aqk
            A[q][k] = s * apk + c * aqk
        for k in range(n):
            vkp, vkq = V[k][p], V[k][q]
            V[k][p] = c * vkp - s * vkq
            V[k][q] = s * vkp + c * vkq
    eig = [A[i][i] for i in range(n)]
    cols = [[V[r][i] for r in range(n)] for i in range(n)]   # column i = eigenvector i
    order = sorted(range(n), key=lambda i: -eig[i])
    return [eig[i] for i in order], [cols[i] for i in order]


def _matvec(m, v):
    return [sum(m[i][j] * v[j] for j in range(len(v))) for i in range(len(m))]


# ----------------------------- diversification / effective bets -----------------------------

def effective_bets(cov, weights_vec, order):
    """Meucci PCA Effective Number of Bets + a correlation-only diversification ratio.
    - cov: covariance matrix (list of lists), rows/cols in `order`.
    - weights_vec: {ticker: w}; renormalized over `order`.
    Returns None if degenerate.

    ENB = exp(-sum p_k ln p_k) where p_k is the fraction of portfolio variance carried by
    principal portfolio k (p_k = v_k^2 * lambda_k / (w'Sigma w), v = E'w). ENB ranges 1..n:
    1 = the whole book is effectively one bet, n = n independent bets.
    Diversification ratio DR = (w . sigma) / sqrt(w'Sigma w): weighted-average standalone
    vol over portfolio vol; higher = more offsetting, ~1 = names move together."""
    n = len(order)
    if n == 0:
        return None
    wsum = sum(weights_vec.get(t, 0.0) for t in order) or 1.0
    w = [weights_vec.get(t, 0.0) / wsum for t in order]
    port_var = sum(w[i] * cov[i][j] * w[j] for i in range(n) for j in range(n))
    if port_var <= 0:
        return None
    # diversification ratio
    stdev = [math.sqrt(cov[i][i]) if cov[i][i] > 0 else 0.0 for i in range(n)]
    wavg_vol = sum(w[i] * stdev[i] for i in range(n))
    dr = wavg_vol / math.sqrt(port_var) if port_var > 0 else None
    # PCA effective bets
    enb = None
    if n >= 2:
        eig, vecs = jacobi_eigen(cov)
        v = [sum(vecs[k][i] * w[i] for i in range(n)) for k in range(n)]  # E'w, k-th principal exposure
        contrib = [(v[k] ** 2) * max(eig[k], 0.0) for k in range(n)]
        tot = sum(contrib)
        if tot > 0:
            p = [c / tot for c in contrib]
            ent = -sum(pk * math.log(pk) for pk in p if pk > 0)
            enb = math.exp(ent)
    return {"effective_bets": round(enb, 2) if enb is not None else None,
            "diversification_ratio": round(dr, 2) if dr is not None else None,
            "n_holdings": n}


def top_correlations(cmat, order, k=8):
    """The k largest-magnitude off-diagonal pairwise correlations — where hidden
    co-movement (a book that's secretly one factor bet) shows up."""
    pairs = []
    n = len(order)
    for i in range(n):
        for j in range(i + 1, n):
            c = cmat[i][j]
            if c is not None:
                pairs.append({"a": order[i], "b": order[j], "corr": round(c, 2)})
    pairs.sort(key=lambda p: -abs(p["corr"]))
    return pairs[:k]


# ----------------------------- OLS factor regression -----------------------------

def _solve(A, b):
    """Solve A x = b for small A by Gaussian elimination with partial pivoting. None if singular."""
    n = len(A)
    M = [A[i][:] + [b[i]] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-14:
            return None
        M[col], M[piv] = M[piv], M[col]
        pv = M[col][col]
        M[col] = [x / pv for x in M[col]]
        for r in range(n):
            if r != col and M[r][col] != 0:
                f = M[r][col]
                M[r] = [M[r][k] - f * M[col][k] for k in range(n + 1)]
    return [M[i][n] for i in range(n)]


def ols(y, cols):
    """Ordinary least squares of y on an intercept + the given factor columns.
    cols: list of equal-length factor series. Returns {intercept, betas[], r2, n} or None."""
    n = len(y)
    if n < 8 or not cols:
        return None
    k = len(cols)
    if any(len(c) != n for c in cols):
        m = min([n] + [len(c) for c in cols])
        y = y[-m:]
        cols = [c[-m:] for c in cols]
        n = m
        if n < 8:
            return None
    # design matrix X with leading 1s
    X = [[1.0] + [cols[j][i] for j in range(k)] for i in range(n)]
    # normal equations X'X b = X'y
    XtX = [[sum(X[r][a] * X[r][b] for r in range(n)) for b in range(k + 1)] for a in range(k + 1)]
    Xty = [sum(X[r][a] * y[r] for r in range(n)) for a in range(k + 1)]
    beta = _solve(XtX, Xty)
    if beta is None:
        return None
    yhat = [sum(beta[a] * X[r][a] for a in range(k + 1)) for r in range(n)]
    ybar = mean(y)
    ss_tot = sum((y[r] - ybar) ** 2 for r in range(n))
    ss_res = sum((y[r] - yhat[r]) ** 2 for r in range(n))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else None
    return {"intercept": beta[0], "betas": beta[1:], "r2": r2, "n": n}


def factor_exposure(port_excess_daily, factor_series, factor_names):
    """Regress the portfolio's DAILY EXCESS return on the FF factor set.
    factor_series: list of daily factor return series (already in decimal, aligned to
    port_excess_daily). factor_names: matching labels (e.g. Mkt-RF, SMB, HML, RMW, CMA, Mom).
    Returns per-factor beta, annualized alpha (intercept*252), and R^2."""
    res = ols(port_excess_daily, factor_series)
    if not res:
        return None
    betas = {factor_names[i]: round(res["betas"][i], 3) for i in range(len(factor_names))}
    return {"factor_betas": betas,
            "alpha_annualized_pct": round(res["intercept"] * TRADING_DAYS * 100, 2),
            "r2": round(res["r2"], 3) if res["r2"] is not None else None,
            "n_days": res["n"],
            "note": "OLS of daily portfolio excess return on Fama-French 5 + momentum "
                    "(daily factors from the Ken French library). Beta ~1 on Mkt-RF = one "
                    "unit of market risk; positive SMB = small-cap tilt, negative HML = growth "
                    "tilt, positive Mom = momentum tilt. Alpha is the annualized unexplained "
                    "return (noisy on short/one-name-heavy samples)."}


# ----------------------------- ratios -----------------------------

def calmar(cagr_pct, max_drawdown_pct):
    """Calmar = annualized return / |max drawdown|. Higher = more return per unit of the
    worst peak-to-trough pain. max_drawdown_pct is a negative number (e.g. -35)."""
    if not max_drawdown_pct:
        return None
    dd = abs(max_drawdown_pct)
    if dd == 0:
        return None
    return round(cagr_pct / dd, 2)


def treynor(ann_return_pct, risk_free_pct, beta):
    """Treynor = (return - risk free) / beta. Return per unit of SYSTEMATIC (market) risk,
    unlike Sharpe which divides by total risk. None if beta missing/zero."""
    if beta in (None, 0):
        return None
    return round((ann_return_pct - risk_free_pct) / beta, 2)
