# polbot intelligence file

Everything gathered while building this bot (Nov 18, 2026), with sources.
This is the "accumulate every intelligence" document: platform mechanics, fees,
the academic literature, wallet studies, API quirks, and the strategy
conclusions the backtester confirmed.

---

## 1. Market mechanics (verified live)

- 5-minute Up/Down markets for BTC, ETH, SOL, XRP; new window every 300s,
  aligned to unix timestamps divisible by 300. Also 15m crypto, 5m/15m metals.
- Deterministic slugs: `{asset}-updown-5m-{window_start_ts}`. Events are
  created ~a day ahead; `acceptingOrders` is true early.
- Settlement: **Chainlink Data Streams** (BTC/USD), NOT Binance, NOT UMA. The
  "price to beat" is the oracle print at window open; the winner is decided by
  the oracle print at close. Live stream topic: `crypto_prices_chainlink`
  (also exposed via the official SDK's realtime subscriptions).
- Order book: hybrid CLOB (off-chain matching, on-chain CTF settlement on
  Polygon). GTC/GTD = maker orders, **5-share minimum**; FOK/FAK = taker,
  $1 notional minimum. Orders are EIP-712 signed.
- April 28, 2026: **CLOB V2 migration** — new exchange contracts, pUSD
  collateral, signing domain v2. The old `py-clob-client` (and the interim
  `py-clob-client-v2`) are deprecated; the official client is the unified
  **`polymarket-client`** SDK (`PublicClient` / `SecureClient`).
- Data API v1 shut down Nov 2, 2026 → **v2** (`data-api.polymarket.com/v2/...`).
- Winners must **redeem** shares to convert to collateral (the SDK has
  `redeem_positions` / collateral-return planning).
- Integer-division truncation on multi-fill settlements: you can receive a
  fraction less than requested; don't try to sell the exact requested amount.

## 2. Fees and rebates (the regime change)

- Taker fee formula: `fee = shares × feeRate × price × (1−price)`.
  Crypto feeRate **0.07** through Nov 20, 2026 23:59 UTC; then:
  - **BTC 5m/15m up/down: 0.05**
  - all other crypto: 0.01
  - politics (non-election) / economics / culture / weather / mentions: 0.01
  - sports / finance / tech / elections / combos / other: 0.05; geopolitics: 0
- **Makers always pay 0.** Fee is deducted in shares on buys, in USDC on sells.
- Maker rebates: from **Nov 20, 2026**, daily pools are replaced by 30-day
  maker-volume tiers: <$200K → **0%**, $200K–$1M → 3%, $1M–$4M → 7%,
  $4M–$10M → 10%, ≥$10M → 15%. Weighted volume = `shares × p × (1−p)`.
- Taker rebates: 0% under $200K 30-day taker volume; up to 60% at $75M+.
- Consequence: small passive quoters earn zero rebates from Nov 20 — the
  maker-spread game now requires scale or genuine edge.

## 3. The wallet studies (who actually makes money)

**Telonex, Feb 2026** (46,945 wallets, one week of 15m crypto fills, $153M
volume, on-chain):
- Only **36.8%** of wallets finished the week in profit; median PnL ≈ −$3.
- Top wallet +$270K/week; biggest loser **−$140K — a majority-MAKER wallet**
  (adverse selection in the flesh).
- **Makers netted +$728K despite winning only 47% of fills**; takers won 53% of
  fills and lost money overall (spread + fees).
- Volume concentrates at the **price extremes (near $0/$1)** and in the
  **final minutes** — "conviction" trades late, price discovery early.
- Two profitable archetypes: **snipers** (few markets, huge bets, external
  price signals — Binance lead-lag) and **100%-maker bots** (2,000+ markets,
  spread harvesting at scale).
- BTC is the liquidity hub: 19 of the top 20 wallets are BTC-primary.

**Our own tape mining** (this repo, `scripts/analyze_tape_wallets.py`,
299 BTC windows, Nov 17–18 2026):
- 1,272 wallets active; **52% profitable** among those with ≥25 windows
  (survivor-flavored sample), total population PnL **−$58K ≈ fee leakage**.
- **168 wallets (13%) have ≥90% window win rate**; dozens at 100%.
- Cohort fingerprint: **late-window favorite harvesting** — buy 95–99¢
  (avg 93.8¢) with ~1–2 minutes left, one side per window, never sell, hold to
  settlement. Median entry T−110s; 21% of entries in the last 60s. Avg order
  $50–$1,200. Best economics: ~+2% per window on deployed capital
  (e.g. 92.1% win rate, +$1,705 / $93K volume / 140 windows).
- The **all-time PnL leaderboard does not play this game**: 19 of the top 20
  wallets have zero 5m-market activity in our tape; the one exception is
  **−$5,602** on 5m markets while being hugely profitable overall elsewhere.

## 4. The academic literature

**"Settlement Manipulation in Prediction Markets" (arXiv 2606.31675, Jun 2026)**:
- The widely-quoted "$8.2M made on Polymarket's 5-minute Bitcoin markets" was
  earned by **821 suspected manipulators** who pushed **Binance spot order flow
  in the final ~10 seconds** so the settlement print landed their way; prices
  reverted right after settlement.
- Implications for a legitimate bot: (a) final-10s flow is toxic — stop
  quoting/flat before it; (b) many "95% win rate" screenshots are manipulation
  or luck, not strategy; (c) we do not implement this.

**Basis measurement (ours)**: Binance end-of-window direction matches the
Chainlink-settled outcome in only **87.6%** of 299 windows. Flips concentrate
at small gaps: |gap|<0.02% → 68% agree; 0.05–0.1% → 91%; 0.1–0.2% → 97.4%;
>0.2% → **100% (40/40)**. Fitted Gaussian basis sigma ≈ **4.6 bps of spot**.
This is now a mandatory term in `polbot/fairvalue.py` (`BASIS_SIGMA`).

## 5. Community bot knowledge (what we adopted)

- **xmayeth-ladder v0.3.2** (the demo video): full 1¢–99¢ bid ladder both
  books, ~1 repost/second, status `quoting → pulled`, positions panel with
  if-UP/if-DOWN PnL, **endgame hedge by buying the expensive opposite side**
  (e.g. 94 DOWN @ 95.5¢ to flatten a cheap-UP book), roll to the next window.
  Round shown resolved DOWN at a controlled −$10.65.
- **warproxxx/poly-maker** (reference architecture): strategy as a pure
  function `(book, inventory, params, clock) → TargetQuotes`; post-only
  everywhere; two-sided bids that sum < $1 (complete-set mint guard); fair
  value = depth-weighted microprice + EWMA of signed flow; inventory skew;
  vol/toxicity EWMAs widening spread; regime machine; SQLite state; REST
  reconcile + heartbeat dead-man switch. We adopted: post-only, the
  self-cross guard, inventory skew, toxicity pauses, cancel-on-stale.
- Polymarket's own market-making docs: batch order submission, GTD for known
  catalysts, cancel-and-replace (no edits), price guards vs midpoint, kill
  switch, real-time fills monitoring, split/merge inventory management.
- Adverse-selection war stories (multiple independent sources): "5 bid fills in
  a row with no ask fill while the midpoint drops" → pause. Biggest risk is
  not being wrong about direction, it's **who is on the other side of your fill**.

## 6. API quirks we hit (save yourself the hours)

- `data-api /v2/trades`: the `offset` parameter is **broken** (returns 0 rows);
  paginate with `next_cursor` only. `condition=` (not `market=`) filters by
  market. Trades exist ≥7 days back. Trades can appear **hours before the
  window opens** (markets are created a day ahead) — filter by timestamp.
- Closed markets lose their order books (`book?token_id=` → "No orderbook
  exists") — backtests must use the trade tape / price history, not books.
- `prices-history` supports `startTs/endTs/fidelity` (fidelity in minutes,
  min 1) — fine for coarse backtests, too coarse for fill simulation.
- Binance REST: shared-egress IPs get rate-banned fast (HTTP 418 with an
  "until <ts>" ban timestamp); 1s klines exist for ≥7 days; pace requests and
  parse the ban timestamp for exact sleep. Use bookTicker/WebSocket in live.
- Gamma events for a window can report `makerBaseFee/takerBaseFee = 1000`
  regardless of the real fee schedule — trust the docs fee table instead.

## 7. Strategy scorecard (tick backtest, 299 BTC windows)

| strategy | verdict | note |
|---|---|---|
| legacy 1–99¢ ladder | **−$10.49/window** | pure adverse-selection food |
| FV-anchored ladder | −$6.44/window | 39% better, still negative |
| + endgame hedge | −$5.95/window | tail-risk cut, still negative |
| lead-lag sniper (1s Binance) | −$8.72/window | tape leads the signal; needs Chainlink + sub-second infra |
| panic-dip buying (sub-10¢) | lottery | +$694 pre-fee concentrated in 5 fills; first half −$1.2K, second +$1.9K |
| late favorite harvesting (strict) | **−$0.02/window, 83% win rate** | the real cohort's game; break-even at our information quality |

## 8. Conclusions

1. The viral strategy is real mechanics wrapped around false economics.
2. Win rate and profit are different axes: the harvester proves you can buy a
   90%+ win rate; making it pay requires the Chainlink feed, order-flow
   reading, and discipline — the pros' edge is informational, not mechanical.
3. Everything is reproducible from this repo. Trust receipts, not screenshots.

## Sources

- Polymarket docs: fees (docs.polymarket.com/trading/fees), Nov-2026 fee/rebate
  changes (docs.polymarket.com/programs/fee-and-rebate-changes-november-2026),
  market-making guide, V2 migration guide, unified SDK docs.
- Telonex research: "$270K in One Week: Inside the Strategies of Polymarket's
  Top Crypto Traders" (telonex.io/research/top-crypto-traders-polymarket-15m,
  Feb 13 2026).
- "Settlement Manipulation in Prediction Markets", arXiv 2606.31675 (2026).
- warproxxx/poly-maker; KaustubhPatange/polymarket-trade-engine (LEARNING.md).
- Polymarket Data API v2 (leaderboard, trades, biggest-winners), Gamma API,
  CLOB API — all queried live Nov 18 2026.
- The uploaded xmayeth-ladder v0.3.2 demo video (frame analysis in
  docs/RESEARCH.md).
