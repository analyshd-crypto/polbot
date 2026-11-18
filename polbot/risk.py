"""Risk management: budget caps, inventory caps, loss halt, kill switch."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import BotConfig
from .pnl import RunStats

log = logging.getLogger("polbot.risk")

_EPS = 1e-9


@dataclass
class RiskManager:
    """Evaluates whether the bot may keep quoting / keep running."""

    cfg: BotConfig
    halted: bool = False
    halt_reason: str = ""
    consecutive_errors: int = 0
    _realized_pnl: float = 0.0
    _windows: list = field(default_factory=list)

    # -------------------------------------------------------------- running --
    def on_window(self, result) -> None:
        self._realized_pnl += result.pnl
        self._windows.append(result)
        if (self.cfg.daily_loss_limit > 0
                and -self._realized_pnl >= self.cfg.daily_loss_limit - _EPS):
            self.halt(
                f"daily loss limit hit ({self._realized_pnl:+.2f} <= "
                f"-{self.cfg.daily_loss_limit:.2f})"
            )

    def halt(self, reason: str) -> None:
        if not self.halted:
            self.halted = True
            self.halt_reason = reason
            log.error("RISK HALT: %s", reason)

    # ------------------------------------------------------------- refreshing --
    def on_cycle_ok(self) -> None:
        self.consecutive_errors = 0

    def on_cycle_error(self, exc: BaseException) -> None:
        self.consecutive_errors += 1
        log.warning("cycle error %d/%d: %s",
                    self.consecutive_errors, self.cfg.consecutive_error_limit, exc)
        if self.consecutive_errors >= self.cfg.consecutive_error_limit:
            self.halt(f"{self.consecutive_errors} consecutive cycle errors")

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    # --------------------------------------------------------------- quoting --
    def may_run(self) -> bool:
        return not self.halted

    def stats(self) -> RunStats:
        s = RunStats()
        for r in self._windows:
            s.record(r)
        return s
