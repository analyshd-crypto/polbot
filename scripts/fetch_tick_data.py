#!/usr/bin/env python3
"""Fetch tick-level backtest data: trade tapes + Binance 1s klines.

Usage:
    python scripts/fetch_tick_data.py --asset btc --windows 300 [--end now]
Caches to data/trades/<asset>/ and data/binance/<asset>/.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polbot.binance import fetch_klines  # noqa: E402
from polbot.config import BotConfig, WINDOW_SECONDS  # noqa: E402
from polbot.net import HttpClient  # noqa: E402
from polbot.tradesdata import fetch_trades_windows, load_cached_trades  # noqa: E402

DATA_API = "https://data-api.polymarket.com"
BINANCE = "https://api.binance.com"
SYMBOLS = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}


def fetch_kline_range(http, symbol, start_ms, end_ms, cache_path):
    """Fetch 1s klines covering [start_ms, end_ms] in 1000-candle chunks.

    Resumable: progress is checkpointed to ``<cache_path>.part`` after every
    chunk; rate-limit bans (HTTP 418/429) are waited out.
    """
    if os.path.exists(cache_path):
        with open(cache_path) as fh:
            return json.load(fh)
    part_path = cache_path + ".part"
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    rows = []
    cur = start_ms
    if os.path.exists(part_path):
        with open(part_path) as fh:
            rows = json.load(fh)
        if rows:
            cur = rows[-1][0] + 1000
    while cur < end_ms:
        for attempt in range(6):
            try:
                chunk = fetch_klines(http, BINANCE, symbol, interval="1s",
                                     start_ms=cur, end_ms=end_ms, limit=1000)
                break
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                wait = 30.0
                if "179" in msg or "banned" in msg.lower():
                    import re
                    m = re.search(r"until (\d{10,13})", msg)
                    if m:
                        until = int(m.group(1))
                        if until > 10**12:
                            until //= 1000
                        wait = max(5.0, until - time.time() + 2)
                print(f"  klines blocked ({msg[:80]}); waiting {wait:.0f}s "
                      f"(attempt {attempt+1})")
                time.sleep(wait)
        else:
            print("  giving up on klines after retries")
            break
        if not chunk:
            break
        rows.extend([[k.open_ts, k.open, k.high, k.low, k.close, k.close_ts]
                     for k in chunk])
        cur = chunk[-1].open_ts + 1000
        with open(part_path + ".tmp", "w") as fh:
            json.dump(rows, fh)
        os.replace(part_path + ".tmp", part_path)
        time.sleep(1.2)  # gentle pacing (shared egress IP)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w") as fh:
        json.dump(rows, fh)
    if os.path.exists(part_path):
        os.remove(part_path)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="btc")
    ap.add_argument("--windows", type=int, default=300)
    ap.add_argument("--end", type=int, default=0, help="end window ts (default: now)")
    ap.add_argument("--cache", default="data")
    args = ap.parse_args()

    cfg = BotConfig(asset=args.asset)
    http = HttpClient(timeout=cfg.request_timeout_s)
    end_ts = args.end or ((int(time.time()) // WINDOW_SECONDS) * WINDOW_SECONDS)

    print(f"fetching {args.windows} trade tapes for {args.asset} "
          f"(ending window {end_ts})...")
    recs = fetch_trades_windows(
        http, cfg.gamma_url, DATA_API, args.asset, end_ts, args.windows,
        cache_dir=args.cache, max_workers=6, max_pages=6,
    )
    print(f"got {len(recs)} usable trade tapes")

    if not recs:
        return
    lo = min(r.window_ts for r in recs) - 1800
    hi = max(r.end_ts for r in recs) + 60
    print(f"fetching 1s klines {lo}..{hi} ({(hi-lo)//60} minutes)...")
    path = os.path.join(args.cache, "binance", args.asset, f"{lo}_{hi}.json")
    rows = fetch_kline_range(http, SYMBOLS[args.asset], lo * 1000, hi * 1000, path)
    print(f"klines cached: {len(rows)} candles -> {path}")

    total_trades = sum(len(r.trades) for r in recs)
    print(f"done. {len(recs)} windows, {total_trades} raw trades total.")


if __name__ == "__main__":
    main()
