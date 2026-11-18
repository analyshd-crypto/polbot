# polbot

**A research-grade trading bot + honest backtester for Polymarket's 5-minute crypto
Up/Down markets (BTC / ETH / SOL / XRP).**

Born from the viral "$810/day ladder bot" X post and demo video, rebuilt the way it
should be: every claim tested against **real trade-tape data**, every strategy
measured, every fee modeled, and the results reported — even when they're ugly.

> **Read this first — the honest summary**
>
> We implemented the viral strategy exactly (two-sided 1¢–99¢ ladder, 5-second
> reposts, pull at T−60s, hold to settlement) and backtested it on **299 real BTC
> windows (405,828 actual trades, tick-level fills, conservative trade-through
> fill model)**: it **loses $3,135** (−$10.49/window, 36% win rate).
> The refined fair-value ladder loses 43% less. The late-window "favorite
> harvester" — the strategy the real ≥90%-win-rate wallets use — is the only
> structure that comes close to break-even at our data quality, and it wins
> 76–83% of windows, not 90%+.
>
> **Win rate ≥ 90% is real on these markets** — we found 168 of 1,272 active
> wallets doing it — but their edge is thin (+~2%/window on capital) and runs on
> information advantages (Chainlink settlement feed, order-flow, latency) that a
> Binance-anchored bot cannot fully replicate. Anyone promising you a bot with a
> guaranteed 90% win rate *and* fat profits is selling a story. The numbers
> below are the receipts.

---

## What's in the box

| piece | what it does |
|---|---|
| `polbot run` | live engine: market discovery, two-sided quoting, 5s cancel/repost, pull at T−60s, endgame hedge, kill switches. Paper mode by default; live requires an explicit unlock env var. |
| `polbot backtest` | v0 backtester on 1-minute price history (fast, coarse) |
| `polbot tick-backtest` + `scripts/` | **tick-level backtester**: replays every actual trade + 1-second Binance klines, conservative fill model, taker fees, harvester/sniper/ladder strategies |
| `polbot tape-wallets` | mines cached trade tapes for wallet-level win rates — this is how we found the real ≥90% cohort |
| `polbot top-traders` | pulls the Polymarket leaderboard and profiles the top wallets' 5m-market activity |
| `polbot/fairvalue.py` | gap→probability model with **basis noise calibrated from 299 real windows** (Binance direction matches Chainlink settlement only 87.6% of the time) |
| risk layer | per-market caps, daily-loss halt, toxicity guard (pauses a side after N one-sided fills), stale-data halt, error-storm kill |

## Test results (all reproducible)

### 1. Tick-level backtest — 299 BTC windows, Nov 17–18 2026 (405,828 trades)

Fill model: resting bids fill **only on trade-through** (a real trade prints
strictly below the bid). Taker legs fill at the next real trade price, capped by
demonstrated liquidity ×0.5. Taker fee coefficient 0.07 (the rate during the
sample; it drops to 0.05 for BTC 5m on Nov 20 2026). Makers pay 0.

| strategy | win rate | total PnL | avg/window | worst | max DD | fills |
|---|---|---|---|---|---|---|
| `legacy` (the viral ladder, exactly) | 36.4% | **−$3,135** | −$10.49 | −$120 | $3,444 | 18,150 |
| `fv` (fair-value anchored ladder) | 33.8% | **−$1,925** | −$6.44 | −$105 | $1,999 | 8,327 |
| `fv_hedge` (+ endgame flatten) | 34.4% | **−$1,780** | −$5.95 | −$105 | $1,853 | 8,413 |
| `sniper` (lead-lag taker, 1s data) | 30.4% | **−$2,607** | −$8.72 | −$59 | $2,892 | 868 |
| `harvester` (late-window favorites, strict) | 83.3% | **−$6.66** | −$0.02 | −$8.61 | $8.61 | 11 |
| `harvester` (relaxed gates) | 81.2% | −$35.66 | −$0.12 | −$50 | $50 | 60 |

Cross-asset check (118 SOL windows): legacy −$11.70/window, fv −$4.32/window. Same story.

**Why the ladder loses** — adverse selection, exactly as the on-chain data
predicts: your resting bids fill precisely when informed flow blasts through
them. You buy the losing side all the way down. The Telonex wallet study
(46,945 wallets) found the single biggest loser on 15m crypto markets was a
majority-maker wallet at **−$140K/week**.

**Why the sniper loses at 1-second data** — fill-level calibration shows the
model claims P(win) = 28.6% / 39.6% / 54.1% by price bucket while reality is
3.6% / 11.6% / 26.6%: **the Polymarket tape leads a 1s Binance signal.** The
professional snipers run on the Chainlink stream and sub-second feeds; that
edge is a latency arms race, not a backtestable free lunch.

**Why the harvester is the closest** — it replicates what the ≥90% cohort
actually does (see below), but with Binance-only information the entries it can
identify are ~break-even. The cohort's edge comes from better inputs.

### 2. The ≥90% win-rate cohort (found by mining our 299-window tape)

`python scripts/analyze_tape_wallets.py` ranks every wallet in the tape:

- **168 of 1,272 active wallets (13%) win ≥90% of their windows**
- Best: 134/134 windows won (+$390, $35K volume); largest PnL: 92.1% win rate,
  +$1,705 over 140 windows ($93K volume)
