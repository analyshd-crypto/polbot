"""Position and PnL accounting, mirroring the demo dashboard's numbers.

The bot never sells: fills accumulate inventory; at resolution the winning
side pays $1/share and the losing side $0. Realized PnL per window is
``winning_shares - total_cost``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .markets import FiveMinMarket


@dataclass(frozen=True)
class Fill:
    ts: float
    token_id: str
    label: str  # "Up" | "Down"
    price: float
    shares: float

    @property
    def cost(self) -> float:
        return self.price * self.shares


@dataclass
class SidePosition:
    shares: float = 0.0
    cost: float = 0.0

    def add(self, price: float, shares: float) -> None:
        self.shares += shares
        self.cost += price * shares

    @property
    def avg_price(self) -> Optional[float]:
        return self.cost / self.shares if self.shares > 0 else None

    def value_if_win(self) -> float:
        return self.shares  # $1 per winning share


@dataclass
class WindowLedger:
    """All fills and settlement for one 5-minute window."""

    market: FiveMinMarket
    up: SidePosition = field(default_factory=SidePosition)
    down: SidePosition = field(default_factory=SidePosition)
    fills: list[Fill] = field(default_factory=list)
    winner: Optional[str] = None  # "up" | "down"
    realized_pnl: Optional[float] = None

    def apply(self, fill: Fill) -> None:
        self.fills.append(fill)
        if fill.label.lower() == "up":
            self.up.add(fill.price, fill.shares)
        else:
            self.down.add(fill.price, fill.shares)

    @property
    def spent(self) -> float:
        return self.up.cost + self.down.cost

    def pnl_if(self, winner: str) -> float:
        win_side = self.up if winner == "up" else self.down
        return win_side.value_if_win() - self.spent

    def settle(self, winner: str) -> float:
        self.winner = winner
        self.realized_pnl = self.pnl_if(winner)
        return self.realized_pnl

    def summary(self) -> str:
        up_avg = f"{self.up.avg_price:.3f}" if self.up.avg_price is not None else "-"
        dn_avg = f"{self.down.avg_price:.3f}" if self.down.avg_price is not None else "-"
        return (
            f"up {self.up.shares:.0f}sh@{up_avg} (${self.up.cost:.2f}) | "
            f"down {self.down.shares:.0f}sh@{dn_avg} (${self.down.cost:.2f}) | "
            f"ifUP {self.pnl_if('up'):+.2f} ifDOWN {self.pnl_if('down'):+.2f}"
        )


@dataclass
class WindowResult:
    window_ts: int
    slug: str
    winner: Optional[str]
    up_shares: float
    down_shares: float
    spent: float
    pnl: float
    fills: int
    peak_quotes: int

    @property
    def won(self) -> bool:
        return self.pnl > 0


@dataclass
class RunStats:
    """Cumulative run statistics (the bot's bottom status bar)."""

    windows: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    total_fills: int = 0
    total_shares: float = 0.0
    total_spent: float = 0.0
    worst_pnl: float = 0.0
    best_pnl: float = 0.0
    _peak_pnl: float = 0.0
    _max_drawdown: float = 0.0

    def record(self, result: WindowResult) -> None:
        self.windows += 1
        if result.pnl > 0:
            self.wins += 1
        elif result.pnl < 0:
            self.losses += 1
        self.total_pnl += result.pnl
        self.total_fills += result.fills
        self.total_shares += result.up_shares + result.down_shares
        self.total_spent += result.spent
        self.worst_pnl = min(self.worst_pnl, result.pnl)
        self.best_pnl = max(self.best_pnl, result.pnl)
        self._peak_pnl = max(self._peak_pnl, self.total_pnl)
        self._max_drawdown = max(self._max_drawdown, self._peak_pnl - self.total_pnl)

    @property
    def win_rate(self) -> float:
        decided = self.wins + self.losses
        return self.wins / decided if decided else 0.0

    @property
    def max_drawdown(self) -> float:
        return self._max_drawdown

    def line(self) -> str:
        return (
            f"windows={self.windows} W/L={self.wins}/{self.losses} "
            f"win%={self.win_rate:.1%} pnl={self.total_pnl:+.2f} "
            f"fills={self.total_fills} worst={self.worst_pnl:+.2f} "
            f"maxDD={self.max_drawdown:.2f}"
        )
