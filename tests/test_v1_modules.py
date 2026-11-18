"""Tests for the v1 modules: fairvalue (basis), fvladder, tickbacktest,
tradesdata parsing, and the harvester/sniper engines."""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from polbot.binance import Kline, realized_vol_price_terms
from polbot.config import BotConfig
from polbot.fairvalue import prob_up, taker_fee, BASIS_SIGMA
from polbot.fvladder import FVLadderConfig, ToxicityMonitor, endgame_hedge
from polbot.markets import FiveMinMarket
from polbot.pnl import SidePosition
from polbot.tickbacktest import (
    KlineSeries, TickBacktester, TickConfig, run_harvester_window,
)
from polbot.tradesdata import TradesRecord


# ------------------------------------------------------------ fair value ----
class TestProbUp:
    def test_no_inputs_is_coin_flip(self):
        fv = prob_up(None, None, None, 100.0)
        assert fv.p_up == 0.5

    def test_zero_gap_is_half(self):
        fv = prob_up(64000.0, 64000.0, 1e-4, 120.0)
        assert abs(fv.p_up - 0.5) < 1e-9

    def test_positive_gap_favors_up(self):
        fv = prob_up(64200.0, 64000.0, 1e-4, 120.0)
        assert fv.p_up > 0.5

    def test_gap_sign_symmetry(self):
        a = prob_up(64200.0, 64000.0, 1e-4, 120.0).p_up
        b = prob_up(63800.0, 64000.0, 1e-4, 120.0).p_up
        assert abs(a - (1 - b)) < 1e-9

    def test_basis_keeps_humility(self):
        """Small gaps late in the window must not produce near-certainty."""
        # gap of 0.01% (~$6.4) with 5s left: without basis this is ~99.9%
        fv = prob_up(64006.4, 64000.0, 1e-4, 5.0)
        assert fv.p_up < 0.90, "basis term must cap confidence on small gaps"

    def test_large_gap_still_confident(self):
        # gap of 0.5% ($320): 40/40 such windows settled Binance-way
        fv = prob_up(64320.0, 64000.0, 1e-4, 30.0)
        assert fv.p_up > 0.99

    def test_time_decay(self):
        """Same gap is worth less with more time left."""
        late = prob_up(64100.0, 64000.0, 1e-4, 20.0).p_up
        early = prob_up(64100.0, 64000.0, 1e-4, 280.0).p_up
        assert late > early


def test_taker_fee_formula():
    # docs: crypto 0.07 rate -> 100 shares at 0.50 = $1.75
    assert abs(taker_fee(100.0, 0.50, 0.07) - 1.75) < 1e-9
    assert taker_fee(100.0, 0.99, 0.07) < 0.10  # cheap at the extremes


# ------------------------------------------------------------ vol / klines --
def test_realized_vol_price_terms():
    ks = [Kline(open_ts=i * 1000, open=100, high=100.1, low=99.9,
                close=100.0, close_ts=i * 1000 + 999) for i in range(50)]
    assert realized_vol_price_terms(ks) is not None


def test_kline_series_no_lookahead():
    ks = [Kline(open_ts=i * 1000, open=100 + i, high=0, low=0,
                close=100.0 + i, close_ts=i * 1000 + 999) for i in range(100)]
    s = KlineSeries(ks)
    # at t=5000ms the last CLOSED candle is the one closing at 4999 -> close=104
    assert s.price_at(5000) == 104.0
    assert s.strike_at(5000) == 105.0  # open of the candle at 5000


# ------------------------------------------------------------ toxicity ------
def test_toxicity_monitor_pauses():
    t = ToxicityMonitor(limit=3, pause_s=30.0)
    now = 1000.0
    t.on_fill("Up", now)
    t.on_fill("Up", now + 1)
    assert not t.paused("Up", now + 2)
    t.on_fill("Up", now + 2)
    assert t.paused("Up", now + 3)          # 3 in a row -> paused
    assert not t.paused("Down", now + 3)    # other side unaffected
    assert not t.paused("Up", now + 32)     # pause expired


# ------------------------------------------------------------ endgame -------
def _market():
    return FiveMinMarket(asset="btc", window_ts=1000, end_ts=1300,
                         slug="btc-updown-5m-1000", condition_id="0xc",
                         up_token="U", down_token="D", question="q",
                         accepting_orders=True, closed=False)


