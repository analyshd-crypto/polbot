"""Tick-level backtest data: every trade of a window, from the Data API v2.

The v0 backtester replayed 1-minute price points (5-6 per window) — enough to
test mechanics, too coarse to trust PnL. This module fetches the *complete
trade tape* per window:

    GET data-api.polymarket.com/v2/trades?condition=<cond>&limit=500[&offset=..]

Each trade carries side, price, size, timestamp, outcome token. Cached as
``data/trades/<asset>/<window_ts>.json``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .config import WINDOW_SECONDS
from .net import HttpClient
from .markets import slug_for

log = logging.getLogger("polbot.tradesdata")

DATA_API = "https://data-api.polymarket.com"


@dataclass
class Trade:
    ts: int
    price: float
    size: float
    side: str          # BUY | SELL (taker side)
    outcome: str       # "Up" | "Down"
    outcome_index: int
    wallet: str = ""

    def as_up_price(self) -> tuple[float, str]:
        """(price, taker_side) expressed on the UP token."""
        if self.outcome.lower() == "up":
            return self.price, self.side
        return round(1.0 - self.price, 6), ("SELL" if self.side == "BUY" else "BUY")


@dataclass
class TradesRecord:
    window_ts: int
    end_ts: int
    slug: str
    condition_id: str
    up_token: str
    down_token: str
    winner: Optional[str]
    trades: list = field(default_factory=list)   # list[dict] raw

    def parsed(self) -> list[Trade]:
        """Trades inside the window only, oldest first.

        The condition can trade for hours before the 5-minute window opens
        (markets are created a day ahead); those pre-window prints are
        excluded so the backtest sees only the live window tape.
        """
        out = []
        for t in self.trades:
            try:
                ts = int(t["timestamp"])
                if not (self.window_ts <= ts < self.end_ts):
                    continue
                out.append(Trade(
                    ts=ts, price=float(t["price"]),
                    size=float(t["size"]), side=str(t.get("side", "")),
                    outcome=str(t.get("outcome", "")),
                    outcome_index=int(t.get("outcome_index", 0)),
                    wallet=str(t.get("proxy_wallet", "")),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        out.sort(key=lambda t: t.ts)
        return out

    def any(self) -> bool:
        return any(self.window_ts <= int(t.get("timestamp", -1)) < self.end_ts
                   for t in self.trades)


def _path(cache_dir: str, asset: str, window_ts: int) -> str:
    return os.path.join(cache_dir, "trades", asset, f"{window_ts}.json")


def fetch_window_trades(
    http: HttpClient,
    data_api: str,
    condition_id: str,
    max_pages: int = 6,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """All trades for one condition (cursor pagination; newest-first pages).

    NOTE: the ``offset`` parameter of this endpoint is broken server-side
    (returns 0 rows); pagination only works via ``next_cursor``.
    """
    out: list[dict[str, Any]] = []
    cursor: Optional[str] = None
    for _ in range(max_pages):
        params: dict[str, object] = {"condition": condition_id, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        js = http.get_json_or_none(f"{data_api}/v2/trades", params=params)
        if not isinstance(js, dict):
            break
        rows = js.get("data") or []
        out.extend(rows)
        cursor = (js.get("pagination") or {}).get("next_cursor")
        if not cursor or len(rows) < limit:
            break
    return out


def fetch_trades_one(
    http: HttpClient,
    gamma_url: str,
    data_api: str,
    asset: str,
    window_ts: int,
    cache_dir: Optional[str],
    max_pages: int = 4,
) -> Optional[TradesRecord]:
    """Fetch (or load cached) the trade tape for one resolved window."""
    if cache_dir:
        p = _path(cache_dir, asset, window_ts)
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    return TradesRecord(**json.load(fh))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

    slug = slug_for(asset, window_ts)
    evs = http.get_json_or_none(f"{gamma_url}/events", params={"slug": slug})
    if not evs or not isinstance(evs, list) or not evs:
        return None
    ev = evs[0]
    markets = ev.get("markets") or []
    if not markets:
        return None
    m = markets[0]

    def _loads(v):
        return json.loads(v) if isinstance(v, str) else v

    outcomes = _loads(m.get("outcomes")) or []
    tokens = _loads(m.get("clobTokenIds")) or []
    prices = _loads(m.get("outcomePrices")) or []
    if len(outcomes) != 2 or len(tokens) != 2 or not ev.get("closed"):
        return None
    lower = [str(o).strip().lower() for o in outcomes]
    i_up = lower.index("up") if "up" in lower else 0
    i_down = 1 - i_up
    if len(prices) == 2:
        try:
            p_up = float(prices[i_up])
            winner = "up" if p_up >= 0.5 else "down"
        except (TypeError, ValueError):
            winner = None
    else:
        winner = None
    cond = str(m.get("conditionId") or "")
    if not cond:
        return None

    trades = fetch_window_trades(http, data_api, cond, max_pages=max_pages)
    rec = TradesRecord(
        window_ts=window_ts,
        end_ts=window_ts + WINDOW_SECONDS,
        slug=slug,
        condition_id=cond,
        up_token=str(tokens[i_up]),
        down_token=str(tokens[i_down]),
        winner=winner,
        trades=trades,
    )
    if not rec.any():
        return None
    if cache_dir:
        p = _path(cache_dir, asset, window_ts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(asdict(rec), fh)
    return rec


def fetch_trades_windows(
    http: HttpClient,
    gamma_url: str,
    data_api: str,
    asset: str,
    end_window_ts: int,
    count: int,
    cache_dir: Optional[str] = None,
    max_workers: int = 4,
    max_pages: int = 4,
    progress_every: int = 25,
) -> list[TradesRecord]:
    """Fetch the ``count`` most recent resolved windows ending at end_window_ts."""
    starts = [end_window_ts - i * WINDOW_SECONDS for i in range(count)]
    records: list[TradesRecord] = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = [
            pool.submit(fetch_trades_one, http, gamma_url, data_api,
                        asset, wts, cache_dir, max_pages)
            for wts in starts
        ]
        for i, fut in enumerate(futs, start=1):
            try:
                rec = fut.result()
            except Exception as exc:  # noqa: BLE001
                log.debug("trades fetch failed: %s", exc)
                rec = None
            if rec is not None:
                records.append(rec)
            if i % progress_every == 0:
                log.info("trades fetched %d/%d windows (%.0fs, %d usable)",
                         i, count, time.time() - t0, len(records))
    records.sort(key=lambda r: r.window_ts)
    return records


def load_cached_trades(cache_dir: str, asset: str) -> list[TradesRecord]:
    d = os.path.join(cache_dir, "trades", asset)
    if not os.path.isdir(d):
        return []
    out = []
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name), "r", encoding="utf-8") as fh:
                out.append(TradesRecord(**json.load(fh)))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return out
