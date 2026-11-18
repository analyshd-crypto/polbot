"""Client-side token-bucket rate limiting for CLOB order/cancel requests.

Polymarket enforces per-signer token buckets (Standard tier: 40 order
tokens/s with burst 60, 80 cancel tokens/s with burst 120; a batch of N
orders costs N tokens; cancel-all/cancel-market costs 1 + canceled orders).
We mirror those buckets client-side so a refresh never trips a 429.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

log = logging.getLogger("polbot.ratelimit")


class TokenBucket:
    """Classic continuous-refill token bucket."""

    def __init__(
        self,
        rate_per_s: float,
        burst: float,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rate = float(rate_per_s)
        self.burst = float(burst)
        self.tokens = float(burst)
        self._now = now
        self._sleep = sleep

    def _refill(self) -> None:
        t = self._now()
        if self._last < 0:
            self._last = t
            return
        elapsed = max(0.0, t - self._last)
        self._last = t
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate)

    _last: float = -1.0

    def acquire(self, n: float = 1.0) -> None:
        """Block until ``n`` tokens are available, then consume them."""
        while True:
            self._refill()
            if self.tokens >= n:
                self.tokens -= n
                return
            need = n - self.tokens
            wait = need / self.rate
            log.debug("rate limit: need %.1f more tokens, waiting %.2fs", need, wait)
            self._sleep(wait)

    def try_acquire(self, n: float = 1.0) -> bool:
        self._refill()
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


class RateLimits:
    """Order + cancel buckets, as enforced per signer by the CLOB."""

    def __init__(
        self,
        order_rate_per_s: float,
        order_burst: float,
        cancel_rate_per_s: float,
        cancel_burst: float,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.orders = TokenBucket(order_rate_per_s, order_burst, now, sleep)
        self.cancels = TokenBucket(cancel_rate_per_s, cancel_burst, now, sleep)
