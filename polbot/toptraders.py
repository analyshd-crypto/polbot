"""Top-trader analysis: find who really makes money on 5-minute crypto markets.

Uses Polymarket's public Data API v2:
- ``/v2/leaderboard``                  top wallets by PnL (any window)
- ``/v2/trades?user=...``              a wallet's fills (with window/tape)
- ``/v2/user-stats?user=...``          realized PnL / volume aggregates

Pipeline:
1. pull the day/week/all leaderboard;
2. for each top wallet, pull its trades on up/down 5m markets;
3. compute per-window PnL (buys vs settlement) and the wallet's win rate,
   maker share, average size, time-of-entry profile;
4. rank by win rate among wallets with enough windows to be meaningful,
   and print the >= 90% win-rate cohort with everything we know about them.
"""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

from .net import HttpClient

log = logging.getLogger("polbot.toptraders")

DATA_API = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"


# ------------------------------------------------------------------- api ----
def fetch_leaderboard(http: HttpClient, data_api: str = DATA_API,
                      window: str = "all", rank_by: str = "pnl", limit: int = 100
                      ) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor: Optional[str] = None
    pages = 0
    while pages * 100 < limit:
        params: dict[str, object] = {"window": window, "rank_by": rank_by,
                                     "limit": min(100, limit - len(out))}
        if cursor:
            params["cursor"] = cursor
        js = http.get_json_or_none(f"{data_api}/v2/leaderboard", params=params)
        if not isinstance(js, dict):
            break
        out.extend(js.get("data") or [])
        cursor = (js.get("pagination") or {}).get("next_cursor")
        pages += 1
        if not cursor or not (js.get("data")):
            break
    return out


def fetch_user_trades(http: HttpClient, user: str, data_api: str = DATA_API,
                      max_pages: int = 10, limit: int = 500
                      ) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor: Optional[str] = None
    for _ in range(max_pages):
        params: dict[str, object] = {"user": user, "limit": limit,
                                     "sort_direction": "DESC"}
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


# --------------------------------------------------------------- analysis ---
@dataclass
class WalletProfile:
    user: str
    name: str = ""
    windows: int = 0
    wins: int = 0
    losses: int = 0
    fills: int = 0
    maker_fills: int = 0
    volume: float = 0.0
    cost: float = 0.0
    payout: float = 0.0
    updown_trades: int = 0
    other_trades: int = 0
    avg_entry_tleft: float = 0.0
    _tlefts: list = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        d = self.wins + self.losses
        return self.wins / d if d else 0.0

    @property
    def pnl(self) -> float:
        return self.payout - self.cost

    @property
    def maker_share(self) -> float:
        return self.maker_fills / self.fills if self.fills else 0.0

    def row(self) -> str:
        return (f"{self.user[:10]}… win={self.win_rate:6.1%} n={self.windows:4d} "
                f"pnl={self.pnl:+11.2f} vol={self.volume:10.0f} "
                f"maker%={self.maker_share:5.1%} fills={self.fills:5d} "
                f"avgT-{self.avg_entry_tleft:5.0f}s")


def analyze_wallet(user: str, name: str, trades: list[dict[str, Any]]) -> WalletProfile:
    """Per-window PnL for one wallet from its 5m up/down trade list.

    PnL per window = settlement payout (winner side shares bought) − cost of
    all shares bought in that window. Sells complicate this; on these markets
    most bots only buy, so windows containing sells are still counted with
    buys-only accounting (documented approximation).
    """
    p = WalletProfile(user=user, name=name)
    by_window: dict[str, dict[str, Any]] = {}
    for t in trades:
        slug = str(t.get("event_slug") or t.get("slug") or "")
        if "-updown-" not in slug:
            p.other_trades += 1
            continue
        p.updown_trades += 1
        w = by_window.setdefault(slug, {"buys": [], "sells": []})
        side = str(t.get("side", "")).upper()
        try:
            price = float(t.get("price", 0.0))
            size = float(t.get("size", 0.0))
            ts = int(t.get("timestamp", 0))
        except (TypeError, ValueError):
            continue
        p.volume += price * size
        p.fills += 1
        # a BUY (as recorded) on the UP token is a bullish buy; maker/taker
        # role is not in this payload, so we approximate maker share by
        # resting-order prices later; count roles when present
        if t.get("maker") is not None:
            if bool(t.get("maker")):
                p.maker_fills += 1
        (w["buys"] if side == "BUY" else w["sells"]).append(
            {"price": price, "size": size, "ts": ts,
             "outcome": str(t.get("outcome", ""))})
    # settle each window using its slug-implied outcome is not possible from
    # trades alone; PnL here is (winner-side shares - cost) per window using
    # the outcome embedded in the window's later trades when clear, else skip
    # -> we fetch outcomes lazily below (see analyze_top_traders).
    p._windows_raw = by_window
    return p


