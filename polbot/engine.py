"""The market-making engine.

Per 5-minute window the engine runs a small state machine mirroring the
demonstrated bot:

    quoting  ->  refresh every cfg.refresh_interval_s (cancel + repost ladder)
             ->  pulled  at end_ts - cfg.pull_before_end_s
             ->  (v1) endgame hedge window: flatten via the opposite side
             ->  resolved once the outcome is known (hold to resolution)

The engine is execution-agnostic: it drives an
:class:`polbot.backends.ExecutionBackend` (paper for dry-run / backtest,
live for real orders) and reads market state from a
:class:`MarketData` provider (live REST or replayed history).

v1 adds an optional fair-value anchor: a :class:`FairValueFeed` supplies
spot/strike/sigma and the ladder is anchored on the model probability
(:mod:`polbot.fvladder`) instead of blindly quoting every cent.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

from .clock import Clock
from .clob import Book
from .config import BotConfig
from .fairvalue import FairValue, prob_up
from .fvladder import FVLadderConfig, ToxicityMonitor, build_fv_ladder, endgame_hedge
from .grid import build_ladder
from .markets import FiveMinMarket, fetch_market, fetch_outcome, next_window_start, window_start
from .net import HttpClient
from .pnl import RunStats, WindowLedger, WindowResult
from .risk import RiskManager

log = logging.getLogger("polbot.engine")


# --------------------------------------------------------------------- data --
class MarketData(ABC):
    """Source of market metadata, order books and outcomes."""

    @abstractmethod
    async def get_market(self, window_ts: int) -> Optional[FiveMinMarket]:
        ...

    @abstractmethod
    async def get_books(self, market: FiveMinMarket) -> Optional[tuple[Book, Book]]:
        ...

    @abstractmethod
    async def get_outcome(self, market: FiveMinMarket) -> Optional[str]:
        """Return "up"/"down" once resolved. May poll; None if unavailable."""

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None


class LiveMarketData(MarketData):
    """Reads Gamma + public CLOB endpoints (no auth needed).

    The HTTP calls are synchronous (stdlib urllib); at our cadence (a couple
    of reads per refresh) blocking the event loop for ~100ms is acceptable
    and keeps the read path dependency-free.
    """

    def __init__(self, cfg: BotConfig, http: Optional[HttpClient] = None,
                 clock: Optional[Clock] = None) -> None:
        self.cfg = cfg
        self.http = http or HttpClient(
            timeout=cfg.request_timeout_s,
            retry_attempts=cfg.http_retry_attempts,
            retry_backoff_s=cfg.http_retry_backoff_s,
        )
        self.clock = clock

    async def get_market(self, window_ts: int) -> Optional[FiveMinMarket]:
        return fetch_market(self.http, self.cfg.gamma_url, self.cfg.asset, window_ts)

    async def get_books(self, market: FiveMinMarket) -> Optional[tuple[Book, Book]]:
        from .clob import fetch_books

        return fetch_books(self.http, self.cfg.clob_url, market.up_token, market.down_token)

    async def get_outcome(self, market: FiveMinMarket) -> Optional[str]:
        import asyncio

        deadline = None
        if self.clock is not None:
            deadline = self.clock.now() + self.cfg.settle_timeout_s
        while True:
            outcome = fetch_outcome(self.http, self.cfg.gamma_url, market)
            if outcome is not None:
                return outcome
            if deadline is None:
                return None
            if self.clock.now() >= deadline:
                return None
            await asyncio.sleep(3.0)


class FairValueFeed:
    """Supplies the model fair value for the live engine.

    Strike ("price to beat"): the first Binance spot print observed at/after
    the window open. In live trading the Chainlink stream (SDK
    ``crypto_prices_chainlink``) is the settlement source and should be
    preferred; Binance is the fallback anchor and always the vol source.
    """

    def __init__(self, cfg: BotConfig, http: Optional[HttpClient] = None,
                 binance_url: str = "https://api.binance.com") -> None:
        from .binance import BinanceFeed

        self.cfg = cfg
        self.http = http or HttpClient(timeout=10.0)
        self.feed = BinanceFeed(self.http, binance_url, cfg.asset)
        self._strike: Optional[float] = None
        self._strike_window: Optional[int] = None

    def fair_value(self, market: FiveMinMarket, now: float) -> FairValue:
        self.feed.refresh(now)
        spot = self.feed.spot()
        if self._strike_window != market.window_ts:
            # reset the strike for a new window
            self._strike = None
            self._strike_window = market.window_ts
        if self._strike is None and spot is not None:
            self._strike = spot  # first print of the window (approximation)
        t_left = market.end_ts - now
        return prob_up(spot, self._strike, self.feed.sigma(), t_left)


# ------------------------------------------------------------------ engine --
class Engine:
    def __init__(self, cfg: BotConfig, data: MarketData, backend, clock: Clock,
                 risk: Optional[RiskManager] = None,
                 stats: Optional[RunStats] = None,
                 fvc: Optional[FVLadderConfig] = None,
                 fv_feed: Optional["FairValueFeed"] = None) -> None:
        self.cfg = cfg
        self.data = data
        self.backend = backend
        self.clock = clock
        self.risk = risk or RiskManager(cfg)
        self.stats = stats or RunStats()
        # v1 strategy layer: None => legacy v0 ladder (quote every cent)
        self.fvc = fvc
        self.fv_feed = fv_feed

    # ------------------------------------------------------------- public --
    async def run_forever(self, max_windows: Optional[int] = None) -> RunStats:
        """Live scheduler: join the current window, then every next window."""
        done = 0
        misses = 0
        while self.risk.may_run():
            if max_windows is not None and done >= max_windows:
                break
            now = self.clock.now()
            wts = window_start(now)
            market = await self.data.get_market(wts)
            if market is None:
                misses += 1
                if misses >= self.cfg.consecutive_error_limit:
                    self.risk.halt("market discovery kept failing")
                    break
                await self.clock.sleep(3.0)
                continue
            misses = 0
            pull_at = market.end_ts - self.cfg.pull_before_end_s
            if now >= pull_at:
                # too late for this window; wait for the next one
                nxt = next_window_start(now) + self.cfg.join_delay_s
                await self.clock.sleep(max(1.0, nxt - now))
                continue
            await self.run_one(market)
            done += 1
        await self.backend.cancel_market()
        return self.stats

    async def run_one(self, market: FiveMinMarket) -> WindowResult:
        """Run the full lifecycle for a single window."""
        cfg = self.cfg
        ledger = WindowLedger(market=market)
        log.info(
            "[%s #%d] window %d-%d | %s",
            cfg.asset, market.window_ts, market.window_ts, market.end_ts,
            market.question,
        )

        # wait for the open (+ join delay)
        start_at = market.window_ts + cfg.join_delay_s
        now = self.clock.now()
        if now < start_at:
            await self.clock.sleep(start_at - now)

        await self.backend.start(market)
        pull_at = market.end_ts - cfg.pull_before_end_s
        hedge_at = (market.end_ts - self.fvc.hedge_before_end_s
                    if self.fvc else None)
        peak_quotes = 0
        toxicity = ToxicityMonitor(self.fvc.toxicity_fills,
                                   self.fvc.toxicity_pause_s) if self.fvc else None

        # ---------------- quoting loop ----------------
        while True:
            now = self.clock.now()
            if now >= pull_at or not self.risk.may_run():
                break
            try:
                books = await self.data.get_books(market)
                if books is not None and (books[0].best_ask or books[1].best_ask):
                    # 1) fills from market moves since the last refresh
                    await self.backend.on_books(books[0], books[1])
                    for fill in await self.backend.poll_fills():
                        ledger.apply(fill)
                        if toxicity is not None:
                            toxicity.on_fill(fill.label, now)
                        log.info(
                            "[%s #%d] FILL %s %.2f x %.0f",
                            cfg.asset, market.window_ts, fill.label,
                            fill.price, fill.shares,
                        )
                    # 2) cancel + repost the ladder
                    if self.fvc is not None and self.fv_feed is not None:
                        fv = self.fv_feed.fair_value(market, now)
                        ledger._toxicity = toxicity
                        quotes = build_fv_ladder(
                            market, books[0], books[1], cfg, self.fvc, fv,
                            ledger, now,
                        )
                    else:
                        quotes = build_ladder(
                            market, books[0], books[1], cfg,
                            spent=ledger.spent,
                            up_shares=ledger.up.shares,
                            down_shares=ledger.down.shares,
                        )
                    # 2b) endgame hedge: aggressive buys of the opposite side
                    if (self.fvc is not None and self.fvc.endgame_hedge
                            and hedge_at is not None and now >= hedge_at):
                        hedges = endgame_hedge(market, ledger, self.fvc, now,
                                               books)
                        if hedges:
                            log.info(
                                "[%s #%d] endgame hedge: %s",
                                cfg.asset, market.window_ts,
                                [(h.label, round(h.price, 2), round(h.size, 1))
                                 for h in hedges],
                            )
                            quotes = quotes + hedges
                    await self.backend.cancel_market()
                    await self.backend.place(quotes)
                    peak_quotes = max(peak_quotes, len(quotes))
                    log.info(
                        "[%s #%d] quoting t-%ds | up %.2f/%.2f dn %.2f/%.2f | q %d | %s | total %s",
                        cfg.asset, market.window_ts, int(market.end_ts - now),
                        books[0].best_bid or 0, books[0].best_ask or 0,
                        books[1].best_bid or 0, books[1].best_ask or 0,
                        len(quotes), ledger.summary(), self.stats.line(),
                    )
                self.risk.on_cycle_ok()
            except Exception as exc:  # noqa: BLE001
                self.risk.on_cycle_error(exc)
                if self.risk.halted:
                    break
            await self.clock.sleep(cfg.refresh_interval_s)

        # ---------------- pull ----------------
        await self.backend.cancel_market()
        log.info("[%s #%d] pulled (t-%ds) | %s",
                 cfg.asset, market.window_ts, int(market.end_ts - self.clock.now()),
                 ledger.summary())

        # ---------------- resolve & settle ----------------
        winner = await self.data.get_outcome(market)
        if winner is None:
            log.warning("[%s #%d] outcome unavailable; window not settled",
                        cfg.asset, market.window_ts)
            pnl = 0.0
        else:
            pnl = ledger.settle(winner)
        result = WindowResult(
            window_ts=market.window_ts,
            slug=market.slug,
            winner=winner,
            up_shares=ledger.up.shares,
            down_shares=ledger.down.shares,
            spent=ledger.spent,
            pnl=pnl,
            fills=len(ledger.fills),
            peak_quotes=peak_quotes,
        )
        self.stats.record(result)
        self.risk.on_window(result)
        log.info(
            "[%s #%d] resolved %s | pnl %+.2f | %s",
            cfg.asset, market.window_ts, (winner or "?").upper(), pnl,
            self.stats.line(),
        )
        return result
