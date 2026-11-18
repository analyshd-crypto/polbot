# Polymarket 5-Minute Crypto Markets — Research Dossier

> Compiled 2026-11-18 from live API inspection (Gamma, CLOB, Data API, Binance), Polymarket
> documentation, whale-wallet autopsies (PR&R), a Stanford/SMU manipulation study, and
> community research (Reddit r/PredictionsMarkets 7-month strategy autopsy).
> Every number below was verified against a primary source or measured directly from the APIs.

---

## 1. The market itself (measured live)

Polymarket runs recurring **"X Up or Down" 5-minute binary markets** for BTC, ETH, SOL, XRP,
DOGE, HYPE and BNB, plus 15m / 1h / 4h / 1d variants.

| Property | Value (measured from APIs, 2026-11-18) |
| --- | --- |
| Series slugs | `btc-up-or-down-5m`, `eth-up-or-down-5m`, `sol-up-or-down-5m`, `xrp-up-or-down-5m`, … |
| Event slug pattern | `btc-updown-5m-<window_start_epoch>` (e.g. `btc-updown-5m-1795016400` = 15:40–15:45 UTC) |
| Window alignment | 5-minute UTC boundaries (:00, :05, …) |
| Markets pre-created | ~40 windows ahead (~3h20m) — order books exist and trade **before** the window opens (near 50/50) |
| Outcomes | `Up` / `Down` (token 0 = Up, token 1 = Down in `clobTokenIds`) |
| Tick size | 0.01 (min tick); `minimum_order_size` = 5 shares |
| negRisk | false |
| Typical liquidity | BTC 5m ≈ $16–23k per window, ETH ≈ $4–9k, SOL ≈ $3–6k, XRP ≈ $2.5k |
| Order types | GTC / GTD / FAK / FOK (all are limit orders underneath); **post-only supported** in `post_orders` |
| Settlement | **Chainlink 30-second TWAP** at both ends (since 2026-08-07; previously spot snapshot) |
| Up wins if | close TWAP ≥ open TWAP (strike) |

### Order book anatomy (live sample, BTC 5m near end of window)

- Losing side (`Up`, ~1% to win): 99 ask levels stacked from **$0.01 × 5,790 shares**,
  $0.02 × 5,464, $0.03 × 2,819 … ≈ **70,844 shares** of resting asks.
- Winning side (`Down`): bids stacked $0.99 × 5,503, $0.98 × 5,428, $0.97 × 2,839 …
  ≈ **70,483 shares**.
- Interpretation: large makers quote the extremes via **CTF complete sets** (split 1 USDC →
  1 Up + 1 Down, then sell both sides). This is exactly the deep-tail liquidity the ladder
  strategy wants to lean against — and compete with.

---

## 2. Fees and rebates (the single most important design input)

**Formula (docs.polymarket.com/trading/fees):**

```
fee = C × rate × p × (1 − p)      # C = shares, p = price
```

| Category (polymarket.com) | Taker rate | Maker rate | Maker rebate |
| --- | --- | --- | --- |
| **Crypto (all 5m markets)** | **0.07** | **0** | **20%** of taker fees (daily pool) |
| Sports | 0.05 | 0 | 15% |
| Politics / Finance | 0.04 | 0 | 25% |

- **Makers never pay fees.** Taker fees peak at 50¢ ($1.75 per 100 shares at rate 0.07) and
  vanish at the extremes.
- Taker break-even win rate at 50¢ ≈ **51.8%**; as a maker it is exactly 50.0%.
- **Change effective 2026-11-20 23:59 UTC:** maker rebates move from daily pools to **volume
  tiers**. Polymarket US's parallel schedule (effective 2026-11-11) is the template:
  - Taker Θ tiers: 0.095 (<$10k 30d vol) → 0.0695 (≥$250k); new accounts get 0.0695 for 7 days.
  - Maker rebate tiers: **0% below $10M/30d maker volume**, −0.0035 (≈5%) at $10–50M,
    −0.0070 at $50–200M, −0.0104 at $200M+.
