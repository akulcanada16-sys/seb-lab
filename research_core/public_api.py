"""Small, offline-only public adapter around the copied SEB research engine."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from seb_engine.backtest import Backtester
from seb_engine.data import SyntheticFeed
from seb_engine.features import FeatureEngine
from seb_engine.levels import LevelEngine
from seb_engine.models import Bar
from seb_engine.risk import RiskGuard
from seb_engine.strategies import StrategySuite

ENGINE_VERSION = "seb-research-public-1.0.0"
ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "frozen_config"
TICK = 0.25
POINT_VALUE = 2.0
REFERENCE_BALANCE = 50_000.0


def _normalized_source_bytes(path: Path) -> bytes:
    """Make source identity independent of Git's Windows/Linux line endings."""
    return _normalize_newlines(path.read_bytes())


def _normalize_newlines(value: bytes) -> bytes:
    return value.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _source_digest(paths: list[Path]) -> str:
    return _digest_named_sources([(path.name, _normalized_source_bytes(path)) for path in paths])


def _digest_named_sources(items: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for name, contents in sorted(items, key=lambda item: item[0]):
        digest.update(name.encode() + b"\0" + _normalize_newlines(contents))
    return digest.hexdigest()


def _engine_fingerprint() -> str:
    return _source_digest([ROOT / "public_api.py", *sorted((ROOT / "seb_engine").glob("*.py")), *sorted(CONFIG.glob("*.json"))])


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _load(name: str) -> dict[str, Any]:
    return json.loads((CONFIG / name).read_text(encoding="utf-8"))


def _tick(value: float) -> bool:
    return math.isclose(value / TICK, round(value / TICK), abs_tol=1e-8)


def _parse_timestamp(value: Any, row: int) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Row {row}: timestamp must be an ISO date and time with a timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Row {row}: invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Row {row}: timestamp must include a timezone, for example +00:00 or Z")
    return parsed.astimezone(UTC)


def _number(value: Any, key: str, row: int) -> float:
    if isinstance(value, bool):
        raise ValueError(f"Row {row}: {key} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Row {row}: {key} must be a number") from exc
    if not math.isfinite(number):
        raise ValueError(f"Row {row}: {key} must be finite")
    return number


def _rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if "bars" in payload:
        values = payload["bars"]
        if not isinstance(values, list):
            raise ValueError("bars must be a list")
        return values
    source = payload.get("csv")
    if not isinstance(source, str):
        raise ValueError("Provide either bars or csv")
    if len(source.encode("utf-8")) > 5_000_000:
        raise ValueError("CSV is too large; limit it to 20,000 bars")
    reader = csv.DictReader(io.StringIO(source))
    if not reader.fieldnames:
        raise ValueError("CSV needs a header row")
    return list(reader)


def _canonical_bars(payload: dict[str, Any]) -> tuple[list[Bar], list[dict[str, Any]], list[dict[str, Any]]]:
    raw = _rows(payload)
    if not 35 <= len(raw) <= 20_000:
        raise ValueError("Provide between 35 and 20,000 one-minute bars")
    market_tz = ZoneInfo("America/New_York")
    parsed: list[tuple[datetime, dict[str, Any]]] = []
    required = ("open", "high", "low", "close", "volume")
    for row_no, source in enumerate(raw, 1):
        if not isinstance(source, dict):
            raise ValueError(f"Row {row_no}: each bar must be an object")
        timestamp = _parse_timestamp(source.get("timestamp", source.get("time")), row_no)
        values = {key: _number(source.get(key), key, row_no) for key in required}
        if values["volume"] < 0:
            raise ValueError(f"Row {row_no}: volume cannot be negative")
        if values["high"] < max(values["open"], values["close"]) or values["low"] > min(values["open"], values["close"]) or values["low"] > values["high"]:
            raise ValueError(f"Row {row_no}: OHLC prices are inconsistent")
        if not all(_tick(values[key]) for key in ("open", "high", "low", "close")):
            raise ValueError(f"Row {row_no}: MNQ prices must use {TICK:g}-point ticks")
        parsed.append((timestamp, values))
    for index in range(1, len(parsed)):
        if parsed[index][0] <= parsed[index - 1][0]:
            raise ValueError("Bars must be strictly chronological with no duplicate timestamps")
    gaps: list[dict[str, Any]] = []
    segment = 0
    bars: list[Bar] = []
    canonical: list[dict[str, Any]] = []
    for index, (timestamp, values) in enumerate(parsed):
        if timestamp.second or timestamp.microsecond:
            raise ValueError(f"Row {index + 1}: timestamps must be aligned to a full minute")
        if index:
            elapsed_seconds = (timestamp - parsed[index - 1][0]).total_seconds()
            if elapsed_seconds % 60:
                raise ValueError("Timestamps must use an exact whole-minute cadence")
            delta = int(elapsed_seconds // 60)
            if delta != 1:
                segment += 1
                gaps.append({"after": parsed[index - 1][0].isoformat(), "before": timestamp.isoformat(), "missing_minutes": max(0, delta - 1)})
        item = {"timestamp": timestamp.isoformat(), **{key: values[key] for key in required}}
        canonical.append(item)
        bars.append(Bar(timestamp_utc=timestamp, market_time=timestamp.astimezone(market_tz), symbol=f"MNQ-S{segment}", timeframe_minutes=1, source="PUBLIC_RESEARCH_INPUT", is_final=True, **values))
    return bars, canonical, gaps


def generate_sample(seed: int = 7331) -> list[dict[str, Any]]:
    """Return deterministic, tick-aligned synthetic sessions; never market data."""
    feed = SyntheticFeed(seed=seed)
    values = []
    for bar in feed.sessions(count=10, bars_per_session=180):
        values.append({"timestamp": bar.timestamp_utc.isoformat(), "open": round(bar.open / TICK) * TICK, "high": round(bar.high / TICK) * TICK, "low": round(bar.low / TICK) * TICK, "close": round(bar.close / TICK) * TICK, "volume": bar.volume})
    return values


def _engine(settings: dict[str, Any]) -> Backtester:
    risk = deepcopy(_load("risk.json"))
    paper = deepcopy(_load("paper.json"))
    backtest = deepcopy(_load("backtest.json"))
    # Bound only the public report's resampling work. It does not alter a strategy rule.
    backtest["block_bootstrap_samples"] = 250
    backtest["robustness"]["session_block_monte_carlo_samples"] = 250
    risk["estimated_round_trip_commission"] = float(settings["commission_per_contract"])
    risk["slippage_ticks_each_side"] = float(settings["slippage_ticks"])
    configs: dict[str, dict[str, Any]] = {}
    for strategy_id in ("momentum_breakout", "failed_break_reclaim", "balance_rotation"):
        config = deepcopy(_load(f"strategy_{strategy_id}.json"))
        config["version_hash"] = _hash(config)[:16]
        configs[strategy_id] = config
    guard = RiskGuard(risk, account_state_path=None)
    suite = StrategySuite(configs, guard, minimum_target_room_r=float(risk["minimum_target_room_r"]))
    return Backtester(FeatureEngine(), LevelEngine(), suite, backtest, risk, journal=None, paper_config=paper)


def _metric(trades: list[dict[str, Any]]) -> dict[str, Any]:
    net = sum(trade["pnl"] for trade in trades)
    gross = sum(trade["gross_pnl"] for trade in trades)
    costs = sum(trade["costs"] for trade in trades)
    wins = [trade for trade in trades if trade["pnl"] > 0]
    positives = sum(trade["pnl"] for trade in trades if trade["pnl"] > 0)
    negatives = -sum(trade["pnl"] for trade in trades if trade["pnl"] < 0)
    balance = REFERENCE_BALANCE
    peak = balance
    drawdown = 0.0
    for trade in trades:
        balance += trade["pnl"]
        peak = max(peak, balance)
        drawdown = max(drawdown, peak - balance)
    return {"net_pnl": net, "gross_pnl": gross, "costs": costs, "max_drawdown": drawdown, "win_rate": len(wins) / len(trades) if trades else 0.0, "profit_factor": positives / negatives if negatives else None, "trade_count": len(trades), "expectancy_r": sum(trade["result_r"] for trade in trades) / len(trades) if trades else 0.0}


def _globex_session(timestamp: str) -> str:
    local = datetime.fromisoformat(timestamp).astimezone(ZoneInfo("America/New_York"))
    return (local.date().fromordinal(local.date().toordinal() + (1 if local.hour >= 18 else 0))).isoformat()


def _split_metrics(trades: list[dict[str, Any]], bars: list[dict[str, Any]]) -> dict[str, Any]:
    sessions = sorted({_globex_session(bar["timestamp"]) for bar in bars})
    if not sessions:
        return {"design": _metric([]), "validation": _metric([]), "lockbox": _metric([]), "session_boundaries": {"design": [], "validation": [], "lockbox": []}, "out_of_sample_retention": None}
    design_end = max(1, int(len(sessions) * .6))
    validation_end = max(design_end, int(len(sessions) * .8))
    groups = {"design": sessions[:design_end], "validation": sessions[design_end:validation_end], "lockbox": sessions[validation_end:]}
    result = {name: _metric([trade for trade in trades if _globex_session(trade["entry_time"]) in dates]) for name, dates in groups.items()}
    design_r = result["design"]["expectancy_r"]
    result["session_boundaries"] = groups
    result["methodology"] = "POSTHOC_ENTRY_SESSION_SEGMENTS_WITH_POSSIBLE_CARRYOVER"
    result["out_of_sample_retention"] = result["lockbox"]["expectancy_r"] / design_r if design_r > 0 else None
    return result


def run_research(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    settings = {"commission_per_contract": 1.9, "slippage_ticks": 1, "strategy": "combined"}
    supplied_settings = payload.get("settings") or {}
    if not isinstance(supplied_settings, dict) or set(supplied_settings) - set(settings):
        raise ValueError("settings may contain only commission_per_contract, slippage_ticks, and strategy")
    settings.update(supplied_settings)
    if settings["strategy"] not in {"combined", "momentum_breakout", "failed_break_reclaim", "balance_rotation"}:
        raise ValueError("Unknown strategy")
    for field in ("commission_per_contract", "slippage_ticks"):
        if isinstance(settings[field], bool) or not isinstance(settings[field], (int, float)) or not math.isfinite(float(settings[field])) or float(settings[field]) < 0:
            raise ValueError(f"{field} must be a non-negative number")
    if float(settings["commission_per_contract"]) > 100 or float(settings["slippage_ticks"]) > 100 or float(settings["slippage_ticks"]) != int(float(settings["slippage_ticks"])):
        raise ValueError("commission_per_contract must be at most 100 and slippage_ticks must be a whole number from 0 to 100")
    bars, canonical, gaps = _canonical_bars(payload)
    engine = _engine(settings)
    raw = engine.run(bars, strategy_id=settings["strategy"], persist=False)
    records: list[dict[str, Any]] = []
    for source in raw["trades"]:
        quantity = int(source["quantity"])
        costs = float(source["execution_cost_dollars"])
        net = float(source["result_dollars"])
        gross = net + costs
        risk_dollars = abs(float(source["entry"]) - float(source["stop"])) * POINT_VALUE * quantity
        result_r = net / risk_dollars if risk_dollars else 0.0
        conditions = source.get("conditions") or []
        fingerprint = {key: source[key] for key in ("strategy_id", "direction", "entry", "exit", "stop", "target", "entry_time", "exit_time", "quantity")}
        records.append({"id": _hash(fingerprint)[:20], "strategy": source["strategy_id"], "direction": source["direction"], "entry": source["entry"], "exit": source["exit"], "stop": source["stop"], "target": source["target"], "entry_time": source["entry_time"], "exit_time": source["exit_time"], "entry_index": source["entry_index"], "exit_index": source["exit_index"], "quantity": quantity, "pnl": net, "gross_pnl": gross, "costs": costs, "result_r": result_r, "bars_held": source["bars_held"], "exit_reason": source["first_hit"], "evidence": {"conditions": conditions, "features": source["features"]}})
    records.sort(key=lambda trade: (trade["exit_time"], trade["id"]))
    balance = REFERENCE_BALANCE
    peak = balance
    equity = []
    for trade in records:
        balance += trade["pnl"]
        peak = max(peak, balance)
        equity.append({"timestamp": trade["exit_time"], "balance": balance, "drawdown": peak - balance})
    summaries = []
    for strategy in ("momentum_breakout", "failed_break_reclaim", "balance_rotation"):
        group = [trade for trade in records if trade["strategy"] == strategy]
        if group:
            metrics = _metric(group)
            summaries.append({"strategy": strategy, "trade_count": metrics["trade_count"], "net_pnl": metrics["net_pnl"], "win_rate": metrics["win_rate"]})
    dataset = {"label": str(payload.get("label") or "Untitled dataset"), "kind": str(payload.get("kind") or "provided"), "bar_count": len(canonical), "start": canonical[0]["timestamp"], "end": canonical[-1]["timestamp"], "sha256": _hash(canonical)}
    engine_fingerprint = _engine_fingerprint()
    public_engine_version = f"{ENGINE_VERSION}+{engine_fingerprint[:16]}"
    run_id = _hash({"dataset": dataset["sha256"], "settings": settings, "engine_fingerprint": engine_fingerprint})[:24]
    equity.insert(0, {"timestamp": dataset["start"], "balance": REFERENCE_BALANCE, "drawdown": 0.0})
    return {"dataset": dataset, "settings": settings, "engine_version": public_engine_version, "run_id": run_id, "bars": canonical, "trades": records, "equity": equity, "metrics": _metric(records), "strategy_summary": summaries, "split_metrics": _split_metrics(records, canonical), "diagnostics": {"gaps": gaps, "gap_count": len(gaps), "unresolved_open_positions": engine.last_unresolved_positions, "closed_trade_count": len(records), "combined_mode": "INDEPENDENT_STRATEGY_AGGREGATION" if settings["strategy"] == "combined" else "SINGLE_STRATEGY", "engine_fingerprint": engine_fingerprint}, "methodology": ["Offline deterministic research using the copied SEB engine; no broker, live-data, or database connection is created.", "Signals use completed bars and enter on the next bar open after execution-risk revalidation.", "Stops use adverse-first resolution when stop and target occur in the same bar; adverse opening gaps fill at the opening price.", "Gaps split causal histories so no fills are fabricated through missing minutes.", "Combined results aggregate independent strategy simulations and are not a shared-account executable portfolio.", "Split segments are posthoc entry-session summaries and trades may carry over between them.", "Dollar and R results include configured commission and slippage. Equity starts at $50,000 and changes only when a closed trade exits; unresolved positions are excluded."]}
