"""Backtest report rendering (markdown + console)."""

from __future__ import annotations

import json
from typing import Optional

from .engine import BacktestResult


def render_markdown(res: BacktestResult, title: str = "polbot backtest",
                    meta: Optional[dict] = None) -> str:
    s = res.summary()
    bands = res.band_table()
    lines = [f"# {title}", ""]
    if meta:
        lines.append(f"- data: {meta.get('data', '?')}")
        lines.append(f"- params: `{json.dumps(vars(res.params))}`")
        if meta.get("config"):
            lines.append(f"- config: `{meta['config']}`")
        lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| metric | value |")
    lines.append("| --- | --- |")
    for k, v in s.items():
        lines.append(f"| {k} | {v} |")
    lines.append("")
    lines.append("## Win rate by entry price band (per filled share)")
    lines.append("")
    lines.append("| band | fills | shares | win rate | deployed | pnl |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for band, d in bands.items():
        wr = "n/a" if d["win_rate"] is None else f"{d['win_rate']*100:.1f}%"
        lines.append(f"| {band} | {d['fills']} | {d['shares']:.0f} | {wr} | "
                     f"${d['deployed']:.2f} | ${d['pnl']:+.2f} |")
    lines.append("")
    top = sorted(res.windows, key=lambda r: r.pnl)
    lines.append("## Worst 5 windows")
    lines.append("")
    for r in top[:5]:
        lines.append(f"- `{r.slug}`: pnl ${r.pnl:+.2f} "
                     f"(up {r.up_shares:.0f}sh/${r.up_cost:.2f}, "
                     f"down {r.down_shares:.0f}sh/${r.down_cost:.2f}, "
                     f"settled {'Up' if r.settle_up else 'Down'})")
    lines.append("")
    lines.append("## Best 5 windows")
    lines.append("")
    for r in top[::-1][:5]:
        lines.append(f"- `{r.slug}`: pnl ${r.pnl:+.2f} "
                     f"(up {r.up_shares:.0f}sh/${r.up_cost:.2f}, "
                     f"down {r.down_shares:.0f}sh/${r.down_cost:.2f}, "
                     f"settled {'Up' if r.settle_up else 'Down'})")
    lines.append("")
    return "\n".join(lines)


def save(res: BacktestResult, out_prefix: str, title: str,
         meta: Optional[dict] = None) -> None:
    md = render_markdown(res, title, meta)
    with open(out_prefix + ".md", "w") as f:
        f.write(md)
    payload = {
        "summary": res.summary(),
        "bands": res.band_table(),
        "params": vars(res.params),
        "meta": meta or {},
        "windows": [vars(w) for w in res.windows],
    }
    with open(out_prefix + ".json", "w") as f:
        json.dump(payload, f, indent=1)
