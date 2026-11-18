"""Tests for the v2 mm engine (polbot.mm.*)."""

from __future__ import annotations

import math
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polbot.mm.config import Config
from polbot.mm.engine.fairvalue import FairValueModel, TwapTracker, norm_cdf
from polbot.mm.engine.kelly import KellySizer
from polbot.mm.engine.fees import FeeModel
from polbot.mm.engine.grid import LadderBuilder
from polbot.mm.models import Window, Quote
from polbot.mm.exec.paper import PaperExecutor
from polbot.mm.data.gamma import parse_window_start


# ---------------------------------------------------------------- fairvalue
def _model(**kw):
    m = FairValueModel(vol_halflife_s=90, min_vol_annual=0.15, max_vol_annual=3.0,
                       twap_seconds=30, **kw)
    for i, px in enumerate([100.0 + 0.01 * ((-1) ** i) for i in range(200)]):
        m.update(1_000_000 + i, px)
    return m


def test_fairvalue_at_strike_is_half():
    m = _model()
    p, _ = m.prob_up(spot=100.0, strike=100.0, remaining_s=120)
    assert abs(p - 0.5) < 0.02


def test_fairvalue_monotone_in_spot():
    m = _model()
    ps = [m.prob_up(spot=s, strike=100.0, remaining_s=120)[0]
          for s in (99.0, 99.9, 100.0, 100.1, 101.0)]
    assert ps == sorted(ps)
    assert ps[0] < 0.2 and ps[-1] > 0.8


def test_fairvalue_deep_gap_saturates():
    m = _model()
    p_hi, _ = m.prob_up(spot=103.0, strike=100.0, remaining_s=10)
    p_lo, _ = m.prob_up(spot=97.0, strike=100.0, remaining_s=10)
    assert p_hi > 0.95
    assert p_lo < 0.05


def test_fairvalue_basis_humility():
    """Small gaps must NOT print ~1.0 with a Binance-anchored feed."""
    m = _model(basis_sigma=0.00046)
    p, _ = m.prob_up(spot=100.06, strike=100.0, remaining_s=60)  # 6bps gap
    assert p < 0.90
    m0 = _model(basis_sigma=0.0)
    p0, _ = m0.prob_up(spot=100.06, strike=100.0, remaining_s=60)
    assert p0 > p  # oracle-anchored model is sharper


def test_fairvalue_twap_zero_remaining_deterministic():
    m = _model()
    p, _ = m.prob_up(100.5, 100.0, 0.0, twap_sum=100.4 * 30)
    assert p == 1.0
    p2, _ = m.prob_up(99.5, 100.0, 0.0, twap_sum=99.6 * 30)
    assert p2 == 0.0


def test_twap_tracker_sum():
    tt = TwapTracker(30.0)
    end = 1_000_030.0
    for i in range(30):
        tt.update(end - 30 + i, 100.0, end)
    s, obs = tt.sum(end)
    assert abs(obs - 30.0) < 1.01
    assert abs(s - 100.0 * 30.0) < 120  # ~constant price integrates to px*30


# ---------------------------------------------------------------- kelly
def test_kelly_known_value():
    k = KellySizer(fraction=1.0, max_clip_usd=1e9, min_clip_usd=0.0)
    # q=0.6, p=0.5: f* = (0.6-0.5)/0.5 = 0.2
    assert abs(k.kelly_fraction(0.6, 0.5) - 0.2) < 1e-9
    assert k.kelly_fraction(0.5, 0.6) == 0.0  # no edge -> no bet


def test_kelly_fractional_and_cap():
    k = KellySizer(fraction=0.25, max_clip_usd=40.0, min_clip_usd=1.0)
    usd = k.size_usd(bankroll=1000.0, q=0.9, p=0.5)
    assert usd == 40.0  # (0.4/0.5)*0.25*1000 = 200 -> capped
    assert k.size_usd(1000.0, 0.501, 0.5) == 0.0  # below min clip


# ---------------------------------------------------------------- fees
def test_fee_formula_matches_docs():
    fm = FeeModel(regime="legacy", taker_rate=0.07)
    # docs example: crypto, 100 shares at $0.50 -> $1.75
    assert abs(fm.taker_fee(100, 0.50) - 1.75) < 1e-9
    assert fm.maker_fee(100, 0.50) == 0.0


def test_fee_tiered_regime():
    fm = FeeModel(regime="tiered", taker_theta=0.05)
    assert abs(fm.taker_fee(100, 0.50) - 1.25) < 1e-9
    assert fm.maker_rebate(100, 0.5) == 0.0  # small maker, no rebate tier


def test_fee_break_even():
    fm = FeeModel(regime="legacy", taker_rate=0.07)
    be = fm.taker_break_even(0.50)
    assert abs(be - (0.5 + 0.07 * 0.25)) < 1e-9


# ---------------------------------------------------------------- grid
def _win():
    return Window(series="btc", event_slug="btc-updown-5m-1795016400",
                  condition_id="0xc", tokens=["tok-up", "tok-down"],
                  outcomes=["Up", "Down"], start_ts=1795016400.0,
                  end_ts=1795016700.0, strike=100.0)


def test_grid_quotes_below_fair_only():
    cfg = Config.load(None)
    lb = LadderBuilder(cfg)
    q = lb.build(_win(), fair_up=0.60, bankroll=1000.0, free_budget=120.0)
    assert q, "ladder must produce quotes"
    for quote in q:
        fair = 0.60 if quote.outcome == "Up" else 0.40
        assert quote.price <= fair - cfg.quoting.min_edge + 1e-9
        assert quote.size >= 5.0 - 1e-9  # CLOB minimum


