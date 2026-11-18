"""Discovery of Polymarket 5-minute crypto Up/Down markets.

Key facts (validated against the live API):

- Windows are aligned to unix timestamps divisible by 300.
- Event slugs are deterministic: ``{asset}-updown-5m-{window_ts}``
  (assets: btc, eth, sol, xrp).
- ``GET {gamma}/events?slug=...`` returns the event; its single market carries
  ``outcomes`` ("Up"/"Down") and ``clobTokenIds`` (parallel array).
- After the window closes, ``outcomePrices`` converges to ["1","0"] or ["0","1"].
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from .config import WINDOW_SECONDS
from .net import HttpClient

log = logging.getLogger("polbot.markets")


def window_start(ts: float) -> int:
    """Window start (unix seconds) containing ``ts``."""
    return int(ts) - (int(ts) % WINDOW_SECONDS)


def next_window_start(ts: float) -> int:
    """Start of the first window strictly after ``ts``."""
    return window_start(ts) + WINDOW_SECONDS


def slug_for(asset: str, window_ts: int) -> str:
    return f"{asset}-updown-5m-{int(window_ts)}"


@dataclass(frozen=True)
class FiveMinMarket:
    """Everything the engine needs about one 5-minute Up/Down market."""

    asset: str
    window_ts: int
    end_ts: int
    slug: str
    condition_id: str
    up_token: str
    down_token: str
    question: str
    accepting_orders: bool
    closed: bool

    @property
    def tokens(self) -> tuple[str, str]:
        return (self.up_token, self.down_token)


def _loads_maybe(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def parse_event(asset: str, ev: dict[str, Any]) -> Optional[FiveMinMarket]:
    """Extract a :class:`FiveMinMarket` from a Gamma event payload."""
    markets = ev.get("markets") or []
    if not markets:
        return None
    m = markets[0]
    outcomes = _loads_maybe(m.get("outcomes")) or []
    tokens = _loads_maybe(m.get("clobTokenIds")) or []
    if len(outcomes) != 2 or len(tokens) != 2:
        return None
    # Map outcome names to token ids robustly ("Up"/"Down", case-insensitive).
    lower = [str(o).strip().lower() for o in outcomes]
    if "up" in lower:
        i_up, i_down = lower.index("up"), lower.index("down")
    else:  # fall back to positional
        i_up, i_down = 0, 1
    slug = ev.get("slug") or m.get("slug") or ""
    window_ts = _window_ts_from_slug(slug)
    if window_ts is None:
        return None
    return FiveMinMarket(
        asset=asset,
        window_ts=window_ts,
        end_ts=window_ts + WINDOW_SECONDS,
        slug=slug,
        condition_id=str(m.get("conditionId") or m.get("condition_id") or ""),
        up_token=str(tokens[i_up]),
        down_token=str(tokens[i_down]),
        question=str(m.get("question") or ev.get("title") or slug),
        accepting_orders=bool(m.get("acceptingOrders", False)),
        closed=bool(ev.get("closed", False)),
    )


def _window_ts_from_slug(slug: str) -> Optional[int]:
    try:
        ts = int(slug.rsplit("-", 1)[-1])
    except (ValueError, IndexError):
        return None
    if ts <= 0 or ts % WINDOW_SECONDS != 0:
        return None
    return ts


def fetch_market(
    http: HttpClient,
    gamma_url: str,
    asset: str,
    window_ts: int,
) -> Optional[FiveMinMarket]:
    """Fetch the market for a window; ``None`` if it does not exist (yet)."""
    slug = slug_for(asset, window_ts)
    evs = http.get_json_or_none(f"{gamma_url}/events", params={"slug": slug})
    if not evs or not isinstance(evs, list):
        return None
    return parse_event(asset, evs[0])


def fetch_outcome(
    http: HttpClient,
    gamma_url: str,
    market: FiveMinMarket,
) -> Optional[str]:
    """Return ``"up"`` / ``"down"`` once resolved, else ``None``.

    Resolved markets report ``outcomePrices`` of ["1","0"] (Up won) or
    ["0","1"] (Down won). While a window is live the two prices simply track
    the market (e.g. 0.20/0.80) — that is NOT a resolution, so we require the
    winning side to be at >= 0.99.
    """
    evs = http.get_json_or_none(f"{gamma_url}/events", params={"slug": market.slug})
    if not evs or not isinstance(evs, list) or not evs:
        return None
    markets = evs[0].get("markets") or []
    if not markets:
        return None
    prices = _loads_maybe(markets[0].get("outcomePrices")) or []
    if len(prices) != 2:
        return None
    try:
        p_up, p_down = float(prices[0]), float(prices[1])
    except (TypeError, ValueError):
        return None
    if p_up >= 0.99 and p_down <= 0.01:
        return "up"
    if p_down >= 0.99 and p_up <= 0.01:
        return "down"
    return None