def settle_profile(p: WalletProfile, outcomes: dict[str, str]) -> None:
    """Apply window outcomes (slug -> 'up'/'down') to a profile."""
    for slug, w in getattr(p, "_windows_raw", {}).items():
        winner = outcomes.get(slug)
        if winner is None:
            continue
        cost = 0.0
        payout = 0.0
        traded = False
        for b in w["buys"]:
            cost += b["price"] * b["size"]
            if b["outcome"].lower() == winner:
                payout += b["size"]
            traded = True
        # approximate: sells exit at their price (rare for bots here)
        for s in w["sells"]:
            cost -= s["price"] * s["size"]
            if s["outcome"].lower() == winner:
                payout -= s["size"]
            traded = True
        if not traded:
            continue
        p.cost += cost
        p.payout += payout
        pnl = payout - cost
        if pnl > 0:
            p.wins += 1
        elif pnl < 0:
            p.losses += 1
        p.windows += 1
        # time of entry relative to window end
        try:
            end_ts = int(slug.rsplit("-", 1)[-1]) + 300
            for b in w["buys"]:
                p._tlefts.append(max(0, end_ts - b["ts"]))
        except ValueError:
            pass
    if p._tlefts:
        p.avg_entry_tleft = sum(p._tlefts) / len(p._tlefts)


def fetch_outcomes_for_slugs(http: HttpClient, slugs: list[str],
                             gamma: str = GAMMA) -> dict[str, str]:
    """Resolve winner for event slugs (batched, cached by caller if needed)."""
    out: dict[str, str] = {}
    for slug in slugs:
        evs = http.get_json_or_none(f"{gamma}/events", params={"slug": slug})
        if not evs or not isinstance(evs, list) or not evs:
            continue
        m = (evs[0].get("markets") or [{}])[0]
        prices = m.get("outcomePrices")
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except json.JSONDecodeError:
                prices = None
        if not prices or len(prices) != 2:
            continue
        try:
            p_up, p_down = float(prices[0]), float(prices[1])
        except (TypeError, ValueError):
            continue
        if p_up >= 0.99:
            out[slug] = "up"
        elif p_down >= 0.99:
            out[slug] = "down"
    return out


def analyze_top_traders(http: HttpClient, top_n: int = 25,
                        min_windows: int = 20) -> list[WalletProfile]:
    """Full pipeline; returns profiles sorted by win rate."""
    lb = fetch_leaderboard(http, window="all", limit=top_n)
    profiles: list[WalletProfile] = []
    for i, row in enumerate(lb):
        user = str(row.get("user_id") or "")
        if not user:
            continue
        trades = fetch_user_trades(http, user, max_pages=4)
        prof = analyze_wallet(user, str(row.get("user_name") or ""), trades)
        slugs = list(getattr(prof, "_windows_raw", {}).keys())
        outcomes = fetch_outcomes_for_slugs(http, slugs[:120])
        settle_profile(prof, outcomes)
        profiles.append(prof)
        log.info("leaderboard #%d %s windows=%d win=%.1f%%",
                 i + 1, user[:10], prof.windows, prof.win_rate * 100)
    return sorted(profiles, key=lambda p: (p.win_rate, -p.pnl), reverse=True)
