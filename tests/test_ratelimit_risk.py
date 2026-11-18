"""Tests for the client-side rate limiter and risk manager."""

from __future__ import annotations

import pytest

from polbot.config import BotConfig
from polbot.pnl import WindowResult
from polbot.ratelimit import TokenBucket
from polbot.risk import RiskManager


class FakeTime:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def test_token_bucket_burst_and_refill():
    ft = FakeTime()
    b = TokenBucket(rate_per_s=10.0, burst=10.0, now=ft.now, sleep=ft.sleep)
    assert b.try_acquire(10) is True       # full burst
    assert b.try_acquire(1) is False       # empty
    ft.t += 0.1                            # +1 token
    assert b.try_acquire(1) is True
    assert b.try_acquire(1) is False
    b.acquire(5)                           # blocks: needs 5 tokens => sleeps 0.5s
    assert ft.t == pytest.approx(0.6)      # 0.1 + 0.5
    assert b.tokens == pytest.approx(0.0)


def test_token_bucket_never_exceeds_burst():
    ft = FakeTime()
    b = TokenBucket(rate_per_s=100.0, burst=5.0, now=ft.now, sleep=ft.sleep)
    ft.t += 10.0  # long idle
    assert b.try_acquire(6) is False
    assert b.try_acquire(5) is True


def _win(pnl: float) -> WindowResult:
    return WindowResult(window_ts=1, slug="s", winner="up", up_shares=1,
                        down_shares=0, spent=1.0, pnl=pnl, fills=1, peak_quotes=1)


def test_risk_halts_on_daily_loss():
    cfg = BotConfig(daily_loss_limit=100.0)
    risk = RiskManager(cfg)
    risk.on_window(_win(-40.0))
    assert not risk.halted
    risk.on_window(_win(-40.0))
    risk.on_window(_win(-30.0))  # cumulative -110 <= -100
    assert risk.halted
    assert "loss limit" in risk.halt_reason
    assert not risk.may_run()
    assert risk.stats().windows == 3


def test_risk_halts_on_consecutive_errors():
    cfg = BotConfig(consecutive_error_limit=3)
    risk = RiskManager(cfg)
    for i in range(3):
        risk.on_cycle_error(RuntimeError(f"boom {i}"))
    assert risk.halted
    risk2 = RiskManager(BotConfig(consecutive_error_limit=3))
    risk2.on_cycle_error(RuntimeError("x"))
    risk2.on_cycle_ok()
    risk2.on_cycle_error(RuntimeError("y"))
    assert not risk2.halted  # reset by the successful cycle
