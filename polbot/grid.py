"""Ladder (grid) construction.

The strategy: rest BUY limit orders at (nearly) every tick below the current
price on BOTH outcome books. Two invariants must hold:

1. **Never cross the spread** — a bid above the book's best ask would execute
   immediately as a taker (paying fees and defeating the strategy). The top
   ladder level is ``best_ask - buffer_ticks * tick``.
2. **Never cross yourself** — a BUY of Up at ``p_u`` is synthetically a SELL
   of Down at ``1 - p_u``. If ``p_u + p_d > 1`` for any pair of our own bids,
   the matching engine can mint a complete set against us for more than $1
   (instant loss). We therefore trim levels so that
   ``max_up + max_down <= 1 - tick``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .clob import Book
from .config import BotConfig
from .markets import FiveMinMarket

_EPS = 1e-9


@dataclass(frozen=True)
class Quote:
    """One resting BUY order we want on the book."""

    token_id: str
    label: str  # "Up" | "Down"
    price: float
    size: float


def ladder_top(book: Book, cfg: BotConfig, other_top: Optional[float] = None,
               tick: float = 0.01) -> float:
    """Highest price we may bid on this side right now.

    Constraints applied (most binding wins):
    - below the current ask: ``best_ask - buffer_ticks * tick``
    - at most ``cfg.max_price``
    - self-cross guard vs the other side's top: ``1 - tick - other_top``
    """
    top = cfg.max_price
    if book.best_ask is not None:
        top = min(top, book.best_ask - cfg.quote_buffer_ticks * tick)
    if other_top is not None:
        top = min(top, 1.0 - tick - other_top)
    # snap down to the tick grid anchored at 0
    steps = int((top + _EPS) / tick)
    return round(steps * tick, 6)


def level_prices(cfg: BotConfig, top: float, tick: float) -> list[float]:
    """Price levels from ``cfg.min_price`` to ``top`` inclusive, tick-aligned."""
    lo = round(cfg.min_price, 6)
    if top < lo - _EPS:
        return []
    n = int((top - lo + _EPS) / tick)
    return [round(lo + i * tick, 6) for i in range(n + 1)]


def build_ladder(
    market: FiveMinMarket,
    up_book: Book,
    down_book: Book,
    cfg: BotConfig,
    spent: float = 0.0,
    up_shares: float = 0.0,
    down_shares: float = 0.0,
) -> list[Quote]:
    """Build the full two-sided ladder subject to all risk caps.

    ``spent`` is cash already committed to fills in this market; ``up_shares``
    / ``down_shares`` are current inventories. Sides over the inventory cap
    are skipped; levels are kept from the top down until the market budget
    (fills + resting notional) is exhausted.
    """
    tick = up_book.tick_size or 0.01
    size = max(cfg.size_per_level, up_book.min_order_size or 0.0)

    # tentative tops (cross-spread rule only)
    up_top = ladder_top(up_book, cfg, tick=tick)
    down_top = ladder_top(down_book, cfg, tick=tick)
    # apply the self-cross guard symmetrically
    if up_top + down_top > 1.0 - tick + _EPS:
        excess = up_top + down_top - (1.0 - tick)
        up_top = round(up_top - excess / 2, 6)
        down_top = round(down_top - excess / 2, 6)

    quotes: list[Quote] = []
    remaining = cfg.max_cost_per_market - spent

    def add_side(book: Book, label: str, token_id: str, top: float,
                 inventory: float) -> None:
        nonlocal remaining
        if inventory >= cfg.max_shares_per_side - _EPS:
            return
        levels = level_prices(cfg, top, tick)
        if len(levels) > cfg.max_levels_per_side:
            levels = levels[-cfg.max_levels_per_side:]  # keep the topmost N
        for price in reversed(levels):  # top-down
            cost = price * size
            if cost > remaining + _EPS:
                break
            remaining -= cost
            quotes.append(
                Quote(token_id=token_id, label=label, price=price, size=size)
            )

    add_side(up_book, "Up", market.up_token, up_top, up_shares)
    add_side(down_book, "Down", market.down_token, down_top, down_shares)
    return quotes
