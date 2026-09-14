from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Iterator
from uuid import uuid4
from zoneinfo import ZoneInfo

from .data import DataQualityMonitor
from .features import FeatureEngine
from .journal import Journal
from .levels import LevelEngine
from .models import Bar, DataQuality, Direction, Level, StrategyEvaluation
from .strategies import StrategySuite
from .trailing import VolatilityTrailingStop


@dataclass(slots=True)
class BacktestTrade:
    backtest_trade_id: str
    signal_id: str
    strategy_id: str
    strategy_version: str
    timestamp: str
    entry: float
    exit: float
    stop: float
    target: float
    direction: str
    result_points: float
    result_r: float
    result_dollars: float
    mae: float
    mfe: float
    time_to_mae_bars: int
    time_to_mfe_bars: int
    bars_held: int
    first_hit: str
    forward_returns: dict[str, float | None]
    features: dict[str, Any]
    parameters: dict[str, Any]
    execution_cost_dollars: float = 0.0
    # Added in the public research copy so consumers do not have to guess
    # lifecycle facts from a randomly generated ticket id.
    quantity: int = 0
    entry_index: int = -1
    exit_index: int = -1
    entry_time: str = ""
    exit_time: str = ""
    conditions: list[dict[str, Any]] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _data_fingerprint(bars: list[Bar]) -> str:
    digest = hashlib.sha256()
    for bar in bars:
        digest.update(
            f"{bar.timestamp_utc.isoformat()}|{bar.open}|{bar.high}|{bar.low}|{bar.close}|{bar.volume}\n".encode()
        )
    return digest.hexdigest()[:20]


