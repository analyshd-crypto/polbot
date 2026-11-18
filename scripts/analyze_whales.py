"""Snapshot the Polymarket 30d leaderboard (data-api v2) and compute ROI.
Used for the wallet-watcher research module; run: python scripts/analyze_whales.py
"""
import json
import urllib.request

URL = "https://data-api.polymarket.com/v2/leaderboard?window=30d&limit=25"

req = urllib.request.Request(URL, headers={"User-Agent": "polbot/1.0"})
with urllib.request.urlopen(req, timeout=20) as r:
    data = json.loads(r.read().decode())
rows = data.get("leaderboard") or data.get("data") or data
print(f"{'rank':>4} {'name':<32} {'pnl':>12} {'volume':>12} {'roi':>7}")
for row in rows:
    name = row.get("user_name") or (row.get("user_id") or "")[:12]
    pnl = float(row.get("pnl") or 0)
    vol = float(row.get("volume") or 1)
    print(f"{row.get('rank'):>4} {name:<32} {pnl:>12,.0f} {vol:>12,.0f} {pnl/vol*100:>6.0f}%")
