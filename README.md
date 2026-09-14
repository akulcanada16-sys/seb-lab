# SEB — Strategy Evidence Bench

An executable futures research workbench: import one-minute MNQ data, evaluate three frozen strategies, inspect every simulated trade, and export a reproducible experiment.

Built by Akul with AI coding assistance. SEB combines a Python research engine with a browser interface; the same public adapter runs locally and inside the browser.

## Try it in two minutes

Requires Python 3.11 or newer. From this folder:

```sh
python -m http.server 8016 --directory web
```

Open **http://localhost:8016**. A seeded synthetic experiment loads immediately. Select **Run experiment** to calculate it again in your browser. The first run downloads Python and timezone data from jsDelivr; imported market data remains in browser memory.

1. Inspect the balance curve, costs, and strategy contributions.
2. Open a trade to see its entry, exit, stop, target, and decision conditions.
3. Change commission or slippage and rerun.
4. Import your own MNQ CSV, then export the complete experiment or trade ledger.

The sample is generated data and loses money under the default assumptions. It demonstrates software behavior, not an investment result.

## What is implemented

- Momentum breakout, failed break/reclaim, and balance rotation, using frozen strategy definitions.
- Causal bar-by-bar evaluation, next-bar execution, actual-fill risk revalidation and resizing.
- Conservative stop/target collisions and adverse stop-opening gaps.
- Strict data validation, chronological ordering, explicit missing-data boundaries.
- Net results after commission and slippage, exit-ordered balance, closed-trade drawdown.
- Chronological session summaries, individual trade evidence, CSV and JSON exports.
- Identical source logic in the browser worker and offline Python runner; experiment identity includes data, settings, frozen configuration, and engine source.

## Run and verify the Python application

```sh
python -m pip install -r research_core/requirements.txt
python research_core/cli.py --output experiment.json
python -m unittest discover -s research_core -p "test_*.py" -v
```

To run an imported dataset, supply a JSON file with `csv` text or a `bars` array and optional `settings`. See [the runner guide](research_core/README.md). Rebuild the browser source bundle after changing Python code:

```sh
node build.cjs
```

Node is required only to rebuild the browser bundle, not to run the checked-in application.

## System design

```mermaid
flowchart LR
  A[CSV or seeded sample] --> B[Validate timestamps and prices]
  B --> C[SEB Python adapter]
  C --> D[Features and reference levels]
  D --> E[Three strategy definitions]
  E --> F[Risk checks and simulated execution]
  F --> G[Trades, costs, metrics and identity]
  G --> H[Browser charts and exports]
```

The browser uses a dedicated worker running Pyodide. Cancelling terminates that worker. The offline runner uses the same adapter. No broker credentials, live account services, trading database, or private market history are required.

## Research boundaries

Combined results aggregate independent strategy simulations; they are **not a shared-account portfolio**. Drawdown includes only completed trades and does not measure intratrade risk. Unfinished positions are counted and excluded. Missing minutes reset histories rather than inventing price paths. Session summaries are descriptive segments of one run, not independently trained and tested holdouts.

This is research software, not a live execution system or a claim of profitable trading. See [methodology](METHODOLOGY.md) and [source provenance](research_core/PROVENANCE.md).

## Project structure

| Path | Purpose |
|---|---|
| `research_core/public_api.py` | Supported input and result contract |
| `research_core/seb_engine/` | Copied and reviewed strategy/execution components |
| `research_core/frozen_config/` | Fixed research definitions |
| `research_core/test_public_api.py` | Behavioral and execution regression checks |
| `web/` | Complete runnable browser application |
| `build.cjs` | Explicit public-source bundle builder |

## Engineering choices worth inspecting

The difficult part is making a simulated result explainable. Entry risk is checked again at the actual opening price; costs cannot disappear from reported R; data gaps cannot silently carry positions; a changed experiment receives a different identity. The tests exercise those behaviors rather than only checking that the application starts.

## Feedback

Issues with a small reproducible dataset, the exported experiment, and expected behavior are welcome. Do not include private trading records or credentials in public issues.
