"""Event-driven backtester for the polbot strategy over 1-second crypto bars.

Fill model (documented in BACKTEST.md):
- The "market path" for each outcome is our fair-value model computed on a LAGGED spot
  feed (`lag_seconds`, default 3) — the CLOB re-prices within ~1-30s of the spot tape
  (RESEARCH.md §3.1). polbot requotes at `fast_refresh_interval` (2s), so it is the
  faster requoter; the lagged path represents stale takers/makers who hit our bids.
- A resting bid at price p on side S fills (fraction `queue_fill_fraction` of level
  size) when the market path for S trades at or below p - `trade_through_ticks` * tick.
- Settlement follows the live market exactly: Up wins iff close-TWAP(30s) >= open-TWAP(30s),
  computed from the same 1s bars (Binance is a proxy for Chainlink; basis documented).
- Maker fills pay zero fees; rebates are reported separately (legacy regime).

Everything the backtester calls to decide quotes is the SAME production code the live
bot uses (LadderBuilder, FairValueModel, KellySizer), so backtest results exercise the
production logic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from polbot.mm.config import Config
from polbot.mm.engine.fairvalue import FairValueModel, TwapTracker
from polbot.mm.engine.fees import FeeModel
from polbot.mm.engine.grid import LadderBuilder
from polbot.mm.models import Window


@dataclass
class BacktestParams:
    lag_seconds: float = 3.0
    trade_through_ticks: int = 1
    queue_fill_fraction: float = 0.6
    noise_ticks: float = 0.0
    quote_pre_open: bool = False
    seed: int = 7


@dataclass
class Bar:
    ts: float
    open: float
    high: float
    low: float
    close: float


@dataclass
class BTFill:
    ts: float
    slug: str
    outcome: str
    price: float
    size: float
    fair_at_fill: float


@dataclass
class BTWindowResult:
    slug: str
    series: str
    settle_up: bool
    up_shares: float = 0.0
    down_shares: float = 0.0
    up_cost: float = 0.0
    down_cost: float = 0.0
    pnl: float = 0.0
    fills: int = 0


@dataclass
class BacktestResult:
    params: BacktestParams
    windows: list[BTWindowResult] = field(default_factory=list)
    fills: list[BTFill] = field(default_factory=list)
    equity_curve: list[tuple[float, float]] = field(default_factory=list)
    total_pnl: float = 0.0
    total_rebates: float = 0.0
    max_deployed: float = 0.0

    def band_table(self) -> dict[str, dict]:
        bands = [(0.0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5),
                 (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.0)]
        settle = {r.slug: r.settle_up for r in self.windows}
        out = {}
        for lo, hi in bands:
            fills = [f for f in self.fills if lo <= f.price < hi]
            if not fills:
                continue
            won = sum(f.size for f in fills
                      if (f.outcome == "Up") == settle.get(f.slug, True))
            tot = sum(f.size for f in fills)
            out[f"{lo:.0%}-{hi:.0%}"] = {
                "fills": len(fills), "shares": tot,
                "win_rate": round(won / tot, 4) if tot else None,
                "deployed": round(sum(f.price * f.size for f in fills), 2),
                "pnl": round(sum((1.0 if (f.outcome == "Up") == settle.get(f.slug, True) else 0.0) * f.size
                                 - f.price * f.size for f in fills), 2),
            }
        return out

    def summary(self) -> dict:
        n = len(self.windows)
        traded = [r for r in self.windows if r.fills > 0]
        wins = [r for r in traded if r.pnl > 0]
        pnl = sum(r.pnl for r in self.windows)
        peak = 0.0
        maxdd = 0.0
        eq = 0.0
        for _, v in self.equity_curve:
            eq = v
            peak = max(peak, eq)
            maxdd = max(maxdd, peak - eq)
        return {
            "windows": n,
            "windows_traded": len(traded),
            "profitable_windows": len(wins),
            "win_rate_windows": round(len(wins) / len(traded), 4) if traded else None,
            "total_fills": len(self.fills),
            "total_pnl": round(pnl, 2),
            "rebates_est": round(self.total_rebates, 2),
            "max_deployed": round(self.max_deployed, 2),
            "max_drawdown": round(maxdd, 2),
            "pnl_per_window": round(pnl / n, 4) if n else 0.0,
        }


class BacktestEngine:
    def __init__(self, cfg: Config, bars: dict[str, list[Bar]],
                 params: Optional[BacktestParams] = None):
        self.cfg = cfg
        self.bars = bars
        self.p = params or BacktestParams()
        self.ladder = LadderBuilder(cfg)
        self.fees = FeeModel(cfg.fees.regime, cfg.fees.taker_rate,
                             cfg.fees.maker_rebate_share)
        import random
        self.rng = random.Random(self.p.seed)

    # ------------------------------------------------------------------ windows
    @staticmethod
    def _windows_from_bars(bars: list[Bar], series: str) -> list[tuple[str, float, float, list[Bar]]]:
        if not bars:
            return []
        step = 300.0
        first = bars[0].ts
        t0 = math.floor(first / step) * step
        out = []
        by_t = {b.ts: b for b in bars}
        while t0 + step <= bars[-1].ts + 1:
            win_bars = [by_t[t] for t in sorted(by_t) if t0 <= t < t0 + step]
            if len(win_bars) >= 150:  # require >=50% of 300s for a tradeable window
                slug = f"{series}-bt-{int(t0)}"
                out.append((slug, t0, t0 + step, win_bars))
            t0 += step
        return out

    # ------------------------------------------------------------------ run
    def run(self) -> BacktestResult:
        res = BacktestResult(params=self.p)
        series_by_symbol = {v: k for k, v in self.cfg.data.binance_symbols.items()}

        for symbol, bars in self.bars.items():
            series = series_by_symbol.get(symbol, symbol.lower())
            model = FairValueModel(self.cfg.model.vol_halflife_s,
                                   self.cfg.model.min_vol_annual,
                                   self.cfg.model.max_vol_annual,
                                   self.cfg.model.twap_seconds)
            # warm up vol on the first 10 minutes of data
            for b in bars[:600]:
                model.update(b.ts, b.close)

            for slug, t0, t1, win_bars in self._windows_from_bars(bars, series):
                self._run_window(res, slug, series, t0, t1, win_bars, model)
        # equity curve
        eq = 0.0
        for r in sorted(res.windows, key=lambda x: x.slug):
            eq += r.pnl
            res.equity_curve.append((r.slug, round(eq, 4)))
        res.total_pnl = eq
        return res

    # ------------------------------------------------------------------ one window
    def _run_window(self, res: BacktestResult, slug: str, series: str,
                    t0: float, t1: float, win_bars: list[Bar],
                    model: FairValueModel) -> None:
        cfg = self.cfg
        tick = 0.01
        strike = sum(b.close for b in win_bars[:30]) / max(1, len(win_bars[:30]))
        settle_twap = sum(b.close for b in win_bars[-30:]) / max(1, len(win_bars[-30:]))
        settle_up = settle_twap >= strike

        win = Window(series=series, event_slug=slug, condition_id="",
                     tokens=["up", "down"], outcomes=["Up", "Down"],
                     start_ts=t0, end_ts=t1, strike=strike)
        win.model = model  # shared per-symbol model keeps vol state across windows

        bt = BTWindowResult(slug=slug, series=series, settle_up=settle_up)
        twap = TwapTracker(cfg.model.twap_seconds)
        resting: dict[str, dict[float, float]] = {"Up": {}, "Down": {}}
        last_quote_ts = -1e9
        last_fair = 0.5
        deployed = 0.0
        budget_cap = cfg.sizing.max_window_exposure
        bars_by_ts = {b.ts: b for b in win_bars}
        hist = [(b.ts, b.close) for b in win_bars]

        # simulate our fair at (t - lag) to represent the market's stale pricing
        def market_fair_at(ts: float) -> Optional[float]:
            tl = ts - self.p.lag_seconds
            # nearest bar at/Before tl
            lo = None
            for t, c in hist:
                if t <= tl:
                    lo = (t, c)
                else:
                    break
            if lo is None:
                return None
            tt = TwapTracker(cfg.model.twap_seconds)
            for t, c in hist:
                if t1 - cfg.model.twap_seconds <= t <= ts:
                    tt.update(t, c, t1)
            twap_sum, obs = tt.sum(t1)
            p, _ = model.prob_up(lo[1], strike, max(0.0, t1 - ts),
                                 twap_sum if obs > 0 else None)
            if self.p.noise_ticks > 0:
                p += self.rng.gauss(0, self.p.noise_ticks * tick)
            return min(max(p, 0.001), 0.999)

        for ts in sorted(bars_by_ts):
            bar = bars_by_ts[ts]
            model.update(ts, bar.close)
            twap.update(ts, bar.close, t1)
            remaining = t1 - ts
            twap_sum, obs = twap.sum(t1)
            fair_up, _ = model.prob_up(bar.close, strike, remaining,
                                       twap_sum if obs > 0 else None)
            win.fair_up = fair_up

            # ---- fills against lagged market path
            if resting["Up"] or resting["Down"]:
                mf = market_fair_at(ts)
                if mf is not None:
                    for outcome, sign in (("Up", +1.0), ("Down", -1.0)):
                        path = mf if outcome == "Up" else 1.0 - mf
                        for p in sorted(list(resting[outcome]), reverse=True):
                            if path <= p - self.p.trade_through_ticks * tick:
                                size = resting[outcome][p] * self.p.queue_fill_fraction
                                if size < 1e-9:
                                    resting[outcome].pop(p)
                                    continue
                                resting[outcome][p] -= size
                                if resting[outcome][p] < 5.0:
                                    resting[outcome].pop(p)
                                cost = size * p
                                win.inventory[outcome] += size
                                win.cost[outcome] += cost
                                deployed += cost
                                bt.fills += 1
                                bt.up_cost = win.cost["Up"]
                                bt.down_cost = win.cost["Down"]
                                res.fills.append(BTFill(ts, slug, outcome, p,
                                                        round(size, 2), fair_up))
                                res.total_rebates += self.fees.maker_rebate(size, p)
                                res.max_deployed = max(res.max_deployed, deployed)

            if remaining <= cfg.quoting.cancel_before_end:
                resting["Up"].clear()
                resting["Down"].clear()
                continue

            # ---- requote cadence (same logic as live)
            moved = abs(fair_up - last_fair)
            interval = cfg.quoting.refresh_interval
            if moved >= cfg.quoting.fast_refresh_move:
                interval = cfg.quoting.fast_refresh_interval
            if ts - last_quote_ts < interval:
                continue
            last_quote_ts = ts
            last_fair = fair_up

            budget = min(budget_cap - deployed, self.cfg.bankroll * 0.5)
            if budget <= 0:
                continue
            quotes = self.ladder.build(win, fair_up, self.cfg.bankroll, budget)
            resting = {"Up": {}, "Down": {}}
            for q in quotes:
                resting[q.outcome][q.price] = q.size

        # ---- settle
        payout = win.inventory["Up"] * (1.0 if settle_up else 0.0) \
            + win.inventory["Down"] * (1.0 if not settle_up else 0.0)
        bt.up_shares = win.inventory["Up"]
        bt.down_shares = win.inventory["Down"]
        bt.pnl = payout - (win.cost["Up"] + win.cost["Down"])
        res.windows.append(bt)
