"""
mcblock.py — block-bootstrap return sequences for the Monte Carlo (Fable, Part-4).

The headline Monte Carlo draws each year's return i.i.d. from Normal(mean, vol). That throws away
two things real markets have: SERIAL STRUCTURE (bad years cluster into bad decades; strong runs
mean-revert) and FAT TAILS. This module builds a third draw mode — a CIRCULAR BLOCK BOOTSTRAP over
the real historical annual-return series — so a simulated path can inherit an actual multi-year
sequence (a 1929-32 or 2000-02 or 2007-09 run), which is exactly the sequence-of-returns risk a
retiree is exposed to.

Approach: we use history only for its SHAPE, not its level. Each historical annual return is
standardized to a z-score (removing the historical mean/vol), and the block-sampled z-sequence is
then re-mapped to the PLAN's real mean and volatility: r = mean_real + z * vol. This preserves the
autocorrelation, skew and fat tails of history while honoring the plan's own return assumptions
(and sidesteps needing a separate inflation series to convert to real). Circular (wrap-around)
blocks avoid end-of-sample bias.

Source series: S&P 500 annual TOTAL returns (incl. dividends), 1928-2024, NYU Stern / Damodaran
"Historical Returns on Stocks, Bonds and Bills" (updated Jan 2026). Values in percent.
"""

# S&P 500 total annual returns %, 1928..2024 (97 years), verbatim from the NYU Stern table.
HIST_SP500_TR = (
    43.81, -8.30, -25.12, -43.84, -8.64, 49.98, -1.19, 46.74, 31.94, -35.34,   # 1928-1937
    29.28, -1.10, -10.67, -12.77, 19.17, 25.06, 19.03, 35.82, -8.43, 5.20,      # 1938-1947
    5.70, 18.30, 30.81, 23.68, 18.15, -1.21, 52.56, 32.60, 7.44, -10.46,        # 1948-1957
    43.72, 12.06, 0.34, 26.64, -8.81, 22.61, 16.42, 12.40, -9.97, 23.80,        # 1958-1967
    10.81, -8.24, 3.56, 14.22, 18.76, -14.31, -25.90, 37.00, 23.83, -6.98,      # 1968-1977
    6.51, 18.52, 31.74, -4.70, 20.42, 22.34, 6.15, 31.24, 18.49, 5.81,          # 1978-1987
    16.54, 31.48, -3.06, 30.23, 7.49, 9.97, 1.33, 37.20, 22.68, 33.10,          # 1988-1997
    28.34, 20.89, -9.03, -11.85, -21.97, 28.36, 10.74, 4.83, 15.61, 5.48,       # 1998-2007
    -36.55, 25.94, 14.82, 2.10, 15.89, 32.15, 13.52, 1.38, 11.77, 21.61,        # 2008-2017
    -4.23, 31.21, 18.02, 28.47, -18.04, 26.06, 24.88,                            # 2018-2024
)

_MEAN = sum(HIST_SP500_TR) / len(HIST_SP500_TR)
_STD = (sum((x - _MEAN) ** 2 for x in HIST_SP500_TR) / len(HIST_SP500_TR)) ** 0.5
# Standardized shocks (z-scores) — carry history's sequence, skew and fat tails, mean 0 / std 1.
_Z = tuple((x - _MEAN) / _STD for x in HIST_SP500_TR)


def block_sequence(n, rng, mean_real, vol, block=5):
    """A length-`n` annual real-return sequence built from circular blocks of the standardized
    historical series, re-scaled to (mean_real, vol). `rng` is a random source with .randrange.
    Preserves multi-year serial structure (bad decades cluster) that i.i.d. draws miss."""
    N = len(_Z)
    block = max(1, int(block))
    out = []
    while len(out) < n:
        start = rng.randrange(N)
        for k in range(block):
            out.append(mean_real + _Z[(start + k) % N] * vol)
            if len(out) >= n:
                break
    return out[:n]


def source_stats():
    """Summary of the embedded series — used by the self-check / grounding."""
    return {"years": len(HIST_SP500_TR), "mean_pct": round(_MEAN, 2), "std_pct": round(_STD, 2),
            "min_pct": min(HIST_SP500_TR), "max_pct": max(HIST_SP500_TR),
            "source": "S&P 500 total annual returns 1928-2024, NYU Stern/Damodaran (Jan 2026)"}
