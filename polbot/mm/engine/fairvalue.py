"""Fair-value engine: P(Up) for a 5-minute Up/Down window.

Model (per RESEARCH.md §1/§4):

- The window pays Up iff the 30s TWAP at close >= the 30s TWAP at open (strike).
- While more than TWAP_SECONDS remain, approximate both TWAPs by their anchors
  (strike known at open; current spot as the close anchor) and use a driftless GBM
  digital option:  P(Up) = Phi( ln(S/K) / (sigma * sqrt(tau)) ).
- Inside the final TWAP_SECONDS, the settlement TWAP is partially observed:
  TWAP_hat = (known_sum + remaining * spot) / TWAP_SECONDS, with variance
  Var[TWAP] ~= (sigma_s * spot)^2 * (r/30)^2 * r/3  (mean of a BM over r seconds,
  scaled into the 30s window). P(Up) = Phi( ln(TWAP_hat/K) / sd_log ).

Volatility: EWMA of 1-second log returns with configurable half-life, annualized,
floored/capped. Feeds: live SpotFeed (WS trades) or backtest bars.

Basis humility (calibrated on 299 real BTC windows, see research/INTEL.md §4):
Binance end-of-window direction matches the Chainlink-settled outcome only 87.6%
of the time; the fitted Gaussian basis sigma is ~4.6 bps of spot. When the spot
feed is Binance (not the Chainlink Data Streams feed), that term is MANDATORY —
it keeps the model from printing 0.99 probabilities on gaps smaller than the
settlement basis. ``basis_sigma=0`` is correct only when the feed IS the
settlement oracle.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Optional

SECONDS_PER_YEAR = 31_556_952.0

#: Chainlink-vs-Binance settlement basis, price-relative (see research/INTEL.md).
BASIS_SIGMA = 0.00046


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class FairValueModel:
    def __init__(self, vol_halflife_s: float = 90.0, min_vol_annual: float = 0.15,
                 max_vol_annual: float = 3.0, twap_seconds: float = 30.0,
                 min_conf_vol: float = 0.0, basis_sigma: float = BASIS_SIGMA):
        self.halflife = float(vol_halflife_s)
        self.min_vol = float(min_vol_annual)
        self.max_vol = float(max_vol_annual)
        self.twap_seconds = float(twap_seconds)
        self.basis_sigma = float(basis_sigma)
        self._last_price: Optional[float] = None
        self._last_ts: Optional[float] = None
        self._var_ewma: Optional[float] = None  # per-second log-return variance
        self._samples = 0
        self.min_conf_vol = min_conf_vol  # samples needed before trusting vol

    # ------------------------------------------------------------------ inputs
    def update(self, ts: float, price: float) -> None:
        """Feed one spot observation (any frequency; typically 1s or trade ticks)."""
        if self._last_price is not None and self._last_ts is not None:
            dt = ts - self._last_ts
            if dt <= 0:
                return
            r = math.log(price / self._last_price)
            lam = math.exp(-math.log(2.0) * dt / self.halflife)
            v = r * r / dt  # per-second variance contribution of this interval
            if self._var_ewma is None:
                self._var_ewma = v
            else:
                self._var_ewma = lam * self._var_ewma + (1 - lam) * v
        self._last_price = price
        self._last_ts = ts
        self._samples += 1

    def update_series(self, points: list[tuple[float, float]]) -> None:
        for ts, px in points:
            self.update(ts, px)

    @property
    def vol_annual(self) -> float:
        if self._var_ewma is None or self._samples < max(2, self.min_conf_vol):
            return self.min_vol
        v = max(self._var_ewma, 1e-16)
        vol = math.sqrt(v * SECONDS_PER_YEAR)
        return min(max(vol, self.min_vol), self.max_vol)

    @property
    def vol_per_second(self) -> float:
        return self.vol_annual / math.sqrt(SECONDS_PER_YEAR)

    # ------------------------------------------------------------------ pricing
    def prob_up(self, spot: float, strike: float, remaining_s: float,
                twap_sum: Optional[float] = None) -> tuple[float, dict]:
        """P(Up wins). twap_sum: sum of observed spot prices inside the final TWAP
        window (only used when remaining < twap_seconds). Returns (p, diagnostics)."""
        remaining_s = max(0.0, float(remaining_s))
        if strike is None or strike <= 0 or spot is None or spot <= 0:
            return 0.5, {"note": "no strike"}
        sig = self.vol_per_second

        if remaining_s <= 0.0:
            # fully deterministic from whatever we know
            if twap_sum is not None:
                twap = twap_sum / self.twap_seconds
                return (1.0 if twap >= strike else 0.0), {"mode": "done", "twap": twap}
            return (1.0 if spot >= strike else 0.0), {"mode": "done"}

        if remaining_s > self.twap_seconds or twap_sum is None:
            # standard driftless GBM digital on the close anchor,
            # variance += settlement-basis noise (feed != oracle)
            tau = remaining_s
            # log-space variance: market diffusion + settlement basis noise
            var = (sig ** 2) * tau + self.basis_sigma ** 2
            d = math.log(spot / strike) / math.sqrt(var)
            p = norm_cdf(d)
            return p, {"mode": "gbm", "d": d, "vol": self.vol_annual, "tau": tau}

        # --- final TWAP blend ---
        r = remaining_s                      # seconds still unknown
        n = self.twap_seconds - r            # seconds already observed
        known = twap_sum if twap_sum is not None else n * spot
        twap_hat = (known + r * spot) / self.twap_seconds
        if twap_hat <= 0 or strike <= 0:
            return 0.5, {"mode": "twap-degenerate"}
        # sd of the TWAP estimate (mean of GBM over r seconds, scaled by r/30)
        var_twap = (sig * spot) ** 2 * (r / self.twap_seconds) ** 2 * (r / 3.0)
        # basis noise shrinks as the TWAP becomes observed, but never below the
        # fraction of the window still unobserved times the full basis sigma
        basis = (self.basis_sigma * spot) ** 2 * (r / self.twap_seconds)
        sd_log = math.sqrt(var_twap + basis) / twap_hat
        sd_log = max(sd_log, 1e-9)
        d = math.log(twap_hat / strike) / sd_log
        p = norm_cdf(d)
        return p, {"mode": "twap", "d": d, "twap_hat": twap_hat, "sd_log": sd_log,
                   "known_s": n, "r": r}

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def prob_both_sides_fill_hint(p_up: float) -> float:
        """If we quote both sides at mid-band, the chance at least one side ends ITM
        is 1 by construction; kept for API symmetry/reporting."""
        return 1.0


class TwapTracker:
    """Accumulates spot observations inside the final settlement TWAP window."""

    def __init__(self, twap_seconds: float = 30.0):
        self.twap_seconds = twap_seconds
        self._points: deque[tuple[float, float]] = deque()

    def reset(self) -> None:
        self._points.clear()

    def update(self, ts: float, price: float, window_end: float) -> None:
        """Record a point if it falls inside [window_end - twap, window_end]."""
        lo = window_end - self.twap_seconds
        if lo <= ts <= window_end:
            self._points.append((ts, price))

    def sum(self, window_end: float) -> tuple[float, float]:
        """Return (twap_sum, observed_seconds) using trapezoid integration of stored points."""
        pts = sorted(p for p in self._points if window_end - self.twap_seconds <= p[0] <= window_end)
        if not pts:
            return 0.0, 0.0
        if len(pts) == 1:
            return pts[0][1] * 1.0, 1.0  # one sample: assume it covers ~1s
        total = 0.0
        for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
            total += 0.5 * (p0 + p1) * (t1 - t0)
        observed = pts[-1][0] - pts[0][0]
        # count the head/tail second too
        total += pts[0][1] * 0.5 + pts[-1][1] * 0.5
        observed += 1.0
        return total, min(observed, self.twap_seconds)
