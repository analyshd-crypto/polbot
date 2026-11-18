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

    # v1 strategy layer: fv (fair-value anchored ladder) or legacy (v0 full ladder)
    fvc = None
    fv_feed = None
    if args.strategy in ("fv", "fv_hedge"):
        from .engine import FairValueFeed
        from .fvladder import FVLadderConfig

        fvc = FVLadderConfig(endgame_hedge=(args.strategy == "fv_hedge"))
        fv_feed = FairValueFeed(cfg, http=_http(cfg))

    if cfg.dry_run:
        backend = PaperBackend(fill_ratio=cfg.paper_fill_ratio)
        print(f"[polbot] DRY-RUN (paper) | strategy={args.strategy} | {cfg.describe()}")
    else:
        pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        wallet = os.environ.get("POLYMARKET_WALLET") or None
        if not pk:
            print("live mode requires POLYMARKET_PRIVATE_KEY (see .env.example)")
            return 2
        unlock = os.environ.get("POLBOT_LIVE_UNLOCK", "")
        if unlock != "I-UNDERSTAND-THE-RISK":
            print("live mode requires POLBOT_LIVE_UNLOCK=I-UNDERSTAND-THE-RISK in .env")
            print("paper mode is the default for a reason: see README 'Honest results'")
            return 2
        backend = LiveBackend(cfg, pk, wallet, clock=clock)
        print(f"[polbot] LIVE TRADING | strategy={args.strategy} | {cfg.describe()}")

    risk = RiskManager(cfg)
    stats = RunStats()
    engine = Engine(cfg, data, backend, clock, risk, stats, fvc=fvc, fv_feed=fv_feed)

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


# ----------------------------------------------------------- tick-backtest --
def cmd_tick_backtest(args: argparse.Namespace) -> int:
    """Delegates to scripts/run_tick_backtest.py logic (kept in sync)."""
    script = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                          "scripts", "run_tick_backtest.py")
    if not os.path.exists(script):  # installed package fallback
        print("tick backtest requires the repo checkout (scripts/ directory)")
        return 1
    import subprocess

    argv = [sys.executable, script, "--asset", args.asset,
            "--strategies", args.strategies,
            "--fee-rate", str(args.fee_rate),
            "--edge-min", str(args.edge_min),
            "--fv-min", str(args.fv_min)]
    if args.json_out:
        argv += ["--json-out", args.json_out]
    return subprocess.call(argv)


def cmd_top_traders(args: argparse.Namespace) -> int:
    script = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                          "scripts", "analyze_top_traders.py")
    if not os.path.exists(script):
        print("requires the repo checkout (scripts/ directory)")
        return 1
    import subprocess

    return subprocess.call([sys.executable, script, "--top", str(args.top)])


def cmd_tape_wallets(args: argparse.Namespace) -> int:
    script = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                          "scripts", "analyze_tape_wallets.py")
    if not os.path.exists(script):
        print("requires the repo checkout (scripts/ directory)")
        return 1
    import subprocess

    return subprocess.call([sys.executable, script, "--asset", args.asset,
                            "--min-windows", str(args.min_windows)])


# ---------------------------------------------------------------- mm engine --
def _mm_config(path: str):
    from .mm.config import Config
    cfg = Config.load(path or "mm-config.yaml")
    return cfg


def _mm_logging(cfg) -> None:
    os.makedirs(os.path.dirname(cfg.logging.file) or ".", exist_ok=True)
    fmt = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    logging.basicConfig(level=getattr(logging, cfg.logging.level.upper(), logging.INFO),
                        format=fmt, force=False,
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(cfg.logging.file)])


def cmd_mm_run(args: argparse.Namespace) -> int:
    """Run the v2 multi-market maker engine (paper by default)."""
    cfg = _mm_config(args.config)
    if args.bankroll:
        cfg.bankroll = args.bankroll
    if args.mode:
        cfg.mode = args.mode
    _mm_logging(cfg)
    from .mm.bot import PolBot
    if cfg.mode == "live":
        unlock = os.environ.get("POLBOT_LIVE_UNLOCK", "")
        if unlock != "I-UNDERSTAND-THE-RISK":
            print("live mode requires POLBOT_LIVE_UNLOCK=I-UNDERSTAND-THE-RISK")
            return 2
    bot = PolBot(cfg)
    bot.run()
    return 0


