"""Backtest flow tests on synthetic records + paper backend."""

from __future__ import annotations

import asyncio

import pytest

from polbot.backtest import run_backtest, results_to_rows
from polbot.config import BotConfig
from polbot.data import WindowRecord

W = 1_800_000


def rec(window_ts, up_hist, down_hist, winner):
    return WindowRecord(
        window_ts=window_ts, end_ts=window_ts + 300,
        slug=f"btc-updown-5m-{window_ts}", condition_id="0xabc",
        up_token="UP", down_token="DOWN", winner=winner,
        up_hist=up_hist, down_hist=down_hist,
    )


def test_run_backtest_over_multiple_windows():
    records = [
        rec(W, [[W + 10, 0.50], [W + 70, 0.35], [W + 130, 0.15], [W + 190, 0.02]],
           [[W + 10, 0.50], [W + 70, 0.65], [W + 130, 0.85], [W + 190, 0.98]],
           "up"),
        rec(W + 300,
           [[W + 310, 0.50], [W + 370, 0.45], [W + 430, 0.40], [W + 490, 0.35],
            [W + 560, 0.02]],
           [[W + 310, 0.50], [W + 370, 0.55], [W + 430, 0.60], [W + 490, 0.65],
            [W + 560, 0.98]],
           "down"),
    ]
    report = asyncio.run(run_backtest(BotConfig(), records))
    assert report.windows == 2
    assert report.stats.windows == 2
    # window 1: +178.80 (crash & recover, up wins)
    # window 2: -31.50 (drift fills, post-pull crash, down wins)
    assert report.stats.total_pnl == pytest.approx(178.80 - 31.50)
    rows = results_to_rows(report.results)
    assert len(rows) == 2
    assert rows[0]["pnl"] == pytest.approx(178.80)
    assert rows[1]["pnl"] == pytest.approx(-31.50)
    assert rows[1]["winner"] == "down"


def test_run_backtest_empty():
    report = asyncio.run(run_backtest(BotConfig(), []))
    assert report.windows == 0
    assert report.stats.total_pnl == 0.0
