# Portfolio Risk Metrics — Frameworks Reference (2026)

_Corpus doc for the Fiduciary knowledge RAG (G4 companion). Concepts behind the numbers `risk.py` computes and RISK · EXPOSURE → RISK / ATTRIBUTION / PERFORMANCE display. Not investment advice._

## Tail risk: VaR and CVaR
- **VaR (Value at Risk)** at confidence level c and horizon h is the loss threshold exceeded only (1−c) of the time — e.g. a 95% 1-month VaR of 8% means a loss worse than 8% happens in roughly 1 of 20 months historically/statistically. VaR answers "how bad, how often" but says nothing about how much worse the tail beyond that threshold actually gets.
- **CVaR (Conditional VaR, a.k.a. Expected Shortfall)** is the AVERAGE loss conditional on being in that worst-(1−c) tail. It's the more honest number for concentrated or fat-tailed books because two portfolios with identical VaR can have very different CVaR — one where the tail drops off a cliff past the VaR line, one where it doesn't.
- Both can be computed parametrically (assuming a return distribution, usually normal or Student-t) or historically (empirical percentile of actual observed returns) — historical/empirical is more robust to fat tails and skew but needs enough history to populate the tail meaningfully; the app favors historical where enough daily snapshots exist and notes when it's still building.

## Risk-adjusted return ratios
- **Sharpe ratio** = (portfolio return − risk-free rate) ÷ total volatility (standard deviation of returns). Penalizes upside and downside volatility equally, which understates quality for a portfolio with asymmetric (positively skewed) returns.
- **Sortino ratio** = same numerator ÷ DOWNSIDE deviation only (volatility of below-target returns) — a fairer lens when upside volatility shouldn't count against the strategy.
- **Calmar ratio** = annualized return ÷ max drawdown — rewards strategies with a shallow worst-case path rather than just low day-to-day noise.
- **Treynor ratio** = excess return ÷ beta (systematic risk only, not total volatility) — appropriate for a position judged as one holding inside a diversified book, where idiosyncratic vol is expected to average out.
- **Max drawdown** = the largest peak-to-trough decline over the measured window, in percent — the number that most directly maps to "how bad did it get to hold this."

## Factor exposure
- Modern factor models decompose a return series into exposure to known, persistent risk premia rather than treating all outperformance as skill. The **Fama-French 5-factor model** adds SIZE (small minus big market cap), VALUE (high minus low book-to-market), PROFITABILITY (robust minus weak operating profitability), and INVESTMENT (conservative minus aggressive asset growth) on top of the classic MARKET factor; **momentum** (winners minus losers over the past 12 months, excluding the most recent month) is commonly added as a sixth.
- A regression of portfolio returns against these factor return series yields **loadings** (how exposed the book is to each factor) and an **alpha** (return unexplained by any factor — the closest thing to a real skill estimate, though even that requires a long enough sample to be statistically meaningful).
- Reading loadings: a book with a large positive SIZE loading behaves partly like a small-cap fund regardless of what the holdings are "supposed" to be; a large negative VALUE loading means the book's growth tilt will underperform in a value-rotation regime. This reframes "why did the portfolio move" away from single-stock stories and toward systematic exposure.

## Concentration
- **HHI (Herfindahl-Hirschman Index)** = sum of squared position weights (as decimals). A perfectly equal-weighted N-position book has HHI = 1/N; a single 100% position has HHI = 1. Originally an antitrust/market-concentration metric, repurposed here for portfolio concentration.
- **Effective number of bets** = 1/HHI (on a risk-weighted or correlation-adjusted basis, ideally, not just dollar weight) — translates the abstract HHI number into "how many truly independent bets am I actually making," which is usually a much smaller and more sobering number than the raw position count once correlated names are accounted for.

## Return measurement: TWR vs money-weighted (XIRR)
- **TWR (Time-Weighted Return)** chain-links sub-period returns and is DELIBERATELY insensitive to the size and timing of deposits/withdrawals — the correct number for judging a strategy or manager against a benchmark, since it isolates the return the STRATEGY generated independent of your cash-flow decisions.
- **Money-weighted return (XIRR / IRR)** solves for the discount rate that zeroes the NPV of your actual cash flows — the correct number for "what did I personally earn," since it's sensitive to when you added or removed money (adding right before a rally or right before a drawdown changes this number even though the underlying strategy didn't change).
- The two can diverge substantially for the same portfolio if cash flows were poorly timed relative to the strategy's own return path — that divergence is itself informative (large deposits into a subsequent drawdown will show TWR outperforming XIRR).

## Sources
Philippe Jorion, *Value at Risk* (VaR/CVaR standard treatment); Fama & French, "A five-factor asset pricing model" (2015, *Journal of Financial Economics*), and the Kenneth French Data Library (factor return series definitions/construction); CFA Institute Level II/III curriculum readings on risk-adjusted performance measures (Sharpe/Sortino/Calmar/Treynor) and time- vs money-weighted return; standard portfolio-concentration literature on the Herfindahl-Hirschman Index. Definitional/methodological content is standard and stable; the app's own VaR/CVaR/factor-loading OUTPUTS are computed from its own accrued price history and Stooq/Ken-French data feeds, not from this doc — this doc explains what the numbers mean, not what today's specific values are.
