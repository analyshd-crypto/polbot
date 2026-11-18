"""Command-line interface.

    polbot status                     # live market snapshot (no auth)
    polbot run --asset btc            # paper mode against live books (default)
    polbot run --live --max-windows 1 # one real window (needs credentials)
    polbot backtest --asset btc --windows 200
    polbot cancel-all                 # kill switch: cancel all live orders
    polbot doctor                     # verify live credentials
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
import time
from typing import Optional

from . import __version__
from .backends import LiveBackend, PaperBackend
from .backtest import run_backtest, results_to_rows
from .clock import RealClock
from .config import BotConfig, WINDOW_SECONDS, SUPPORTED_ASSETS
from .data import fetch_windows, load_cached_windows
from .engine import Engine, LiveMarketData
from .markets import window_start, slug_for
from .net import HttpClient
from .pnl import RunStats
from .risk import RiskManager


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines); existing env vars win."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def _http(cfg: BotConfig) -> HttpClient:
    return HttpClient(
        timeout=cfg.request_timeout_s,
        retry_attempts=cfg.http_retry_attempts,
        retry_backoff_s=cfg.http_retry_backoff_s,
    )


# ------------------------------------------------------------------ status --
def cmd_status(args: argparse.Namespace) -> int:
    cfg = BotConfig.from_env()
    cfg.asset = args.asset
    http = _http(cfg)
    from .clob import fetch_book, fetch_fee_rate
    from .markets import fetch_market

    now = time.time()
    wts = window_start(now)
    print(f"polbot {__version__} | asset={cfg.asset} | now={int(now)} | window={wts} "
          f"(ends in {wts + WINDOW_SECONDS - int(now)}s)")
    market = fetch_market(http, cfg.gamma_url, cfg.asset, wts)
    if market is None:
        print(f"  no event found for slug {slug_for(cfg.asset, wts)} (yet)")
        return 1
    print(f"  market : {market.question}")
    print(f"  slug   : {market.slug}")
    print(f"  tokens : up={market.up_token[:16]}... down={market.down_token[:16]}...")
    print(f"  cond   : {market.condition_id}")
    print(f"  accepting orders: {market.accepting_orders}")
    for name, token in (("UP", market.up_token), ("DOWN", market.down_token)):
        book = fetch_book(http, cfg.clob_url, token)
        fee = fetch_fee_rate(http, cfg.clob_url, token)
        print(
            f"  {name:<4}: bid/ask {book.best_bid}/{book.best_ask} mid {book.mid} "
            f"tick {book.tick_size} min_size {book.min_order_size} "
            f"levels(b/a) {len(book.bids)}/{len(book.asks)} base_fee {fee}"
        )
    return 0


# --------------------------------------------------------------------- run --
def cmd_run(args: argparse.Namespace) -> int:
    cfg = BotConfig.from_env()
    cfg.asset = args.asset
    cfg.dry_run = not args.live
    for key, val in (
        ("size_per_level", args.size_per_level),
        ("refresh_interval_s", args.refresh),
        ("pull_before_end_s", args.pull),
        ("max_cost_per_market", args.max_cost),
    ):
        if val is not None:
            setattr(cfg, key, val)

    clock = RealClock()
    data = LiveMarketData(cfg, http=_http(cfg), clock=clock)

    if cfg.dry_run:
        backend = PaperBackend(fill_ratio=cfg.paper_fill_ratio)
        print(f"[polbot] DRY-RUN (paper) | {cfg.describe()}")
    else:
        pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        wallet = os.environ.get("POLYMARKET_WALLET") or None
        if not pk:
            print("live mode requires POLYMARKET_PRIVATE_KEY (see .env.example)")
            return 2
        backend = LiveBackend(cfg, pk, wallet, clock=clock)
        print(f"[polbot] LIVE TRADING | {cfg.describe()}")

    risk = RiskManager(cfg)
    stats = RunStats()
    engine = Engine(cfg, data, backend, clock, risk, stats)

    async def _run() -> None:
        try:
            await engine.run_forever(max_windows=args.max_windows)
        finally:
            if not cfg.dry_run:
                await backend.kill()
            await backend.close()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("\n[polbot] interrupted; cancelled open orders")
    print(f"[polbot] final: {stats.line()}")
    return 0


# ---------------------------------------------------------------- backtest --
def cmd_backtest(args: argparse.Namespace) -> int:
    cfg = BotConfig.from_env()
    cfg.asset = args.asset
    cfg.dry_run = True
    for key, val in (
        ("size_per_level", args.size_per_level),
        ("refresh_interval_s", args.refresh),
        ("pull_before_end_s", args.pull),
        ("max_cost_per_market", args.max_cost),
        ("paper_fill_ratio", args.fill_ratio),
    ):
        if val is not None:
            setattr(cfg, key, val)

    cache_dir = args.cache_dir or "data/cache"
    records: list = []
    if args.windows:
        end_ts = window_start(time.time()) - WINDOW_SECONDS  # last closed window
        print(f"[polbot] fetching {args.windows} resolved {cfg.asset} windows "
              f"(cache: {cache_dir}) ...")
        records = fetch_windows(cfg, end_ts, args.windows, cache_dir=cache_dir)
    else:
        records = load_cached_windows(cache_dir, cfg.asset)
    if not records:
        print("no usable window data (fetch first with --windows N)")
        return 1
    if not args.halt:
        # the daily-loss halt is a live-trading safety feature; disable it for
        # backtests by default so the whole dataset is simulated
        cfg.daily_loss_limit = float("inf")
    print(f"[polbot] running backtest on {len(records)} windows "
          f"({records[0].window_ts} .. {records[-1].window_ts})")

    report = asyncio.run(run_backtest(cfg, records))
    print()
    print(report.summary_text())
    print()

    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        rows = results_to_rows(report.results)
        if args.out.endswith(".json"):
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "asset": report.asset,
                        "windows": report.windows,
                        "config": report.cfg_summary,
                        "summary": {
                            "win_rate": report.stats.win_rate,
                            "total_pnl": report.stats.total_pnl,
                            "worst_pnl": report.stats.worst_pnl,
                            "max_drawdown": report.stats.max_drawdown,
                            "fills": report.stats.total_fills,
                        },
                        "results": rows,
                    },
                    fh,
                    indent=2,
                )
        else:
            with open(args.out, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
        print(f"[polbot] wrote {args.out}")
    return 0


# -------------------------------------------------------------- cancel-all --
def cmd_cancel_all(args: argparse.Namespace) -> int:
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if not pk:
        print("requires POLYMARKET_PRIVATE_KEY")
        return 2
    cfg = BotConfig.from_env()

    async def _kill() -> None:
        backend = LiveBackend(cfg, pk, os.environ.get("POLYMARKET_WALLET"))
        await backend.kill()
        await backend.close()

    asyncio.run(_kill())
    return 0


# ------------------------------------------------------------------ doctor --
def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = BotConfig.from_env()
    http = _http(cfg)
    from .clob import fetch_book
    from .markets import fetch_market

    print(f"polbot {__version__} doctor")
    market = fetch_market(http, cfg.gamma_url, cfg.asset, window_start(time.time()))
    print(f"  [ok] gamma API reachable: {'yes' if market else 'event not found'}")
    if market:
        book = fetch_book(http, cfg.clob_url, market.up_token)
        print(f"  [ok] clob API reachable: up mid={book.mid} tick={book.tick_size}")
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if not pk:
        print("  [--] POLYMARKET_PRIVATE_KEY not set (required for live mode only)")
        return 0

    async def _check() -> str:
        backend = LiveBackend(cfg, pk, os.environ.get("POLYMARKET_WALLET"))
        try:
            msg = await backend.check_connection()
        finally:
            await backend.close()
        return msg

    try:
        print(f"  [ok] live credentials: {asyncio.run(_check())}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] live credentials: {exc}")
        return 1
    return 0


# --------------------------------------------------------------------- main --
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="polbot",
        description="Market-making bot for Polymarket 5-minute crypto Up/Down markets",
    )
    p.add_argument("--version", action="version", version=f"polbot {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("status", help="live market snapshot (no auth)")
    ps.add_argument("--asset", choices=SUPPORTED_ASSETS, default="btc")
    ps.set_defaults(func=cmd_status)

    pr = sub.add_parser("run", help="run the bot (paper by default)")
    pr.add_argument("--asset", choices=SUPPORTED_ASSETS, default="btc")
    pr.add_argument("--live", action="store_true",
                    help="place REAL orders (requires credentials)")
    pr.add_argument("--max-windows", type=int, default=None)
    pr.add_argument("--size-per-level", type=float, default=None)
    pr.add_argument("--refresh", type=float, default=None,
                    help="cancel/repost cadence seconds")
    pr.add_argument("--pull", type=float, default=None,
                    help="pull orders N seconds before window end")
    pr.add_argument("--max-cost", type=float, default=None,
                    help="max spend per market (fills + resting)")
    pr.add_argument("--fill-ratio", type=float, default=None,
                    help="paper fill fraction per sweep (1.0 optimistic)")
    pr.set_defaults(func=cmd_run)

    pb = sub.add_parser("backtest", help="backtest on real resolved windows")
    pb.add_argument("--asset", choices=SUPPORTED_ASSETS, default="btc")
    pb.add_argument("--windows", type=int, default=None,
                    help="fetch N most recent resolved windows (cached)")
    pb.add_argument("--cache-dir", default="data/cache")
    pb.add_argument("--out", default=None,
                    help="write per-window results to .csv or .json")
    pb.add_argument("--size-per-level", type=float, default=None)
    pb.add_argument("--refresh", type=float, default=None)
    pb.add_argument("--pull", type=float, default=None)
    pb.add_argument("--max-cost", type=float, default=None)
    pb.add_argument("--fill-ratio", type=float, default=None,
                    help="paper fill fraction per sweep (1.0 optimistic, 0.5 conservative)")
    pb.add_argument("--halt", action="store_true",
                    help="keep the daily-loss halt active during the backtest")
    pb.set_defaults(func=cmd_backtest)

    pc = sub.add_parser("cancel-all", help="kill switch: cancel all live orders")
    pc.set_defaults(func=cmd_cancel_all)

    pd = sub.add_parser("doctor", help="check connectivity and credentials")
    pd.set_defaults(func=cmd_doctor)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    _load_dotenv()
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
