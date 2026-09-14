# Research methodology

## Input

The public contract accepts 35–20,000 finalized one-minute MNQ bars with timezone-aware ISO bar-close timestamps and open, high, low, close, volume. Prices must be finite and tick-aligned to 0.25. Duplicate or nonchronological timestamps, invalid OHLC ranges, negative volume, subminute data, and invalid execution settings are rejected.

A gap starts a new causal segment. This also resets indicators at overnight/session gaps in partial-session data. Unfinished trades at a boundary are excluded and counted. This intentionally avoids inferring prices through missing observations; it can make incomplete files unsuitable for evaluating a strategy.

## Decision and execution

The frozen strategies consume completed-bar features and levels. An eligible decision may enter on the subsequent bar's open. The actual entry is rechecked against the risk/reward bracket and sizing policy. Signals may be cancelled or resized.

MNQ is modeled at $2 per point, with 0.25-point ticks. Commission is per contract per round trip. Slippage is per contract per side. If a bar touches stop and target, the stop wins. If its open has already passed the stop adversely, execution uses the worse open plus modeled slippage. Target gaps use the configured target conservatively.

One-minute OHLC data cannot determine the full intrabar event path. The simulator makes explicit assumptions; it cannot establish actual broker fills.

## Accounting

The public adapter is the supported reporting interface. Legacy fields inside the copied engine are internal.

Net P&L includes commission and modeled slippage. Before-cost P&L is the result of those same selected trades with modeled costs added back; it is **not** a rerun with zero costs, since costs can also affect eligibility and size. Public R divides net P&L by entry-to-stop price risk times point value and quantity.

The balance begins at a $50,000 reference value and changes in trade-exit order. Drawdown is measured from that closed-trade balance. It excludes unrealized losses, open positions, margin, liquidity, shared account limits, and concurrent strategy exposure.

Combined mode is independent-strategy aggregation. It must not be interpreted as an executable shared-risk portfolio.

## Chronological summaries

All supplied Globex sessions define approximately 60%/20%/20% chronological segments. A session rolls at 18:00 America/New_York. Trades are assigned by entry session, and may close later. Zero-trade sessions remain in the boundaries.

These are descriptive posthoc segments, not independent training/validation/lockbox runs. No performance or retention ratio establishes an edge.

## Reproduction

Exported reports include canonical input bars, settings, trades, metrics, data fingerprint, and source/configuration identity. The seeded sample checks reproducibility; it is not market evidence. The browser runs the same Python adapter as the command-line application.

The browser downloads Pyodide and timezone data from jsDelivr. Google Fonts may load typography. Imported CSV content is not sent to those services or an application server. Export is explicit; refresh discards the in-memory import.

## Boundaries of this release

No live orders, live feed, brokerage reconciliation, order-book liquidity, account-level risk enforcement, or evidence of future profitability. The supplied sample is synthetic. Use only data you have permission to process.
