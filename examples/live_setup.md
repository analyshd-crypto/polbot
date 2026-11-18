# Live trading setup

polbot never places real orders unless you explicitly run with `--live`
**and** provide credentials. Paper mode (default) uses live market data and
simulates fills — use it for days before risking funds.

## 1. Polymarket account with funds

- Create a Polymarket account and fund it with USDC on Polygon
  (the trading collateral is pUSD; deposits bridge automatically).
- You are responsible for complying with Polymarket's Terms of Service and
  with the rules of your jurisdiction — Polymarket is geo-restricted in some
  countries (including the US for the global platform).

## 2. Export your wallet key

- Polymarket UI: Settings -> Advanced -> Export Private Key.
- Browser wallet (MetaMask etc.): export the account's private key directly.
  If you trade through a proxy/email wallet, the **signing key** differs from
  the **funding address** — set `POLYMARKET_WALLET` to the funder address.

## 3. Configure `.env`

```bash
cp .env.example .env
# then edit:
#   POLYMARKET_PRIVATE_KEY=0x...   (key that signs orders)
#   POLYMARKET_WALLET=0x...        (optional: proxy/funder address)
#   POLBOT_DRY_RUN=0               (only when you are ready)
```

## 4. Check credentials and allowances

```bash
polbot doctor
```

If `doctor` reports missing trading approvals, set token allowances once
(USDC + Conditional Tokens for the exchange contracts). The official SDK
docs describe the programmatic path; doing a tiny trade once from the UI also
configures approvals for proxy wallets.

## 5. First live run — smallest possible size

```bash
# one window only, $30 market budget, 5 shares per level
POLBOT_MAX_COST=30 polbot run --live --asset btc --max-windows 1
```

Watch the log: every order placed/canceled/filled is printed. Afterwards:

```bash
polbot cancel-all   # kill switch: cancels every open order on the account
```

## Safety features baked in

- Orders are **post-only**: you can never accidentally cross the spread and
  pay taker fees.
- Orders are **GTD** expiring at window end (+60s margin), so a crashed bot
  leaves no resting orders.
- The bot pulls all orders `POLBOT_PULL_S` (default 60s) before each window
  closes.
- `POLBOT_DAILY_LOSS` halts the bot after a realized loss limit.
- `polbot cancel-all` is a manual kill switch.

## Capital sizing

With the default `POLBOT_MAX_COST=250`, one window can commit up to ~$250
(the whole ladder can fill in a crash). Since one window runs at a time,
working capital of `max_cost + 20%` is enough — but expect the full budget
to be at risk in every window. Start small.
