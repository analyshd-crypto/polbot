"""Risk manager: exposure caps, daily loss limit, stale-feed guard, kill switch."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RiskState:
    day_start_ts: float = field(default_factory=lambda: time.time())
    day_realized_pnl: float = 0.0
    day_unrealized_pnl: float = 0.0
    halted: bool = False
    halt_reason: str = ""


class RiskManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = RiskState()
        self._window_exposure: dict[str, float] = {}

    # ------------------------------------------------------------------ day
    def _maybe_roll_day(self) -> None:
        now = time.time()
        day = 86400
        if now - self.state.day_start_ts >= day:
            self.state.day_start_ts = now
            self.state.day_realized_pnl = 0.0
            self.state.day_unrealized_pnl = 0.0

    # ------------------------------------------------------------------ checks
    def kill_switch_active(self) -> bool:
        path = self.cfg.risk.kill_switch_file
        return bool(path) and os.path.exists(path)

    def halted(self) -> bool:
        return self.state.halted

    def halt(self, reason: str) -> None:
        self.state.halted = True
        self.state.halt_reason = reason

    def resume(self) -> None:
        self.state.halted = False
        self.state.halt_reason = ""

    def check(self, total_exposure: float, feed_stale: bool,
              free_bankroll: float) -> tuple[bool, str]:
        """Return (ok, reason). Called every loop before quoting."""
        self._maybe_roll_day()
        if self.halted():
            return False, self.state.halt_reason
        if self.kill_switch_active():
            self.halt("kill switch file present")
            return False, "kill switch file present"
        if feed_stale:
            self.halt("spot feed stale")
            return False, "spot feed stale"
        if total_exposure >= self.cfg.risk.max_total_exposure:
            return False, f"total exposure cap {self.cfg.risk.max_total_exposure}"
        day_pnl = self.state.day_realized_pnl + self.state.day_unrealized_pnl
        if day_pnl <= -abs(self.cfg.risk.daily_loss_limit):
            self.halt(f"daily loss limit {self.cfg.risk.daily_loss_limit}")
            return False, self.state.halt_reason
        min_free = self.cfg.bankroll * self.cfg.risk.min_free_bankroll_frac
        if free_bankroll < min_free:
            return False, "free bankroll below reserve"
        return True, "ok"

    # ------------------------------------------------------------------ exposure
    def window_exposure(self, event_slug: str) -> float:
        return self._window_exposure.get(event_slug, 0.0)

    def add_window_exposure(self, event_slug: str, cost_usd: float) -> float:
        self._window_exposure[event_slug] = self._window_exposure.get(event_slug, 0.0) + cost_usd
        return self._window_exposure[event_slug]

    def window_budget_left(self, event_slug: str) -> float:
        return max(0.0, self.cfg.sizing.max_window_exposure -
                   self._window_exposure.get(event_slug, 0.0))

    def close_window(self, event_slug: str) -> None:
        self._window_exposure.pop(event_slug, None)

    # ------------------------------------------------------------------ pnl
    def record_realized(self, pnl: float) -> None:
        self._maybe_roll_day()
        self.state.day_realized_pnl += pnl

    def set_unrealized(self, pnl: float) -> None:
        self._maybe_roll_day()
        self.state.day_unrealized_pnl = pnl
