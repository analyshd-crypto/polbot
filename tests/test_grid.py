"""Tests for the ladder builder."""

from __future__ import annotations

import pytest

from polbot.clob import Book
from polbot.config import BotConfig
from polbot.grid import build_ladder, ladder_top, level_prices
from polbot.markets import FiveMinMarket


def market() -> FiveMinMarket:
    return FiveMinMarket(
        asset="btc", window_ts=1795002600, end_ts=1795002900,
        slug="btc-updown-5m-1795002600", condition_id="0xabc",
        up_token="UP", down_token="DOWN", question="q",
        accepting_orders=True, closed=False,
    )


def book(token: str, bid: float, ask: float, tick=0.01, min_size=5.0) -> Book:
    return Book(
        token_id=token,
        bids=[(bid, 1000.0)],
        asks=[(ask, 1000.0)],
        tick_size=tick,
        min_order_size=min_size,
    )


def test_level_prices_tick_aligned():
    cfg = BotConfig()
    levels = level_prices(cfg, 0.10, 0.01)
    assert levels == [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10]
    assert level_prices(cfg, 0.005, 0.01) == []  # below min_price


def test_ladder_top_respects_ask_and_max():
    cfg = BotConfig()
    b = book("UP", 0.44, 0.46)
    assert ladder_top(b, cfg, tick=0.01) == pytest.approx(0.45)
    # no ask on the book -> fall back to max_price
    empty = Book(token_id="UP", bids=[], asks=[], tick_size=0.01)
    assert ladder_top(empty, cfg, tick=0.01) == pytest.approx(0.99)
    # other side's top applies the self-cross guard
    assert ladder_top(b, cfg, other_top=0.60, tick=0.01) == pytest.approx(0.39)


def test_build_ladder_two_sided_below_ask():
    cfg = BotConfig()
    up, down = book("UP", 0.44, 0.46), book("DOWN", 0.53, 0.54)
    quotes = build_ladder(market(), up, down, cfg)
    up_prices = sorted(q.price for q in quotes if q.token_id == "UP")
    down_prices = sorted(q.price for q in quotes if q.token_id == "DOWN")
    assert up_prices[-1] == pytest.approx(0.45)   # ask 0.46 - 1 tick
    assert down_prices[-1] == pytest.approx(0.53)  # ask 0.54 - 1 tick
    assert up_prices[0] == pytest.approx(cfg.min_price)
    assert all(q.size == 5 for q in quotes)
    # no self-cross: our own up bid + down bid must never exceed 1
    assert max(up_prices) + max(down_prices) <= 1.0 - 0.01 + 1e-9


def test_build_ladder_self_cross_guard():
    """Wide books where naive tops would cross each other (0.59 + 0.59 > 1)."""
    cfg = BotConfig()
    up, down = book("UP", 0.55, 0.60), book("DOWN", 0.55, 0.60)
    quotes = build_ladder(market(), up, down, cfg)
    up_top = max(q.price for q in quotes if q.token_id == "UP")
    down_top = max(q.price for q in quotes if q.token_id == "DOWN")
    assert up_top + down_top <= 1.0 - 0.01 + 1e-9


def test_build_ladder_budget_trims():
    cfg = BotConfig(max_cost_per_market=50.0)
    up, down = book("UP", 0.44, 0.46), book("DOWN", 0.53, 0.54)
    quotes = build_ladder(market(), up, down, cfg, spent=0.0)
    planned = sum(q.price * q.size for q in quotes)
    assert planned <= 50.0 + 1e-6
    # expensive (top) levels are kept first when trimming
    up_prices = sorted(q.price for q in quotes if q.token_id == "UP")
    assert up_prices[-1] == pytest.approx(0.45)


def test_build_ladder_inventory_cap_skips_side():
    cfg = BotConfig(max_shares_per_side=100.0)
    up, down = book("UP", 0.44, 0.46), book("DOWN", 0.53, 0.54)
    quotes = build_ladder(market(), up, down, cfg, spent=0.0,
                          up_shares=100.0, down_shares=0.0)
    assert all(q.token_id == "DOWN" for q in quotes)


def test_build_ladder_respects_min_order_size():
    cfg = BotConfig(size_per_level=3.0)
    up, down = book("UP", 0.44, 0.46, min_size=5.0), book("DOWN", 0.53, 0.54, min_size=5.0)
    quotes = build_ladder(market(), up, down, cfg)
    assert all(q.size >= 5.0 for q in quotes)


def test_build_ladder_caps_levels_when_tick_shrinks():
    """Tick drops to 0.001 near extremes; the ladder must stay bounded."""
    cfg = BotConfig()
    up, down = book("UP", 0.979, 0.98, tick=0.001), book("DOWN", 0.019, 0.02, tick=0.001)
    quotes = build_ladder(market(), up, down, cfg)
    up_n = sum(1 for q in quotes if q.token_id == "UP")
    down_n = sum(1 for q in quotes if q.token_id == "DOWN")
    assert up_n <= cfg.max_levels_per_side
    assert down_n <= cfg.max_levels_per_side
    # and the self-cross guard still holds
    up_top = max(q.price for q in quotes if q.token_id == "UP")
    down_top = max(q.price for q in quotes if q.token_id == "DOWN")
    assert up_top + down_top <= 1.0 - 0.001 + 1e-9
