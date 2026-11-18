"""Backtest data: fetch real 5-minute windows from Polymarket and cache them.

For every window we store:

- the event/market metadata (slug, token ids, final outcome),
- the CLOB price history for BOTH tokens at 1-minute fidelity (real market
  prices, ~5-6 points per window),
- the resolved winner (from ``outcomePrices`` after close).

Data is cached as JSON under ``data/cache/<asset>/<window_ts>.json`` so
repeated backtests are instant and reproducible.
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
from .markets import _loads_maybe, slug_for

log = logging.getLogger("polbot.data")


@dataclass
class WindowRecord:
    window_ts: int
    end_ts: int
    slug: str
    condition_id: str
    up_token: str
    down_token: str
    winner: Optional[str]          # "up" | "down" | None (unresolved)
    up_hist: list = field(default_factory=list)    # [(t, price), ...]
    down_hist: list = field(default_factory=list)

    def any_history(self) -> bool:
        return bool(self.up_hist or self.down_hist)


def _cache_path(cache_dir: str, asset: str, window_ts: int) -> str:
    return os.path.join(cache_dir, asset, f"{window_ts}.json")


def _load_cached(cache_dir: str, asset: str, window_ts: int) -> Optional[WindowRecord]:
    path = _cache_path(cache_dir, asset, window_ts)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return WindowRecord(**json.load(fh))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _save_cached(cache_dir: str, asset: str, rec: WindowRecord) -> None:
    path = _cache_path(cache_dir, asset, rec.window_ts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(asdict(rec), fh)


def _fetch_one(
    http: HttpClient,
    gamma_url: str,
    clob_url: str,
    asset: str,
    window_ts: int,
    cache_dir: Optional[str],
) -> Optional[WindowRecord]:
    """Fetch (or load from cache) one window's record, or None if unusable."""
    if cache_dir:
        cached = _load_cached(cache_dir, asset, window_ts)
        if cached is not None and cached.winner is not None:
            return cached

    slug = slug_for(asset, window_ts)
    evs = http.get_json_or_none(f"{gamma_url}/events", params={"slug": slug})
    if not evs or not isinstance(evs, list) or not evs:
        return None
    ev = evs[0]
    markets = ev.get("markets") or []
    if not markets:
        return None
    m = markets[0]
    outcomes = _loads_maybe(m.get("outcomes")) or []
    tokens = _loads_maybe(m.get("clobTokenIds")) or []
    prices = _loads_maybe(m.get("outcomePrices")) or []
    if len(outcomes) != 2 or len(tokens) != 2 or not ev.get("closed"):
        return None
    lower = [str(o).strip().lower() for o in outcomes]
    i_up = lower.index("up") if "up" in lower else 0
    i_down = lower.index("down") if "down" in lower else 1
    if len(prices) != 2:
        return None
    try:
        p_up, p_down = float(prices[i_up]), float(prices[i_down])
    except (TypeError, ValueError):
        return None
    if p_up <= 0.5 and p_down <= 0.5:
        return None  # unresolved / ambiguous
    winner = "up" if p_up > 0.5 else "down"

    from .clob import fetch_price_history

    up_hist = fetch_price_history(
        http, clob_url, tokens[i_up], window_ts, window_ts + WINDOW_SECONDS, fidelity=1
    )
    down_hist = fetch_price_history(
        http, clob_url, tokens[i_down], window_ts, window_ts + WINDOW_SECONDS, fidelity=1
    )
    rec = WindowRecord(
        window_ts=window_ts,
        end_ts=window_ts + WINDOW_SECONDS,
        slug=slug,
        condition_id=str(m.get("conditionId") or ""),
        up_token=str(tokens[i_up]),
        down_token=str(tokens[i_down]),
        winner=winner,
        up_hist=[[t, p] for t, p in up_hist],
        down_hist=[[t, p] for t, p in down_hist],
    )
    if not rec.any_history():
        return None
    if cache_dir:
        _save_cached(cache_dir, asset, rec)
    return rec


def fetch_windows(
    cfg,
    end_window_ts: int,
    count: int,
    cache_dir: Optional[str] = None,
    max_workers: int = 4,
    progress_every: int = 25,
) -> list[WindowRecord]:
    """Fetch the ``count`` most recent resolved windows ending at ``end_window_ts``.

    Windows are fetched newest-first in parallel and returned oldest-first.
    Missing/unresolved windows are skipped.
    """
    http = HttpClient(
        timeout=cfg.request_timeout_s,
        retry_attempts=cfg.http_retry_attempts,
        retry_backoff_s=cfg.http_retry_backoff_s,
    )
    starts = [end_window_ts - i * WINDOW_SECONDS for i in range(count)]
    records: list[WindowRecord] = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(_fetch_one, http, cfg.gamma_url, cfg.clob_url,
                        cfg.asset, wts, cache_dir)
            for wts in starts
        ]
        for idx, fut in enumerate(futures, start=1):
            try:
                rec = fut.result()
            except Exception as exc:  # noqa: BLE001
                log.debug("window fetch failed: %s", exc)
                rec = None
            if rec is not None:
                records.append(rec)
            if idx % progress_every == 0:
                log.info("fetched %d/%d windows (%.0fs elapsed, %d usable)",
                         idx, count, time.time() - t0, len(records))
    records.sort(key=lambda r: r.window_ts)
    return records


def load_cached_windows(cache_dir: str, asset: str) -> list[WindowRecord]:
    """Load every cached window for an asset, oldest first."""
    d = os.path.join(cache_dir, asset)
    if not os.path.isdir(d):
        return []
    out = []
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name), "r", encoding="utf-8") as fh:
                rec = WindowRecord(**json.load(fh))
            if rec.winner is not None:
                out.append(rec)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return out
