#!/usr/bin/env python3
"""Run the tick-level backtest across strategies on real trade tapes.

Usage:
    python scripts/run_tick_backtest.py --asset btc --strategies legacy,fv,fv_hedge,sniper
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polbot.binance import Kline  # noqa: E402
from polbot.config import BotConfig  # noqa: E402
from polbot.fvladder import FVLadderConfig  # noqa: E402
from polbot.tickbacktest import (  # noqa: E402
    HarvesterConfig, KlineSeries, MMTickConfig, TickBacktester, TickConfig,
    run_harvester_window, run_mm_window, run_sniper_window,
)
from polbot.tradesdata import load_cached_trades  # noqa: E402


def load_klines(cache_dir: str, asset: str) -> dict:
    """Load cached 1s klines; returns per-second price lookup built lazily."""
    rows = []
    d = os.path.join(cache_dir, "binance", asset)
    for path in sorted(glob.glob(os.path.join(d, "*.json"))):
        with open(path) as fh:
            rows.extend(json.load(fh))
    rows.sort(key=lambda r: r[0])
    klines = [Kline(open_ts=r[0], open=r[1], high=r[2], low=r[3],
                    close=r[4], close_ts=r[5]) for r in rows]
    return KlineSeries(klines)


def summarize(results) -> str:
    n = len(results)
    if not n:
        return "no results"
    wins = sum(1 for r in results if r.pnl > 0)
    losses = sum(1 for r in results if r.pnl < 0)
    total = sum(r.pnl for r in results)
    worst = min(r.pnl for r in results)
    best = max(r.pnl for r in results)
    peak = dd = 0.0
    cum = 0.0
    for r in results:
        cum += r.pnl
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    fill_w = 0
    fill_n = 0
    for r in results:
        fill_n += r.maker_fills + r.taker_fills
    avg = total / n
    return (f"windows={n} W/L={wins}/{losses} win_rate={wins/max(1,wins+losses):.1%} "
            f"pnl={total:+.2f} avg={avg:+.3f} worst={worst:+.2f} best={best:+.2f} "
            f"maxDD={dd:.2f} fills={fill_n}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="btc")
    ap.add_argument("--cache", default="data")
    ap.add_argument("--strategies", default="legacy,fv,fv_hedge,sniper")
    ap.add_argument("--fee-rate", type=float, default=0.07,
                    help="taker fee coefficient (0.07 pre 2026-11-20, 0.05 after, BTC 5m)")
    ap.add_argument("--fill-mode", default="through", choices=["through", "touch"])
    ap.add_argument("--winrate-mode", action="store_true",
                    help="fv_hedge: always flatten before settlement")
    ap.add_argument("--edge-min", type=float, default=0.06,
                    help="sniper: min fee-adjusted edge (probability units)")
    ap.add_argument("--tleft-min", type=float, default=0.0,
                    help="sniper: skip when less than this many seconds remain")
    ap.add_argument("--tleft-max", type=float, default=300.0,
                    help="sniper: skip when more than this many seconds remain")
    ap.add_argument("--max-price", type=float, default=1.0,
                    help="sniper: never buy above this price")
    ap.add_argument("--fv-min", type=float, default=0.965,
                    help="harvester: minimum model probability")
    ap.add_argument("--harvest-margin", type=float, default=0.015,
                    help="harvester: price must be this far below fair value")
    ap.add_argument("--tleft-start", type=float, default=120.0,
                    help="harvester: begin at T-minus this many seconds")
    ap.add_argument("--limit", type=int, default=0, help="limit number of windows")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    recs = load_cached_trades(args.cache, args.asset)
    if args.limit:
        recs = recs[-args.limit:]
    if not recs:
        print("no cached trade tapes; run scripts/fetch_tick_data.py first")
        return
    series = load_klines(args.cache, args.asset)

    cfg = BotConfig(asset=args.asset, max_cost_per_market=150.0)
    fvc = FVLadderConfig(winrate_mode=args.winrate_mode)
    hc = HarvesterConfig(fv_min=args.fv_min, margin=args.harvest_margin,
                         tleft_start=args.tleft_start)
    mmc = MMTickConfig()
    all_out = {}

    for strat in args.strategies.split(","):
        strat = strat.strip()
        tc = TickConfig(strategy=strat, fee_rate=args.fee_rate,
                        fill_mode=args.fill_mode, edge_min=args.edge_min)
        tc._tleft_min = args.tleft_min
        tc._tleft_max = args.tleft_max
        tc._max_price = args.max_price
        bt = TickBacktester(cfg, fvc, tc)
        results = []
        for rec in recs:
            try:
                if strat == "sniper":
                    r = run_sniper_window(rec, series, tc, cfg)
                elif strat == "harvester":
                    r = run_harvester_window(rec, series, tc, hc)
                elif strat == "mm":
                    r = run_mm_window(rec, series, tc, mmc)
                else:
                    r = bt.run_window(rec, series)
                results.append(r)
            except Exception as exc:  # noqa: BLE001
                print(f"  window {rec.window_ts} failed: {exc}")
        print(f"[{args.asset}] {strat:10s} {summarize(results)}")
        all_out[strat] = [
            {k: getattr(r, k) for k in
             ("window_ts", "winner", "up_shares", "down_shares", "spent",
              "pnl", "fills", "hedge_cost", "taker_fees")}
            for r in results
        ]

    if args.json_out:
        os.makedirs(os.path.dirname(args.json_out) or ".", exist_ok=True)
        with open(args.json_out, "w") as fh:
            json.dump(all_out, fh)
        print(f"per-window results -> {args.json_out}")


if __name__ == "__main__":
    main()
