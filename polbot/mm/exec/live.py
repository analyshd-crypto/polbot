"""Live executor for the mm engine: official polymarket-client SDK (post V2-migration).

Runs an ``AsyncSecureClient`` on a dedicated asyncio thread and exposes a sync
multi-market interface to ``PolBot``:

    place_quotes(win, quotes)   cancel-and-replace for one window (post-only GTC)
    cancel_window(win)          cancel orders on a window's tokens
    cancel_all()                emergency flatten of all resting orders
    poll_fills(wins)            account-trade polling -> Fill list

Order placement uses ``place_limit_order(..., post_only=True)`` so every fill is
a maker fill: zero fees under both the legacy and the post-2026-11-20 schedules.
Falls back with a clear error if the SDK is missing.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional

from ..models import Fill, Quote, Window

log = logging.getLogger("polbot.mm.live")


class _AsyncRunner:
    """Owns an event loop thread; lets sync code schedule coroutines."""

    def __init__(self):
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="mm-live", daemon=True)
        self._thread.start()
        self._started.wait(10)

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._started.set()
        self.loop.run_forever()

    async def _await(self, coro, timeout: float = 20.0):
        return await asyncio.wait_for(coro, timeout=timeout)

    def run(self, coro, timeout: float = 20.0):
        self.start()
        fut = asyncio.run_coroutine_threadsafe(self._await(coro, timeout), self.loop)
        return fut.result(timeout=timeout + 5)

    def stop(self):
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.loop.stop)


class LiveExecutor:
    def __init__(self, cfg):
        self.cfg = cfg
        if not cfg.private_key:
            raise RuntimeError("POLBOT_PRIVATE_KEY not set — cannot run live")
        self.runner = _AsyncRunner()
        self.client = None
        self.wallet = cfg.funder
        self._seen_trades: set[str] = set()
        self._labels: dict[str, str] = {}      # token_id -> outcome
        self._condition_by_token: dict[str, str] = {}

    # ------------------------------------------------------------------ setup
    async def _ensure(self):
        if self.client is None:
            from polymarket import AsyncSecureClient
            self.client = await AsyncSecureClient.create(
                private_key=self.cfg.private_key,
                wallet=self.wallet or None,
            )
        return self.client

    def _register(self, wins: list[Window]):
        for w in wins:
            for i, tok in enumerate(w.tokens):
                self._labels[tok] = w.outcomes[i]
                self._condition_by_token[tok] = w.condition_id

    # ------------------------------------------------------------------ orders
    def place_quotes(self, win: Window, quotes: list[Quote]) -> dict:
        self.cancel_window(win)
        if not quotes:
            return {"placed": 0, "rejected": 0}

        async def _place_all():
            client = await self._ensure()
            placed = rejected = 0
            batch = self.cfg.execution.batch_size
            for i in range(0, len(quotes), batch):
                chunk = quotes[i:i + batch]
                results = await asyncio.gather(
                    *[client.place_limit_order(
                        asset_id=q.token_id,
                        price=f"{q.price:.2f}",
                        size=str(int(q.size)) if q.size >= 1 else f"{q.size:.2f}",
                        side="BUY",
                        post_only=self.cfg.execution.post_only,
                    ) for q in chunk],
                    return_exceptions=True,
                )
                for q, res in zip(chunk, results):
                    if isinstance(res, BaseException) or getattr(res, "ok", True) is False:
                        rejected += 1
                        log.debug("rejected %s %.2f: %s", q.outcome, q.price, res)
                    else:
                        placed += 1
                await asyncio.sleep(0.05)
            return {"placed": placed, "rejected": rejected}

        try:
            return self.runner.run(_place_all(), timeout=30.0)
        except Exception as e:  # noqa: BLE001
            log.warning("place_quotes failed: %s", e)
            return {"placed": 0, "rejected": len(quotes), "error": str(e)}

    def cancel_window(self, win: Window) -> None:
        conds = {w.condition_id for w in [win] if w.condition_id}

        async def _cancel():
            client = await self._ensure()
            for cond in conds:
                try:
                    await client.cancel_market_orders(market=cond)
                except Exception as e:  # noqa: BLE001
                    log.debug("cancel market %s: %s", cond[:10], e)

        try:
            self.runner.run(_cancel(), timeout=15.0)
        except Exception as e:  # noqa: BLE001
            log.warning("cancel_window failed: %s", e)

    def cancel_all(self) -> None:
        async def _kill():
            client = await self._ensure()
            await client.cancel_all()

        try:
            self.runner.run(_kill(), timeout=15.0)
        except Exception as e:  # noqa: BLE001
            log.warning("cancel_all failed: %s", e)

    # ------------------------------------------------------------------ fills
    def poll_fills(self, wins: list[Window]) -> list[Fill]:
        self._register(wins)
        conds = {w.condition_id for w in wins if w.condition_id}

        async def _poll():
            client = await self._ensure()
            fills: list[Fill] = []
            for cond in conds:
                try:
                    pages = client.list_account_trades(market=cond)
                    n = 0
                    try:
                        async for page in pages:
                            self._consume(page.items, fills)
                            n += 1
                            if n >= 2:
                                break
                    except TypeError:
                        for page in pages:
                            self._consume(page.items, fills)
                            n += 1
                            if n >= 2:
                                break
                except Exception as e:  # noqa: BLE001
                    log.debug("trades poll %s: %s", cond[:10], e)
            return fills

        try:
            return self.runner.run(_poll(), timeout=20.0)
        except Exception as e:  # noqa: BLE001
            log.warning("poll_fills failed: %s", e)
            return []

    def _consume(self, trades, out: list[Fill]) -> None:
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
            out.append(Fill(token_id=asset, outcome=label, price=price, size=size,
                            ts=time.time(), fee=0.0, order_id=str(tid)))

    # ------------------------------------------------------------------ misc
    def balance(self) -> float:
        async def _bal():
            client = await self._ensure()
            res = await client.get_balance_allowance()
            return res
        try:
            res = self.runner.run(_bal(), timeout=15.0)
            return float(getattr(res, "balance", 0) or 0)
        except Exception as e:  # noqa: BLE001
            log.warning("balance failed: %s", e)
            return 0.0

    def close(self) -> None:
        try:
            if self.client is not None:
                self.runner.run(self.client.close(), timeout=10.0)
        except Exception:  # noqa: BLE001
            pass
        self.runner.stop()
