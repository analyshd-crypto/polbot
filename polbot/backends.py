"""Execution backends.

- :class:`PaperBackend` — dry-run / backtest engine. Keeps the ladder in
  memory and simulates fills when the live (or replayed) market trades at or
  below our resting bid prices. No orders are ever sent.

- :class:`LiveBackend` — places real post-only GTC/GTD BUY orders through the
  official ``polymarket-client`` SDK (``AsyncSecureClient``), cancels per
  market, polls account trades for fills, and exposes a kill switch.

Paper fill model (documented, conservative):
a resting BUY at price ``p`` fills when the observed book shows selling at or
below ``p``: a *new* last-trade print <= p, or best ask <= p. In replay mode
(only mid prices available) a fill is recorded when the mid <= p. Partial
depth is not modelled: levels fill fully.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .clock import Clock
from .clob import Book
from .config import BotConfig
from .markets import FiveMinMarket
from .pnl import Fill
from .ratelimit import RateLimits

log = logging.getLogger("polbot.backends")

_EPS = 1e-9


class ExecutionBackend:  # pragma: no cover - interface
    """What the engine drives."""

    async def start(self, market: FiveMinMarket) -> None: ...

    async def on_books(self, up_book: Book, down_book: Book) -> None: ...

    async def place(self, quotes) -> None: ...

    async def cancel_market(self) -> None: ...

    async def kill(self) -> None: ...

    async def poll_fills(self) -> list[Fill]: ...

    async def close(self) -> None: ...

    def status(self) -> str:
        return ""


# -------------------------------------------------------------------- paper --
class PaperBackend(ExecutionBackend):
    """Simulates the ladder locally; fills from observed market prices.

    ``fill_ratio`` (from the config) models queue position: each sweep event
    fills that fraction of a level's remaining size. 1.0 is optimistic
    (front of queue), 0.5 conservative (sharing the level with other makers).
    """

    def __init__(self, fill_ratio: float = 1.0) -> None:
        # token_id -> {price: shares}
        self.resting: dict[str, dict[float, float]] = {}
        self.fill_ratio = max(0.0, min(1.0, fill_ratio))
        self._pending: list[Fill] = []
        self._last_trade: dict[str, Optional[float]] = {}
        self.cash_spent = 0.0
        self.total_fills = 0

    async def start(self, market: FiveMinMarket) -> None:
        self.resting = {market.up_token: {}, market.down_token: {}}
        self._labels = {market.up_token: "Up", market.down_token: "Down"}
        self._last_trade = {}

    async def on_books(self, up_book: Book, down_book: Book) -> None:
        for book in (up_book, down_book):
            self._apply_book(book.token_id, book)

    def _apply_book(self, token_id: str, book: Book) -> None:
        levels = self.resting.get(token_id)
        if not levels:
            return
        prev = self._last_trade.get(token_id, None)
        new_trade = book.last_trade_price
        trade_is_new = new_trade is not None and (prev is None or abs(new_trade - prev) > _EPS)
        self._last_trade[token_id] = new_trade
        ask = book.best_ask
        for price in sorted(levels, reverse=True):
            filled = False
            if trade_is_new and new_trade is not None and new_trade <= price + _EPS:
                filled = True
            elif ask is not None and ask <= price + _EPS:
                filled = True
            elif ask is None and new_trade is None and book.mid is not None \
                    and book.mid <= price + _EPS:
                # replay mode: only a mid is available
                filled = True
            if filled:
                remaining_size = levels[price]
                fill_shares = remaining_size if self.fill_ratio >= 1.0 \
                    else remaining_size * self.fill_ratio
                if fill_shares >= remaining_size - _EPS:
                    fill_shares = remaining_size
                    del levels[price]
                else:
                    levels[price] = remaining_size - fill_shares
                if fill_shares <= _EPS:
                    continue
                self.cash_spent += price * fill_shares
                self.total_fills += 1
                self._pending.append(
                    Fill(ts=0.0, token_id=token_id,
                         label=self._labels.get(token_id, "?"),
                         price=price, shares=fill_shares)
                )

    _labels: dict[str, str] = {}

    async def place(self, quotes) -> None:
        # place() assumes cancel_market() just ran; rebuild the book
        for q in quotes:
            self.resting.setdefault(q.token_id, {})[q.price] = q.size

    async def cancel_market(self) -> None:
        for levels in self.resting.values():
            levels.clear()

    async def kill(self) -> None:
        await self.cancel_market()

    async def poll_fills(self) -> list[Fill]:
        out = self._pending
        self._pending = []
        return out

    async def close(self) -> None:
        return None

    def resting_count(self) -> int:
        return sum(len(v) for v in self.resting.values())

    def resting_notional(self) -> float:
        return sum(p * s for levels in self.resting.values() for p, s in levels.items())

    def status(self) -> str:
        return f"paper resting={self.resting_count()} notional={self.resting_notional():.2f}"


# --------------------------------------------------------------------- live --
class LiveBackend(ExecutionBackend):
    """Real orders via the official polymarket-client SDK."""

    #: place orders in chunks of this size (fits the Standard-tier burst)
    CHUNK = 40

    def __init__(
        self,
        cfg: BotConfig,
        private_key: str,
        wallet: Optional[str] = None,
        clock: Optional[Clock] = None,
    ) -> None:
        if not private_key:
            raise ValueError("live mode requires POLYMARKET_PRIVATE_KEY")
        self.cfg = cfg
        self.private_key = private_key
        self.wallet = wallet
        self.clock = clock
        self.client = None
        self.market: Optional[FiveMinMarket] = None
        self._active_orders: dict[str, tuple[str, float, float]] = {}
        self._seen_trades: set[str] = set()
        self._pending: list[Fill] = []
        self._labels: dict[str, str] = {}
        self.limiter = RateLimits(
            cfg.order_rate_per_s, cfg.order_burst,
            cfg.cancel_rate_per_s, cfg.cancel_burst,
        )

    # ---------------------------------------------------------------- setup --
    async def _ensure_client(self):
        if self.client is None:
            from polymarket import AsyncSecureClient

            self.client = await AsyncSecureClient.create(
                private_key=self.private_key,
                wallet=self.wallet or None,
            )
        return self.client

    async def start(self, market: FiveMinMarket) -> None:
        await self._ensure_client()
        self.market = market
        self._labels = {market.up_token: "Up", market.down_token: "Down"}
        # pick up any of our orders left resting in this market
        await self.cancel_market()

    async def check_connection(self) -> str:
        """Connectivity/credentials probe used by `polbot doctor`."""
        client = await self._ensure_client()
        pages = client.list_open_orders()
        n = 0
        try:
            async for page in pages:
                n += len(page.items)
                break
        except TypeError:  # sync paginator fallback
            for page in pages:
                n += len(page.items)
                break
        return f"authenticated; open orders visible: {n}"

    # --------------------------------------------------------------- quoting --
    def _price_str(self, price: float) -> str:
        tick = 0.01
        s = f"{tick:.10f}".rstrip("0")
        decimals = len(s.split(".")[1]) if "." in s else 0
        return f"{price:.{max(2, decimals)}f}"

    def _expiration(self) -> Optional[int]:
        if not self.cfg.use_gtd or self.market is None or self.clock is None:
            return None
        exp = int(self.market.end_ts + 60)  # CLOB kills GTD 60s early by design
        if exp - self.clock.now() < 180:  # minimum GTD lifetime
            return None
        return exp

    async def place(self, quotes) -> None:
        client = await self._ensure_client()
        quotes = list(quotes)
        if not quotes:
            return
        expiration = self._expiration()
        for i in range(0, len(quotes), self.CHUNK):
            chunk = quotes[i:i + self.CHUNK]
            self.limiter.orders.acquire(len(chunk))

            async def _place(q):
                return await client.place_limit_order(
                    asset_id=q.token_id,
                    price=self._price_str(q.price),
                    size=self._price_str(q.size) if q.size < 1 else str(int(q.size)),
                    side="BUY",
                    post_only=self.cfg.post_only,
                    expiration=expiration,
                )

            results = await asyncio.gather(
                *[_place(q) for q in chunk], return_exceptions=True
            )
            for q, res in zip(chunk, results):
                if isinstance(res, BaseException):
                    log.warning("order rejected (%s %.2f): %s", q.label, q.price, res)
                    continue
                ok = getattr(res, "ok", None)
                if ok is False:
                    log.warning(
                        "order rejected (%s %.2f): %s %s", q.label, q.price,
                        getattr(res, "code", "?"), getattr(res, "message", ""),
                    )
                    continue
                oid = getattr(res, "order_id", None)
                if oid:
                    self._active_orders[oid] = (q.token_id, q.price, q.size)
        log.debug("placed %d orders (exp=%s)", len(quotes), expiration)

    async def cancel_market(self) -> None:
        if self.client is None or self.market is None:
            return
        est = 1 + max(1, len(self._active_orders))
        self.limiter.cancels.acquire(est)
        try:
            resp = await self.client.cancel_market_orders(
                market=self.market.condition_id
            )
            canceled = getattr(resp, "canceled", None) or []
            if canceled:
                log.info("canceled %d orders", len(canceled))
        except Exception as exc:  # noqa: BLE001
            log.warning("cancel_market_orders failed: %s", exc)
        self._active_orders.clear()

    async def kill(self) -> None:
        if self.client is None:
            return
        try:
            await self.client.cancel_all()
            log.warning("KILL SWITCH: canceled all open orders")
        except Exception as exc:  # noqa: BLE001
            log.error("kill switch failed: %s", exc)
        self._active_orders.clear()

    # ----------------------------------------------------------------- fills --
    async def on_books(self, up_book: Book, down_book: Book) -> None:
        return None  # fills are detected from account trades, not the book

    async def poll_fills(self) -> list[Fill]:
        if self.client is None or self.market is None:
            return []
        fills: list[Fill] = []
        try:
            pages = self.client.list_account_trades(market=self.market.condition_id)
            seen_pages = 0
            try:
                async for page in pages:
                    self._consume_trades(page.items, fills)
                    seen_pages += 1
                    if seen_pages >= 3:
                        break
            except TypeError:  # sync paginator fallback
                for page in pages:
                    self._consume_trades(page.items, fills)
                    seen_pages += 1
                    if seen_pages >= 3:
                        break
        except Exception as exc:  # noqa: BLE001
            log.warning("polling trades failed: %s", exc)
        return fills

    def _consume_trades(self, trades, out: list[Fill]) -> None:
        for t in trades or []:
            tid = getattr(t, "id", None)
            if tid is None or tid in self._seen_trades:
                continue
            self._seen_trades.add(tid)
            asset = getattr(t, "asset_id", None) or getattr(t, "token_id", None)
            label = self._labels.get(asset)
            if label is None:
                continue
            try:
                price = float(getattr(t, "price"))
                size = float(getattr(t, "size"))
            except (TypeError, ValueError):
                continue
            out.append(Fill(ts=0.0, token_id=asset, label=label,
                            price=price, shares=size))

    async def close(self) -> None:
        if self.client is not None:
            try:
                await self.client.close()
            except Exception:  # noqa: BLE001
                pass
            self.client = None

    def status(self) -> str:
        return f"live active_orders={len(self._active_orders)}"