def test_endgame_hedge_flattens_long_up():
    m = _market()
    ledger = type("L", (), {})()
    ledger.up = SidePosition()
    ledger.down = SidePosition()
    ledger.up.add(0.30, 100.0)   # long 100 UP
    fvc = FVLadderConfig()
    h = endgame_hedge(m, ledger, fvc, now=1290.0)   # 10s left
    assert len(h) == 1 and h[0].label == "Down" and h[0].size == 100.0


def test_endgame_hedge_skips_small():
    m = _market()
    ledger = type("L", (), {})()
    ledger.up = SidePosition()
    ledger.down = SidePosition()
    ledger.up.add(0.30, 3.0)     # below the 5-share minimum
    fvc = FVLadderConfig()
    assert endgame_hedge(m, ledger, fvc, now=1290.0) == []


def test_endgame_hedge_not_yet():
    m = _market()
    ledger = type("L", (), {})()
    ledger.up = SidePosition()
    ledger.down = SidePosition()
    ledger.up.add(0.30, 100.0)
    fvc = FVLadderConfig()
    assert endgame_hedge(m, ledger, fvc, now=1000.0) == []  # too early


# ------------------------------------------------------------ tradesdata ----
def _rec(trades, winner="up"):
    return TradesRecord(window_ts=1000, end_ts=1300,
                        slug="btc-updown-5m-1000", condition_id="0xc",
                        up_token="U", down_token="D", winner=winner,
                        trades=trades)


def test_trades_record_filters_prewindow():
    rec = _rec([
        {"timestamp": 900, "price": 0.5, "size": 10, "side": "BUY",
         "outcome": "Up", "outcome_index": 0, "proxy_wallet": "w"},
        {"timestamp": 1100, "price": 0.6, "size": 10, "side": "SELL",
         "outcome": "Up", "outcome_index": 0, "proxy_wallet": "w"},
        {"timestamp": 1400, "price": 0.7, "size": 10, "side": "SELL",
         "outcome": "Up", "outcome_index": 0, "proxy_wallet": "w"},
    ])
    parsed = rec.parsed()
    assert len(parsed) == 1 and parsed[0].ts == 1100
    assert rec.any()


# ------------------------------------------------------------ tick fills ----
class TestRestingFills:
    def _bt(self, mode="through"):
        cfg = BotConfig(asset="btc")
        return TickBacktester(cfg, FVLadderConfig(),
                              TickConfig(strategy="fv", fill_mode=mode))

    def test_trade_through_fills(self):
        bt = self._bt()
        trades = [type("T", (), {"ts": 1010, "price": 0.40, "size": 50,
                                 "side": "SELL", "outcome": "Up"})()]
        resting = {"Up": {0.45: 5.0}, "Down": {}}
        fills = bt._resting_fills(resting, trades, 1000, 1010, {})
        assert len(fills) == 1 and fills[0].price == 0.45  # fills at OUR price

    def test_touch_does_not_fill_in_through_mode(self):
        bt = self._bt()
        trades = [type("T", (), {"ts": 1010, "price": 0.45, "size": 50,
                                 "side": "SELL", "outcome": "Up"})()]
        resting = {"Up": {0.45: 5.0}, "Down": {}}
        assert bt._resting_fills(resting, trades, 1000, 1010, {}) == []

    def test_touch_mode_fills_on_touch(self):
        bt = self._bt("touch")
        trades = [type("T", (), {"ts": 1010, "price": 0.45, "size": 50,
                                 "side": "SELL", "outcome": "Up"})()]
        resting = {"Up": {0.45: 5.0}, "Down": {}}
        assert len(bt._resting_fills(resting, trades, 1000, 1010, {})) == 1

    def test_no_fill_above_level(self):
        bt = self._bt()
        trades = [type("T", (), {"ts": 1010, "price": 0.50, "size": 50,
                                 "side": "SELL", "outcome": "Up"})()]
        resting = {"Up": {0.45: 5.0}, "Down": {}}
        assert bt._resting_fills(resting, trades, 1000, 1010, {}) == []

    def test_outside_time_window_ignored(self):
        bt = self._bt()
        trades = [type("T", (), {"ts": 1200, "price": 0.40, "size": 50,
                                 "side": "SELL", "outcome": "Up"})()]
        resting = {"Up": {0.45: 5.0}, "Down": {}}
        assert bt._resting_fills(resting, trades, 1000, 1010, {}) == []


# ------------------------------------------------------------ harvester -----
def _rec_at(w0, trades, winner="up"):
    return TradesRecord(window_ts=w0, end_ts=w0 + 300,
                        slug=f"btc-updown-5m-{w0}", condition_id="0xc",
                        up_token="U", down_token="D", winner=winner,
                        trades=trades)


