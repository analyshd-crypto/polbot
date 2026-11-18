"""Shared test fixtures."""

from __future__ import annotations

import json
from typing import Any, Callable


class FakeHttp:
    """Canned-response HTTP client for tests."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        # routes: url -> payload (or list for repeated calls); "?slug=..." keys
        self.routes = routes or {}
        self.calls: list[str] = []

    def get_json(self, url: str, params: dict | None = None):
        key = url
        if params:
            key = f"{url}?{ '&'.join(f'{k}={v}' for k, v in sorted(params.items()))}"
        self.calls.append(key)
        if key in self.routes:
            val = self.routes[key]
            return val() if callable(val) else val
        raise AssertionError(f"unexpected request: {key}")

    def get_json_or_none(self, url: str, params: dict | None = None):
        try:
            return self.get_json(url, params)
        except AssertionError:
            return None


#: Real event payload captured from gamma-api (btc window 1795002600),
#: trimmed to the fields polbot reads.
REAL_EVENT = {
    "slug": "btc-updown-5m-1795002600",
    "title": "Bitcoin Up or Down - November 18, 6:50AM-6:55AM ET",
    "closed": False,
    "markets": [
        {
            "id": "7117384",
            "conditionId": "0xabc123",
            "question": "Bitcoin Up or Down - November 18, 6:50AM-6:55AM ET",
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["5945...UP", "3258...DOWN"]',
            "outcomePrices": '["0.205", "0.795"]',
            "orderPriceMinTickSize": 0.01,
            "acceptingOrders": True,
        }
    ],
}


def resolved_event(winner: str = "up") -> dict:
    ev = json.loads(json.dumps(REAL_EVENT))
    ev["closed"] = True
    prices = ["1", "0"] if winner == "up" else ["0", "1"]
    ev["markets"][0]["outcomePrices"] = json.dumps(prices)
    ev["markets"][0]["acceptingOrders"] = False
    return ev
