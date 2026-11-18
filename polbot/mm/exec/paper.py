"""Paper executor: real market data, simulated fills, zero risk.

Fill model: our resting bid at price `p` on outcome S is filled when the live book's
best ask for S trades at or below `p` (someone is offering to sell at/below our bid —
in a real CLOB our order would have been matched or front-run by that seller crossing).
This is deliberately conservative: it requires the book to move through our level,
not merely touch it. Fills are polled from the CLOB public API.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from ..data.clob import ClobPublic
from ..models import Fill, Window

log = logging.getLogger("polbot.paper")


class PaperExecutor:
    def __init__(self, cfg, clob: Optional[ClobPublic] = None):
        self.cfg = cfg
        self.clob = clob or ClobPublic(cfg.execution.clob_host)
        self.cash_used = 0.0
        self.fills: list[Fill] = []
        # resting orders: token_id -> {price: size}
        self.resting: dict[str, dict[float, float]] = {}
        self._last_books: dict[str, tuple[float, float]] = {}
        self.est_rebates = 0.0
        self._taker_volume_proxy = 0.0

    # ------------------------------------------------------------------ orders
    def place_quotes(self, win: Window, quotes: list) -> dict:
        """Replace resting orders for this window with the given quote set."""
        for tok in win.tokens:
            self.resting[tok] = {}
        placed = 0
        for q in quotes:
            self.resting.setdefault(q.token_id, {})[q.price] = q.size
            placed += 1
        return {"placed": placed}

    def cancel_window(self, win: Window) -> None:
        for tok in win.tokens:
            self.resting.pop(tok, None)

    def cancel_all(self) -> None:
        self.resting.clear()

    # ------------------------------------------------------------------ fills
    def poll_fills(self, wins: list[Window]) -> list[Fill]:
        """Check current books against resting bids; return new fills."""
        new: list[Fill] = []
        toks = set()
        for w in wins:
            toks.update(w.tokens)
        for tok, levels in list(self.resting.items()):
            if tok not in toks or not levels:
                continue
            try:
                bb, ba = self.clob.best_bid_ask(tok)
            except Exception as e:  # noqa: BLE001
                log.debug("book poll failed %s: %s", tok[:12], e)
                continue
            self._last_books[tok] = (bb, ba)
            for price in sorted(levels, reverse=True):
                if ba <= price:  # book offered at/below our bid -> we'd be filled
                    size = levels.pop(price)
                    fill = Fill(token_id=tok, outcome=self._outcome_for(wins, tok),
                                price=price, size=size, ts=time.time(), fee=0.0)
                    new.append(fill)
                    self.cash_used += fill.notional
                    self._taker_volume_proxy += fill.notional
        if new:
            self.fills.extend(new)
        return new

    @staticmethod
    def _outcome_for(wins: list[Window], token_id: str) -> str:
        for w in wins:
            if token_id in w.tokens:
                return w.outcomes[w.tokens.index(token_id)]
        return "?"

    # ------------------------------------------------------------------ reporting
    def market_value(self, wins: list[Window]) -> float:
        val = 0.0
        for w in wins:
            for i, tok in enumerate(w.tokens):
                shares = w.inventory.get(w.outcomes[i], 0.0)
                if shares <= 0:
                    continue
                bb, _ = self._last_books.get(tok, (0.0, 1.0))
                val += shares * bb
        return val
