"""Configuration for polbot.

All tunables live in one dataclass. Values can be overridden with ``POLBOT_*``
environment variables (see ``.env.example``).
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Optional

SUPPORTED_ASSETS = ("btc", "eth", "sol", "xrp")

#: 5-minute windows are aligned to unix timestamps divisible by 300.
WINDOW_SECONDS = 300

_TRUTHY = {"1", "true", "yes", "y", "on"}


def _to_float(value: Any) -> float:
    return float(value)


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUTHY


@dataclass
class BotConfig:
    """All runtime configuration for one bot instance."""

    # --- market selection ---------------------------------------------------
    asset: str = "btc"

    # --- ladder (grid) parameters --------------------------------------------
    #: shares per price level; the CLOB rejects orders below its min size (5).
    size_per_level: float = 5.0
    #: lowest ladder price (inclusive)
    min_price: float = 0.01
    #: highest ladder price (inclusive, subject to "below the ask" rule)
    max_price: float = 0.99
    #: top of ladder = best_ask - buffer_ticks * tick
    quote_buffer_ticks: int = 1
    #: cap ladder density per side (tick size drops to 0.001 near price
    #: extremes, which would otherwise create ~1000 levels per side)
    max_levels_per_side: int = 99

    # --- timing ---------------------------------------------------------------
    #: cancel & repost cadence (seconds)
    refresh_interval_s: float = 5.0
    #: pull all orders this many seconds before the window closes
    pull_before_end_s: float = 60.0
    #: wait this long after window open before first quoting
    join_delay_s: float = 5.0
    #: how long to wait for resolution after window end before giving up
    settle_timeout_s: float = 300.0

    # --- risk limits ----------------------------------------------------------
    #: stop quoting a market once (fills so far + resting notional) exceeds this
    max_cost_per_market: float = 250.0
    #: stop quoting a side once inventory (shares) exceeds this
    max_shares_per_side: float = 500.0
    #: halt the bot after this much realized loss in the run (positive number)
    daily_loss_limit: float = 250.0
    #: abort the run after this many consecutive failed refresh cycles
    consecutive_error_limit: int = 8

    # --- order handling ---------------------------------------------------------
    #: post-only orders guarantee we are always the maker (never pay taker fees)
    post_only: bool = True
    #: use GTD orders expiring at window end so a crashed bot leaves no orders;
    #: the CLOB requires GTD expiration >= now+180s, so the final stretch falls
    #: back to GTC (and we pull manually anyway).
    use_gtd: bool = True

    # --- client-side rate limits (Polymarket "Standard" tier by default) ------
    #: POST /order tokens per second; a ladder refresh spends one token/order.
    order_rate_per_s: float = 40.0
    order_burst: float = 55.0
    #: DELETE tokens per second; cancel-market costs 1 + orders canceled.
    cancel_rate_per_s: float = 80.0
    cancel_burst: float = 110.0

    # --- execution --------------------------------------------------------------
    #: True = paper mode (simulate fills against live books; no orders placed)
    dry_run: bool = True
    #: fraction of a resting level that fills per sweep event in paper mode.
    #: 1.0 = optimistic (full fill, ignores queue position); 0.5 = conservative.
    paper_fill_ratio: float = 1.0

    # --- endpoints / http --------------------------------------------------------
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    request_timeout_s: float = 15.0
    http_retry_attempts: int = 3
    http_retry_backoff_s: float = 0.75

    def __post_init__(self) -> None:
        if self.asset not in SUPPORTED_ASSETS:
            raise ValueError(
                f"asset must be one of {SUPPORTED_ASSETS}, got {self.asset!r}"
            )
        if self.size_per_level <= 0:
            raise ValueError("size_per_level must be positive")
        if not 0 < self.min_price < self.max_price < 1:
            raise ValueError("require 0 < min_price < max_price < 1")
        if self.refresh_interval_s <= 0:
            raise ValueError("refresh_interval_s must be positive")
        if not 0 < self.pull_before_end_s < WINDOW_SECONDS:
            raise ValueError(
                f"pull_before_end_s must be in (0, {WINDOW_SECONDS})"
            )
        if self.max_cost_per_market <= 0 or self.max_shares_per_side <= 0:
            raise ValueError("risk limits must be positive")
        if self.quote_buffer_ticks < 1:
            raise ValueError("quote_buffer_ticks must be >= 1")
        if self.max_levels_per_side < 1:
            raise ValueError("max_levels_per_side must be >= 1")
        if not 0 < self.paper_fill_ratio <= 1:
            raise ValueError("paper_fill_ratio must be in (0, 1]")

    # ------------------------------------------------------------------ env --
    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "BotConfig":
        """Build a config from ``POLBOT_*`` environment overrides."""
        env = dict(os.environ if env is None else env)

        def get(key: str, default: Any, cast=lambda v: v) -> Any:
            raw = env.get(key)
            return default if raw is None or raw == "" else cast(raw)

        return cls(
            asset=get("POLBOT_ASSET", "btc", str).lower(),
            size_per_level=get("POLBOT_SIZE_PER_LEVEL", 5.0, _to_float),
            min_price=get("POLBOT_MIN_PRICE", 0.01, _to_float),
            max_price=get("POLBOT_MAX_PRICE", 0.99, _to_float),
            refresh_interval_s=get("POLBOT_REFRESH_S", 5.0, _to_float),
            pull_before_end_s=get("POLBOT_PULL_S", 60.0, _to_float),
            join_delay_s=get("POLBOT_JOIN_DELAY_S", 5.0, _to_float),
            max_cost_per_market=get("POLBOT_MAX_COST", 250.0, _to_float),
            max_shares_per_side=get("POLBOT_MAX_SHARES_SIDE", 500.0, _to_float),
            daily_loss_limit=get("POLBOT_DAILY_LOSS", 250.0, _to_float),
            paper_fill_ratio=get("POLBOT_PAPER_FILL_RATIO", 1.0, _to_float),
            dry_run=get("POLBOT_DRY_RUN", True, _to_bool),
            gamma_url=get("POLBOT_GAMMA_URL", "https://gamma-api.polymarket.com", str),
            clob_url=get("POLBOT_CLOB_URL", "https://clob.polymarket.com", str),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    def describe(self) -> str:
        d = self.to_dict()
        keys = (
            "asset size_per_level min_price max_price refresh_interval_s "
            "pull_before_end_s max_cost_per_market max_shares_per_side "
            "daily_loss_limit dry_run post_only use_gtd"
        ).split()
        return " | ".join(f"{k}={d[k]}" for k in keys)