class Backtester:
    def __init__(
        self,
        feature_engine: FeatureEngine,
        level_engine: LevelEngine,
        suite: StrategySuite,
        config: dict[str, Any],
        risk_config: dict[str, Any],
        journal: Journal | None = None,
        paper_config: dict[str, Any] | None = None,
    ):
        self.feature_engine = feature_engine
        self.level_engine = level_engine
        self.suite = suite
        self.config = config
        self.risk_config = risk_config
        self.journal = journal
        self.trailing_stop = VolatilityTrailingStop(
            (paper_config or {}).get("trailing_stop", {}),
            float(risk_config.get("tick_size", 0.25)),
        )

    def prepare_frames(self, bars: list[Bar]) -> list[tuple[int, dict[str, Any], list[Level], DataQuality]]:
        """Build causal feature frames once so bounded challenger runs reuse identical data."""
        bars = sorted((bar for bar in bars if bar.is_final), key=lambda bar: bar.timestamp_utc)
        feature_frames = self.feature_engine.prepare_causal_frames(bars)
        level_frames = self.level_engine.prepare_causal_levels(bars, feature_frames)
        frames: list[tuple[int, dict[str, Any], list[Level], DataQuality]] = []
        for index, compact_features in feature_frames:
            quality = DataQuality(
                source="BACKTEST",
                source_type="RECORDED_RESEARCH",
                active_symbol=bars[index].symbol,
                last_event_timestamp=bars[index].timestamp_utc,
                warmup_complete=True,
            )
            frames.append((index, compact_features, level_frames[index], quality))
        return frames

    def _historical_frame_batches(
        self, bars: list[Bar],
    ) -> Iterator[tuple[int, list[tuple[int, dict[str, Any], list[Level], DataQuality]]]]:
        """Prepare one futures contract at a time, preserving rollover causality.

        The supplied continuous archive keeps each source contract identity.
        Indicators and levels must restart when that identity changes; otherwise
        a mechanical rollover price gap can look like a strategy setup.  Returning
        contract-sized batches also avoids retaining five years of feature frames
        in memory at once.
        """
        start = 0
        while start < len(bars):
            symbol = bars[start].symbol
            end = start + 1
            while end < len(bars) and bars[end].symbol == symbol:
                end += 1
            local = self.prepare_frames(bars[start:end])
            yield start, [
                (start + index, features, levels, quality)
                for index, features, levels, quality in local
            ]
            start = end

    def run(
        self,
        bars: list[Bar],
        strategy_id: str = "combined",
        persist: bool = True,
        prepared_frames: list[tuple[int, dict[str, Any], list[Level], DataQuality]] | None = None,
        research_dataset: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        bars = sorted((bar for bar in bars if bar.is_final), key=lambda bar: bar.timestamp_utc)
        if len(bars) < 35:
            raise ValueError("Backtest requires at least 35 finalized one-minute bars")
        outcomes: list[BacktestTrade] = []
        self.last_unresolved_positions = 0
        in_use_until: dict[str, int] = {}
        armed = {strategy.strategy_id: True for strategy in self.suite.strategies}
        threshold = float(self.config.get("entry_threshold", 0.70))
        adaptive_policy = self.config.get("decision_policy") in {
            "ADAPTIVE_EVIDENCE_AGENT", "ADAPTIVE_BAYESIAN_BANDIT",
        }
        if prepared_frames is not None and len({bar.symbol for bar in bars}) > 1:
            raise ValueError("Prepared frames cannot span multiple contracts; prepare each causal history separately")
        frame_batches = (
            [(0, prepared_frames)] if prepared_frames is not None
            else self._historical_frame_batches(bars)
        )
        for _batch_start, frames in frame_batches:
            # A new source contract is a new causal history. No arming or open
            # position state is allowed to leak across the rollover boundary.
            #
            # `in_use_until` must reset here too, and forgetting it invalidated
            # the first five-year run (see EXPERIMENTS entry 20, run d2a438fa):
            # a trade still open when its contract's bars run out returns
            # `len(bars) - 1` from `_simulate_trade` -- the last index of the
            # WHOLE archive, not of the contract -- so the strategy was blocked
            # for every remaining bar of every remaining contract. Failed-break
            # silently stopped at 2022-03-11 and momentum at 2022-12-09 while
            # the run still claimed coverage through 2026-08-27.
            armed = {strategy.strategy_id: True for strategy in self.suite.strategies}
            in_use_until = {}
            for index, features, levels, quality in frames:
                evaluations = (
                    self.suite.evaluate_all(features, levels, quality)
                    if strategy_id == "combined"
                    else [next(strategy for strategy in self.suite.strategies if strategy.strategy_id == strategy_id).evaluate(features, levels, quality)]
                )
                for evaluation in evaluations:
                    if strategy_id != "combined" and evaluation.strategy_id != strategy_id:
                        continue
                    completion = evaluation.completion_ratio
                    strategy = next(
                        item for item in self.suite.strategies
                        if item.strategy_id == evaluation.strategy_id
                    )
                    condition_map = {condition.key: condition.satisfied for condition in evaluation.conditions}
                    required = set(strategy.config.get("required_conditions", []))
                    core_ready = all(condition_map.get(key, False) for key in required)
                    qualifies = core_ready if adaptive_policy else completion >= threshold and core_ready
                    if not qualifies:
                        armed[evaluation.strategy_id] = True
                        continue
                    if not armed.get(evaluation.strategy_id, True) or not evaluation.paper_candidate:
                        continue
                    if in_use_until.get(evaluation.strategy_id, -1) >= index:
                        continue
                    outcome, exit_index = self._simulate_trade(bars, index, evaluation)
                    armed[evaluation.strategy_id] = False
                    in_use_until[evaluation.strategy_id] = exit_index
                    if outcome is not None:
                        outcomes.append(outcome)
        metrics = calculate_metrics(outcomes, bars)
        split_metrics = chronological_validation(outcomes, bars, self.config)
        version = "portfolio"
        if strategy_id != "combined":
            strategy = next(item for item in self.suite.strategies if item.strategy_id == strategy_id)
            version = strategy.version
        run_id = str(uuid4())
        run = {
            "run_id": run_id,
            "created_at": datetime.now(UTC).isoformat(),
            "strategy_id": strategy_id,
            "strategy_version": version,
            "date_start": bars[0].market_time.date().isoformat(),
            "date_end": bars[-1].market_time.date().isoformat(),
            "mode": "HISTORICAL_RESEARCH" if research_dataset else "BACKTEST",
            "parameters": {
                "backtest": self.config,
                "paper_lifecycle": {
                    "entry_model": "NEXT_COMPLETED_BAR_OPEN",
                    "same_bar_resolution": "ADVERSE_FIRST",
                    "time_exit_enabled": False,
                    "session_end_flatten_enabled": False,
                    "dataset_end_position_handling": "OPEN_POSITIONS_EXCLUDED_FROM_CLOSED_TRADE_RESULTS",
                },
                "position_scaling": {
                    "enabled": False,
                    "entry_scaling_enabled": False,
                    "exit_scaling_enabled": False,
                    "status": "NO_POSITION_SCALING_RULES",
                    "description": "Each simulated trade uses its single validated entry quantity; no scale-ins or partial exits are modeled.",
                },
                "risk": self.risk_config,
                "trailing_stop": {
                    "enabled": self.trailing_stop.enabled,
                    "policy_version": self.trailing_stop.policy_version,
                    "eligible_strategies": sorted(self.trailing_stop.eligible_strategies),
                    "activation_mfe_r": self.trailing_stop.activation_r,
                    "atr_multiple": self.trailing_stop.atr_multiple,
                    "minimum_distance_r": self.trailing_stop.minimum_distance_r,
                    "target_handoff": self.trailing_stop.target_handoff,
                },
                "strategy": {
                    strategy.strategy_id: strategy.config
                    for strategy in self.suite.strategies
                    if strategy_id == "combined" or strategy.strategy_id == strategy_id
                },
                "historical_dataset": research_dataset,
            },
            "data_fingerprint": _data_fingerprint(bars),
            # These legacy engine calculations retain their historical gross-R
            # and calendar-date semantics. They are not a public result
            # contract; public_api.py derives fee-normalized, Globex-aligned
            # reporting from lifecycle records instead.
            "internal_legacy_metrics": metrics,
            "internal_legacy_split_metrics": split_metrics,
            "status": "COMPLETED",
            "trades": [trade.as_dict() for trade in outcomes],
        }
        if persist and self.journal:
            self.journal.record_backtest_run(run, run["trades"])
        return run

    def run_all(self, bars: list[Bar], persist: bool = True) -> dict[str, dict[str, Any]]:
        result = {
            strategy.strategy_id: self.run(bars, strategy.strategy_id, persist)
            for strategy in self.suite.strategies
        }
        result["combined"] = self.run(bars, "combined", persist)
        return result

    def _simulate_trade(
        self, bars: list[Bar], entry_index: int, evaluation: StrategyEvaluation
    ) -> tuple[BacktestTrade | None, int]:
        ticket = evaluation.paper_candidate
        assert ticket is not None
        if entry_index + 1 >= len(bars) or bars[entry_index + 1].symbol != bars[entry_index].symbol:
            return None, entry_index
        direction_sign = 1 if ticket.direction == Direction.LONG else -1
        tick_size = float(self.risk_config.get("tick_size", 0.25))
        slippage_ticks = float(self.risk_config.get("slippage_ticks_each_side", 0.0))
        slip = slippage_ticks * tick_size
        fill_bar = bars[entry_index + 1]
        entry = fill_bar.open + slip * direction_sign
        stop = ticket.stop
        initial_stop = stop
        target = ticket.target
        valid_bracket = stop < entry < target if direction_sign == 1 else target < entry < stop
        if not valid_bracket:
            # This exactly mirrors the paper broker's invalid-next-open cancellation.
            return None, entry_index + 1
        execution = self.suite.risk_guard.assess_execution(
            entry=entry, stop=stop, target=target, direction=ticket.direction,
            requested_quantity=ticket.quantity, instrument=("MNQ" if "MNQ" in ticket.symbol.upper() else "NQ"),
            minimum_reward_risk=float(self.risk_config.get("minimum_target_room_r", 1.0)),
        )
        if execution.blocked:
            return None, entry_index + 1
        quantity = execution.quantity
        # Historical research follows the live paper lifecycle: a position stays
        # open until its stop or target resolves.  The backtest-only 15-bar and
        # RTH-flatten settings remain legacy configuration metadata, not exits.
        # A position still open at the end of the supplied data is not a closed
        # result and is deliberately excluded from performance statistics.
        path = bars[entry_index + 1 :]
        exit_price: float | None = None
        first_hit: str | None = None
        exit_offset = 0
        excursions: list[tuple[float, float]] = []
        trail_context: dict[str, Any] = {
            "features": evaluation.relevant_features,
            "trailing_stop": {"active": False},
        }
        for offset, bar in enumerate(path, start=1):
            if bar.symbol != fill_bar.symbol:
                break
            favorable = (bar.high - entry) if direction_sign == 1 else (entry - bar.low)
            adverse = (entry - bar.low) if direction_sign == 1 else (bar.high - entry)
            excursions.append((max(0.0, adverse), max(0.0, favorable)))
            # When a bar opens through a protective stop the unavailable prices
            # between bars are never invented: exit at the worse opening price.
            stop_gap = bar.open < stop if direction_sign == 1 else bar.open > stop
            if stop_gap:
                first_hit = "STOP_GAP"
                exit_price = bar.open - slip * direction_sign
                exit_offset = offset
                break
            stop_hit = bar.low <= stop if direction_sign == 1 else bar.high >= stop
            target_hit = bar.high >= target if direction_sign == 1 else bar.low <= target
            if stop_hit and target_hit:
                adverse_first = self.config.get("same_bar_resolution", "ADVERSE_FIRST") == "ADVERSE_FIRST"
                first_hit = (
                    "TRAILING_STOP"
                    if adverse_first and self.trailing_stop.state(trail_context).get("active")
                    else "STOP" if adverse_first else "TARGET"
                )
                exit_price = (
                    stop - slip * direction_sign
                    if first_hit in {"STOP", "TRAILING_STOP"}
                    else target - slip * direction_sign
                )
                exit_offset = offset
                break
            if stop_hit:
                first_hit = (
                    "TRAILING_STOP"
                    if self.trailing_stop.state(trail_context).get("active") else "STOP"
                )
                exit_price = stop - slip * direction_sign
                exit_offset = offset
                break
            high_water = entry + max(value[1] for value in excursions) if direction_sign == 1 else entry
            low_water = entry - max(value[1] for value in excursions) if direction_sign == -1 else entry
            trailing_update = self.trailing_stop.update(
                strategy_id=evaluation.strategy_id,
                direction_sign=direction_sign,
                entry_price=entry,
                initial_stop=initial_stop,
                current_stop=stop,
                favorable_high=high_water,
                favorable_low=low_water,
                bars_held=offset,
                context=trail_context,
            )
            target_handoff = bool(
                trailing_update and trailing_update.active and self.trailing_stop.target_handoff
            )
            if target_hit and not target_handoff:
                first_hit = "TARGET"
                exit_price = target - slip * direction_sign
                exit_offset = offset
                break
            if trailing_update and trailing_update.active:
                state = self.trailing_stop.state(trail_context)
                state.update({
                    "active": True,
                    "current_stop": trailing_update.new_stop,
                    "mfe_r": trailing_update.mfe_r,
                    "policy_version": self.trailing_stop.policy_version,
                })
                trail_context["trailing_stop"] = state
                stop = trailing_update.new_stop
        if exit_price is None or first_hit is None:
            self.last_unresolved_positions = getattr(self, "last_unresolved_positions", 0) + 1
            return None, len(bars) - 1
        path = path[:exit_offset]
        result_points = (exit_price - entry) * direction_sign
        stop_risk = abs(entry - initial_stop) or tick_size
        result_r = result_points / stop_risk
        instrument = "MNQ" if "MNQ" in ticket.symbol.upper() else "NQ"
        point_value = float(self.risk_config.get("point_values", {}).get(instrument, 2.0))
        commission = float(self.risk_config.get("estimated_round_trip_commission", 0.0)) * quantity
        slippage_cost = 2.0 * slip * point_value * quantity
        result_dollars = result_points * point_value * quantity - commission
        mae = max((value[0] for value in excursions), default=0.0)
        mfe = max((value[1] for value in excursions), default=0.0)
        time_to_mae = next((i + 1 for i, value in enumerate(excursions) if value[0] == mae), 0)
        time_to_mfe = next((i + 1 for i, value in enumerate(excursions) if value[1] == mfe), 0)
        forwards: dict[str, float | None] = {}
        for horizon in self.config.get("forward_return_bars", [1, 3, 5, 10, 15]):
            target_index = entry_index + 1 + int(horizon)
            forwards[str(horizon)] = (
                (bars[target_index].close - entry) * direction_sign
                if target_index < len(bars) and bars[target_index].symbol == fill_bar.symbol else None
            )
        return BacktestTrade(
            backtest_trade_id=str(uuid4()), signal_id=ticket.signal_id,
            strategy_id=evaluation.strategy_id, strategy_version=evaluation.strategy_version,
            timestamp=ticket.timestamp.isoformat(), entry=entry, exit=exit_price, stop=initial_stop,
            target=target, direction=ticket.direction.value, result_points=result_points,
            result_r=result_r, result_dollars=result_dollars, mae=mae, mfe=mfe,
            time_to_mae_bars=time_to_mae, time_to_mfe_bars=time_to_mfe,
            bars_held=exit_offset, first_hit=first_hit, forward_returns=forwards,
            features=evaluation.relevant_features, parameters=self.config,
            execution_cost_dollars=commission + slippage_cost,
            quantity=quantity,
            entry_index=entry_index + 1,
            exit_index=entry_index + exit_offset,
            entry_time=fill_bar.timestamp_utc.isoformat(),
            exit_time=bars[entry_index + exit_offset].timestamp_utc.isoformat(),
            conditions=[condition.as_dict() for condition in evaluation.conditions],
        ), entry_index + exit_offset


def _max_drawdown(values: list[float]) -> float:
    peak = 0.0
    equity = 0.0
    drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def calculate_metrics(trades: list[BacktestTrade], bars: list[Bar] | None = None) -> dict[str, Any]:
    r_values = [trade.result_r for trade in trades]
    points = [trade.result_points for trade in trades]
    dollars = [trade.result_dollars for trade in trades]
    wins = [value for value in r_values if value > 0]
    losses = [value for value in r_values if value < 0]
    sessions = sorted({bar.market_time.date() for bar in (bars or []) if 9 <= bar.market_time.hour <= 16})
    counts: dict[str, int] = {str(day): 0 for day in sessions}
    for trade in trades:
        counts[str(datetime.fromisoformat(trade.timestamp).date())] = counts.get(str(datetime.fromisoformat(trade.timestamp).date()), 0) + 1
    losing_streak = worst = 0
    for value in r_values:
        losing_streak = losing_streak + 1 if value < 0 else 0
        worst = max(worst, losing_streak)
    expectancy_r = statistics.fmean(r_values) if r_values else 0.0
    positive_sum = sum(value for value in dollars if value > 0)
    negative_sum = abs(sum(value for value in dollars if value < 0))
    stdev = statistics.stdev(r_values) if len(r_values) > 1 else 0.0
    downside = [value for value in r_values if value < 0]
    downside_dev = math.sqrt(statistics.fmean([value * value for value in downside])) if downside else 0.0
    zero_sessions = sum(value == 0 for value in counts.values())
    trades_per_session = len(trades) / len(sessions) if sessions else 0.0
    bucket = "under 1" if trades_per_session < 1 else "1 to 3" if trades_per_session < 3 else "3 to 5" if trades_per_session < 5 else "5 to 8" if trades_per_session <= 8 else "over 8"
    return {
        "total_trades": len(trades),
        "trades_per_session": trades_per_session,
        "frequency_bucket": bucket,
        "frequency_by_day": counts,
        "zero_signal_session_percentage": 100.0 * zero_sessions / len(sessions) if sessions else 0.0,
        "win_rate": len(wins) / len(r_values) if r_values else 0.0,
        "average_win_r": statistics.fmean(wins) if wins else 0.0,
        "average_loss_r": statistics.fmean(losses) if losses else 0.0,
        "payoff_ratio": statistics.fmean(wins) / abs(statistics.fmean(losses)) if wins and losses else 0.0,
        "expectancy_r": expectancy_r,
        "expectancy_points": statistics.fmean(points) if points else 0.0,
        "expectancy_dollars": statistics.fmean(dollars) if dollars else 0.0,
        "expected_r_per_session": expectancy_r * trades_per_session,
        "profit_factor": positive_sum / negative_sum if negative_sum else (999.0 if positive_sum else 0.0),
        "max_drawdown_r": _max_drawdown(r_values),
        "max_drawdown_dollars": _max_drawdown(dollars),
        "worst_losing_streak": worst,
        "average_hold_bars": statistics.fmean([trade.bars_held for trade in trades]) if trades else 0.0,
        "sharpe_per_trade": statistics.fmean(r_values) / stdev if stdev else 0.0,
        "sortino_per_trade": statistics.fmean(r_values) / downside_dev if downside_dev else 0.0,
        "commission_slippage_drag_dollars": sum(
            trade.execution_cost_dollars for trade in trades
        ),
        "net_result_r": sum(r_values),
        "net_result_dollars": sum(dollars),
        "mean_mae": statistics.fmean([trade.mae for trade in trades]) if trades else 0.0,
        "mean_mfe": statistics.fmean([trade.mfe for trade in trades]) if trades else 0.0,
    }


def chronological_validation(trades: list[BacktestTrade], bars: list[Bar], config: dict[str, Any]) -> dict[str, Any]:
    ordered = sorted(trades, key=lambda trade: trade.timestamp)
    sessions = sorted({bar.market_time.date().isoformat() for bar in bars})
    if len(sessions) >= 3:
        design_end = max(1, min(len(sessions) - 2, int(len(sessions) * float(config.get("design_fraction", 0.6)))))
        validation_count = max(1, int(len(sessions) * float(config.get("validation_fraction", 0.2))))
        validation_end = min(len(sessions) - 1, design_end + validation_count)
        session_splits = {
            "design": set(sessions[:design_end]),
            "validation": set(sessions[design_end:validation_end]),
            "lockbox": set(sessions[validation_end:]),
        }
        splits = {
            name: [trade for trade in ordered if trade.timestamp[:10] in days]
            for name, days in session_splits.items()
        }
    else:
        design_end = int(len(ordered) * float(config.get("design_fraction", 0.6)))
        validation_end = design_end + int(len(ordered) * float(config.get("validation_fraction", 0.2)))
        splits = {"design": ordered[:design_end], "validation": ordered[design_end:validation_end], "lockbox": ordered[validation_end:]}
    result = {name: calculate_metrics(part) for name, part in splits.items()}
    session_chunks = _chunks(sessions, max(1, math.ceil(len(sessions) / 4)))
    result["walk_forward"] = [
        {"window": index + 1, "sessions": chunk, "metrics": calculate_metrics([trade for trade in ordered if trade.timestamp[:10] in set(chunk)])}
        for index, chunk in enumerate(session_chunks)
    ]
    result["bootstrap_expectancy_95"] = _session_bootstrap(ordered, int(config.get("block_bootstrap_samples", 250)))
    result["session_boundaries"] = {name: sorted(days) for name, days in session_splits.items()} if len(sessions) >= 3 else {}
    design_expectancy = result["design"]["expectancy_r"]
    lockbox_expectancy = result["lockbox"]["expectancy_r"]
    result["out_of_sample_retention"] = lockbox_expectancy / design_expectancy if design_expectancy else 0.0
    result["robustness"] = _session_block_monte_carlo(ordered, result, config)
    return result


def _chunks(values: list[Any], size: int) -> list[list[Any]]:
    return [values[index:index+size] for index in range(0, len(values), size)] if values else []


def _session_bootstrap(trades: list[BacktestTrade], samples: int) -> list[float]:
    by_day: dict[str, list[float]] = {}
    for trade in trades:
        by_day.setdefault(_globex_session(trade.timestamp), []).append(trade.result_r)
    days = sorted(by_day)
    if not days:
        return [0.0, 0.0]
    randomizer = random.Random(20260724)
    estimates = []
    for _ in range(max(20, samples)):
        drawn = [randomizer.choice(days) for _ in days]
        values = [value for day in drawn for value in by_day[day]]
        estimates.append(statistics.fmean(values) if values else 0.0)
    estimates.sort()
    return [estimates[int(len(estimates)*0.025)], estimates[min(len(estimates)-1, int(len(estimates)*0.975))]]


def _globex_session(timestamp: str) -> str:
    """Group Sunday-evening through next-day futures activity as one session."""
    value = datetime.fromisoformat(timestamp)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    market = value.astimezone(ZoneInfo("America/New_York"))
    session_date = market.date() + timedelta(days=1) if market.hour >= 18 else market.date()
    return session_date.isoformat()


def _percentile(values: list[float], fraction: float) -> float:
    """Deterministic nearest-rank percentile, including empty samples."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def _session_block_monte_carlo(
    trades: list[BacktestTrade], splits: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    """Resample whole sessions, preserving within-session trade order and dependence.

    This is a robustness stress check of the supplied historical sample, not a
    forecast of future returns.  A fixed seed makes saved research reproducible.
    """
    robustness = dict(config.get("robustness", {}))
    requested = int(robustness.get("session_block_monte_carlo_samples", config.get("block_bootstrap_samples", 250)))
    simulations = max(0, requested) if robustness.get("enabled", True) else 0
    # Trades are grouped as complete Globex sessions, never as individually
    # shuffled outcomes.  This preserves the order and clustering that occurred
    # within each session.
    by_session: dict[str, list[float]] = {}
    for trade in trades:
        by_session.setdefault(_globex_session(trade.timestamp), []).append(float(trade.result_r))
    sessions = sorted(by_session)
    paths: list[tuple[float, float, int]] = []
    if sessions and simulations:
        randomizer = random.Random(int(robustness.get("seed", 20260827)))
        block_length = max(1, int(robustness.get("session_block_length", 1)))
        for _ in range(simulations):
            drawn_days: list[str] = []
            while len(drawn_days) < len(sessions):
                start = randomizer.randrange(len(sessions))
                drawn_days.extend(sessions[(start + offset) % len(sessions)] for offset in range(block_length))
            values = [value for day in drawn_days[:len(sessions)] for value in by_session[day]]
            paths.append((sum(values), _max_drawdown(values), _worst_losing_streak(values)))
    endings = [path[0] for path in paths]
    drawdowns = [path[1] for path in paths]
    streaks = [float(path[2]) for path in paths]
    positive_windows = sum(
        float(window.get("metrics", {}).get("expectancy_r") or 0.0) > 0.0
        for window in splits.get("walk_forward", [])
    )
    window_count = len(splits.get("walk_forward", []))
    trade_count = len(trades)
    if trade_count < int(robustness.get("minimum_trades_for_informative", 30)) or len(sessions) < int(robustness.get("minimum_sessions_for_informative", 5)):
        grade = "TOO_LITTLE_EVIDENCE"
    elif (float(splits.get("validation", {}).get("expectancy_r") or 0.0) > 0.0
          and float(splits.get("lockbox", {}).get("expectancy_r") or 0.0) > 0.0
          and positive_windows >= max(1, math.ceil(window_count * 0.75))):
        grade = "CONSISTENT_HISTORICAL_EVIDENCE"
    else:
        grade = "MIXED_HISTORICAL_EVIDENCE"
    return {
        "method": "DETERMINISTIC_SESSION_BLOCK_MONTE_CARLO",
        "simulations": simulations,
        "evidence_grade": grade,
        "plain_language_summary": (
            "This is a stress check of the same historical sessions in different orders; it is not a prediction of future profit. "
            + ("There is not enough history yet to judge consistency." if grade == "TOO_LITTLE_EVIDENCE" else
               "The held-out periods and rolling windows were checked alongside the stress paths.")
        ),
        "probability_profitable": sum(value > 0.0 for value in endings) / len(endings) if endings else 0.0,
        "ending_r": {"p05": _percentile(endings, .05), "median": _percentile(endings, .50), "p95": _percentile(endings, .95)},
        "max_drawdown_r": {"median": _percentile(drawdowns, .50), "p95": _percentile(drawdowns, .95)},
        "worst_losing_streak": {"median": int(_percentile(streaks, .50)), "p95": int(_percentile(streaks, .95))},
        "positive_walk_forward_windows": positive_windows,
        "walk_forward_windows": window_count,
        "validation_positive": float(splits.get("validation", {}).get("expectancy_r") or 0.0) > 0.0,
        "lockbox_positive": float(splits.get("lockbox", {}).get("expectancy_r") or 0.0) > 0.0,
        "session_count": len(sessions),
        "trade_count": trade_count,
    }


def _worst_losing_streak(values: list[float]) -> int:
    streak = worst = 0
    for value in values:
        streak = streak + 1 if value < 0.0 else 0
        worst = max(worst, streak)
    return worst


def pareto_frontier(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = sorted(runs, key=lambda run: run["metrics"]["trades_per_session"])
    frontier: list[dict[str, Any]] = []
    best_expectancy = -math.inf
    for run in reversed(candidates):
        expectancy = run["metrics"]["expectancy_r"]
        if expectancy >= best_expectancy:
            frontier.append(run)
            best_expectancy = expectancy
    return list(reversed(frontier))
