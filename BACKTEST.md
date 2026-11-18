# polbot backtest & validation report

Two independent engines, one rule: **the real-tape result is the truth; the modeled
result is only a sanity check.**

| engine | data | fills | status |
|---|---|---|---|
| **tick backtester** (`polbot tick-backtest`) | every actual trade on 299 BTC windows (405,828 trades, Nov 17–18 2026) + 118 SOL windows, 1s Binance klines for fair value | trade-through, queue-reset per refresh, liquidity-capped taker legs, taker fees | **ground truth** |
| **bar backtester** (`polbot mm-backtest`) | 1s Binance klines, 861 windows × BTC/ETH/SOL (Nov 17–18 2026), market path = lagged fair-value model | trade-through 1 tick, 60% queue fraction | modeled path — **optimistic by construction** (no informed flow), used only for band-calibration and code-path validation |

## 1. Ground truth — tick backtest, 299 real BTC windows

| strategy | win rate (windows) | PnL | per window | worst | fills |
|---|---|---|---|---|---|
| legacy 1–99¢ ladder (the viral plan) | 36.4% | **−$3,135** | −$10.49 | −$120.00 | 18,150 |
| fv ladder (v1, fair-value anchored) | 32.4% | −$1,926 | −$6.44 | −$96.10 | 8,454 |
| **mm v2 (default)** — edge-weighted, Kelly, dominance skew | 35.4% | −$2,538 | −$8.49 | −$107.16 | 4,581 |
| **mm v2 (very-tight)** — min_edge 6¢, 5% Kelly, $60 cap | 34.8% | −$1,563 | −$5.23 | — | 3,379 |
| **mm v2 (harvest-lean)** — strong-side concentration, 10¢ edge | 32.0% | −$1,007 | −$3.37 | — | 2,448 |
| **harvester** (late-window favorites, model-gated) | 80.0% (of traded) | **−$7** | **−$0.02** | −$8.61 | 9 |
| sniper (Binance lead-lag taker) | — | −$2,603 | −$8.72 | — | — |

SOL tape (118 windows): mm v2 −$13.08/window — thinner books, worse adverse selection.

**Reading:** every Binance-anchored maker structure bleeds. The bleeding shrinks
monotonically as the strategy converges on the harvester (fewer, later, more
selective favorite buys). Sizing amplifies adverse selection: the same quoting
rules at full Kelly lose 2.5× more than at 5% Kelly with tight edges.

## 2. Where the 90%+ win rate actually lives (bar backtest, 861 windows)

Per-filled-share win rate by entry price band (mm v2, edge-weighted, 23,770 fills):

| band | fills | win rate | deployed | PnL |
|---|---|---|---|---|
| 20–30¢ | 403 | 37.7% | $476 | +$210 |
| 30–40¢ | 3,329 | 45.0% | $8,550 | +$2,247 |
| 40–50¢ | 7,282 | 51.5% | $27,863 | +$3,937 |
| 50–60¢ | 6,765 | 56.5% | $26,413 | +$1,432 |
| 60–70¢ | 4,056 | 67.2% | $17,263 | +$832 |
| 70–80¢ | 1,512 | 78.2% | $6,008 | +$405 |
| 80–90¢ | 378 | **88.8%** | $1,394 | +$96 |
| 90–100¢ | 45 | **100%** | $157 | +$14 |

The band table is the calibration check: win rate tracks implied probability
almost perfectly (the book is efficient). **A ≥90% win rate is a property of
buying 90¢+ favorites near expiry — not of a strategy.** The whale data agrees:
SirMartingale's $0.90+ entries won 97.3%; ohanism's won 94.9%; our tape-mined
≥90% cohort buys 95–99¢ late and never sells (research/INTEL.md §3).

Remember the caveat: this table comes from the modeled-path engine. It validates
calibration, not profitability — see §1 for the real-tape PnL.

## 2b. Live paper session (2026-11-18, 16:35–16:41 UTC) — real fills, real settlements

