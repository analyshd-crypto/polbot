"""Fee and rebate model for Polymarket (see research/INTEL.md §2, docs).

polymarket.com formula:  fee = C * rate * p * (1 - p)   (takers only; makers pay zero)

- legacy regime (until 2026-11-20 23:59 UTC): crypto taker rate 0.07,
  maker rebate pool = 20% of taker fees redistributed daily.
- tiered regime (from 2026-11-20), per docs.polymarket.com
  /programs/fee-and-rebate-changes-november-2026:
    * taker rates: BTC 5m/15m up/down 0.05; other crypto 0.01;
      politics/economics/culture/weather/mentions 0.01; sports/finance/tech/
      elections/combos/other 0.05; geopolitics 0.
    * maker rebates (weighted volume = shares * p * (1-p), trailing 30d):
      <$200K -> 0%, $200K-$1M -> 3%, $1M-$4M -> 7%, $4M-$10M -> 10%, >=$10M -> 15%.
"""

from __future__ import annotations

from dataclasses import dataclass

LEGACY_SWITCH_TS = 1792694340  # 2026-11-20 23:59:00 UTC (approx, epoch s)


@dataclass
class FeeModel:
    regime: str = "legacy"            # legacy | tiered
    taker_rate: float = 0.07          # legacy crypto rate
    maker_rebate_share: float = 0.20  # legacy: share of the counterparty taker fee
    # tiered-regime parameters (international polymarket.com schedule)
    taker_theta: float = 0.05         # BTC 5m/15m up/down after Nov 20
    maker_rebate_theta: float = 0.0   # <$200K/30d weighted maker volume -> 0%

    def taker_fee(self, shares: float, price: float) -> float:
        rate = self.taker_rate if self.regime == "legacy" else self.taker_theta
        return round(shares * rate * price * (1.0 - price), 6)

    def maker_fee(self, shares: float, price: float) -> float:
        return 0.0  # makers never pay fees on polymarket.com

    def maker_rebate(self, shares: float, price: float) -> float:
        """Estimated rebate for maker volume (bonus, not core PnL)."""
        if self.regime == "legacy":
            # legacy: 20% of the taker fees our resting order's counterparty paid
            return round(self.maker_rebate_share * self.taker_fee(shares, price), 6)
        return round(-self.maker_rebate_theta * shares * price * (1.0 - price), 6)

    def taker_break_even(self, price: float) -> float:
        """Win probability needed for a taker buy at `price` to break even."""
        fee = self.taker_rate if self.regime == "legacy" else self.taker_theta
        per_share_cost = price + fee * price * (1 - price)
        return per_share_cost  # payout 1.0 * p  =>  p = cost

    @staticmethod
    def auto_regime(ts: float) -> str:
        return "legacy" if ts < LEGACY_SWITCH_TS else "tiered"
