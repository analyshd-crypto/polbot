"""Tests for window math, slug construction and event parsing."""

from __future__ import annotations

import pytest

from polbot.config import WINDOW_SECONDS
from polbot.markets import (
    fetch_market,
    fetch_outcome,
    next_window_start,
    parse_event,
    slug_for,
    window_start,
)

from conftest import FakeHttp, REAL_EVENT, resolved_event


def test_window_math():
    assert WINDOW_SECONDS == 300
    assert window_start(1795002714) == 1795002600
    assert window_start(1795002600) == 1795002600
    assert window_start(1795002899) == 1795002600
    assert window_start(1795002900) == 1795002900
    assert next_window_start(1795002714) == 1795002900
    assert next_window_start(1795002600) == 1795002900


def test_slug():
    assert slug_for("btc", 1795002600) == "btc-updown-5m-1795002600"
    assert slug_for("eth", 1795002600) == "eth-updown-5m-1795002600"


def test_parse_event_maps_outcomes():
    m = parse_event("btc", REAL_EVENT)
    assert m is not None
    assert m.asset == "btc"
    assert m.window_ts == 1795002600
    assert m.end_ts == 1795002900
    assert m.slug == "btc-updown-5m-1795002600"
    assert m.up_token == "5945...UP"
    assert m.down_token == "3258...DOWN"
    assert m.condition_id == "0xabc123"
    assert m.accepting_orders is True
    assert m.closed is False


def test_parse_event_rejects_bad_payloads():
    assert parse_event("btc", {"markets": []}) is None
    assert parse_event("btc", {"markets": [{"outcomes": "[]"}]}) is None
    # slug without a valid trailing timestamp
    bad = {"slug": "btc-updown-5m-nope", "markets": REAL_EVENT["markets"]}
    assert parse_event("btc", bad) is None


def test_fetch_market_and_outcome():
    http = FakeHttp(
        {
            "https://gamma.test/events?slug=btc-updown-5m-1795002600": [REAL_EVENT],
        }
    )
    m = fetch_market(http, "https://gamma.test", "btc", 1795002600)
    assert m is not None and m.window_ts == 1795002600

    http2 = FakeHttp(
        {
            "https://gamma.test/events?slug=btc-updown-5m-1795002600": [
                resolved_event("down")
            ],
        }
    )
    assert fetch_outcome(http2, "https://gamma.test", m) == "down"

    http3 = FakeHttp(
        {
            "https://gamma.test/events?slug=btc-updown-5m-1795002600": [
                REAL_EVENT  # still live: 0.205 / 0.795 mid-market
            ],
        }
    )
    assert fetch_outcome(http3, "https://gamma.test", m) is None
