# polbot

**A market-making bot for Polymarket's 5-minute crypto Up/Down markets (BTC / ETH / SOL / XRP) — a faithful, tested, open-source implementation of the "ladder" bot described in the viral X post and demo video.**

> Every 5 minutes a new market opens: *will BTC be up or down at the close?*
> The bot rests BUY limit orders on **both** outcome books at every cent
> below the current price, cancels and reposts every ~5 seconds, pulls
> everything about a minute before the close, and holds whatever it bought
> to resolution. It never sells.

- Paper mode by default (live data, simulated fills, zero risk)
- Real-order mode through the **official `polymarket-client` SDK** with post-only orders, GTD expiries, client-side rate limiting and a kill switch
- Event-driven **backtester over real resolved windows** fetched from Polymarket's own APIs
- 29 unit/integration tests, CI on Python 3.10–3.12

---

## Read this first: our own test results (honest)

We built this bot, tested it, and measured it. **The mechanics work exactly as described — but the profitability claim does not reproduce on historical data.** Numbers below are real, reproducible with the commands shown.

### Live dry-run (paper fills against live books, 5-second polling)

| window | asset | fills | capital committed | paper PnL |
|---|---|---|---|---|
| #1795005000 | BTC | 98 | $249.90 / $250 | **+210.10** |
| #1795006800 | ETH | 103 | $249.95 / $250 | **+135.05** |

Both windows happened to be violent two-way whipsaws — the strategy's best
case. They prove the machinery (quoting, fills, pull, settlement) works, not
that it is profitable on average.

### Backtest over real resolved windows (1-minute price history, real outcomes)

Default parameters (full 1¢–99¢ ladder, 5 shares/level, $250 budget, pull at T-60s, optimistic 100% fill per sweep):

| asset | windows | win rate | total PnL | avg/window | worst window | max DD |
|---|---|---|---|---|---|---|
| BTC | 300 | 20.5% | **-6,064** | -20.2 | -141.3 | 6,089 |
| ETH | 149 | 24.2% | **-2,220** | -14.9 | -160.3 | 2,517 |
| SOL | 150 | 22.1% | **-2,720** | -18.1 | -133.0 | 2,843 |

Halving simulated fills (queue-position discount) roughly halves the losses — it does not flip the sign.

### Parameter variants (BTC, 300 windows)

| variant | win rate | total PnL |
|---|---|---|
| baseline (1¢–99¢ ladder) | 20.5% | -6,064 |
| only expensive levels (≥50¢) | 50.3% | -1,572 |
| ≥30¢ | 26.2% | -4,017 |
| pull at T-120s | 24.9% | -4,171 |
| ≥50¢ + pull at T-120s | 47.8% | **-655** |

### Interpretation — why the viral claims and this backtest disagree

1. **Adverse selection is real and big.** These markets trend hard within
   their 5-minute life. When price sweeps down through your ladder you are
   systematically buying the losing side. 237 of 300 BTC windows lost.
2. **The claimed profit engine is invisible to historical data.** Polymarket
   only serves price history at 1-minute granularity (we probed: bucket
   minimum is 60s). The video's profits come from intra-minute wicks —
   panic sweeps that bounce within seconds. A 1-minute backtest sees the
   endpoint of each minute, missing both the fill and the recovery.
   Only a live paper run (or WebSocket recording) can measure that.
3. **Queue position is not modelled.** Your resting bid sits behind other
   makers at the same price; real fill rates are lower than simulated.
4. **Maker rebates are not modelled** (20% of taker fees on crypto markets,
   paid daily to makers — note: Polymarket changes rebates to volume tiers
   on Nov 20, 2026). They help, but not by enough to flip the numbers above.
5. **Selection bias.** The "$810/day" screenshot is one account over an
   unknown period; losing market makers rarely go viral.

**Bottom line:** treat this as a well-engineered research tool, not a money
printer. If you run it live, run it small, watch it constantly, and expect
the risk limits to trigger.

---

## The strategy, precisely

For each 5-minute window (windows align to unix timestamps divisible by 300):

1. **Discover** the market deterministically: slug `{asset}-updown-5m-{window_ts}` via the Gamma API; each market has two outcome tokens (Up / Down) with independent order books.
2. **Ladder**: place BUY orders at every tick from 1¢ up to just below the current best ask, on **both** books. Two safety invariants:
   - never bid at/above the ask (would cross and pay taker fees);
   - never let `up_bid + down_bid > 1 - tick` — your own bids can otherwise match *each other* and mint $1 of collateral for more than $1 (instant loss).
3. **Refresh**: cancel all market orders and repost the ladder every `POLBOT_REFRESH_S` seconds (default 5) so it tracks the mid.
4. **Pull**: cancel everything `POLBOT_PULL_S` seconds (default 60) before the window closes.
5. **Hold to resolution**: winners pay $1/share (Chainlink Data Streams resolution); losers pay $0. The bot never sells.

Money is supposed to come from (a) buying the likely winner at 90¢+ when
panickers sweep it down (steady 1.5–5% discounts) and (b) buying the
temporarily-crashed side for pennies when it recovers (20–30x payoffs).
Losses come from trending windows where the crashed side never recovers.

### Fees: makers pay zero

Crypto markets on Polymarket charge **takers only**:
`fee = shares × 0.07 × p × (1 − p)` (e.g. $0.07 per share at 1¢ or 99¢,
$1.75 per 100 shares at 50¢). **Makers are never charged** and receive 20%
rebates of taker fees. Since this bot only posts resting post-only BUYs, it
always trades as maker. (Fee schedule changes were announced for Nov 20,
2026 — re-check with `polbot status`.)