def cmd_mm_test(args: argparse.Namespace) -> int:
    """Connectivity test for the mm engine's data plane."""
    import time as _t
    from .mm.data.binance import SpotFeed, fetch_klines
    from .mm.data.clob import ClobPublic
    from .mm.data.gamma import GammaClient
    cfg = _mm_config(args.config)
    print("== polbot mm engine connection test ==")
    g = GammaClient(cfg.data.gamma_host)
    wins = g.fetch_windows(cfg.markets.series[0], limit=5)
    print(f"[OK] gamma: {len(wins)} {cfg.markets.series[0]} windows; "
          f"next: {wins[0].event_slug if wins else '-'}")
    c = ClobPublic(cfg.execution.clob_host)
    if wins:
        bb, ba = c.best_bid_ask(wins[0].tokens[0])
        print(f"[OK] clob book: best_bid={bb} best_ask={ba}")
    k = fetch_klines("BTCUSDT", "1s", limit=2)
    print(f"[OK] binance klines: last close={float(k[-1][4]):.2f}")
    feed = SpotFeed(["BTCUSDT"], use_websocket=cfg.data.use_websocket)
    feed.start()
    _t.sleep(4.0)
    px = feed.last("BTCUSDT")
    src = "ws" if feed._ws_ok else "rest-fallback"
    print(f"[OK] spot feed ({src}): BTCUSDT={px}")
    feed.stop()
    print("all good.")
    return 0


def cmd_mm_fetch(args: argparse.Namespace) -> int:
    from .mm.data.binance import fetch_klines
    cfg = _mm_config(args.config)
    outdir = args.out or cfg.data.history_cache_dir
    os.makedirs(outdir, exist_ok=True)
    symbols = args.symbols.split(",") if args.symbols else \
        list(cfg.data.binance_symbols.values())
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - int(args.days * 86400 * 1000)
    for sym in symbols:
        path = os.path.join(outdir, f"{sym}_{args.interval}_{args.days}d.csv")
        rows = fetch_klines(sym, args.interval, start_ms=start_ms, end_ms=now_ms)
        with open(path, "w") as f:
            f.write("ts,open,high,low,close,volume\n")
            for r in rows:
                f.write(f"{r[0]},{r[1]},{r[2]},{r[3]},{r[4]},{r[5]}\n")
        print(f"[OK] {sym}: {len(rows)} bars -> {path}")
    return 0


def _mm_load_bars(cfg, args):
    from .mm.backtest.engine import Bar
    outdir = args.cache or cfg.data.history_cache_dir
    bars = {}
    for sym in (args.symbols.split(",") if args.symbols
                else list(cfg.data.binance_symbols.values())):
        path = os.path.join(outdir, f"{sym}_{args.interval}_{args.days}d.csv")
        if not os.path.exists(path):
            raise SystemExit(f"missing cached history: {path} (run mm-fetch first)")
        out = []
        with open(path) as f:
            next(f)
            for line in f:
                ts, o, h, l, c, v = line.strip().split(",")
                out.append(Bar(float(ts) / 1000.0, float(o), float(h),
                               float(l), float(c)))
        bars[sym] = out
        print(f"[OK] loaded {len(out)} bars for {sym}")
    return bars


def cmd_mm_backtest(args: argparse.Namespace) -> int:
    from .mm.backtest.engine import BacktestEngine, BacktestParams
    from .mm.backtest import report
    cfg = _mm_config(args.config)
    if args.mode:
        cfg.quoting.mode = args.mode
    bars = _mm_load_bars(cfg, args)
    params = BacktestParams(
        lag_seconds=args.lag, trade_through_ticks=args.trade_through,
        queue_fill_fraction=args.queue_frac, noise_ticks=args.noise)
    eng = BacktestEngine(cfg, bars, params)
    t0 = time.time()
    res = eng.run()
    dt = time.time() - t0
    out = args.out or os.path.join("results", "mm",
                                   f"{cfg.quoting.mode}_{int(time.time())}")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    report.save(res, out, f"polbot mm backtest — {cfg.quoting.mode}",
                {"data": f"{args.days}d {args.interval} bars, "
                         f"{sum(len(v) for v in bars.values())} bars total",
                 "config": f"mode={cfg.quoting.mode} bankroll={cfg.bankroll} "
                           f"max_window={cfg.sizing.max_window_exposure}"})
    print(json.dumps(res.summary(), indent=1))
    print("bands:", json.dumps(res.band_table(), indent=1))
    print(f"ran in {dt:.1f}s -> {out}.md / .json")
    return 0


