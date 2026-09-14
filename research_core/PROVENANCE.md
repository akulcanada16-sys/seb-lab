# Public research core provenance

`seb_engine/` is a copied, stdlib-only research engine from the local TradeHelper project. Its frozen configuration copies are in `frozen_config/`; the public adapter reads them and never writes to them.

The public adapter makes three reporting and execution-safety additions to the copied engine:

1. It records the actual entry/exit timestamps and indexes, resized quantity, and evaluated conditions for each closed trade.
2. It rechecks the risk bracket at the actual next-bar opening fill and applies the worse opening price when a stop is gapped through.
3. It counts unresolved end-of-dataset positions. The adapter splits histories at supplied gaps so positions and causal indicators cannot cross missing minutes.

`public_api.py` is offline-only. It does not create a broker, live-data, journal, database, or account-state connection. Its public report uses a stable hash-based run id and normalizes random internal identifiers out of the response.

Only `public_api.run_research()` and `cli.py` are supported public result interfaces. The copied engine's `internal_legacy_metrics` fields preserve historical internal calculations and must not be presented as public performance reporting.

The public report limits bootstrap work to 250 samples for browser responsiveness. This is a reporting runtime bound, not a change to any frozen strategy rule. Combined strategy output is an independent-strategy aggregation, not a shared-account portfolio simulation.
