"""Configuration: YAML file + environment variable overrides.

Env vars (all optional except for live trading):
    POLBOT_PRIVATE_KEY     wallet private key (live mode only)
    POLBOT_FUNDER          proxy/Safe wallet address holding USDC (live mode, polymarket.com accounts)
    POLBOT_SIGNATURE_TYPE  0 EOA (default) | 1 proxy | 2 safe
    POLBOT_MODE            overrides `mode` (paper | live)
    POLBOT_BANKROLL        overrides starting bankroll
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import yaml


@dataclass
class MarketCfg:
    series: list = field(default_factory=lambda: [
        "btc-up-or-down-5m", "eth-up-or-down-5m", "sol-up-or-down-5m"])
    quote_pre_open: bool = True
    pre_open_size_usd: float = 2.0
    pre_open_edge: float = 0.03      # distance around 0.50 for pre-open quotes


@dataclass
class QuoteCfg:
    refresh_interval: float = 5.0
    fast_refresh_interval: float = 2.0
    fast_refresh_move: float = 0.03   # fair move (abs) triggering fast refresh
    low: float = 0.01
    high: float = 0.99
    mode: str = "edge_weighted"       # ladder | edge_weighted
    base_size_usd: float = 3.0        # per-level notional in ladder mode; scale in edge mode
    min_edge: float = 0.02            # skip levels priced above fair - min_edge
    max_levels_per_side: int = 25
    min_edge_far: float = 0.10        # extra required edge for deep OTM tail quotes
    cancel_before_end: float = 60.0   # user rule: cancel everything T-60s
    final_seconds_mode: bool = False  # optional: keep quoting 90c+ into the last seconds
    final_seconds_until: float = 10.0
    final_seconds_min_price: float = 0.90


@dataclass
class SizingCfg:
    kelly_fraction: float = 0.25
    max_clip_usd: float = 40.0
    max_window_exposure: float = 120.0
    dominance_threshold: float = 0.15   # |fair-0.5| beyond which we skew
    dominance_skew: float = 3.0         # ohanism: >=3x asymmetry = 77.7% WR bucket


@dataclass
class RiskCfg:
    max_total_exposure: float = 400.0
    daily_loss_limit: float = 150.0
    stale_feed_seconds: float = 10.0
    kill_switch_file: str = "KILL"
    min_free_bankroll_frac: float = 0.25


@dataclass
class FeeCfg:
    regime: str = "legacy"        # legacy (<= 2026-11-20) | tiered (post 2026-11-20)
    taker_rate: float = 0.07      # legacy crypto taker rate
    maker_rebate_share: float = 0.20


@dataclass
class ExecCfg:
    post_only: bool = True
    batch_size: int = 20
    poll_interval: float = 2.0
    max_requests_per_sec: float = 8.0
    clob_host: str = "https://clob.polymarket.com"


@dataclass
class DataCfg:
    binance_symbols: dict = field(default_factory=lambda: {
        "btc-up-or-down-5m": "BTCUSDT",
        "eth-up-or-down-5m": "ETHUSDT",
        "sol-up-or-down-5m": "SOLUSDT",
    })
    spot_source: str = "binance"    # binance | chainlink (settlement oracle feed)
    use_websocket: bool = True
    rest_poll_interval: float = 1.0
    gamma_host: str = "https://gamma-api.polymarket.com"
    history_cache_dir: str = "data/cache"


@dataclass
class ModelCfg:
    vol_halflife_s: float = 90.0
    min_vol_annual: float = 0.15
    max_vol_annual: float = 3.0
    twap_seconds: float = 30.0        # settlement TWAP window (since 2026-08-07)


@dataclass
class LogCfg:
    level: str = "INFO"
    file: str = "logs/polbot.log"


@dataclass
class Config:
    mode: str = "paper"                # paper | live | backtest
    bankroll: float = 1000.0
    markets: MarketCfg = field(default_factory=MarketCfg)
    quoting: QuoteCfg = field(default_factory=QuoteCfg)
    sizing: SizingCfg = field(default_factory=SizingCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    fees: FeeCfg = field(default_factory=FeeCfg)
    execution: ExecCfg = field(default_factory=ExecCfg)
    data: DataCfg = field(default_factory=DataCfg)
    model: ModelCfg = field(default_factory=ModelCfg)
    logging: LogCfg = field(default_factory=LogCfg)

    # ---- loading ----
    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        cfg = cls()
        if path and os.path.exists(path):
            with open(path) as f:
                raw = yaml.safe_load(f) or {}
            cfg._apply(raw)
        # environment overrides
        if os.environ.get("POLBOT_MODE"):
            cfg.mode = os.environ["POLBOT_MODE"]
        if os.environ.get("POLBOT_BANKROLL"):
            cfg.bankroll = float(os.environ["POLBOT_BANKROLL"])
        return cfg

    def _apply(self, raw: dict) -> None:
        for section, values in raw.items():
            if values is None:
                continue
            if section == "mode":
                self.mode = str(values)
            elif section == "bankroll":
                self.bankroll = float(values)
            elif hasattr(self, section):
                sub = getattr(self, section)
                for k, v in values.items():
                    if hasattr(sub, k):
                        setattr(sub, k, v)

    # ---- live-trading secrets (never written to files by polbot) ----
    @property
    def private_key(self) -> Optional[str]:
        return os.environ.get("POLBOT_PRIVATE_KEY")

    @property
    def funder(self) -> Optional[str]:
        return os.environ.get("POLBOT_FUNDER")

    @property
    def signature_type(self) -> int:
        return int(os.environ.get("POLBOT_SIGNATURE_TYPE", "0"))

    def series_symbol(self, series: str) -> Optional[str]:
        return self.data.binance_symbols.get(series)
