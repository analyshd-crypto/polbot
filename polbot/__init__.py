"""polbot — a market-making bot for Polymarket 5-minute crypto Up/Down markets.

Strategy (mirrors the ladder bot popularised on X / the demo video):

1. Every 5 minutes a new binary market opens: will BTC (or ETH/SOL/XRP) be
   Up or Down at the close of the window?
2. The bot rests BUY limit orders on BOTH outcome books at (nearly) every
   cent below the current price — a two-sided ladder.
3. It keeps the ladder up from the open until roughly the last minute, then
   pulls every order.
4. Every ~5 seconds it cancels and reposts so the ladder tracks the market.
5. Whatever it buys, it holds to resolution. It never sells.

Money is made from (a) steady small edge on the expensive side (buying the
likely winner at 90c+ for 1.5-5% discounts when panickers sweep down) and
(b) lottery-style cheap shares (buying the loser at 5-30c when it later
recovers and pays $1). Makers pay zero fees on Polymarket crypto markets and
earn rebates; only takers pay.

This package is engine-agnostic about execution: a paper backend simulates
fills against live books (dry-run), and a live backend places real orders
through the official ``polymarket-client`` SDK.
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
