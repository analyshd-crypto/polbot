"""Public CLOB REST access: order books, price history, fee rates.

None of these endpoints require authentication. Response shapes were
validated against the live API (see docs/RESEARCH.md).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .net import HttpClient

log = logging.getLogger("polbot.clob")

_EPS = 1e-9


@dataclass
class Book:
    """One outcome token's order book (prices in dollars)."""

    token_id: str
    bids: list[tuple[float, float]] = field(default_factory=list)  # best first (desc)
    asks: list[tuple[float, float]] = field(default_factory=list)  # best first (asc)
    tick_size: float = 0.01
    min_order_size: float = 5.0
    last_trade_price: Optional[float] = None

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return self.best_bid or self.best_ask

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"Book(bb={self.best_bid} ba={self.best_ask} tick={self.tick_size} "
            f"min={self.min_order_size} nbids={len(self.bids)} nasks={len(self.asks)})"
        )


def _parse_levels(raw: list[dict]) -> list[tuple[float, float]]:
    out = []
    for lvl in raw or []:
        try:
            out.append((float(lvl["price"]), float(lvl["size"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def fetch_book(http: HttpClient, clob_url: str, token_id: str) -> Book:
    data = http.get_json(f"{clob_url}/book", params={"token_id": token_id})
    bids = sorted(_parse_levels(data.get("bids")), key=lambda x: -x[0])
    asks = sorted(_parse_levels(data.get("asks")), key=lambda x: x[0])
    last = data.get("last_trade_price")
    return Book(
        token_id=token_id,
        bids=bids,
        asks=asks,
        tick_size=float(data.get("tick_size") or 0.01),
        min_order_size=float(data.get("min_order_size") or 5.0),
        last_trade_price=float(last) if last is not None else None,
    )


def fetch_books(
    http: HttpClient, clob_url: str, up_token: str, down_token: str
) -> tuple[Book, Book]:
    return (
        fetch_book(http, clob_url, up_token),
        fetch_book(http, clob_url, down_token),
    )


def fetch_price_history(
    http: HttpClient,
    clob_url: str,
    token_id: str,
    start_ts: int,
    end_ts: int,
    fidelity: int = 1,
) -> list[tuple[int, float]]:
    """Price history for one token as ``[(unix_ts, price), ...]`` (ascending)."""
    data = http.get_json(
        f"{clob_url}/prices-history",
        params={
            "market": token_id,
            "startTs": int(start_ts),
            "endTs": int(end_ts),
            "fidelity": fidelity,
        },
    )
    history = data.get("history") if isinstance(data, dict) else data
    pts: list[tuple[int, float]] = []
    for p in history or []:
        try:
            pts.append((int(p["t"]), float(p["p"])))
        except (KeyError, TypeError, ValueError):
            continue
    pts.sort(key=lambda x: x[0])
    return pts


def fetch_fee_rate(http: HttpClient, clob_url: str, token_id: str) -> Optional[float]:
    """Base taker fee rate for the market (crypto 5m markets: 1000 = 0.07 formula)."""
    data = http.get_json_or_none(f"{clob_url}/fee-rate", params={"token_id": token_id})
    if isinstance(data, dict) and "base_fee" in data:
        return float(data["base_fee"])
    return None
