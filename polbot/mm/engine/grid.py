"""Ladder/grid construction: turn a fair value into a two-sided maker-only quote set.

Modes
-----
- ``ladder``          the original plan: flat notional at every tick on both sides,
                      1c..99c (panic-catcher + favorite-yield).
- ``edge_weighted``   default: fair-value anchored. Level size follows fractional
                      Kelly on (fair - price); levels priced above fair - min_edge
                      are skipped (adverse-selection guard). Deep-OTM tail quotes
                      require extra edge (they fill exactly when the world moves
                      against that side).

Both modes apply: dominance skew (>=3x asymmetry bucket from the ohanism autopsy),
inventory skew, per-window exposure budget, min order size (5 shares).
"""

from __future__ import annotations

from typing import Optional

from ..config import Config
from ..models import Quote, Window
from .kelly import KellySizer


def _ticks(low: float, high: float, tick: float) -> list[float]:
    out = []
    p = low
    while p <= high + 1e-9:
        out.append(round(p, 4))
        p += tick
    return out


class LadderBuilder:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.qcfg = cfg.quoting
        self.kelly = KellySizer(cfg.sizing.kelly_fraction,
                                cfg.sizing.max_clip_usd,
                                min_clip_usd=1.0)

    # ------------------------------------------------------------------ main
    def build(self, win: Window, fair_up: float, bankroll: float,
              free_budget: float) -> list[Quote]:
        """Build quotes for both sides of `win` given P(Up) and remaining budget."""
        cfg = self.qcfg
        fair = {"Up": min(max(fair_up, 0.001), 0.999),
                "Down": min(max(1.0 - fair_up, 0.001), 0.999)}
        skew = self._dominance_skew(fair_up)
        inv = self._inventory_factor(win)

        # budget split across sides by (skew x inventory) weight
        w_up = skew["Up"] * inv["Up"]
        w_down = skew["Down"] * inv["Down"]
        tot = w_up + w_down
        if tot <= 0:
            return []
        b_up = free_budget * w_up / tot
        b_down = free_budget * w_down / tot

        quotes: list[Quote] = []
        quotes += self._side_quotes(win, "Up", fair["Up"], bankroll, b_up)
        quotes += self._side_quotes(win, "Down", fair["Down"], bankroll, b_down)
        return quotes

    # ------------------------------------------------------------------ sides
    def _side_quotes(self, win: Window, outcome: str, fair: float, bankroll: float,
                     side_budget: float) -> list[Quote]:
        cfg = self.qcfg
        tick = win.min_tick
        levels = _ticks(cfg.low, cfg.high, tick)

        if cfg.mode == "ladder":
            # classic full ladder: flat size on every level
            per_level = side_budget / max(1, len(levels))
            quotes = []
            for p in levels:
                q = self._make_quote(win, outcome, p, per_level)
                if q:
                    quotes.append(q)
            return quotes[:cfg.max_levels_per_side]

        # edge_weighted (default): top-down allocation. Start at the top
        # (fair - min_edge) and step DOWN; each level's notional is capped
        # (a fraction of the side budget, shrinking with depth) so Kelly's
        # love of lottery tickets can't eat the whole budget at 1-2 cents.
        quotes = []
        spent = 0.0
        top = min(fair - cfg.min_edge, cfg.high)
        top = round(int(top / tick + 1e-9) * tick, 4)
        step = max(2 * tick, cfg.level_step) if hasattr(cfg, "level_step") else 2 * tick
        p = top
        while p >= cfg.low and len(quotes) < cfg.max_levels_per_side:
            edge = fair - p
            if p <= 0.20:
                # deep tail: require extra edge and cap size hard
                need = cfg.min_edge + cfg.min_edge_far * (0.20 - p) / 0.20
                level_cap = side_budget * 0.05
            else:
                need = cfg.min_edge
                depth = max(0.0, (top - p))          # cents below the top level
                level_cap = side_budget * max(0.05, 0.20 * (0.9 ** (depth / 0.10)))
            if edge < need:
                p = round(p - step, 4)
                continue
            size_usd = min(self.kelly.size_usd(bankroll, fair, p), level_cap,
                           side_budget - spent)
            if size_usd > 0:
                q = self._make_quote(win, outcome, p, size_usd)
                if q:
                    quotes.append(q)
                    spent += q.notional
            if spent >= side_budget:
                break
            p = round(p - step, 4)
        return quotes

    def _make_quote(self, win: Window, outcome: str, price: float,
                    size_usd: float) -> Optional[Quote]:
        tick = win.min_tick
        price = round(round(price / tick) * tick, 4)
        if price < tick or price > 1.0 - tick:
            return None
        shares = round(size_usd / price, 2) if price > 0 else 0.0
        if shares < win.min_size:
            if size_usd >= win.min_size * price:
                shares = win.min_size
            else:
                return None
        return Quote(token_id=win.token_for(outcome), outcome=outcome,
                     price=price, size=shares, side="BUY")

    # ------------------------------------------------------------------ skews
    def _dominance_skew(self, fair_up: float) -> dict:
        s = self.cfg.sizing
        d = abs(fair_up - 0.5)
        if d < s.dominance_threshold:
            return {"Up": 1.0, "Down": 1.0}
        strong = "Up" if fair_up > 0.5 else "Down"
        weak = "Down" if strong == "Up" else "Up"
        return {strong: s.dominance_skew, weak: 1.0 / s.dominance_skew}

    def _inventory_factor(self, win: Window) -> dict:
        """Quote less on sides we already hold; free budget to the other side."""
        cap = max(self.cfg.sizing.max_window_exposure, 1.0)
        out = {}
        for o in ("Up", "Down"):
            inv_cost = win.cost.get(o, 0.0)
            out[o] = 1.0 / (1.0 + 2.0 * inv_cost / cap)
        return out

    # ------------------------------------------------------------------ pre-open
    def pre_open_quotes(self, win: Window, bankroll: float) -> list[Quote]:
        """Small symmetric quotes below 0.50 before the window opens (book trades ~50/50)."""
        size = self.cfg.markets.pre_open_size_usd
        edge = 0.03
        quotes = []
        for outcome in ("Up", "Down"):
            for p in (round(0.50 - edge, 2), round(0.50 - 2 * edge, 2)):
                q = self._make_quote(win, outcome, p, size)
                if q:
                    quotes.append(q)
        return quotes

    # ------------------------------------------------------------------ final seconds
    def final_seconds_quotes(self, win: Window, fair_up: float,
                             bankroll: float, side_budget: float) -> list[Quote]:
        """Optional end-game: only the strong side, high-probability bids."""
        cfg = self.qcfg
        strong = "Up" if fair_up >= 0.5 else "Down"
        f = fair_up if strong == "Up" else 1.0 - fair_up
        quotes = []
        spent = 0.0
        for p in _ticks(cfg.final_seconds_min_price, 0.98, win.min_tick):
            edge = f - p
            if edge < cfg.min_edge:
                continue
            size_usd = min(self.kelly.size_usd(bankroll, f, p),
                           side_budget - spent)
            if size_usd <= 0:
                break
            q = self._make_quote(win, strong, p, size_usd)
            if q:
                quotes.append(q)
                spent += q.notional
        return quotes