- Strategic consequence: **maker-only quoting stays free** in both regimes; the rebate stream
  disappears for small accounts after Nov 20. The bot's edge must come from spread + selection,
  not rebates. (Rebates remain a bonus while they last.)

---

## 3. How the whales actually make money (wallet autopsies)

### 3.1 SirMartingale — latency arbitrageur (PR&R Report 008)

28 days (Apr 2026), 5m/15m BTC + ETH only:

- **+$119,586 net (+82.95% ROI)** on $144k deployed; 23,440 trades; 3,996 markets; 26/27 days green.
- Win rate **38.4%** — profit does NOT require high win rate. Avg buy $0.43, avg sell $0.80.
- Mechanism: spot tape (Binance/Coinbase, ms updates) leads the CLOB requote by **1–30 s**;
  he buys the underpriced side mid-band, then **sells aggressively into the lift** — the SELL
  leg alone returned +$72.7k; settlement of unsold residual added +$46.8k.
- Sizing: bounded clips — median $5.50, P99 $134, **max $366**. Averaging down $10→$12→$17
  (never doubling). Both-sides participation: **0%**.
- ETH 5m = highest-efficiency vertical (SELL/BUY 2.59× vs 1.32× on BTC 5m) but tiny capacity.
- Sunday best ROI (+65%) — less competition. Zero trades 23:00–02:00 UTC.
- **Not copyable:** colocated feed needed to win the race. Copyable: asset whitelist, sell
  discipline, hard size cap.

### 3.2 ohanism — hybrid spread-capture + directional MM (PR&R Report)

27 days, BTC/ETH/SOL/XRP × 5m/15m, 24/7:

- **+$258,687 (+3.02% ROI)** on $8.57M deployed; 694,439 trades; 37,982 markets; 64.4% WR.
- Both-sides rate 33.3%. Median paired cost $1.059; **43.6% of pairs cost < $1.00 → guaranteed
  spread profit** regardless of outcome.
- **Dominance ratio is the conviction signal:** when one side got ≥3× the sizing of the other,
  the dominant side won **77.7%** of the time (8,653 markets, ±0.45 pp at 95% conf).
- Playbook: enter both sides when uncertain → sell the losing leg when the signal clarifies →
  hold the winning leg through resolution. Trades the final minutes, walking the book with
  5–15 fills per market. Median trade $5.42, max $281.93.
- Worst single market: −$3,332 (0% of 167 trades won) — the per-market loss bound at their size.
- Hour 23 UTC best WR (71.1%); price-band ROI is **monotonically inverted**: $0.00–0.10 band
  +53.4% ROI, $0.90–1.00 band +1.1% ROI — the cheap tail carries the alpha per dollar.

### 3.3 The final-10-seconds edge (Stanford/SMU study + community data)

- 821 wallets extracted **$8.2M** from Polymarket's 5-minute BTC markets; the edge concentrated
  in the **final 10 seconds** of each window.
- Two distinct effects:
  1. Pre-fix (pre 2026-08-07): settlement-price manipulation — nudging BTC on Binance in the
     last seconds to flip the snapshot. **Fixed by the 30s TWAP settlement.**
  2. Still present: **makers pull resting quotes in the final seconds** (no time to rebalance,
     settlement risk) → spreads blow out → whoever still quotes captures fat margins. The
     community 7-month study confirms late-window execution quality is the main controllable.
- 7-month study (1.7M candles, 4,600 resolved windows) verdicts:
  - Chainlink-vs-Binance "lag arb": **dead** at fillable size (momentum side resolved 74.8%
    but was priced at 75.3%).
  - Momentum/directional continuation on priors: no edge after fees as taker.
  - Book is "shockingly well-calibrated" — **the controllable is execution (maker, timing,
    final seconds, spread capture), not prediction.**

### 3.4 Leaderboard cross-check (data-api.polymarket.com/v2, measured 2026-11-18)

