"""Event-driven backtester replaying REAL Polymarket price history.

How it works
------------
Each cached window carries the real 1-minute price history for both outcome
tokens plus the real resolution. The engine runs unchanged (paper backend +
simulated clock). The replay data source serves synthetic order books whose
best ask equals the observed market price at that minute, so:

- the ladder tops out just below the observed price (as live),
- resting bids fill when the observed price falls to/below them,
- the pull rule, budget caps and inventory caps all apply,
- settlement uses the REAL outcome.

Known limitations (kept honest, see README):
- 1-minute fidelity under-samples intra-minute sweeps, so the number of
  fills is conservative relative to a 5-second live refresh cadence;
- partial depth is not modelled: each level fills fully at its bid price;
- queue position is not modelled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .backends import PaperBackend
from .clock import SimClock
from .clob import Book
from .config import BotConfig
from .data import WindowRecord
from .engine import Engine, MarketData
from .markets import FiveMinMarket
from .pnl import RunStats, WindowResult
from .risk import RiskManager

log = logging.getLogger("polbot.backtest")

_BIG = 1_000_000.0


@dataclass
class BacktestReport:
    asset: str
    windows: int
    cfg_summary: str
    stats: RunStats
    results: list = field(default_factory=list)

    def summary_text(self) -> str:
        s = self.stats
        avg = s.total_pnl / s.windows if s.windows else 0.0
        lines = [
            f"asset={self.asset}  windows={s.windows}",
            f"config : {self.cfg_summary}",
            f"win rate       : {s.win_rate:.1%}  ({s.wins}W / {s.losses}L)",
            f"total pnl      : {s.total_pnl:+.2f}",
            f"avg per window : {avg:+.3f}",
            f"best window    : {s.best_pnl:+.2f}",
            f"worst window   : {s.worst_pnl:+.2f}",
            f"max drawdown   : {s.max_drawdown:.2f}",
            f"fills          : {s.total_fills} (shares {s.total_shares:.0f})",
            f"capital deployed (sum of fill costs): {s.total_spent:.2f}",
        ]
        return "\n".join(lines)


class ReplayMarketData(MarketData):
    """Serves one window's record at a time, priced at the sim clock's time."""

    def __init__(self, cfg: BotConfig, clock: SimClock) -> None:
        self.cfg = cfg
        self.clock = clock
        self.current: Optional[WindowRecord] = None

    def set_window(self, rec: WindowRecord) -> None:
        self.current = rec

    # ------------------------------------------------------------- history --
    def _history(self) -> list[tuple[int, tuple[Optional[float], Optional[float]]]]:
        rec = self.current
        merged: dict[int, list[Optional[float]]] = {}
        for t, p in rec.up_hist:
            merged.setdefault(int(t), [None, None])[0] = float(p)
        for t, p in rec.down_hist:
            merged.setdefault(int(t), [None, None])[1] = float(p)
        return sorted((t, (v[0], v[1])) for t, v in merged.items())

    # ------------------------------------------------------------ interface --
    def get_market_sync(self, window_ts: int) -> Optional[FiveMinMarket]:
        rec = self.current
        if rec is None or rec.window_ts != window_ts:
            return None
        return FiveMinMarket(
            asset=self.cfg.asset,
            window_ts=rec.window_ts,
            end_ts=rec.end_ts,
            slug=rec.slug,
            condition_id=rec.condition_id,
            up_token=rec.up_token,
            down_token=rec.down_token,
            question=rec.slug,
            accepting_orders=True,
            closed=True,
        )

    async def get_market(self, window_ts: int) -> Optional[FiveMinMarket]:
        return self.get_market_sync(window_ts)

    def _book(self, token_id: str, price: float) -> Book:
        tick = 0.01
        return Book(
            token_id=token_id,
            bids=[(round(price - tick, 6), _BIG)],
            asks=[(round(price, 6), _BIG)],
            tick_size=tick,
            min_order_size=5.0,
            last_trade_price=None,
        )

    async def get_books(self, market: FiveMinMarket) -> Optional[tuple[Book, Book]]:
        rec = self.current
        if rec is None:
            return None
        now = int(self.clock.now())
        up_price = down_price = None
        for t, (u, d) in self._history():
            if t <= now:
                if u is not None:
                    up_price = u
                if d is not None:
                    down_price = d
            else:
                break
        if up_price is None and down_price is None:
            return None
        if up_price is None:
            up_price = 1.0 - (down_price or 0.5)
        if down_price is None:
            down_price = 1.0 - (up_price or 0.5)
        return (
            self._book(market.up_token, up_price),
            self._book(market.down_token, down_price),
        )

    async def get_outcome(self, market: FiveMinMarket) -> Optional[str]:
        return self.current.winner if self.current else None


async def run_backtest(
    cfg: BotConfig,
    records: list[WindowRecord],
) -> BacktestReport:
    """Replay ``records`` (oldest first) through the engine with paper fills."""
    clock = SimClock(start=float(records[0].window_ts) if records else 0.0)
    data = ReplayMarketData(cfg, clock)
    backend = PaperBackend(fill_ratio=cfg.paper_fill_ratio)
    risk = RiskManager(cfg)
    stats = RunStats()
    engine = Engine(cfg, data, backend, clock, risk, stats)

    for rec in records:
        data.set_window(rec)
        clock._now = float(rec.window_ts)
        market = await data.get_market(rec.window_ts)
        if market is None:  # pragma: no cover - defensive
            continue
        await engine.run_one(market)

    return BacktestReport(
        asset=cfg.asset,
        windows=len(records),
        cfg_summary=cfg.describe(),
        stats=stats,
        results=list(risk._windows),
    )


def results_to_rows(results: list[WindowResult]) -> list[dict]:
    return [
        {
            "window_ts": r.window_ts,
            "slug": r.slug,
            "winner": r.winner,
            "up_shares": round(r.up_shares, 2),
            "down_shares": round(r.down_shares, 2),
            "spent": round(r.spent, 4),
            "pnl": round(r.pnl, 4),
            "fills": r.fills,
            "peak_quotes": r.peak_quotes,
        }
        for r in results
    ]
