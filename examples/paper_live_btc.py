"""Run polbot in paper mode from a script.

Paper mode uses live Polymarket market data and simulates every fill —
no credentials, no orders, no risk. Press Ctrl-C to stop.
"""

import asyncio

from polbot.backends import PaperBackend
from polbot.clock import RealClock
from polbot.config import BotConfig
from polbot.engine import Engine, LiveMarketData
from polbot.net import HttpClient
from polbot.pnl import RunStats
from polbot.risk import RiskManager


async def main() -> None:
    cfg = BotConfig(asset="btc")  # btc | eth | sol | xrp
    cfg.size_per_level = 5
    cfg.refresh_interval_s = 5.0
    cfg.pull_before_end_s = 60.0

    clock = RealClock()
    data = LiveMarketData(cfg, clock=clock)
    engine = Engine(
        cfg,
        data,
        PaperBackend(fill_ratio=1.0),
        clock,
        risk=RiskManager(cfg),
        stats=RunStats(),
    )
    try:
        await engine.run_forever(max_windows=3)  # three windows, then stop
    finally:
        print("final:", engine.stats.line())


if __name__ == "__main__":
    asyncio.run(main())
