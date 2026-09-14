"""Offline command line entry point for the public research adapter."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from public_api import generate_sample, run_research


def main() -> None:
    parser = argparse.ArgumentParser(description="Run deterministic offline MNQ research")
    parser.add_argument("input", nargs="?", help="JSON payload file; omit for the seeded synthetic sample")
    parser.add_argument("--output", default="sample-result.json", help="where to save the JSON result")
    args = parser.parse_args()
    payload = json.loads(Path(args.input).read_text(encoding="utf-8")) if args.input else {
        "bars": generate_sample(), "label": "Seeded synthetic example", "kind": "synthetic",
    }
    result = run_research(payload)
    Path(args.output).write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"run_id": result["run_id"], "closed_trades": result["metrics"]["trade_count"], "output": args.output}))


if __name__ == "__main__":
    main()
