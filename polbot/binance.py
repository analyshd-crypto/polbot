"""Binance spot data (the fast leg of the lead-lag signal).

Polymarket's up/down markets settle on a Chainlink data stream, but in
practice Binance spot leads everything else. The bot watches Binance to
estimate the fair probability of "Up", and treats Polymarket quotes that lag
that estimate as tradable mispricing.

Only public REST endpoints are used:
- ``GET /api/v3/klines``            (1s or 1m candles)
- ``GET /api/v3/ticker/bookTicker`` (best bid/ask)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

from .net import HttpClient

log = logging.getLogger("polbot.binance")

SYMBOLS = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}


@dataclass(frozen=True)
class Kline:
    open_ts: int
    open: float
    high: float
    low: float
    close: float
    close_ts: int


def fetch_klines(
    http: HttpClient,
    binance_url: str,
    symbol: str,
    interval: str = "1m",
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    limit: int = 1000,
) -> list[Kline]:
    """Fetch klines (``interval`` in {"1s","1m",...}); returns [] on failure."""
    params: dict[str, object] = {"symbol": symbol, "interval": interval, "limit": limit}
    if start_ms is not None:
        params["startTime"] = int(start_ms)
    if end_ms is not None:
        params["endTime"] = int(end_ms)
    rows = http.get_json_or_none(f"{binance_url}/api/v3/klines", params=params)
    out: list[Kline] = []
    for r in rows or []:
        try:
            out.append(Kline(
                open_ts=int(r[0]), open=float(r[1]), high=float(r[2]),
                low=float(r[3]), close=float(r[4]), close_ts=int(r[6]),
            ))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def fetch_book_ticker(http: HttpClient, binance_url: str, symbol: str
                      ) -> Optional[tuple[float, float]]:
    row = http.get_json_or_none(f"{binance_url}/api/v3/ticker/bookTicker",
                                params={"symbol": symbol})
    if not row:
        return None
    try:
        return float(row["bidPrice"]), float(row["askPrice"])
    except (TypeError, ValueError, KeyError):
        return None


# ------------------------------------------------------------------ vol ----
def realized_vol_price_terms(klines: list[Kline]) -> Optional[float]:
    """Realized volatility of the *price* (not log-return), per sqrt(second).

    Estimated from close-to-close moves: sigma = std(price moves normalized by
    sqrt(seconds between closes)). This is the quantity the fair-value model
    needs: P(up) = Phi(gap / (sigma * sqrt(t_remaining))).
    """
    pts = [(k.close_ts, k.close) for k in klines if k.close_ts > 0]
    if len(pts) < 3:
        return None
    # de-dup & sort by time
    seen: dict[int, float] = {}
    for t, p in pts:
        seen[t] = p
    ts = sorted(seen)
    if len(ts) < 3:
        return None
    vals = [seen[t] for t in ts]
    moves: list[float] = []
    for i in range(1, len(ts)):
        dt = ts[i] - ts[i - 1]
        if dt <= 0:
            continue
        dp = vals[i] - vals[i - 1]
        moves.append(dp / math.sqrt(dt / 1000.0))
    if len(moves) < 2:
        return None
    mean = sum(moves) / len(moves)
    var = sum((m - mean) ** 2 for m in moves) / (len(moves) - 1)
    sigma_move = math.sqrt(var)              # price units per sqrt(second)
    price = vals[-1]
    if price <= 0:
        return None
    return sigma_move / price                # relative per sqrt(second)


class BinanceFeed:
    """Small helper the live engine polls each refresh cycle."""

    def __init__(self, http: HttpClient, binance_url: str, asset: str,
                 vol_window_klines: int = 30) -> None:
        self.http = http
        self.url = binance_url
        self.symbol = SYMBOLS.get(asset, asset.upper() + "USDT")
        self.vol_window_klines = vol_window_klines
        self._klines: list[Kline] = []
        self._last_fetch_ts = 0

    def refresh(self, now_s: float, force: bool = False) -> None:
        if not force and now_s - self._last_fetch_ts < 30:
            return
        self._klines = fetch_klines(
            self.http, self.url, self.symbol, interval="1m",
            limit=self.vol_window_klines,
        )
        self._last_fetch_ts = now_s

    def spot(self) -> Optional[float]:
        if not self._klines:
            return None
        return self._klines[-1].close

    def sigma(self) -> Optional[float]:
        return realized_vol_price_terms(self._klines)