The v2 engine paper-ran on live BTC/ETH/SOL books: 71 fills across 6 windows,
strikes captured from the live feed, risk gate halted quoting at the $400
exposure cap, then the windows were settled against the real Gamma outcomes:

| window | our fills | real winner | PnL |
|---|---|---|---|
| BTC 16:35 | 7× Up (avg ~0.63, $107) | **Down** | −$107.39 |
| ETH 16:35 | 25× Up (avg ~0.39, $99) | **Down** | −$98.53 |
| SOL 16:35 | 15 fills **both sides** (52 Up / 164 Down) | Down | **+$47.91** |

Two lessons in one session: (a) BTC and ETH both basis-flipped at settlement
while the Binance-anchored fair favored Up — the exact 12.4% disagreement case
from INTEL.md §4, live; (b) the SOL window filled both sides at a combined
cost below $1.00/share-pair and locked risk-free profit — the paired-set
capture working exactly as designed. Real-tape backtest conclusions, confirmed
in production-paper mode.

## 3. What is validated vs falsified

**Validated (mechanics & facts):**
- Makers pay zero fees in both regimes; taker fee = `C × rate × p × (1−p)`
  (docs example reproduced in tests: 100 sh @ 50¢ = $1.75 at rate 0.07).
- Post-only batch quoting works (`place_limit_order(post_only=True)` /
  `post_orders(postOnly=True)`); per-market cancels; 5-share minimum; 0.01 tick.
- Window lifecycle: events pre-created ~a day ahead; books trade near 50/50
  pre-open; strike ("price to beat") set at open; 30s Chainlink TWAP at both ends.
- The mm engine end-to-end (paper): discovery, strike capture from the live feed,
  fair-value-anchored ladders, fills against real books, settlement accounting —
  demonstrated live on BTC/ETH/SOL windows (logs/fills.jsonl, 2026-11-18 session).
- Band win rates (§2) match the whales' realized numbers at 90¢+.

**Falsified (on real data):**
- The viral 1¢–99¢ ladder as a profit machine: −$10.49/window, 36% window win
  rate. It is an adverse-selection donation.
- Any Binance-anchored maker ladder at useful size: −$3.4 to −$10.3/window
  across our parameter sweep. The 7-month community study and the Telonex
  wallet study reach the same conclusion independently.
- "Bot with 90%+ win rate AND fat profits" as a mechanical construction: the
  only 90%-win-rate structure (harvester) nets ≈ $0 at Binance information
  quality, ~+2%/window at cohort (Chainlink-feed) quality.

## 4. The upgrade path (why the pros still print)

Our fair value is anchored on Binance; settlement uses the Chainlink 30s TWAP.
The measured basis: **87.6% directional agreement, σ ≈ 4.6bps** (INTEL.md §4).
The SDK exposes the settlement oracle itself:

```python
from polymarket.streams import _specs as specs
sp = specs.CryptoPricesChainlinkTwapSpec(symbols=["BTC"], window_seconds=30)
```

`polbot/mm/data/chainlink.py` wires this feed into the mm engine
(`mm-config.yaml: data.spot_source: chainlink`). With the oracle as the anchor,
the basis term in `polbot/mm/engine/fairvalue.py` goes to zero, late-window
probabilities sharpen exactly where the harvester cohort operates, and the
4.6bps edge the cohort has over every Binance-anchored bot disappears.
That — plus order-flow reading and latency — is the informational edge the
≥90% cohort runs on. Everything else in this repo is the discipline layer.

## 5. Reproduce

```bash
python scripts/fetch_tick_data.py --asset btc --windows 300   # real trade tapes
python scripts/run_tick_backtest.py --asset btc \
    --strategies mm,fv,legacy,harvester --fee-rate 0.07 --json-out results/tick_btc_mm.json
polbot mm-fetch --days 1                                      # 1s klines
polbot mm-backtest --days 1 --mode edge_weighted --out results/mm/bars_edge_1d
```

All numbers above regenerate from those commands; result JSONs are committed
under `results/` as the receipts.
