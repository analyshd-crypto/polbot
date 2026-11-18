"""Fair-value-anchored ladder (v1 strategy) + endgame hedge.

The v0 "legacy" ladder quotes every cent below the market on both sides. The
backtest showed why that loses: fills arrive almost exclusively when the
market is crashing *through* your levels (adverse selection) — you buy the
losing side all the way down.

The v1 ladder anchors on a model fair value ``F`` (Binance/Chainlink gap ->
probability):

- bid UP only at prices <= F - min_edge   (and DOWN at <= (1-F) - min_edge)
- inventory skew: quotes on the side you already own shift down
- toxicity guard: after N consecutive one-sided fills, pause that side
- endgame hedge: in the final seconds, buy the opposite side to flatten
  (the "xmayeth-ladder" move seen in the demo video), optionally always-on
  ("winrate mode") to avoid holding any position through settlement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .clob import Book
from .config import BotConfig
from .fairvalue import FairValue
from .grid import Quote
from .markets import FiveMinMarket
from .pnl import WindowLedger

log = logging.getLogger("polbot.fvladder")

_EPS = 1e-9


@dataclass
class FVLadderConfig:
    """Parameters layered on top of BotConfig for the v1 strategy."""
    min_edge: float = 0.03          # never quote closer than 3c to fair value
    levels_per_side: int = 10
    level_step: float = 0.02        # 2c between levels (not every cent)
    skew_per_side: float = 0.05     # shift quotes down per max_inventory fraction used
    max_quote: float = 0.90         # never bid above 90c (adverse selection zone)
    min_quote: float = 0.05
    endgame_hedge: bool = True      # flatten via opposite side near expiry
    hedge_before_end_s: float = 20.0
    hedge_max_price: float = 0.96
    winrate_mode: bool = False      # always flatten before settlement
    toxicity_fills: int = 4         # consecutive one-sided fills before pause
    toxicity_pause_s: float = 20.0


@dataclass
class ToxicityMonitor:
    """Counts consecutive one-sided fills; pauses the filling side."""
    limit: int = 4
    pause_s: float = 20.0
    last_fill_side: Optional[str] = None
    streak: int = 0
    paused_until: dict = field(default_factory=lambda: {"Up": 0.0, "Down": 0.0})

    def on_fill(self, label: str, now: float) -> None:
        if label == self.last_fill_side:
            self.streak += 1
        else:
            self.streak = 1
            self.last_fill_side = label
        if self.streak >= self.limit:
            self.paused_until[label] = now + self.pause_s
            self.streak = 0
            log.warning("toxicity guard: pausing %s bids for %.0fs", label, self.pause_s)

    def paused(self, label: str, now: float) -> bool:
        return now < self.paused_until.get(label, 0.0)


def build_fv_ladder(
    market: FiveMinMarket,
    up_book: Book,
    down_book: Book,
    cfg: BotConfig,
    fvc: FVLadderConfig,
    fv: FairValue,
    ledger: WindowLedger,
    now: float,
) -> list[Quote]:
    """Two-sided ladder anchored on the model fair value."""
    tick = up_book.tick_size or 0.01
    size = max(cfg.size_per_level, up_book.min_order_size or 0.0)
    p_up = fv.p_up

    budget = cfg.max_cost_per_market - ledger.spent
    if budget <= 0:
        return []

    inv_frac_up = ledger.up.shares / max(1.0, cfg.max_shares_per_side)
    inv_frac_dn = ledger.down.shares / max(1.0, cfg.max_shares_per_side)

    toxicity = getattr(ledger, "_toxicity", None)

    quotes: list[Quote] = []

    def side_ladder(label: str, token: str, book: Book,
                    p_fair: float, inv_frac: float) -> None:
        nonlocal budget
        if toxicity is not None and toxicity.paused(label, now):
            return
        if ledger.up.shares >= cfg.max_shares_per_side - _EPS and label == "Up":
            return
        if ledger.down.shares >= cfg.max_shares_per_side - _EPS and label == "Down":
            return
        # top of the ladder: fair value minus min_edge, minus inventory skew,
        # never above max_quote, and below the current ask (post-only safety)
        top = p_fair - fvc.min_edge - fvc.skew_per_side * min(1.0, inv_frac * 5.0)
        top = min(top, fvc.max_quote)
        if book.best_ask is not None:
            top = min(top, book.best_ask - tick)
        top = max(top, fvc.min_quote)
        # snap to tick grid
        top = round(int((top + _EPS) / tick) * tick, 6)
        for i in range(fvc.levels_per_side):
            price = round(top - i * max(fvc.level_step, tick), 6)
            if price < fvc.min_quote - _EPS:
                break
            cost = price * size
            if cost > budget + _EPS:
                break
            budget -= cost
            quotes.append(Quote(token_id=token, label=label, price=price, size=size))

    # both sides expressed as UP/DOWN bids below their own fair values
    side_ladder("Up", market.up_token, up_book, p_up, inv_frac_up)
    side_ladder("Down", market.down_token, down_book, 1.0 - p_up, inv_frac_dn)

    # self-cross guard: our UP bid at p_u and DOWN bid at p_d must satisfy
    # p_u + p_d <= 1 - tick (else the matcher can mint sets against us)
    up_max = max((q.price for q in quotes if q.label == "Up"), default=None)
    dn_max = max((q.price for q in quotes if q.label == "Down"), default=None)
    if up_max is not None and dn_max is not None and up_max + dn_max > 1.0 - tick + _EPS:
        excess = up_max + dn_max - (1.0 - tick)
        for q in quotes:
            if q.label == "Up":
                q.price = round(max(fvc.min_quote, q.price - excess / 2), 6)
            else:
                q.price = round(max(fvc.min_quote, q.price - excess / 2), 6)
    return quotes


def endgame_hedge(
    market: FiveMinMarket,
    ledger: WindowLedger,
    fvc: FVLadderConfig,
    now: float,
    books: Optional[tuple[Book, Book]] = None,
) -> list[Quote]:
    """Flatten inventory near expiry by buying the opposite side.

    Returns aggressive BUY quotes for the side that cancels our exposure.
    Fill at any price <= hedge_max_price. In winrate mode this always runs
    for both sides (full flatten); otherwise it only de-risks outsized
    one-sided inventory, mirroring the demo bot's "buy DOWN at 95c" move.
    """
    time_left = market.end_ts - now
    if time_left > fvc.hedge_before_end_s or time_left <= 0:
        return []
    up, dn = ledger.up.shares, ledger.down.shares
    if up + dn <= _EPS:
        return []
    net = up - dn
    need = abs(net)
    if need < 5.0 and not fvc.winrate_mode:
        return []  # not worth a 5-share minimum order

    hedge: list[Quote] = []
    if net > 0:  # long UP -> buy DOWN
        price = fvc.hedge_max_price
        if books is not None and books[1].best_ask is not None:
            price = min(price, max(books[1].best_ask, 0.01))
        shares = need if not fvc.winrate_mode else dn and need or need
        shares = need
        hedge.append(Quote(token_id=market.down_token, label="Down",
                           price=round(price, 2), size=shares, ))
    elif net < 0:
        price = fvc.hedge_max_price
        if books is not None and books[0].best_ask is not None:
            price = min(price, max(books[0].best_ask, 0.01))
        hedge.append(Quote(token_id=market.up_token, label="Up",
                           price=round(price, 2), size=need))
    return hedge
