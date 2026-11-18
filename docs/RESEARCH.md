# Research notes: Polymarket 5-minute crypto Up/Down markets

Everything in this document was verified against live APIs or primary
documentation during development (November 2026). Sources listed at the end.

## 1. Market mechanics

- **Assets**: BTC, ETH, SOL, XRP (5-minute cadence); Polymarket also runs
  15-minute crypto markets and 5m/15m gold/silver, and a separate
  CFTC-regulated US app with its own 5-minute markets.
- **Windows** align to unix timestamps divisible by 300.
- **Deterministic slugs**: `{asset}-updown-5m-{window_start_ts}`, e.g.
  `btc-updown-5m-1795002600`. Events are created ~a day in advance and
  `acceptingOrders` is already true; the question is whether the asset
  closes higher or lower than the window's opening print.
- **Two outcome tokens** per market ("Up" / "Down"), each with an
  independent CLOB book. `clobTokenIds` is parallel to `outcomes`.
  Complementarity: buying Down at `p` is synthetically selling Up at `1-p`.
- **Resolution**: Chainlink Data Streams (official oracle for the high-speed
  up/down markets). Live oracle prices stream at
  `wss://ws-live-data.polymarket.com` (topic `crypto_prices_chainlink`);
  the window's "price to beat" is the first oracle print at/after the
  boundary. After close, Gamma reports `outcomePrices` of `["1","0"]` /
  `["0","1"]` (observed ~1–3 minutes after window end).
- **Books are deep and violent**: mid-window snapshots show thousands of
  shares stacked at 1–14¢ and 85–99¢; median within-window price range in
  our 300-window BTC sample was **0.455** (i.e. routinely 5¢ → 50¢ moves).

## 2. The demo video, frame by frame

The uploaded 24-second screen recording shows a terminal-UI ladder bot
("xmayeth-ladder v0.3.2"). Verified details that shaped this repo:

- One market at a time: header with market id, strike ("price to beat"),
  live spot and delta; a time-progress bar; status word
  (`quoting` → `pulled` → `resolved DOWN`).
- Two ladders (UP/DOWN) with bid/ask, held shares, depth bars with
  1/50/51/99¢ level labels — i.e. the full 1¢–99¢ range.
- ~98 resting orders open at once (Q counter) = every cent below the mid on
  **both** books (e.g. Up mid 45¢ → Up bids 1–44¢ + Down bids 1–54¢ ≈ 98).
- A `reposts` counter climbing ~1/second — the cancel/repost heartbeat.
- Fill bursts exactly as the text describes: "UP 26¢×3, 27¢×3 …" while spot
  falls, later "UP 5¢×6 … 10¢×6" deeper; then DOWN hedge fills at 95¢–96¢
  when the direction locked in.
- Positions panel with shares/avg/cost and live **if-UP / if-DOWN PnL**;
  status bar with cumulative PNL, W (wins), FILLS, OPEN, Q.
- Round 1 of the video lost **-10.65** (held 45 UP through a Down
  resolution after paying up for 94 DOWN at 95.5¢); round 2 accumulated
  246 UP at 25¢ avg into a recovery — the two faces of the strategy.

## 3. API surface used (all validated live)

| purpose | endpoint |
|---|---|
| event/market metadata | `GET gamma-api.polymarket.com/events?slug=…` |
| order book (tick, min size, last trade) | `GET clob.polymarket.com/book?token_id=…` |
| price history (1-minute buckets, min 60s) | `GET clob.polymarket.com/prices-history?market=…&startTs=…&endTs=…&fidelity=1` |
| fee rate | `GET clob.polymarket.com/fee-rate?token_id=…` → `{"base_fee": 1000}` |
| place order (SDK) | `AsyncSecureClient.place_limit_order(asset_id, price, size, side, post_only, expiration)` |
| cancel per market (SDK) | `cancel_market_orders(market=condition_id)` |
| fills (SDK) | `list_account_trades(market=condition_id)` (dedupe by trade id) |

The archived `py-clob-client` (May 2026) is dead; the official client is
**`polymarket-client`** (github.com/Polymarket/py-sdk, v0.20.0, Nov 2026).
GTD orders expire 60s before their stated expiration and must be ≥ 3 minutes
out; post-only guarantees maker status.

## 4. Fees and rebates (crypto category)

- Taker fee rate **0.07**, maker fee **0**, maker rebate **20%** of taker
  fees (daily pools; announced move to volume tiers on 2026-11-20).
- Taker formula: `fee = shares × feeRate × p × (1-p)` — symmetric around
  50¢; $0.07 per share at 1¢/99¢, $1.75 per 100 shares at 50¢.
