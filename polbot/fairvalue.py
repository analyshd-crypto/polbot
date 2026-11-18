"""Fair value for crypto Up/Down markets: gap -> probability.

Model
-----
With ``T`` seconds to the close, spot ``S`` and strike ``K`` ("price to
beat"), assume the underlying diffuses like a Brownian motion with volatility
``sigma`` (price-relative, per sqrt(second)). Then

    P(Up wins) = P(S_T >= K) = Phi( (S - K) / (sigma * S * sqrt(T)) )

``T >= 1`` to avoid infinities; probabilities are clipped to [0.005, 0.995]
because 5-minute crypto paths have fat tails and the Chainlink settlement
print can deviate from Binance spot.

This is the same math the "snipers" exploit: if Polymarket's implied
probability lags a Binance move, the gap model tells you the honest price and
the lagging quotes become free (or cheap) money.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

MIN_SIGMA = 7e-5          # ~0.007% per sqrt(s) floor (dead-quiet markets)
SIGMA_FLOOR = 7e-5
MAX_SIGMA = 3e-3          # panic regime cap
CLIP_LO, CLIP_HI = 0.005, 0.995

#: Chainlink-vs-Binance basis noise (price-relative), calibrated on 299 real
#: BTC windows: Binance end-of-window direction matches the Chainlink-settled
#: outcome only 87.6% of the time; fitting the flip probabilities yields a
#: Gaussian basis sigma of ~4.6 bps of spot. Gaps larger than ~0.2% (≈4.3
#: basis-sigmas) settled the Binance way in 40/40 observed windows.
#: The SDK's chainlink price stream removes most of this in live trading;
#: for Binance-anchored fair value this term is mandatory.
BASIS_SIGMA = 0.00046


def _phi(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass(frozen=True)
class FairValue:
    p_up: float
    spot: Optional[float]
    gap: Optional[float]          # spot - strike, in price units
    sigma: Optional[float]        # price-relative per sqrt(second)
    t_left: float
    clipped: bool = False

    def p(self, side: str) -> float:
        return self.p_up if side == "up" else 1.0 - self.p_up


def prob_up(spot: Optional[float], strike: Optional[float],
            sigma: Optional[float], t_left: float,
            basis_sigma: float = BASIS_SIGMA) -> FairValue:
    """Gap->probability fair value with settlement-basis noise.

    Variance of the settlement gap = market variance (sigma^2 * t) plus a
    time-independent basis term (basis_sigma * spot)^2, so the model stays
    appropriately humble when the gap is small relative to ~4.6bps.
    Falls back to 0.5 when inputs are missing.
    """
    if spot is None or strike is None or strike <= 0 or t_left <= 0:
        return FairValue(0.5, spot, None, sigma, max(0.0, t_left))
    sig = sigma if sigma else MIN_SIGMA
    sig = max(SIGMA_FLOOR, min(MAX_SIGMA, sig))
    t = max(1.0, t_left)
    gap = spot - strike
    var = (sig * spot) ** 2 * t + (basis_sigma * spot) ** 2
    z = gap / math.sqrt(var)
    p = _phi(z)
    clipped = p < CLIP_LO or p > CLIP_HI
    p = min(CLIP_HI, max(CLIP_LO, p))
    return FairValue(p, spot, gap, sig, t_left, clipped)


def edge_cents(p_fair: float, price: float) -> float:
    """Edge (in cents) of buying at ``price`` when true probability is ``p``."""
    return (p_fair - price) * 100.0


def taker_fee(shares: float, price: float, rate: float = 0.05) -> float:
    """Polymarket crypto taker fee: fee = shares * rate * p * (1-p)."""
    return shares * rate * price * (1.0 - price)


def taker_fee_bps_of_notional(price: float, rate: float = 0.05) -> float:
    """Fee as a fraction of notional (shares*price): rate * (1-p)."""
    return rate * (1.0 - price)


def breakeven_edge(price: float, rate: float = 0.05) -> float:
    """Price edge (probability units) needed to break even after taker fees."""
    return rate * price * (1.0 - price)
