#!/usr/bin/env python3
"""Mine the cached 5-minute trade tapes for wallet-level performance.

Population: every wallet that traded in our ~300 cached windows (~400k
fills). For each wallet we compute per-window PnL using the known settlement,
then rank by win rate among wallets with enough activity to be meaningful.

This answers the real question: do 90%+ win-rate traders exist on these
markets, and what do they actually do?

Usage: python scripts/analyze_tape_wallets.py [--min-windows 25]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polbot.tradesdata import load_cached_trades  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="btc")
    ap.add_argument("--cache", default="data")
    ap.add_argument("--min-windows", type=int, default=25)
    ap.add_argument("--min-volume", type=float, default=100.0)
    ap.add_argument("--json-out", default="results/tape_wallets.json")
    args = ap.parse_args()

    recs = load_cached_trades(args.cache, args.asset)
    print(f"windows: {len(recs)}")

    # wallet -> window -> {up/down: {bought, sold, cash_out}}
    W = defaultdict(lambda: defaultdict(lambda: {
        "up_b": 0.0, "dn_b": 0.0, "up_s": 0.0, "dn_s": 0.0,
        "cash_in": 0.0, "cash_out": 0.0, "fills": 0}))

    for rec in recs:
        winner = rec.winner
        if not winner:
            continue
        for t in rec.trades:
            try:
                ts = int(t["timestamp"])
                if not (rec.window_ts <= ts < rec.end_ts):
                    continue
                w = t["proxy_wallet"]
                price = float(t["price"])
                size = float(t["size"])
                side = str(t.get("side", "")).upper()
                outcome = str(t.get("outcome", "")).lower()
            except (KeyError, TypeError, ValueError):
                continue
            if outcome not in ("up", "down"):
                continue
            s = W[w][rec.window_ts]
            s["fills"] += 1
            if side == "BUY":
                s["cash_in"] += price * size
                if outcome == "up":
                    s["up_b"] += size
                else:
                    s["dn_b"] += size
            else:  # SELL
                s["cash_out"] += price * size
                if outcome == "up":
                    s["up_s"] += size
                else:
                    s["dn_s"] += size

    stats = []
    for w, windows in W.items():
        vol = 0.0
        pnl = 0.0
        wins = losses = 0
        fills = 0
        for wts, s in windows.items():
            winner = next(r.winner for r in recs if r.window_ts == wts)
            net_up = s["up_b"] - s["up_s"]
            net_dn = s["dn_b"] - s["dn_s"]
            payout = net_up if winner == "up" else net_dn
            p = payout + s["cash_out"] - s["cash_in"]
            pnl += p
            vol += s["cash_in"] + s["cash_out"]
            fills += s["fills"]
            if p > 0:
                wins += 1
            elif p < 0:
                losses += 1
        n = wins + losses
        if n <= 0:
            continue
        stats.append({
            "wallet": w, "windows": n, "wins": wins, "losses": losses,
            "win_rate": wins / n, "pnl": pnl, "volume": vol, "fills": fills,
        })

    print(f"wallets seen: {len(stats)}")
    active = [s for s in stats if s["windows"] >= args.min_windows
              and s["volume"] >= args.min_volume]
    active.sort(key=lambda s: -s["pnl"])
    print(f"wallets with >= {args.min_windows} decided windows and "
          f">= ${args.min_volume:.0f} volume: {len(active)}")

    def row(s):
        return (f"{s['wallet'][:12]}… win={s['win_rate']:6.1%} "
                f"n={s['windows']:4d} pnl={s['pnl']:+10.2f} "
                f"vol={s['volume']:9.0f} fills={s['fills']:5d}")

    print("\n--- TOP 15 BY PNL (the actual sharks on these markets) ---")
    for s in active[:15]:
        print(row(s))
    print("\n--- TOP 15 BY WIN RATE ---")
    for s in sorted(active, key=lambda s: (-s["win_rate"], -s["windows"]))[:15]:
        print(row(s))

    hi = [s for s in active if s["win_rate"] >= 0.90]
    print(f"\n=== wallets with win rate >= 90%: {len(hi)} of {len(active)} ===")
    for s in sorted(hi, key=lambda s: -s["pnl"])[:25]:
        print(row(s))
    profitable = [s for s in active if s["pnl"] > 0]
    print(f"\nsummary: {len(profitable)}/{len(active)} active wallets profitable "
          f"({len(profitable)/max(1,len(active)):.0%}); "
          f"total population pnl {sum(s['pnl'] for s in stats):+,.0f}")

    if args.json_out:
        os.makedirs(os.path.dirname(args.json_out) or ".", exist_ok=True)
        with open(args.json_out, "w") as fh:
            json.dump(stats, fh)
        print(f"saved -> {args.json_out}")


if __name__ == "__main__":
    main()