- Their fingerprint: **buy the near-certain side at 95–99¢ (avg 93.8¢) in the
  final ~2 minutes, one side only, never sell, hold to settlement**, sized
  $50–$1,200 per order
- Economics: ~+2% per window on deployed capital — thin, positive, and
  information-hungry (they read the Chainlink feed and the tape; 21% of their
  entries come in the last 60 seconds, where basis flips live)
- The all-time-PnL leaderboard does NOT play this game: 19 of the top 20
  wallets have zero 5m-market activity; the one that does is **−$5,602** on it

### 3. Basis calibration (the quiet killer)

Binance end-of-window direction matches the Chainlink-settled outcome in only
**87.6%** of windows. Flip rate by |gap|: <0.02% → 32% flips; 0.05–0.1% → 8.8%;
0.1–0.2% → 2.6%; >0.2% → 0/40. Fitted basis sigma ≈ **4.6 bps**. This is why
"buying the 97¢ sure thing" on a Binance signal is a trap, and why
`polbot/fairvalue.py` carries the basis term. (Live, use the SDK's
`crypto_prices_chainlink` stream — it's the settlement source itself.)

### 4. Fees and rebates (as of Nov 18 2026 — changing Nov 20!)

- Crypto taker fee: `fee = shares × rate × p × (1−p)`; rate 0.07 now, **0.05 for
  BTC 5m/15m from Nov 20 2026 23:59 UTC**. Makers always pay 0.
- Maker rebates move from daily pools to volume tiers: **0% under $200K 30-day
  maker weighted volume**, 15% at $10M+. A small maker now earns nothing back —
  the passive spread game is structurally worse from Nov 20.
- The academic literature (arXiv 2606.31675) attributes the famous "$8.2M edge"
  on 5-minute BTC markets to **settlement-time spot manipulation** (821
  suspected wallets pushing Binance price in the final 10 seconds). We do not
  implement that; the bot deliberately stops quoting before that window.

## Architecture

```
gamma-api ── market discovery (slug = {asset}-updown-5m-{window_ts})
clob-api  ── books / prices / (SDK SecureClient: post-only GTD orders)
data-api  ── trade tapes (cursor-paginated), leaderboard, wallet stats
binance   ── 1s klines / bookTicker (fair-value leg)
   │
   ▼
FairValueFeed (gap → probability, basis-corrected)
   ▼
strategy: legacy ladder | FV ladder (+toxicity guard) | endgame hedge | harvester | sniper
   ▼
ExecutionBackend: Paper (sim fills vs live books) | Live (polymarket-client SDK)
   ▼
RiskManager (caps, daily-loss halt, error-storm kill) + RunStats (the dashboard)
```

## Quick start

```bash
pip install -e .            # or: pip install -r requirements.txt

polbot status               # live market snapshot, no auth needed
polbot backtest --asset btc --windows 200       # v0 backtest (1-min data)
python scripts/fetch_tick_data.py --asset btc --windows 300
polbot tick-backtest --asset btc --strategies legacy,fv,fv_hedge,harvester,sniper
polbot tape-wallets --asset btc                 # find the ≥90% cohort yourself

polbot run --asset btc --strategy fv_hedge       # PAPER mode vs live books
polbot run --asset btc --strategy legacy --live  # real orders; needs .env unlock
polbot cancel-all                              # kill switch
polbot doctor                                  # connectivity + credentials check
```

`.env` (see `.env.example`): `POLYMARKET_PRIVATE_KEY` for live mode, plus the
explicit `POLBOT_LIVE_UNLOCK=I-UNDERSTAND-THE-RISK` safety. **Start in paper
mode. Stay in paper mode until a strategy has survived hundreds of paper
windows.**

## What we would actually run (and what not to)

1. **Don't run the legacy ladder.** It's implemented for completeness and
   falsification; the data says it bleeds.
2. The `harvester` in **paper mode**, fed by the Chainlink stream instead of
   Binance, is the only configuration with a plausible live edge. Even then:
   expect ~90% win rate on far fewer entries than the pros get, thin margins,
   and the occasional basis-flip loss eating ten wins.
3. If you ever go live: small caps (`--max-cost`), the daily-loss halt on, and
   never funds you need for rent.

## Reproducing everything

```bash
python scripts/fetch_tick_data.py --asset btc --windows 300   # ~10 min, ~320MB
python scripts/run_tick_backtest.py --asset btc --strategies legacy,fv,fv_hedge
python scripts/run_tick_backtest.py --asset btc --strategies harvester --fv-min 0.95
python scripts/sniper_calibration.py 0.15                    # the calibration table
python scripts/analyze_tape_wallets.py --asset btc           # the ≥90% cohort
python scripts/analyze_top_traders.py --top 20               # leaderboard reality
python -m pytest tests/ -q                                    # 52 tests
```

Docs: [docs/RESEARCH.md](docs/RESEARCH.md) (mechanics, API surface),
[research/INTEL.md](research/INTEL.md) (the full intelligence file: fees,
leaderboard studies, the manipulation paper, wallet mining, strategy notes).

## Disclaimer

This software is for research and education. Prediction-market trading is
risky; 63% of wallets on these markets lose money (median ≈ −$3/week). Nothing
here is financial advice. The maintainers are not affiliated with Polymarket.