- Makers are never charged → this bot (post-only only) pays no fees.

## 5. Tick size is dynamic

Mid-window books report `tick_size: 0.01`; near price extremes (< ~4¢ /
> ~96¢) it flips to **0.001**. A naive 1¢–99¢ ladder at 0.001 tick would
try to post ~980 levels per side — polbot caps ladder density with
`max_levels_per_side` (default 99) and re-reads tick from the book each
refresh.

## 6. Rate limits (per signer, enforced since Aug 2026)

Standard tier: **40 order tokens/s (burst 60), 80 cancel tokens/s
(burst 120)**; `POST /orders` costs N tokens for N orders;
`DELETE /cancel-market-orders` costs 1 + orders canceled. Higher tiers
unlock by 30-day volume ($30k → Copper 60/90 … $10M+ → Elite 600/900).
polbot mirrors the Standard bucket client-side and chunks ladders by 40.

## 7. Backtest methodology

- **Data**: for each resolved window, the real 1-minute price history of
  both tokens (CLOB `prices-history`, fidelity 1) plus the real outcome
  (Gamma `outcomePrices`). Cached as JSON under `data/cache/`.
- **Replay**: the unchanged engine runs against synthetic books whose best
  ask equals the observed market price; resting bids fill when the observed
  price falls to/below them; the pull rule, budget caps and inventory caps
  apply; settlement uses the real outcome.
- **Fill ratio**: `--fill-ratio 0.5` fills half of each level per sweep to
  approximate queue position (1.0 = front of queue).

### Results (defaults: 5 sh/level, 1¢–99¢, $250 budget, pull T-60s, ratio 1.0)

| asset | windows | win rate | total PnL | avg/window | worst | max DD | fills |
|---|---|---|---|---|---|---|---|
| BTC | 300 | 20.5% | -6,064.15 | -20.21 | -141.30 | 6,089.60 | 11,464 |
| ETH | 149 | 24.2% | -2,219.85 | -14.90 | -160.25 | 2,517.10 | 5,566 |
| SOL | 150 | 22.1% | -2,719.95 | -18.13 | -132.95 | 2,842.65 | 5,975 |

BTC variants: ≥50¢ only → -1,571.50 (50.3% WR); ≥30¢ → -4,016.60;
pull T-120s → -4,171.25; ≥50¢ + T-120s → **-655.00** (47.8% WR);
fill-ratio 0.5 → -3,036.35.

The first 13 BTC windows under the default **daily-loss halt** lost -299.15
and tripped the halt — i.e. with live risk limits, the default configuration
would have stopped itself within ~1 hour on that dataset.

### Limitations (why live results can differ, in both directions)

1. 1-minute history cannot see intra-minute wicks — the strategy's claimed
   profit engine (panic sweep + recovery within a minute) is invisible; the
   backtest fully counts trend losses but undercounts wick profits.
   Sub-minute history is not served (bucket minimum 60s — verified).
2. Queue position and partial depth are approximated by `fill_ratio`.
3. Maker rebates are not credited.
4. Latency: live quoting reacts every 5s (as the original bot does);
   competitors may be faster.
5. Sample: ~25h of BTC, ~12h each of ETH/SOL (Nov 17–18, 2026).

## 8. Live dry-run evidence (paper fills, 5s polling — sees intra-minute moves)

| window | asset | fills | committed | paper PnL |
|---|---|---|---|---|
| #1795005000 | BTC | 98 | $249.90 | +210.10 |
| #1795006800 | ETH | 103 | $249.95 | +135.05 |

Both windows were violent whipsaws (the favorable regime); n=2 is anecdote,
not statistics.

## Sources

- Polymarket docs — prices & orderbook, place/manage orders, fees, market
  making, CLOB trading rate limits, Python quickstart:
  https://docs.polymarket.com/ (see `/llms.txt` index)
- Official Python SDK: https://github.com/Polymarket/py-sdk
  (archived predecessor: https://github.com/Polymarket/py-clob-client)
- Chainlink × Polymarket 5/15-minute markets (oracle & resolution):
  https://www.prnewswire.com/news-releases/polymarket-deepens-partnership-with-chainlink-as-official-oracle-for-high-speed-updown-markets-and-launches-gold-and-silver-trading-302924082.html
- Public build log of a directional BTC 5m bot (slug pattern, Binance
  cross-checks, Polymarket minimums):
  https://gist.github.com/Archetapp/7680adabc48f812a561ca79d73cbac69
- Live API probes performed by this project: `scripts/probe_gamma.py`,
  `probe_history.py`, `probe_fine_history.py` (in the dev environment).