Top 30d PnL wallets: `nigiri99` +$259.6k on $985k volume (26% ROI), `justwins` +$81.7k on
$77.8k volume (105% ROI), `keeptheblueflagflyinghigh`, `surfandturf`, `mooseborzoi`, …
(v1 leaderboard endpoints are **410 Gone**; use `/v2/leaderboard?window=30d|1m|1w|1d`.)

---

## 4. Strategy synthesis for polbot (what we keep / change vs. the original plan)

Original plan (user): both-side 1¢–99¢ ladder on BTC/ETH/SOL 5m, refresh ~5 s, cancel at
T−60 s, hold to settlement, funded ~$3/window-style sizing, target ~$810/day.

Research verdict — keep the skeleton, upgrade the brain:

| # | Original | Upgraded (evidence) |
| --- | --- | --- |
| 1 | Blind 1¢–99¢ ladder both sides | **Fair-value-anchored ladder**: continuous P(Up) from live spot + realized vol + TWAP state; quote size ∝ edge (fair − price), skip negative-edge levels (kills most adverse selection) |
| 2 | Flat sizing | **Kelly-capped band sizing** + ohanism-style **dominance skew** (signal strength ≥3× asymmetry ⇒ 77.7% WR bucket) + hard per-clip cap (whale discipline: max fill $300) |
| 3 | Refresh every 5 s | **Adaptive refresh**: 5 s calm, 2 s when fair moves >3 ticks (retail-latency version of SirMartingale's requote race, without needing colo) |
| 4 | Cancel at T−60 s | Keep as default; optional **final-seconds mode** (quote 90¢+ only until T−10 s — the documented widest-spread window, now safer under 30s TWAP) |
| 5 | Hold to settlement | Default stays **hold-to-settlement** (zero taker fees on exit, user rule); optional scalper exit module (sell into strength ≥0.85) off by default |
| 6 | — | **Maker-only enforcement** via post-only orders ⇒ zero fees in both fee regimes |
| 7 | — | **Paired-set accounting**: if Σavg(Up)+Σavg(Down) < 1.00 → locked risk-free profit; report it |
| 8 | — | Risk layer: per-window/per-day caps, stale-feed guard, kill switch, daily loss limit |
| 9 | — | **Backtest engine** on real Binance 1s data with trade-through fill model + the 30s-TWAP settlement rule, band-level WR reporting (validating the 90%+ claim on the 90¢+ band) |

On the "90–95% win rate" target: per-fill win rate at the 90¢+ band is realistically
**~93–97%** (SirMartingale's realized 97.3% at $0.90+; ohanism 94.9%; book calibration).
The cheap tail (<20¢) wins 5–17% of the time by design — its job is convexity, not win rate.
Blended per-market profitability comes from spread capture + tail convexity, exactly how both
whales print. The backtest section measures this precisely.

### Sources

- docs.polymarket.com — trading/fees, CLOB API, market making guide, maker-rebate program
- Live API measurement: gamma-api.polymarket.com, clob.polymarket.com, data-api.polymarket.com (2026-11-18)
- PR&R trader reports: polyresearchrobotics.com/reports/sirmartingale, /ohanism (Apr–May 2026)
- Stanford/SMU study coverage (821 wallets / $8.2M / final-10-seconds; TWAP fix 2026-08-07)
- r/PredictionsMarkets: "How efficient are Polymarket's 5-min crypto markets" (1.7M-candle study)
- CoinMarketCap: Polymarket debuts 5-minute BTC markets (2026-11)
- K. Patange, "My Journey Building a Polymarket BTC Trading Engine" (Apr 2026) + polymarket-trade-engine repo

Note: no public references were found for "grok bot", "fable 5.2", or "gpt 6.2 astra pro"
(searched X/Reddit/Google). The design incorporates the patterns those names evoke (news/tape
→ market mapping, autonomous agent loops, cost discipline à la the pasted browser-bot charters)
in `polbot/engine/` and the ops layer.
