"""PolBot orchestrator: discovery -> fair value -> quoting loop -> settlement accounting.

Lifecycle per window (see STRATEGY.md):
    PRE_OPEN  : small symmetric quotes near 0.50 (book trades ~50/50 before strike is set)
    LIVE      : fair-value anchored ladder, both sides, adaptive refresh (5s / 2s)
    WINDDOWN  : T-60s -> cancel everything (user rule); optional final-seconds mode
    ENDED     : wait for Chainlink-TWAP resolution via Gamma
    SETTLED   : book PnL (maker fills pay zero fees), update risk state
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from typing import Optional

from .config import Config
from .data.binance import SpotFeed
from .data.clob import ClobPublic
from .data.gamma import GammaClient
from .engine.fairvalue import FairValueModel, TwapTracker
from .engine.fees import FeeModel
from .engine.grid import LadderBuilder
from .engine.risk import RiskManager
from .exec.paper import PaperExecutor
from .models import Fill, Phase, Window, WindowResult, now_ts

log = logging.getLogger("polbot.bot")


class PolBot:
    def __init__(self, cfg: Config, executor=None):
        self.cfg = cfg
        self.gamma = GammaClient(cfg.data.gamma_host)
        self.clob = ClobPublic(cfg.execution.clob_host)
        self.ladder = LadderBuilder(cfg)
        self.risk = RiskManager(cfg)
        self.fees = FeeModel(cfg.fees.regime, cfg.fees.taker_rate,
                             cfg.fees.maker_rebate_share)
        self.realized_pnl = 0.0
        self.open_cost = 0.0
        self.results: list[WindowResult] = []

        symbols = [cfg.series_symbol(s) for s in cfg.markets.series
                   if cfg.series_symbol(s)]
        if cfg.data.spot_source == "chainlink":
            try:
                from .data.chainlink import ChainlinkFeed
                base = [s.replace("USDT", "") for s in (symbols or ["BTCUSDT"])]
                self.feed = ChainlinkFeed(base, window_seconds=int(cfg.model.twap_seconds))
                log.info("spot source: chainlink settlement feed (basis_sigma term can be zeroed)")
            except Exception as e:  # noqa: BLE001
                log.warning("chainlink feed unavailable (%s); falling back to binance", e)
                self.feed = SpotFeed(symbols or ["BTCUSDT"],
                                     use_websocket=cfg.data.use_websocket,
                                     rest_poll_interval=cfg.data.rest_poll_interval)
        else:
            self.feed = SpotFeed(symbols or ["BTCUSDT"],
                                 use_websocket=cfg.data.use_websocket,
                                 rest_poll_interval=cfg.data.rest_poll_interval)
        self.models: dict[str, FairValueModel] = {
            s: FairValueModel(cfg.model.vol_halflife_s, cfg.model.min_vol_annual,
                              cfg.model.max_vol_annual, cfg.model.twap_seconds)
            for s in cfg.markets.series}
        self.twaps: dict[str, TwapTracker] = {}

        if executor is not None:
            self.executor = executor
        elif cfg.mode == "live":
            from .exec.live import LiveExecutor
            self.executor = LiveExecutor(cfg)
        else:
            self.executor = PaperExecutor(cfg, self.clob)

        self.windows: dict[str, Window] = {}
        self._last_discover = 0.0
        self._last_fair: dict[str, float] = {}
        self._last_quote_ts: dict[str, float] = {}
        self._vol_synced: dict[str, float] = {}
        self._stop = False
        self._fills_log_path = os.path.join("logs", "fills.jsonl")
        os.makedirs("logs", exist_ok=True)
        self._install_signals()

    # ------------------------------------------------------------------ signals
    def _install_signals(self) -> None:
        def handler(signum, frame):  # noqa: ARG001
            log.info("signal %s -> shutting down", signum)
            self._stop = True
        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(s, handler)
            except ValueError:
                pass

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        log.info("polbot %s starting: mode=%s series=%s bankroll=%.2f",
                 "1.0", self.cfg.mode, self.cfg.markets.series, self.cfg.bankroll)
        self.feed.start()
        try:
            while not self._stop:
                loop_t0 = time.time()
                try:
                    self._cycle()
                except Exception as e:  # noqa: BLE001
                    log.exception("cycle error: %s", e)
                    time.sleep(1.0)
                sleep = max(0.2, 1.0 - (time.time() - loop_t0))
                end = time.time() + sleep
                while time.time() < end and not self._stop:
                    time.sleep(0.1)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        log.info("shutting down: cancelling all orders, stopping feed")
        try:
            self.executor.cancel_all()
        except Exception as e:  # noqa: BLE001
            log.warning("cancel_all on shutdown failed: %s", e)
        self.feed.stop()
        self._report()

    # ------------------------------------------------------------------ cycle
    def _cycle(self) -> None:
        now = now_ts()
        if now - self._last_discover > 30.0:
            self._discover()
            self._last_discover = now
        self._sync_models()
        active = [w for w in self.windows.values() if w.phase != Phase.SETTLED]

        # fills first (paper: checked against live books; live: trade poll)
        fills = self.executor.poll_fills(active)
        for f in fills:
            self._handle_fill(f)

        # risk gate
        stale = any(self.feed.stale(self.cfg.series_symbol(w.series) or "BTCUSDT",
                                    self.cfg.risk.stale_feed_seconds)
                    for w in active if w.phase in (Phase.LIVE, Phase.PRE_OPEN))
        free_bankroll = self.cfg.bankroll + self.realized_pnl - self.open_cost
        ok, reason = self.risk.check(self.open_cost, stale, free_bankroll)
        if not ok:
            if self.windows:
                self.executor.cancel_all()
            log.warning("risk gate: %s (exposure=%.2f free=%.2f)", reason,
                        self.open_cost, free_bankroll)
            # still settle finished windows while halted
            for w in active:
                if w.phase in (Phase.ENDED, Phase.SETTLED):
                    self._advance_settlement(w)
            return

        for w in active:
            self._advance(w)

    # ------------------------------------------------------------------ discovery
    def _discover(self) -> None:
        for series in self.cfg.markets.series:
            try:
                wins = self.gamma.fetch_windows(series, limit=60)
            except Exception as e:  # noqa: BLE001
                log.warning("discovery failed for %s: %s", series, e)
                continue
            fresh = 0
            horizon = time.time() + 900.0  # only track the next ~3 windows
            for w in wins:
                if w.event_slug not in self.windows:
                    if time.time() - 60 < w.end_ts and w.start_ts < horizon:
                        self.windows[w.event_slug] = w
                        fresh += 1
            if fresh:
                log.info("discovered %d new window(s) for %s", fresh, series)
        # drop long-settled
        for slug in list(self.windows):
            w = self.windows[slug]
            if w.phase == Phase.SETTLED and w.end_ts < time.time() - 3600:
                del self.windows[slug]

    # ------------------------------------------------------------------ models
    def _sync_models(self) -> None:
        """Feed fair-value models with new spot points; track open/close TWAPs."""
        for series, model in self.models.items():
            sym = self.cfg.series_symbol(series)
            if not sym:
                continue
            hist = self.feed.history(sym)
            last_sync = self._vol_synced.get(series, 0.0)
            new_pts = [(t, p) for (t, p) in hist if t > last_sync]
            if new_pts:
                model.update_series(new_pts)
                self._vol_synced[series] = new_pts[-1][0]

    # ------------------------------------------------------------------ phases
    def _advance(self, w: Window) -> None:
        now = now_ts()
        if now < w.start_ts:
            w.phase = Phase.PRE_OPEN
            self._quote_pre_open(w)
        elif now < w.end_ts:
            w.phase = Phase.LIVE
            self._quote_live(w, now)
        elif w.phase != Phase.SETTLED:
            w.phase = Phase.ENDED
            self._advance_settlement(w)

    def _quote_pre_open(self, w: Window) -> None:
        if not self.cfg.markets.quote_pre_open:
            return
        now = now_ts()
        if now - self._last_quote_ts.get(w.event_slug, 0) < self.cfg.quoting.refresh_interval:
            return
        self._last_quote_ts[w.event_slug] = now
        quotes = self.ladder.pre_open_quotes(w, self._free_bankroll())
        res = self.executor.place_quotes(w, quotes)
        log.debug("%s pre-open quotes=%s", w.event_slug, res.get("placed"))

    def _quote_live(self, w: Window, now: float) -> None:
        cfg = self.cfg
        series = w.series
        sym = self.cfg.series_symbol(series) or "BTCUSDT"
        spot = self.feed.last(sym)
        model = self.models.get(series)
        if spot is None or model is None:
            return

        remaining = w.remaining(now)

        # strike capture / refinement (open TWAP over first 30s)
        if w.strike is None and now >= w.start_ts:
            w.strike = spot
            log.info("%s strike captured %.2f", w.event_slug, spot)
        elif w.strike is not None and now < w.start_ts + cfg.model.twap_seconds:
            hist = self.feed.history(sym)
            pts = [(t, p) for (t, p) in hist if w.start_ts <= t <= now]
            if pts:
                w.strike = sum(p for _, p in pts) / len(pts)

        # TWAP (close) accumulation
        tt = self.twaps.get(w.event_slug)
        if tt is None:
            tt = TwapTracker(cfg.model.twap_seconds)
            self.twaps[w.event_slug] = tt
            for (t, p) in self.feed.history(sym):
                tt.update(t, p, w.end_ts)
        else:
            tt.update(now, spot, w.end_ts)
        twap_sum, obs_s = tt.sum(w.end_ts)
        use_twap = twap_sum if obs_s > 0 else None

        p_up, diag = model.prob_up(spot, w.strike, remaining, use_twap)
        w.fair_up = p_up
        prev = self._last_fair.get(w.event_slug, 0.5)
        moved = abs(p_up - prev)
        self._last_fair[w.event_slug] = p_up

        # winddown / final-seconds handling (user rule: cancel all at T-60s)
        if remaining <= cfg.quoting.cancel_before_end:
            if cfg.quoting.final_seconds_mode and remaining > cfg.quoting.final_seconds_until:
                if now - self._last_quote_ts.get(w.event_slug, 0) >= 1.0:
                    self._last_quote_ts[w.event_slug] = now
                    budget = min(self.risk.window_budget_left(w.event_slug),
                                 self._free_bankroll())
                    quotes = self.ladder.final_seconds_quotes(w, p_up,
                                                              self._free_bankroll(),
                                                              budget)
                    if quotes:
                        self.executor.place_quotes(w, quotes)
                        log.debug("%s final-seconds quotes=%d", w.event_slug, len(quotes))
                    return
            self.executor.cancel_window(w)
            return

        # refresh cadence (adaptive: 5s calm / 2s when fair moved)
        interval = cfg.quoting.refresh_interval
        if moved >= cfg.quoting.fast_refresh_move:
            interval = cfg.quoting.fast_refresh_interval
        if now - self._last_quote_ts.get(w.event_slug, 0) < interval:
            return
        self._last_quote_ts[w.event_slug] = now

        budget = min(self.risk.window_budget_left(w.event_slug), self._free_bankroll())
        if budget <= 0:
            self.executor.cancel_window(w)
            return
        quotes = self.ladder.build(w, p_up, self._free_bankroll(), budget)
        res = self.executor.place_quotes(w, quotes)
        log.debug("%s live fair=%.3f vol=%.2f quotes=%d (%s) rem=%.0fs",
                  w.event_slug, p_up, model.vol_annual, res.get("placed", 0),
                  diag.get("mode", "?"), remaining)

    # ------------------------------------------------------------------ fills
    def _handle_fill(self, f: Fill) -> None:
        for w in self.windows.values():
            if f.token_id in w.tokens:
                outcome = f.outcome
                w.inventory[outcome] = w.inventory.get(outcome, 0.0) + f.size
                w.cost[outcome] = w.cost.get(outcome, 0.0) + f.notional
                self.open_cost += f.notional
                self.risk.add_window_exposure(w.event_slug, f.notional)
                rebate = self.fees.maker_rebate(f.size, f.price)
                log.info("FILL %s %s %.0f sh @ %.2f ($%.2f) rebate~%.4f",
                         w.event_slug, outcome, f.size, f.price, f.notional, rebate)
                self._log_fill(w, f)
                return

    def _log_fill(self, w: Window, f: Fill) -> None:
        try:
            with open(self._fills_log_path, "a") as fh:
                fh.write(json.dumps({
                    "ts": f.ts, "slug": w.event_slug, "series": w.series,
                    "outcome": f.outcome, "price": f.price, "size": f.size,
                    "notional": f.notional, "fee": 0.0}) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------------ settlement
    def _advance_settlement(self, w: Window) -> None:
        if w.settlement is None:
            s = self.gamma.settlement(w.condition_id)
            if s is None:
                return
            w.settlement = s
        if w.phase == Phase.SETTLED:
            return
        up_sh, dn_sh = w.inventory.get("Up", 0.0), w.inventory.get("Down", 0.0)
        up_cost, dn_cost = w.cost.get("Up", 0.0), w.cost.get("Down", 0.0)
        payout = up_sh * (1.0 if w.settlement == 0 else 0.0) \
            + dn_sh * (1.0 if w.settlement == 1 else 0.0)
        pnl = payout - (up_cost + dn_cost)
        w.realized_pnl = pnl
        w.phase = Phase.SETTLED
        self.realized_pnl += pnl
        self.open_cost -= (up_cost + dn_cost)
        self.risk.record_realized(pnl)
        self.risk.close_window(w.event_slug)
        self.twaps.pop(w.event_slug, None)
        res = WindowResult(w.event_slug, w.series, up_sh, dn_sh, up_cost, dn_cost,
                           w.settlement, payout, pnl, w.end_ts)
        self.results.append(res)
        win_side = "Up" if w.settlement == 0 else "Down"
        log.info("SETTLED %s -> %s | up %.1f/$%.2f down %.1f/$%.2f | payout $%.2f | pnl $%+.2f",
                 w.event_slug, win_side, up_sh, up_cost, dn_sh, dn_cost, payout, pnl)

    # ------------------------------------------------------------------ helpers
    def _free_bankroll(self) -> float:
        return max(0.0, self.cfg.bankroll + self.realized_pnl - self.open_cost)

    def _report(self) -> None:
        n = len(self.results)
        if not n:
            log.info("no settled windows this session")
            return
        wins = sum(1 for r in self.results if r.pnl > 0)
        pnl = sum(r.pnl for r in self.results)
        log.info("session: %d windows, %d profitable (%.1f%%), net pnl $%+.2f",
                 n, wins, 100.0 * wins / n, pnl)