def cmd_mm_whales(args: argparse.Namespace) -> int:
    import urllib.request
    url = "https://data-api.polymarket.com/v2/leaderboard?window=30d&limit=20"
    req = urllib.request.Request(url, headers={"User-Agent": "polbot/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode())
    rows = data.get("leaderboard") or data.get("data") or data
    print("top traders by 30d PnL:")
    for row in rows[:20]:
        print(f"  #{row.get('rank')}: {row.get('user_name') or (row.get('user_id') or '')[:10]} "
              f"pnl=${row.get('pnl', 0):,.0f} volume=${row.get('volume', 0):,.0f} "
              f"roi={(row.get('pnl', 0) / max(row.get('volume', 1), 1)) * 100:.0f}%")
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
    pr.add_argument("--strategy", default="legacy",
                    choices=("legacy", "fv", "fv_hedge"),
                    help="legacy = v0 full ladder; fv = fair-value anchored "
                         "ladder; fv_hedge = fv + endgame flatten")
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

    ptb = sub.add_parser("tick-backtest",
                         help="tick-level backtest over real trade tapes "
                              "(see scripts/fetch_tick_data.py first)")
    ptb.add_argument("--asset", choices=SUPPORTED_ASSETS, default="btc")
    ptb.add_argument("--strategies", default="legacy,fv,fv_hedge,harvester,sniper")
    ptb.add_argument("--fee-rate", type=float, default=0.05,
                     help="crypto taker fee coefficient (0.05 for BTC 5m from "
                          "2026-11-20; 0.07 before)")
    ptb.add_argument("--edge-min", type=float, default=0.15)
    ptb.add_argument("--fv-min", type=float, default=0.965)
    ptb.add_argument("--json-out", default="")
    ptb.set_defaults(func=cmd_tick_backtest)

    ptt = sub.add_parser("top-traders",
                         help="analyze Polymarket leaderboard wallets on 5m markets")
    ptt.add_argument("--top", type=int, default=20)
    ptt.set_defaults(func=cmd_top_traders)

    ptw = sub.add_parser("tape-wallets",
                         help="mine cached trade tapes for wallet win rates "
                              "(finds the real >=90%% cohort)")
    ptw.add_argument("--asset", choices=SUPPORTED_ASSETS, default="btc")
    ptw.add_argument("--min-windows", type=int, default=25)
    ptw.set_defaults(func=cmd_tape_wallets)

    # ---- v2 mm engine ----
    pmr = sub.add_parser("mm-run",
                         help="run the v2 multi-market maker engine "
                              "(paper by default; live needs POLBOT_MODE=live + unlock)")
    pmr.add_argument("--config", default="mm-config.yaml")
    pmr.add_argument("--mode", choices=("paper", "live"), default=None)
    pmr.add_argument("--bankroll", type=float, default=None)
    pmr.set_defaults(func=cmd_mm_run)

    pmt = sub.add_parser("mm-test", help="mm engine connectivity test")
    pmt.add_argument("--config", default="mm-config.yaml")
    pmt.set_defaults(func=cmd_mm_test)

    pmf = sub.add_parser("mm-fetch", help="cache 1s Binance klines for mm backtests")
    pmf.add_argument("--config", default="mm-config.yaml")
    pmf.add_argument("--days", type=float, default=2.0)
    pmf.add_argument("--interval", default="1s")
    pmf.add_argument("--symbols", default=None)
    pmf.add_argument("--out", default=None)
    pmf.set_defaults(func=cmd_mm_fetch)

    pmb = sub.add_parser("mm-backtest",
                         help="backtest the mm engine on cached 1s bars")
    pmb.add_argument("--config", default="mm-config.yaml")
    pmb.add_argument("--days", type=float, default=2.0)
    pmb.add_argument("--interval", default="1s")
    pmb.add_argument("--symbols", default=None)
    pmb.add_argument("--cache", default=None)
    pmb.add_argument("--mode", default=None, choices=("edge_weighted", "ladder"))
    pmb.add_argument("--lag", type=float, default=3.0)
    pmb.add_argument("--trade-through", type=int, default=1)
    pmb.add_argument("--queue-frac", type=float, default=0.6)
    pmb.add_argument("--noise", type=float, default=0.0)
    pmb.add_argument("--out", default=None)
    pmb.set_defaults(func=cmd_mm_backtest)

    pmw = sub.add_parser("mm-whales", help="30d leaderboard snapshot (data-api v2)")
    pmw.set_defaults(func=cmd_mm_whales)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    _load_dotenv()
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