def test_grid_budget_respected_and_top_down():
    cfg = Config.load(None)
    lb = LadderBuilder(cfg)
    budget = 120.0
    q = lb.build(_win(), fair_up=0.50, bankroll=1000.0, free_budget=budget)
    total = sum(x.notional for x in q)
    assert total <= budget + 1e-6
    up_prices = [x.price for x in q if x.outcome == "Up"]
    assert up_prices, "must quote Up side"
    assert max(up_prices) <= 0.50 - cfg.quoting.min_edge + 1e-9
    assert max(up_prices) >= 0.40  # top level near fair, not buried in the tail


def test_grid_dominance_skew_splits_budget():
    cfg = Config.load(None)
    lb = LadderBuilder(cfg)
    q = lb.build(_win(), fair_up=0.90, bankroll=1000.0, free_budget=120.0)
    up_usd = sum(x.notional for x in q if x.outcome == "Up")
    dn_usd = sum(x.notional for x in q if x.outcome == "Down")
    assert up_usd > dn_usd  # strong side gets the skew


# ---------------------------------------------------------------- parsing
def test_slug_epoch_parsing():
    assert parse_window_start("btc-updown-5m-1795016400") == 1795016400.0
    assert parse_window_start("no-epoch-here") is None


# ---------------------------------------------------------------- paper exec
class FakeClob:
    def __init__(self, ask):
        self.ask = ask

    def best_bid_ask(self, token_id):
        return 0.0, self.ask


def test_paper_executor_fills_on_cross():
    cfg = Config.load(None)
    ex = PaperExecutor(cfg, clob=FakeClob(ask=0.30))
    w = _win()
    quotes = [Quote(token_id="tok-up", outcome="Up", price=0.35, size=10.0),
              Quote(token_id="tok-down", outcome="Down", price=0.20, size=10.0)]
    ex.place_quotes(w, quotes)
    fills = ex.poll_fills([w])
    # book ask 0.30 <= our 0.35 up bid -> filled; down bid 0.20 not crossed
    assert len(fills) == 1
    assert fills[0].outcome == "Up" and fills[0].price == 0.35
    assert abs(fills[0].size - 10.0) < 1e-9
    # resting level consumed
    assert 0.35 not in ex.resting["tok-up"]


# ---------------------------------------------------------------- backtest
def test_backtest_engine_synthetic_ramp():
    """Constructed window: price ramps up -> Up must win; accounting consistent."""
    from polbot.mm.backtest.engine import BacktestEngine, BacktestParams, Bar

    cfg = Config.load(None)
    t0 = 1_794_000_000
    t0 -= t0 % 300
    bars = []
    px = 100.0
    for i in range(300):
        px += 0.01 if i < 250 else 0.0  # ramp then flat
        bars.append(Bar(t0 + i, px, px, px, px))
    bars.append(Bar(t0 + 300, px, px, px, px))
    bars.append(Bar(t0 + 301, px, px, px, px))
    bars.append(Bar(t0 + 600, px, px, px, px))  # next window (partial, skipped)
    eng = BacktestEngine(cfg, {"BTCUSDT": bars},
                         BacktestParams(lag_seconds=3.0))
    res = eng.run()
    assert len(res.windows) == 1
    w = res.windows[0]
    assert w.settle_up is True
    # spot ~102.5 vs strike ~100.0 -> model strongly favors Up; the engine
    # must have filled Up bids; pnl accounting must equal payout - cost
    assert w.up_shares > 0
    payout = w.up_shares  # Up won
    cost = w.up_cost + w.down_cost
    assert abs(w.pnl - (payout - cost)) < 1e-6


def test_backtest_band_table():
    from polbot.mm.backtest.engine import BacktestEngine, BacktestParams, Bar

    cfg = Config.load(None)
    t0 = 1_794_000_000
    t0 -= t0 % 300
    bars = []
    px = 100.0
    for i in range(302):
        px += 0.02  # steady ramp: Up wins
        bars.append(Bar(t0 + i, px, px, px, px))
    eng = BacktestEngine(cfg, {"BTCUSDT": bars}, BacktestParams())
    res = eng.run()
    bands = res.band_table()
    assert bands, "band table must exist"
    total_shares = sum(b["shares"] for b in bands.values())
    assert total_shares > 0
    # every fill must be attributed
    assert len(res.fills) > 0


# ---------------------------------------------------------------- gamma event
def test_gamma_event_to_window():
    from polbot.mm.data.gamma import GammaClient
    g = GammaClient()
    ev = {
        "slug": "btc-updown-5m-1795016400",
        "endDate": "2026-11-18T15:45:00Z",
        "markets": [{
            "conditionId": "0xabc",
            "clobTokenIds": '["111", "222"]',
            "outcomes": '["Up", "Down"]',
            "negRisk": False,
            "liquidity": "1500",
            "minimum_order_size": 5,
        }],
    }
    w = g._event_to_window(ev, "btc-up-or-down-5m")
    assert w is not None
    assert w.start_ts == 1795016400.0
    assert w.end_ts == 1795016700.0  # 15:45:00Z
    assert w.tokens == ["111", "222"]
    assert w.neg_risk is False
    assert w.min_size == 5.0
