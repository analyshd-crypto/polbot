#!/usr/bin/env python3
"""Fill-level calibration analysis for the sniper model.

Re-runs the sniper decision loop but records EVERY fill: price, shares,
model P(win), realized outcome. Reports calibration by price bucket — the
single most important scientific check of whether the Binance-gap model has
true edge against the Chainlink-settled tape.
"""
from __future__ import annotations

import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polbot.binance import Kline
from polbot.config import BotConfig
from polbot.fairvalue import prob_up, taker_fee
from polbot.tickbacktest import KlineSeries, TickConfig
from polbot.tradesdata import TradesRecord, load_cached_trades

_EPS = 1e-9


def load_series(cache_dir, asset):
    rows = []
    for path in sorted(glob.glob(os.path.join(cache_dir, "binance", asset, "*.json"))):
        rows.extend(json.load(open(path)))
    rows.sort(key=lambda r: r[0])
    return KlineSeries([Kline(open_ts=r[0], open=r[1], high=r[2], low=r[3],
                              close=r[4], close_ts=r[5]) for r in rows])


def main():
    edge_min = float(sys.argv[1]) if len(sys.argv) > 1 else 0.15
    fee_rate = 0.07
    recs = load_cached_trades("data", "btc")
    series = load_series("data", "btc")

    fills = []  # (price, shares, p_model, won, t_left)
    for rec in recs:
        trades = rec.parsed()
        strike = series.strike_at(rec.window_ts * 1000)
        if strike is None:
            continue
        last_price = None
        trade_idx = 0
        exposure = 0.0
        t = rec.window_ts + 5
        while t < rec.end_ts - 10:
            while trade_idx < len(trades) and trades[trade_idx].ts <= t:
                tr = trades[trade_idx]
                last_price = tr.price if tr.outcome == "Up" else round(1.0 - tr.price, 6)
                trade_idx += 1
            if last_price is None:
                t += 1
                continue
            spot = series.price_at(t * 1000)
            if spot is None:
                t += 1
                continue
            fv = prob_up(spot, strike, series.sigma_until(t * 1000), rec.end_ts - t)
            for want_up, mkt_price in ((True, last_price),
                                       (False, round(1.0 - last_price, 6))):
                p_fair = fv.p_up if want_up else 1.0 - fv.p_up
                edge = p_fair - mkt_price
                be = fee_rate * mkt_price * (1.0 - mkt_price)
                if edge - be < edge_min or exposure >= 60.0:
                    continue
                fill_px = fill_depth = None
                for tr in trades[trade_idx:]:
                    if tr.outcome == ("Up" if want_up else "Down"):
                        fill_px, fill_depth = tr.price, tr.size
                        break
                if fill_px is None:
                    continue
                cap = mkt_price + (edge - be - edge_min) * 0.5
                if fill_px > cap + _EPS:
                    continue
                shares = min(25.0 / fill_px, fill_depth * 0.5)
                if shares * fill_px < 1.0:
                    continue
                won = (rec.winner == "up") == want_up
                fills.append((fill_px, shares, p_fair, won, rec.end_ts - t))
                exposure += shares * fill_px + taker_fee(shares, fill_px, fee_rate)
                last_price = None
            t += 1

    print(f"edge_min={edge_min}  total fills={len(fills)}")
    if not fills:
        return
    buckets = [(0.0, 0.10), (0.10, 0.25), (0.25, 0.50), (0.50, 0.75), (0.75, 0.90)]
    print(f"{'price bucket':>14} {'n':>5} {'avg p(win|fill)':>15} "
          f"{'realized win%':>13} {'avg edge@$':>10} {'pnl':>9}")
    for lo, hi in buckets:
        grp = [f for f in fills if lo <= f[0] < hi]
        if not grp:
            continue
        n = len(grp)
        p_avg = sum(f[2] for f in grp) / n
        r_win = sum(1 for f in grp if f[3]) / n
        pnl = sum((f[1] if f[3] else 0.0) - f[0] * f[1] for f in grp)
        usd = sum(f[0] * f[1] for f in grp)
        print(f"{lo:5.2f}-{hi:5.2f}   {n:5d} {p_avg:15.3f} {r_win:13.1%} "
              f"{usd/n:10.2f} {pnl:+9.2f}")
    total_pnl = sum((f[1] if f[3] else 0.0) - f[0] * f[1] for f in fills)
    total_won = sum(1 for f in fills if f[3])
    print(f"TOTAL: {len(fills)} fills, fill win rate {total_won/len(fills):.1%}, "
          f"pre-fee pnl {total_pnl:+.2f}")
    tl = [f[4] for f in fills]
    late = [f for f in fills if f[4] <= 60]
    if late:
        lw = sum(1 for f in late if f[3])
        lpnl = sum((f[1] if f[3] else 0.0) - f[0] * f[1] for f in late)
        print(f"late fills (t_left<=60s): {len(late)}, win {lw/len(late):.1%}, "
              f"pnl {lpnl:+.2f}")


if __name__ == "__main__":
    main()
