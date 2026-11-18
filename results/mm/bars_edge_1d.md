# polbot mm backtest — edge_weighted

- data: 1.0d 1s bars, 259200 bars total
- params: `{"lag_seconds": 3.0, "trade_through_ticks": 1, "queue_fill_fraction": 0.6, "noise_ticks": 0.0, "quote_pre_open": false, "seed": 7}`
- config: `mode=edge_weighted bankroll=1000.0 max_window=120.0`

## Summary

| metric | value |
| --- | --- |
| windows | 861 |
| windows_traded | 861 |
| profitable_windows | 580 |
| win_rate_windows | 0.6736 |
| total_fills | 23770 |
| total_pnl | 9172.65 |
| rebates_est | 578.79 |
| max_deployed | 111.41 |
| max_drawdown | 233.8 |
| pnl_per_window | 10.6535 |

## Win rate by entry price band (per filled share)

| band | fills | shares | win rate | deployed | pnl |
| --- | --- | --- | --- | --- | --- |
| 20%-30% | 403 | 1817 | 37.7% | $475.84 | $+209.96 |
| 30%-40% | 3329 | 24007 | 45.0% | $8549.66 | $+2246.90 |
| 40%-50% | 7282 | 61753 | 51.5% | $27863.30 | $+3937.16 |
| 50%-60% | 6765 | 49312 | 56.5% | $26412.73 | $+1432.19 |
| 60%-70% | 4056 | 26948 | 67.2% | $17263.07 | $+831.54 |
| 70%-80% | 1512 | 8197 | 78.2% | $6008.30 | $+405.00 |
| 80%-90% | 378 | 1678 | 88.8% | $1393.87 | $+95.73 |
| 90%-100% | 45 | 172 | 100.0% | $157.39 | $+14.35 |

## Worst 5 windows

- `btc-up-or-down-5m-bt-1794975600`: pnl $-88.86 (up 227sh/$99.10, down 17sh/$6.67, settled Down)
- `btc-up-or-down-5m-bt-1794999000`: pnl $-86.27 (up 199sh/$98.58, down 20sh/$7.83, settled Down)
- `sol-up-or-down-5m-bt-1794968100`: pnl $-85.34 (up 0sh/$0.00, down 138sh/$85.34, settled Up)
- `btc-up-or-down-5m-bt-1794963900`: pnl $-84.93 (up 154sh/$91.76, down 14sh/$6.83, settled Down)
- `btc-up-or-down-5m-bt-1794993600`: pnl $-78.99 (up 18sh/$7.77, down 157sh/$89.71, settled Up)

## Best 5 windows

- `btc-up-or-down-5m-bt-1794935100`: pnl $+109.23 (up 212sh/$98.77, down 11sh/$3.66, settled Up)
- `eth-up-or-down-5m-bt-1794935100`: pnl $+106.51 (up 206sh/$99.14, down 0sh/$0.00, settled Up)
- `sol-up-or-down-5m-bt-1794935100`: pnl $+104.65 (up 204sh/$99.52, down 0sh/$0.00, settled Up)
- `btc-up-or-down-5m-bt-1794951000`: pnl $+97.09 (up 198sh/$96.96, down 16sh/$4.22, settled Up)
- `sol-up-or-down-5m-bt-1794940500`: pnl $+95.80 (up 40sh/$14.63, down 201sh/$90.91, settled Down)
