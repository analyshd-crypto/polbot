"""Core data structures shared across polbot."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Phase(str, Enum):
    PRE_OPEN = "pre_open"      # before the 5-minute window starts (book trades ~50/50)
    LIVE = "live"              # window running, strike known
    WINDDOWN = "winddown"      # inside the final cancel_before_end seconds: no quotes
    ENDED = "ended"            # window over, awaiting settlement
    SETTLED = "settled"        # outcome known, PnL booked


@dataclass
class Window:
    """One 5-minute Up/Down market window."""
    series: str                      # e.g. btc-up-or-down-5m
    event_slug: str                  # e.g. btc-updown-5m-1795016400
    condition_id: str
    tokens: list[str]                # [token_up, token_down]
    outcomes: list[str]              # ["Up", "Down"]
    start_ts: float                  # window start (epoch s)
    end_ts: float                    # window end (epoch s)
    liquidity: float = 0.0
    min_tick: float = 0.01
    min_size: float = 5.0
    neg_risk: bool = False
    strike: Optional[float] = None   # captured/derived strike ("price to beat")
    phase: Phase = Phase.PRE_OPEN
    # live state
    fair_up: float = 0.5
    inventory: dict = field(default_factory=lambda: {"Up": 0.0, "Down": 0.0})
    cost: dict = field(default_factory=lambda: {"Up": 0.0, "Down": 0.0})
    realized_pnl: float = 0.0
    settlement: Optional[int] = None  # 0 = Up won, 1 = Down won

    @property
    def duration(self) -> float:
        return self.end_ts - self.start_ts

    def remaining(self, now: Optional[float] = None) -> float:
        return max(0.0, self.end_ts - (now if now is not None else time.time()))

    def elapsed_since_open(self, now: Optional[float] = None) -> float:
        return max(0.0, (now if now is not None else time.time()) - self.start_ts)

    def is_live(self, now: Optional[float] = None) -> bool:
        t = now if now is not None else time.time()
        return self.start_ts <= t < self.end_ts

    def token_for(self, outcome: str) -> str:
        return self.tokens[self.outcomes.index(outcome)]


@dataclass
class Quote:
    """A desired resting limit order (maker side only in polbot)."""
    token_id: str
    outcome: str          # "Up" / "Down"
    price: float          # 0.01..0.99 aligned to tick
    size: float           # shares (>= 5, 2dp)
    side: str = "BUY"

    @property
    def notional(self) -> float:
        return round(self.price * self.size, 4)


@dataclass
class Fill:
    token_id: str
    outcome: str
    price: float
    size: float
    ts: float
    fee: float = 0.0      # maker fills pay zero; kept for audit
    order_id: str = ""

    @property
    def notional(self) -> float:
        return round(self.price * self.size, 4)


@dataclass
class WindowResult:
    event_slug: str
    series: str
    up_shares: float
    down_shares: float
    up_cost: float
    down_cost: float
    settlement: Optional[int]        # 0 Up won, 1 Down won, None unknown
    payout: float
    pnl: float
    end_ts: float


def now_ts() -> float:
    return time.time()
