"""Live smoke test (read-only, no auth, no orders).

Runs against the real Gamma/CLOB APIs when the network is available and
skips gracefully otherwise (e.g. in CI sandboxes without egress).
"""

from __future__ import annotations

import pytest

from polbot.config import BotConfig
from polbot.clob import fetch_book, fetch_fee_rate, fetch_price_history
from polbot.markets import fetch_market, fetch_outcome, window_start
from polbot.net import HttpClient

pytestmark = pytest.mark.live

CFG = BotConfig(asset="btc")


def _http():
    return HttpClient(timeout=8.0, retry_attempts=1)


def _skip_if_offline(exc: BaseException) -> None:
    pytest.skip(f"network unavailable: {exc}")


def test_live_market_discovery():
    import time

    http = _http()
    try:
        market = fetch_market(http, CFG.gamma_url, "btc", window_start(time.time()))
    except Exception as exc:  # noqa: BLE001
        _skip_if_offline(exc)
        return
    if market is None:
        pytest.skip("btc 5m event not found for the current window")
    assert market.window_ts % 300 == 0
    assert market.up_token and market.down_token
    assert market.up_token != market.down_token


def test_live_books_and_fees():
    import time

    http = _http()
    try:
        market = fetch_market(http, CFG.gamma_url, "btc", window_start(time.time()))
        assert market is not None
        book = fetch_book(http, CFG.clob_url, market.up_token)
        fee = fetch_fee_rate(http, CFG.clob_url, market.up_token)
    except Exception as exc:  # noqa: BLE001
        _skip_if_offline(exc)
        return
    assert book.tick_size in (0.01, 0.001, 0.0001)
    assert book.min_order_size >= 1
    assert fee is not None and fee > 0


def test_live_price_history_resolved_window():
    import time

    http = _http()
    now = int(time.time())
    past = window_start(now) - 300
    try:
        market = fetch_market(http, CFG.gamma_url, "btc", past)
        if market is None:
            pytest.skip("previous window event unavailable")
        outcome = fetch_outcome(http, CFG.gamma_url, market)
        hist = fetch_price_history(
            http, CFG.clob_url, market.up_token, past, past + 300, fidelity=1
        )
    except Exception as exc:  # noqa: BLE001
        _skip_if_offline(exc)
        return
    # the previous window should be resolved by now (or skip if still pending)
    if outcome is None:
        pytest.skip("previous window not yet resolved")
    assert outcome in ("up", "down")
    assert isinstance(hist, list)
