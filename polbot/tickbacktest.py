"""Tick-level backtester over the REAL trade tape of each window.

This is the honest engine: instead of 1-minute price points it replays every
actual trade (typically 500-2,000 per window) plus 1-second Binance klines
for the fair-value leg.

Fill model (deliberately conservative)
--------------------------------------
- A resting BUY at price ``p`` on token X fills only on **trade-through**:
  some later trade on X prints strictly below ``p`` (mode="through").
  mode="touch" also fills on exact touches (``<= p``), which is optimistic
  because it ignores queue position.
- Fills happen at OUR price ``p`` (the taker got price improvement).
- The ladder is canceled and rebuilt every refresh interval (queue reset).
- Endgame hedges are taker orders: they fill at the first subsequent real
  trade price on the hedge token, capped by ``hedge_max_price``; taker fees
  apply. If the cap misses, the hedge simply does not happen (as live).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

from .binance import Kline, realized_vol_price_terms
from .config import BotConfig
from .fairvalue import prob_up, taker_fee
from .fvladder import FVLadderConfig, ToxicityMonitor, build_fv_ladder, endgame_hedge
from .grid import Quote, build_ladder
from .markets import FiveMinMarket
from .pnl import Fill, RunStats, SidePosition, WindowResult
from .tradesdata import TradesRecord

log = logging.getLogger("polbot.tickbacktest")

_EPS = 1e-9


# --------------------------------------------------------------- utilities --
def synth_book_price(last_trade: Optional[float], fallback: float = 0.5) -> Optional[float]:
    return last_trade if last_trade is not None else fallback


class KlineSeries:
    """1-second klines with no-lookahead accessors."""

    def __init__(self, klines: list[Kline]) -> None:
        self.k = sorted(klines, key=lambda x: x.open_ts)
        self.by_open = {x.open_ts: x for x in self.k}

    def price_at(self, ts_ms: int) -> Optional[float]:
        """Latest price observable at ``ts_ms`` (last CLOSED 1s candle close)."""
        best = None
        # candles are 1s; binary search for the last candle with close_ts <= ts
        lo, hi = 0, len(self.k) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.k[mid].close_ts <= ts_ms:
                best = self.k[mid].close
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def strike_at(self, ts_ms: int) -> Optional[float]:
        """First observable price at/after ts (open of the candle at ts)."""
        c = self.by_open.get(ts_ms - (ts_ms % 1000))
        if c is not None:
            return c.open
        # fall back: nearest candle within 2s
        base = ts_ms - (ts_ms % 1000)
        for d in (1000, -1000, 2000, -2000):
            c = self.by_open.get(base + d)
            if c is not None:
                return c.open
        return None

    def sigma_until(self, ts_ms: int, lookback_s: int = 1800) -> Optional[float]:
        """Cached realized vol (recomputed at most every 30s)."""
        bucket = ts_ms // 30_000
        cached = getattr(self, "_sigma_cache", None)
        if cached is None:
            cached = self._sigma_cache = {}
        if bucket in cached:
            return cached[bucket]
        window = [k for k in self.k if k.close_ts <= ts_ms][-lookback_s:]
        if len(window) < 30:
            window = self.k[:1800]
        sig = realized_vol_price_terms(window)
        cached[bucket] = sig
        return sig


# ------------------------------------------------------------------ engine --
@dataclass
class TickConfig:
    strategy: str = "fv"            # legacy | fv | fv_hedge | sniper
    fill_mode: str = "through"      # through | touch
    fee_rate: float = 0.07          # crypto taker fee coefficient (period-accurate)
    edge_min: float = 0.06          # sniper: min edge (probability units) after fees
    sniper_max_usd: float = 60.0    # sniper: max exposure per window
    refresh_s: float = 5.0
    pull_before_end_s: float = 60.0
    hedge_before_end_s: float = 20.0
    join_delay_s: float = 5.0


@dataclass
class TickResult(WindowResult):
    """WindowResult plus tick-level extras."""
    hedge_cost: float = 0.0
    taker_fees: float = 0.0
    maker_fills: int = 0
    taker_fills: int = 0
    fv_final_p_up: float = 0.5

    # WindowResult fields: window_ts slug winner up_shares down_shares spent pnl fills peak_quotes


class TickBacktester:
    def __init__(self, cfg: BotConfig, fvc: FVLadderConfig, tc: TickConfig) -> None:
        self.cfg = cfg
        self.fvc = fvc
        self.tc = tc

    # ------------------------------------------------------------- helpers --
    def _resting_fills(self, resting: dict[str, dict[float, float]],
                       trades, from_ts: int, to_ts: int, labels: dict[str, str]
                       ) -> list[Fill]:
        """Check resting bids against trades in (from_ts, to_ts]."""
        fills: list[Fill] = []
        strict = self.tc.fill_mode == "through"
        for tr in trades:
            if not (from_ts < tr.ts <= to_ts):
                continue
            levels = resting.get(tr.outcome)
            if not levels:
                continue
            for price in sorted(levels, reverse=True):
                hit = (tr.price < price - _EPS) if strict else (tr.price <= price + _EPS)
                if not hit:
                    break  # levels sorted desc; no deeper level can hit either
                shares = levels.pop(price)
                fills.append(Fill(ts=tr.ts, token_id="", label=tr.outcome,
                                  price=price, shares=shares))
        return fills

    # -------------------------------------------------------------- window --
    def run_window(self, rec: TradesRecord, series: Optional[KlineSeries]
                   ) -> TickResult:
        trades = rec.parsed()
        cfg, fvc, tc = self.cfg, self.fvc, self.tc
        market = FiveMinMarket(
            asset=rec.slug.split("-", 1)[0], window_ts=rec.window_ts,
            end_ts=rec.end_ts, slug=rec.slug, condition_id=rec.condition_id,
            up_token=rec.up_token, down_token=rec.down_token,
            question=rec.slug, accepting_orders=True, closed=True,
        )
        up = SidePosition()
        dn = SidePosition()
        resting: dict[str, dict[float, float]] = {"Up": {}, "Down": {}}
        last_price: dict[str, Optional[float]] = {"Up": None, "Down": None}
        toxicity = ToxicityMonitor(fvc.toxicity_fills, fvc.toxicity_pause_s)
        maker_fills = taker_fills = peak_quotes = 0
        hedge_cost = taker_fees = 0.0
        final_p = 0.5

        start = rec.window_ts + tc.join_delay_s
        pull_at = rec.end_ts - tc.pull_before_end_s
        hedge_at = rec.end_ts - tc.hedge_before_end_s

        # event stream: refresh ticks + hedge moment + settlement
        events: list[tuple[int, str]] = []
        t = start
        while t < pull_at:
            events.append((t, "refresh"))
            t += tc.refresh_s
        if tc.strategy == "fv_hedge":
            events.append((max(hedge_at, pull_at), "hedge"))
        events.sort()

        last_processed_ts = start - 1

        def apply_fills(fills: list[Fill]) -> None:
            nonlocal maker_fills
            for f in fills:
                if f.label == "Up":
                    up.add(f.price, f.shares)
                else:
                    dn.add(f.price, f.shares)
                maker_fills += 1
                toxicity.on_fill(f.label, float(f.ts))

        class _LedgerLike:  # minimal adapter for build_fv_ladder
            pass

        ledger = _LedgerLike()
        ledger.up, ledger.down = up, dn
        ledger.spent = 0.0

        for ev_ts, kind in events:
            if kind != "refresh":
                continue
            # 1) fills from trades since last event
            fills = self._resting_fills(resting, trades, last_processed_ts, ev_ts,
                                        {"Up": "Up", "Down": "Down"})
            apply_fills(fills)
            last_processed_ts = ev_ts
            ledger.spent = up.cost + dn.cost

            # 2) rebuild the ladder
            for tr in trades:
                if tr.ts <= last_processed_ts:
                    last_price[tr.outcome] = tr.price
            lp_up = last_price.get("Up")
            lp_dn = last_price.get("Down")
            if lp_up is None and lp_dn is not None:
                lp_up = round(1.0 - lp_dn, 6)
            if lp_dn is None and lp_up is not None:
                lp_dn = round(1.0 - lp_up, 6)
            if lp_up is None:
                continue  # nothing has traded yet

            if tc.strategy == "legacy":
                from .clob import Book
                b_up = Book(token_id="U", bids=[(round(lp_up - 0.01, 6), 1e9)],
                            asks=[(round(lp_up, 6), 1e9)], tick_size=0.01,
                            min_order_size=5.0, last_trade_price=lp_up)
                b_dn = Book(token_id="D", bids=[(round(lp_dn - 0.01, 6), 1e9)],
                            asks=[(round(lp_dn, 6), 1e9)], tick_size=0.01,
                            min_order_size=5.0, last_trade_price=lp_dn)
                quotes = build_ladder(market, b_up, b_dn, cfg,
                                      spent=up.cost + dn.cost,
                                      up_shares=up.shares, down_shares=dn.shares)
                resting = {"Up": {}, "Down": {}}
                for q in quotes:
                    resting[q.label][q.price] = q.size
                peak_quotes = max(peak_quotes, len(quotes))
            else:  # fv | fv_hedge
                fv = prob_up(None, None, None, 1.0)
                if series is not None:
                    spot = series.price_at(ev_ts * 1000)
                    strike = series.strike_at(rec.window_ts * 1000)
                    sigma = series.sigma_until(ev_ts * 1000)
                    fv = prob_up(spot, strike, sigma, rec.end_ts - ev_ts)
                final_p = fv.p_up
                ledger._toxicity = toxicity
                quotes = build_fv_ladder(market, _synth_book(lp_up), _synth_book(lp_dn),
                                         cfg, fvc, fv, ledger, float(ev_ts))
                resting = {"Up": {}, "Down": {}}
                for q in quotes:
                    resting[q.label][q.price] = q.size
                peak_quotes = max(peak_quotes, len(quotes))

        # final maker fills up to hedge/pull time
        last_event_ts = max(last_processed_ts, pull_at - 1)
        fills = self._resting_fills(resting, trades, last_processed_ts, pull_at, {})
        apply_fills(fills)
        last_processed_ts = pull_at

        # endgame hedge (taker)
        if tc.strategy == "fv_hedge":
            hedge_quotes = endgame_hedge(market, _ledger(up, dn), fvc,
                                         float(rec.end_ts - 1), None)
            for hq in hedge_quotes:
                # fill at the first real trade on that token at/after hedge time
                px = None
                for tr in trades:
                    if tr.ts >= hedge_at and tr.outcome == hq.label:
                        px = tr.price
                        break
                if px is None:
                    # no tape after hedge time: use last seen price
                    px = last_price.get(hq.label)
                if px is None or px > fvc.hedge_max_price + _EPS:
                    continue  # hedge missed (price ran past the cap)
                shares = hq.size
                fee = taker_fee(shares, px, self.tc.fee_rate)
                taker_fees += fee
                hedge_cost += px * shares + fee
                taker_fills += 1
                if hq.label == "Up":
                    up.add(px, shares)
                else:
                    dn.add(px, shares)

        # settle
        spent = up.cost + dn.cost
        win_side = up if rec.winner == "up" else dn
        pnl = win_side.shares - spent
        return TickResult(
            window_ts=rec.window_ts, slug=rec.slug, winner=rec.winner,
            up_shares=up.shares, down_shares=dn.shares, spent=spent, pnl=pnl,
            fills=maker_fills + taker_fills, peak_quotes=peak_quotes,
            hedge_cost=hedge_cost, taker_fees=taker_fees,
            maker_fills=maker_fills, taker_fills=taker_fills,
            fv_final_p_up=final_p,
        )


def _synth_book(price: float):
    from .clob import Book
    return Book(token_id="x", bids=[(round(price - 0.01, 6), 1e9)],
                asks=[(round(price, 6), 1e9)], tick_size=0.01,
                min_order_size=5.0, last_trade_price=price)


def _ledger(up: SidePosition, dn: SidePosition):
    class L:
        pass
    l = L()
    l.up, l.down = up, dn
    l.spent = up.cost + dn.cost
    return l


# -------------------------------------------------------------- harvester --
@dataclass
class HarvesterConfig:
    """Late-window favorite harvesting (the >=90% win-rate cohort's game)."""
    fv_min: float = 0.965          # model probability required
    max_buy_price: float = 0.97    # never pay above this
    margin: float = 0.015          # require price <= fv - margin - fee
    tleft_start: float = 120.0     # begin evaluating at T-120s
    tleft_stop: float = 12.0       # stop at T-12s (settlement manipulation zone)
    max_usd: float = 100.0         # per-window exposure cap
    per_buy_usd: float = 40.0
    both_sides_ok: bool = False    # cohort almost never holds both sides


def run_harvester_window(rec: TradesRecord, series: KlineSeries, tc: TickConfig,
                         hc: HarvesterConfig) -> TickResult:
    """Buy near-certain favorites late in the window; hold to settlement.

    Mirrors the observed >=90%-win-rate cohort: entries at 95-99c with
    1-2 minutes left, model probability (basis-corrected Binance gap) as the
    gate, taker fills at the next real tape price, no selling.
    """
    trades = rec.parsed()
    strike = series.strike_at(rec.window_ts * 1000)
    up = SidePosition()
    dn = SidePosition()
    fees = 0.0
    n_fills = 0
    exposure = 0.0
    last_price: Optional[float] = None
    trade_idx = 0

    t = rec.end_ts - hc.tleft_start
    t_end = rec.end_ts - hc.tleft_stop
    while t < t_end:
        while trade_idx < len(trades) and trades[trade_idx].ts <= t:
            tr = trades[trade_idx]
            last_price = tr.price if tr.outcome == "Up" else round(1.0 - tr.price, 6)
            trade_idx += 1
        if last_price is not None and strike is not None:
            spot = series.price_at(t * 1000)
            if spot is not None:
                fv = prob_up(spot, strike, series.sigma_until(t * 1000),
                             rec.end_ts - t)
                for want_up in (True, False):
                    # cohort behavior: one side per window (no straddles)
                    if not hc.both_sides_ok:
                        if want_up and dn.shares > 1e-9:
                            continue
                        if (not want_up) and up.shares > 1e-9:
                            continue
                    p_fair = fv.p_up if want_up else 1.0 - fv.p_up
                    if p_fair < hc.fv_min:
                        continue
                    mkt_price = last_price if want_up else round(1.0 - last_price, 6)
                    fee_frac = tc.fee_rate * mkt_price * (1.0 - mkt_price)
                    if mkt_price > hc.max_buy_price + _EPS:
                        continue
                    if mkt_price > p_fair - hc.margin - fee_frac - _EPS:
                        continue
                    if exposure >= hc.max_usd:
                        continue
                    fill_px = fill_depth = None
                    for tr in trades[trade_idx:]:
                        if tr.outcome == ("Up" if want_up else "Down"):
                            fill_px, fill_depth = tr.price, tr.size
                            break
                    if fill_px is None or fill_px > hc.max_buy_price + _EPS:
                        t += 1
                        continue
                    usd = min(hc.per_buy_usd, hc.max_usd - exposure)
                    shares = min(usd / fill_px, fill_depth * 0.5)
                    if shares * fill_px < 1.0:
                        t += 1
                        continue
                    fee = taker_fee(shares, fill_px, tc.fee_rate)
                    fees += fee
                    exposure += shares * fill_px + fee
                    n_fills += 1
                    if want_up:
                        up.add(fill_px, shares)
                    else:
                        dn.add(fill_px, shares)
                    last_price = None
        t += 1

    spent = up.cost + dn.cost
    win_side = up if rec.winner == "up" else dn
    pnl = win_side.shares - spent
    return TickResult(
        window_ts=rec.window_ts, slug=rec.slug, winner=rec.winner,
        up_shares=up.shares, down_shares=dn.shares, spent=spent, pnl=pnl,
        fills=n_fills, peak_quotes=0, hedge_cost=0.0, taker_fees=fees,
        maker_fills=0, taker_fills=n_fills, fv_final_p_up=0.5,
    )
@dataclass
class SniperResult(TickResult):
    edge_taken: float = 0.0


def run_sniper_window(rec: TradesRecord, series: KlineSeries, tc: TickConfig,
                      cfg: BotConfig) -> SniperResult:
    """Lead-lag sniper: taker buys when the model says Polymarket is mispriced.

    Every second we compute fair P(Up) from Binance and compare with the last
    observed Polymarket trade price. When the edge exceeds fees + margin, we
    buy the underpriced side as taker, paying the NEXT real trade price on
    that token (no lookahead; if the tape runs past our cap the trade misses).
    """
    trades = rec.parsed()
    strike = series.strike_at(rec.window_ts * 1000)
    up = SidePosition()
    dn = SidePosition()
    exposure = 0.0
    fees = 0.0
    edge_sum = 0.0
    n_fills = 0
    last_price: Optional[float] = None
    trade_idx = 0

    t0 = rec.window_ts + 5
    t_end = rec.end_ts - 10
    t = t0
    while t < t_end:
        t_left = rec.end_ts - t
        if t_left < getattr(tc, "_tleft_min", 0.0) or \
                t_left > getattr(tc, "_tleft_max", 300.0):
            # still advance the tape
            while trade_idx < len(trades) and trades[trade_idx].ts <= t:
                tr = trades[trade_idx]
                last_price = tr.price if tr.outcome == "Up" else round(1.0 - tr.price, 6)
                trade_idx += 1
            t += 1
            continue
        # advance the tape to time t
        while trade_idx < len(trades) and trades[trade_idx].ts <= t:
            tr = trades[trade_idx]
            last_price = tr.price if tr.outcome == "Up" else round(1.0 - tr.price, 6)
            trade_idx += 1
        if last_price is None or strike is None:
            t += 1
            continue
        spot = series.price_at(t * 1000)
        if spot is None:
            t += 1
            continue
        sigma = series.sigma_until(t * 1000)
        fv = prob_up(spot, strike, sigma, rec.end_ts - t)
        max_price = getattr(tc, "_max_price", 1.0)
        # candidate: buy UP if market underprices it
        for want_up, mkt_price in ((True, last_price), (False, round(1.0 - last_price, 6))):
            if mkt_price > max_price + _EPS:
                continue
            p_fair = fv.p_up if want_up else 1.0 - fv.p_up
            edge = p_fair - mkt_price
            be = tc.fee_rate * mkt_price * (1.0 - mkt_price)
            if edge - be < tc.edge_min:
                continue
            if exposure >= tc.sniper_max_usd:
                continue
            cap = mkt_price + (edge - be - tc.edge_min) * 0.5  # chase limit
            # fill at the next real trade on that token after t; we cannot
            # fill for more than the liquidity that trade demonstrated
            fill_px = None
            fill_depth = None
            for tr in trades[trade_idx:]:
                if tr.outcome == ("Up" if want_up else "Down"):
                    fill_px = tr.price
                    fill_depth = tr.size
                    break
            if fill_px is None or fill_px > cap + _EPS:
                continue
            usd = min(tc.sniper_max_usd - exposure, 25.0)
            max_shares_by_liquidity = max(0.0, fill_depth * 0.5)
            shares = min(usd / fill_px, max_shares_by_liquidity)
            if shares * fill_px < 1.0:
                continue  # below the $1 taker minimum
            fee = taker_fee(shares, fill_px, tc.fee_rate)
            fees += fee
            exposure += shares * fill_px + fee
            n_fills += 1
            edge_sum += edge
            if want_up:
                up.add(fill_px, shares)
            else:
                dn.add(fill_px, shares)
            last_price = None  # re-evaluate from scratch after a fill
        t += 1

    spent = up.cost + dn.cost
    win_side = up if rec.winner == "up" else dn
    pnl = win_side.shares - spent
    return SniperResult(
        window_ts=rec.window_ts, slug=rec.slug, winner=rec.winner,
        up_shares=up.shares, down_shares=dn.shares, spent=spent, pnl=pnl,
        fills=n_fills, peak_quotes=0, hedge_cost=0.0, taker_fees=fees,
        maker_fills=0, taker_fills=n_fills, fv_final_p_up=0.5,
        edge_taken=edge_sum,
    )
