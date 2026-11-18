"""Tests for PnL accounting and run statistics."""

from __future__ import annotations

import pytest

from polbot.markets import FiveMinMarket
from polbot.pnl import Fill, RunStats, WindowLedger, WindowResult


def mk() -> FiveMinMarket:
    return FiveMinMarket(
        asset="btc", window_ts=1800000, end_ts=1800300,
        slug="btc-updown-5m-1800000", condition_id="0xabc",
        up_token="UP", down_token="DOWN", question="q",
        accepting_orders=True, closed=False,
    )


def test_ledger_accumulates_and_settles():
    led = WindowLedger(market=mk())
    led.apply(Fill(ts=1, token_id="UP", label="Up", price=0.30, shares=10))
    led.apply(Fill(ts=2, token_id="UP", label="Up", price=0.20, shares=10))
    led.apply(Fill(ts=3, token_id="DOWN", label="Down", price=0.95, shares=20))
    assert led.up.shares == 20
    assert led.up.cost == pytest.approx(5.0)
    assert led.down.shares == 20
    assert led.down.cost == pytest.approx(19.0)
    assert led.spent == pytest.approx(24.0)
    # if Up wins: 20 shares pay $1 each -> 20 - 24 = -4
    assert led.pnl_if("up") == pytest.approx(-4.0)
    # if Down wins: 20 - 24 = -4 as well here
    assert led.pnl_if("down") == pytest.approx(-4.0)
    assert led.settle("up") == pytest.approx(-4.0)
    assert led.winner == "up"


def test_ledger_winning_side_math():
    led = WindowLedger(market=mk())
    led.apply(Fill(ts=1, token_id="UP", label="Up", price=0.25, shares=40))
    led.apply(Fill(ts=2, token_id="DOWN", label="Down", price=0.90, shares=10))
    # Up wins: 40 - (10 + 9) = +21
    assert led.pnl_if("up") == pytest.approx(21.0)
    # Down wins: 10 - 19 = -9
    assert led.pnl_if("down") == pytest.approx(-9.0)


def _result(pnl: float) -> WindowResult:
    return WindowResult(
        window_ts=1800000, slug="s", winner="up" if pnl > 0 else "down",
        up_shares=10, down_shares=0, spent=5.0, pnl=pnl, fills=2, peak_quotes=90,
    )


def test_run_stats():
    s = RunStats()
    for pnl in (10.0, -4.0, 6.0, -2.0, 8.0):
        s.record(_result(pnl))
    assert s.windows == 5
    assert s.wins == 3
    assert s.losses == 2
    assert s.win_rate == pytest.approx(0.6)
    assert s.total_pnl == pytest.approx(18.0)
    assert s.best_pnl == 10.0
    assert s.worst_pnl == -4.0
    # equity: 10, 6, 12, 10, 18 -> peaks 10,10,12,12,18
    # drawdowns: 10->6 = 4, 12->10 = 2  => max drawdown 4
    assert s.max_drawdown == pytest.approx(4.0)
