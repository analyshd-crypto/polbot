"""Kelly position sizing for binary 'buy shares at p, win pays 1' bets.

For a bet costing `p` per share with true win probability `q`:
    f* = (q - p) / (1 - p)      (fraction of bankroll to deploy at cost value)

polbot uses fractional Kelly (default 1/4) plus hard caps — the whale data
(SirMartingale max fill $366 over 23k trades; ohanism max $282) shows bounded
sizing is what keeps these books alive.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class KellySizer:
    fraction: float = 0.25      # fractional Kelly multiplier
    max_clip_usd: float = 40.0  # hard per-order cap
    min_clip_usd: float = 1.0   # below this, skip the level entirely

    def edge(self, q: float, p: float) -> float:
        return q - p

    def kelly_fraction(self, q: float, p: float) -> float:
        if p <= 0.0 or p >= 1.0:
            return 0.0
        if q <= p:
            return 0.0
        return (q - p) / (1.0 - p)

    def size_usd(self, bankroll: float, q: float, p: float) -> float:
        """Recommended USD notional for buying at price p with true probability q."""
        f = self.kelly_fraction(q, p) * self.fraction
        if f <= 0:
            return 0.0
        raw = bankroll * f
        if raw < self.min_clip_usd:
            return 0.0
        return min(raw, self.max_clip_usd)
