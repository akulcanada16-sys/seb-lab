# Verification

Verified on 2026-09-14.

- 13 Python behavior checks passed in an isolated environment with the declared timezone dependency.
- Additional independent review checked actual-open risk cancellation/resizing and adverse long/short stop-gap fills.
- Interface event tests passed: sample rendering, navigation, pagination, strategy filtering, changed settings, worker payload, cancellation, failure preservation, empty-trade state, JSON report export and sample CSV export.
- The browser worker completed the real Python calculation. Its data fingerprint, run identity and all displayed metrics matched the offline runner.
- The bundled synthetic run contains 1,440 bars and 167 completed trades. Net P&L is -$2,796.50 after modeled costs. These are synthetic demonstration results.

Browser interaction automation did not reliably deliver mouse events in the development environment; control event behavior was therefore checked against the actual interface code in an isolated DOM test environment. The browser computation itself was run through the application's exposed research action. This does not certify every browser/device combination.

Run identity: `d8d6738b92d21f63ac3c5b2b`.

Run locally:

```sh
python -m pip install -r research_core/requirements.txt
python -m unittest discover -s research_core -p "test_*.py" -v
npm ci
npm test
```

The GitHub workflow repeats the Python and interface checks. A green workflow confirms those checks; it does not establish trading profitability or live execution correctness.

