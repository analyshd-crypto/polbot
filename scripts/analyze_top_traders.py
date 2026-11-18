#!/usr/bin/env python3
"""Analyze Polymarket's top traders on 5-minute crypto markets.

Answers: do 90%+ win-rate traders exist, and what do they actually do?

Usage: python scripts/analyze_top_traders.py [--top 25] [--min-windows 20]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polbot.net import HttpClient  # noqa: E402
from polbot.toptraders import analyze_top_traders  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--min-windows", type=int, default=20)
    ap.add_argument("--json-out", default="results/top_traders.json")
    args = ap.parse_args()

    http = HttpClient(timeout=25.0)
    profiles = analyze_top_traders(http, top_n=args.top,
                                   min_windows=args.min_windows)

    print(f"\n=== top-{args.top} wallets by all-time PnL, their 5m crypto record ===")
    eligible = [p for p in profiles if p.windows >= args.min_windows]
    for p in sorted(profiles, key=lambda x: -x.pnl):
        print(p.row())
    print(f"\n--- win-rate ranking (only wallets with >= {args.min_windows} "
          f"analyzed windows) ---")
    for p in eligible:
        print(p.row())
    hi = [p for p in eligible if p.win_rate >= 0.90]
    print(f"\nwallets with win rate >= 90% on 5m markets: {len(hi)} of {len(eligible)}")
    for p in hi:
        print("  ", p.row())

    if args.json_out:
        os.makedirs(os.path.dirname(args.json_out) or ".", exist_ok=True)
        with open(args.json_out, "w") as fh:
            json.dump([{
                "user": p.user, "name": p.name, "windows": p.windows,
                "wins": p.wins, "losses": p.losses, "win_rate": p.win_rate,
                "pnl": p.pnl, "volume": p.volume, "fills": p.fills,
                "maker_share": p.maker_share,
                "avg_entry_tleft": p.avg_entry_tleft,
            } for p in profiles], fh, indent=2)
        print(f"\nsaved -> {args.json_out}")


if __name__ == "__main__":
    main()