---

## Install

```bash
git clone https://github.com/analyshd-crypto/polbot.git
cd polbot
pip install -e ".[dev]"      # or: pip install -e . && pip install pytest
pytest -m "not live" -q      # 29 tests should pass
```

Python 3.10+. The read/paper/backtest paths use only the standard library;
`polymarket-client` is required only for live orders.

## Usage

```bash
# Live market snapshot: books, tick, fees (no auth)
polbot status --asset btc

# Paper trading against live books (default, no credentials needed)
polbot run --asset btc                    # runs forever, Ctrl-C to stop
polbot run --asset btc --max-windows 1    # one window

# Backtest on real resolved windows (cached under data/cache)
polbot backtest --asset btc --windows 300 --out results/btc.csv
polbot backtest --asset eth              # replay whatever is cached
polbot backtest --asset btc --fill-ratio 0.5        # conservative fills
POLBOT_MIN_PRICE=0.50 polbot backtest --asset btc   # expensive-only variant

# Live trading (requires .env — see examples/live_setup.md)
polbot doctor                             # verify credentials
polbot run --live --asset btc --max-windows 1
polbot cancel-all                         # KILL SWITCH: cancel all orders
```

Key runtime knobs (env or CLI, see `.env.example`):

| env | default | meaning |
|---|---|---|
| `POLBOT_ASSET` | btc | btc / eth / sol / xrp |
| `POLBOT_SIZE_PER_LEVEL` | 5 | shares per ladder level (CLOB min 5) |
| `POLBOT_MIN_PRICE` / `POLBOT_MAX_PRICE` | 0.01 / 0.99 | ladder price range |
| `POLBOT_REFRESH_S` | 5 | cancel & repost cadence |
| `POLBOT_PULL_S` | 60 | pull orders N s before close |
| `POLBOT_MAX_COST` | 250 | max spend per market (fills + resting) |
| `POLBOT_MAX_SHARES_SIDE` | 500 | per-side inventory cap |
| `POLBOT_DAILY_LOSS` | 250 | halt after realized loss |
| `POLBOT_PAPER_FILL_RATIO` | 1.0 | paper fill fraction per sweep (queue modelling) |
| `POLBOT_DRY_RUN` | 1 | 1 = paper (default), 0 = live |
| `POLYMARKET_PRIVATE_KEY` | — | live orders only |
| `POLYMARKET_WALLET` | — | funder/proxy wallet (if applicable) |

## Architecture

```
polbot/
├── config.py     # BotConfig dataclass + env loading + validation
├── clock.py      # real / simulated clocks (backtests run in milliseconds)
├── net.py        # stdlib HTTP with retries (no auth needed for reads)
├── markets.py    # window math, deterministic slugs, event parsing, outcomes
├── clob.py       # order books, price history, fee rates (public CLOB REST)
├── grid.py       # ladder builder: cross-spread + self-cross guards, budget
├── pnl.py        # fills -> positions -> settlement; run statistics
├── risk.py       # budget/inventory caps, daily-loss halt, error backoff
├── ratelimit.py  # client-side token buckets mirroring CLOB per-signer limits
├── backends.py   # PaperBackend (sim fills) / LiveBackend (official SDK)
├── engine.py     # per-window state machine: quote -> refresh -> pull -> settle
├── data.py       # fetch + cache real resolved windows for backtests
├── backtest.py   # event-driven replay through the unchanged engine
└── cli.py        # status / run / backtest / cancel-all / doctor
```

The engine is identical in live, paper and backtest modes — only the
execution backend and clock differ. Backtests therefore exercise the real
strategy code, not a reimplementation.

### Rate-limit safety

Polymarket enforces per-signer token buckets (Standard tier: 40 order
tokens/s, burst 60; 80 cancel tokens/s, burst 120; a batch of N orders
costs N tokens; cancel-market costs 1 + orders canceled). polbot mirrors
those buckets client-side, splits ladders into chunks of 40, and paces
itself — a full ~99-order refresh every 5s stays within Standard tier.

## Risks — read before going live

- **This strategy lost money in every backtest we ran** (see tables above).
  The viral profit claims could not be reproduced from historical data.
- A single window can consume the entire `POLBOT_MAX_COST` budget.
- 5-minute markets are extremely competitive; other market makers with
  lower latency and better queue position are on the same books.
- Polymarket fee/rebate schedules change (next change announced for
  2026-11-20); tick size flips from 0.01 to 0.001 near price extremes.
- Geo-restrictions: you must be allowed to use Polymarket where you live.
- Software risk: order rejection, API changes, network failures. The bot
  backs off and halts after `POLBOT_DAILY_LOSS` or repeated errors, and
  GTD expiries ensure a crashed bot leaves no resting orders.
- **Nothing here is financial advice. Use at your own risk.**

## Roadmap ideas

- WebSocket user-channel fills (instead of trade polling)
- Delta-repost (only cancel/replace changed levels) to cut rate-limit usage
- Inventory skew (widen the side you're heavy on)
- Maker-rebate accounting in PnL
- Multi-window/multi-asset concurrency (currently one asset, one window)

## Research notes

See [docs/RESEARCH.md](docs/RESEARCH.md) for the full intelligence report:
how the markets resolve (Chainlink Data Streams), API mechanics, fee
schedule, tick-size dynamics, the demo-video teardown, and the complete
backtest methodology with all results.

## License

MIT — see [LICENSE](LICENSE).
