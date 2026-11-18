"""End-to-end engine tests on synthetic (deterministic) replay data.

Scenario A — crash-and-recover (the "lottery" case):
    Up prints 0.50 -> 0.35 -> 0.15 -> 0.02, then a print AFTER the pull, and
    Up wins.  Resting Up bids fill level-by-level as the market sweeps down.

Scenario B — grind-down (the "tail risk" case):
    Up drifts 0.50 -> 0.45 -> 0.40 -> 0.35 then CRASHES to 0.02 one minute
    AFTER the pull.  Down wins.  The post-pull crash must produce no fills
    because the bot pulled its ladder.
"""

from __future__ import annotations

import asyncio

import pytest

from polbot.backends import PaperBackend
from polbot.backtest import ReplayMarketData
from polbot.clock import SimClock
from polbot.config import BotConfig
from polbot.data import WindowRecord
from polbot.engine import Engine
from polbot.pnl import RunStats
from polbot.risk import RiskManager

W = 1_800_000  # divisible by 300


def record(window_ts: int, up_hist, down_hist, winner: str) -> WindowRecord:
    return WindowRecord(
        window_ts=window_ts,
        end_ts=window_ts + 300,
        slug=f"btc-updown-5m-{window_ts}",
        condition_id="0xabc",
        up_token="UP",
        down_token="DOWN",
        winner=winner,
        up_hist=up_hist,
        down_hist=down_hist,
    )


def run_record(rec: WindowRecord, cfg: BotConfig | None = None):
    async def _inner():
        cfg_ = cfg or BotConfig()
        clock = SimClock(start=float(rec.window_ts))
        data = ReplayMarketData(cfg_, clock)
        backend = PaperBackend()
        risk = RiskManager(cfg_)
        stats = RunStats()
        engine = Engine(cfg_, data, backend, clock, risk, stats)
        data.set_window(rec)
        market = data.get_market_sync(rec.window_ts)
        result = await engine.run_one(market)
        return result, backend, stats

    return asyncio.run(_inner())


def test_crash_and_recover_scenario():
    rec = record(
        W,
        up_hist=[[W + 10, 0.50], [W + 70, 0.35], [W + 130, 0.15],
                 [W + 190, 0.02], [W + 250, 0.60]],
        down_hist=[[W + 10, 0.50], [W + 70, 0.65], [W + 130, 0.85],
                   [W + 190, 0.98], [W + 250, 0.40]],
        winner="up",
    )
    result, backend, stats = run_record(rec)

    # ladder placed at open: every cent 0.01..0.49 on both sides
    assert result.peak_quotes == 98
    # fills: 0.35-0.49 (15 lv), 0.15-0.34 (20 lv), 0.02-0.14 (13 lv) -> 48
    assert result.fills == 48
    assert result.up_shares == pytest.approx(48 * 5)
    assert result.down_shares == pytest.approx(0.0)
    assert result.spent == pytest.approx(31.50 + 24.50 + 5.20)
    # Up wins: 240 shares pay $1 vs $61.20 cost
    assert result.pnl == pytest.approx(240.0 - 61.20)
    # nothing left resting after the pull
    assert backend.resting_count() == 0
    assert stats.windows == 1 and stats.wins == 1


def test_pull_prevents_late_fills():
    rec = record(
        W,
        up_hist=[[W + 10, 0.50], [W + 70, 0.45], [W + 130, 0.40],
                 [W + 190, 0.35], [W + 260, 0.02]],  # crash AFTER the pull
        down_hist=[[W + 10, 0.50], [W + 70, 0.55], [W + 130, 0.60],
                   [W + 190, 0.65], [W + 260, 0.98]],
        winner="down",
    )
    result, backend, stats = run_record(rec)

    # only the gentle drift fills (5 levels per drop): 0.45-49, 0.40-44, 0.35-39
    assert result.fills == 15
    assert result.up_shares == pytest.approx(75.0)
    assert result.spent == pytest.approx(11.75 + 10.50 + 9.25)
    # Down wins: held Up shares are worthless -> lose the full spend
    assert result.pnl == pytest.approx(-31.50)
    assert stats.losses == 1


def test_budget_cap_limits_ladder():
    cfg = BotConfig(max_cost_per_market=30.0)
    rec = record(
        W,
        up_hist=[[W + 10, 0.50]],
        down_hist=[[W + 10, 0.50]],
        winner="up",
    )
    result, backend, _ = run_record(rec, cfg)
    # the two full 49-level ladders cost ~$122 together; with a $30 budget
    # only the topmost levels are quoted, so nothing fills at a flat 0.50
    assert result.fills == 0
    assert result.pnl == pytest.approx(0.0)
    assert backend.resting_notional() <= cfg.max_cost_per_market + 1e-6


def test_risk_halt_stops_quoting():
    cfg = BotConfig(daily_loss_limit=10.0)
    rec = record(
        W,
        up_hist=[[W + 10, 0.50], [W + 70, 0.10]],
        down_hist=[[W + 10, 0.50], [W + 70, 0.90]],
        winner="down",
    )

    async def _inner():
        clock = SimClock(start=float(rec.window_ts))
        data = ReplayMarketData(cfg, clock)
        backend = PaperBackend()
        risk = RiskManager(cfg)
        stats = RunStats()
        engine = Engine(cfg, data, backend, clock, risk, stats)
        data.set_window(rec)
        risk._realized_pnl = -9.0  # simulate an existing loss
        market = data.get_market_sync(rec.window_ts)
        result = await engine.run_one(market)
        return result, risk

    result, risk = asyncio.run(_inner())
    assert result.winner == "down"
    assert risk.halted  # realized -9 + pnl <= -10 trips the halt