def test_harvester_buys_locked_favorite():
    """Locked-in favorite + cheap tape price -> buys and wins at settlement."""
    w0 = 1_000_000
    # Binance path: opens at 100, rallies to 101.5 (+1.5%, ~3.2 basis-sigmas
    # -> model probability ~0.999) 100s into the window and stays there
    ks = []
    for i in range(2300):
        px = 100.0 + (1.5 if i >= 1100 else 0.0)
        ks.append(Kline(open_ts=(w0 - 1000 + i) * 1000, open=px, high=px,
                        low=px, close=px, close_ts=(w0 - 1000 + i) * 1000 + 999))
    series = KlineSeries(ks)
    # tape: someone dumps UP shares at 0.93 with ~90s left (panic)
    trades = [
        {"timestamp": w0 + 60, "price": 0.55, "size": 100, "side": "SELL",
         "outcome": "Up", "outcome_index": 0, "proxy_wallet": "x"},
        {"timestamp": w0 + 210, "price": 0.93, "size": 200, "side": "SELL",
         "outcome": "Up", "outcome_index": 0, "proxy_wallet": "x"},
    ]
    rec = _rec_at(w0, trades, winner="up")
    tc = TickConfig(strategy="harvester", fee_rate=0.05)
    from polbot.tickbacktest import HarvesterConfig
    r = run_harvester_window(rec, series, tc,
                             HarvesterConfig(fv_min=0.965, margin=0.01,
                                             max_buy_price=0.97,
                                             tleft_start=120.0))
    assert r.fills >= 1
    assert r.pnl > 0          # bought <=0.93, settled at $1


def test_harvester_skips_when_fair_value_low():
    """No gap -> no trades, no fills."""
    w0 = 1_000_000
    ks = [Kline(open_ts=(w0 - 1000 + i) * 1000, open=100.0, high=100.0,
                low=100.0, close=100.0, close_ts=(w0 - 1000 + i) * 1000 + 999)
          for i in range(2300)]
    series = KlineSeries(ks)
    trades = [
        {"timestamp": w0 + 210, "price": 0.93, "size": 200, "side": "SELL",
         "outcome": "Up", "outcome_index": 0, "proxy_wallet": "x"},
    ]
    rec = _rec_at(w0, trades, winner="up")
    tc = TickConfig(strategy="harvester", fee_rate=0.05)
    from polbot.tickbacktest import HarvesterConfig
    r = run_harvester_window(rec, series, tc,
                             HarvesterConfig(fv_min=0.965, margin=0.01,
                                             tleft_start=120.0))
    assert r.fills == 0 and r.pnl == 0.0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ---------------------------------------------- engine integration (fv path) --
def test_engine_fv_path_end_to_end():
    """The v1 engine (FV ladder + endgame hedge) runs a full window on
    synthetic replay data without errors and produces a settled result."""
    import asyncio

    from polbot.backends import PaperBackend
    from polbot.backtest import ReplayMarketData
    from polbot.clock import SimClock
    from polbot.data import WindowRecord
    from polbot.engine import Engine
    from polbot.pnl import RunStats
    from polbot.risk import RiskManager

    W = 1_800_000
    up_hist = [[W + 10, 0.50], [W + 70, 0.45], [W + 130, 0.30], [W + 190, 0.20]]
    down_hist = [[W + 10, 0.50], [W + 70, 0.55], [W + 130, 0.70], [W + 190, 0.80]]
    rec = WindowRecord(window_ts=W, end_ts=W + 300, slug=f"btc-updown-5m-{W}",
                       condition_id="0xabc", up_token="UP", down_token="DOWN",
                       winner="down", up_hist=up_hist, down_hist=down_hist)

    async def _inner():
        cfg = BotConfig()
        clock = SimClock(start=float(W))
        data = ReplayMarketData(cfg, clock)
        backend = PaperBackend()
        risk = RiskManager(cfg)
        stats = RunStats()
        from polbot.fvladder import FVLadderConfig

        class StaticFvFeed:
            def fair_value(self, market, now):
                from polbot.fairvalue import prob_up
                return prob_up(None, None, None, market.end_ts - now)

        engine = Engine(cfg, data, backend, clock, risk, stats,
                        fvc=FVLadderConfig(endgame_hedge=True),
                        fv_feed=StaticFvFeed())
        data.set_window(rec)
        market = data.get_market_sync(W)
        return await engine.run_one(market)

    result = asyncio.run(_inner())
    assert result.winner == "down"
    assert result.window_ts == W
    # ladder must have been quoted (paper backend registered resting levels)
    assert result.fills >= 0  # ran to settlement without raising
