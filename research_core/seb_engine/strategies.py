from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .levels import closest_reference, nearest_level
from .models import (
    Condition,
    DataQuality,
    Direction,
    Level,
    StrategyEvaluation,
    StrategyState,
    TradeTicket,
)
from .risk import RiskGuard


def parameter(config: dict[str, Any], name: str, fallback: Any = None) -> Any:
    raw = config.get("parameters", {}).get(name, fallback)
    return raw.get("default", fallback) if isinstance(raw, dict) else raw


def _round_tick(value: float, tick_size: float = 0.25) -> float:
    return round(value / tick_size) * tick_size


class BaseStrategy:
    strategy_id = "base"

    def __init__(self, config: dict[str, Any], risk_guard: RiskGuard, minimum_target_room_r: float = 1.0):
        self.config = config
        self.risk_guard = risk_guard
        self.minimum_target_room_r = minimum_target_room_r
        self.name = config["name"]
        self.version = config["version_hash"]

    def evaluate(self, features: dict[str, Any], levels: list[Level], quality: DataQuality) -> StrategyEvaluation:
        raise NotImplementedError

    def _finish(
        self,
        *,
        features: dict[str, Any],
        quality: DataQuality,
        direction: Direction,
        reference: Level | None,
        conditions: list[Condition],
        invalidation: str,
        entry: float | None,
        stop: float | None,
        target: float | None,
        trigger: str,
        explanation: str,
        developing_state: StrategyState = StrategyState.DEVELOPING,
    ) -> StrategyEvaluation:
        timestamp = features["bars_1m"][-1].market_time
        missing = [condition.label for condition in conditions if not condition.satisfied]
        state = developing_state
        ticket = None
        paper_candidate = None
        if not quality.actionable:
            state = StrategyState.STALE_DATA if quality.is_stale else StrategyState.BLOCKED
            missing = [*missing, *quality.blocking_reasons]
        elif direction != Direction.NONE and entry is not None and stop is not None and target is not None:
            instrument = "MNQ" if "MNQ" in quality.active_symbol.upper() else "NQ"
            paper_sizing = self.risk_guard.size(entry, stop, instrument, apply_account_constraints=False)
            if paper_sizing.blocked:
                state = StrategyState.BLOCKED
                missing.append(paper_sizing.reason)
            else:
                paper_candidate = TradeTicket(
                    strategy_name=self.name,
                    strategy_version=self.version,
                    symbol=quality.active_symbol,
                    direction=direction,
                    timestamp=timestamp,
                    entry=_round_tick(entry),
                    stop=_round_tick(stop),
                    target=_round_tick(target),
                    quantity=paper_sizing.quantity,
                    expiration_time=timestamp + timedelta(minutes=int(parameter(self.config, "expiration_bars", 3))),
                    reference_level=reference.as_dict() if reference else {},
                    trigger_description=trigger,
                    rule_checklist=conditions,
                    data_source=quality.source,
                    freshness_state="FRESH",
                    signal_id=str(uuid5(
                        NAMESPACE_URL,
                        f"seb:paper:{self.strategy_id}:{self.version}:{quality.active_symbol}:{timestamp.isoformat()}:{direction.value}",
                    )),
                )
                if all(condition.satisfied for condition in conditions):
                    live_sizing = self.risk_guard.size(entry, stop, instrument)
                    if live_sizing.blocked:
                        state = StrategyState.BLOCKED
                        missing.append(live_sizing.reason)
                    else:
                        state = StrategyState.READY
                        ticket = TradeTicket(
                            strategy_name=self.name,
                            strategy_version=self.version,
                            symbol=quality.active_symbol,
                            direction=direction,
                            timestamp=timestamp,
                            entry=paper_candidate.entry,
                            stop=paper_candidate.stop,
                            target=paper_candidate.target,
                            quantity=live_sizing.quantity,
                            expiration_time=paper_candidate.expiration_time,
                            reference_level=paper_candidate.reference_level,
                            trigger_description=trigger,
                            rule_checklist=conditions,
                            data_source=quality.source,
                            freshness_state="FRESH",
                            signal_id=str(uuid5(
                                NAMESPACE_URL,
                                f"seb:manual:{self.strategy_id}:{self.version}:{quality.active_symbol}:{timestamp.isoformat()}:{direction.value}",
                            )),
                        )
        elif not any(condition.satisfied for condition in conditions):
            state = StrategyState.IDLE
        return StrategyEvaluation(
            strategy_id=self.strategy_id,
            strategy_name=self.name,
            strategy_version=self.version,
            state=state,
            direction=direction,
            timestamp=timestamp,
            conditions=conditions,
            missing_conditions=missing,
            invalidation=invalidation,
            reference_level=reference,
            relevant_features=self._public_features(features),
            explanation=explanation,
            ticket=ticket,
            paper_candidate=paper_candidate,
        )

    @staticmethod
    def _public_features(features: dict[str, Any]) -> dict[str, Any]:
        latest = features.get("latest", {})
        one = features.get("timeframes", {}).get("1m", {}).get("latest", {})
        five = features.get("timeframes", {}).get("5m", {}).get("latest", {})
        keys = (
            "atr", "body_to_range", "close_location", "same_minute_relative_volume",
            "rolling_volume_ratio", "vwap", "vwap_sd", "vwap_crossing_count",
            "directional_efficiency", "vwap_slope_atr", "distance_to_vwap",
            "recent_range_position", "recent_range_zone", "distance_to_recent_high_atr",
            "distance_to_recent_low_atr", "session_range_position", "session_range_zone",
            "session_range_atr", "distance_to_session_high_atr",
            "distance_to_session_low_atr", "rth_range_position", "rth_range_zone",
            "hour_bucket", "volatility_ratio", "volatility_regime", "vwap_zscore",
            "vwap_zone",
        )
        return {
            **{key: latest.get(key) for key in keys},
            "1m_ema9": one.get("ema9"),
            "1m_ema21": one.get("ema21"),
            "5m_ema9": five.get("ema9"),
            "5m_ema21": five.get("ema21"),
            "5m_ema21_slope": five.get("ema21_slope"),
        }

    def _target(self, levels: list[Level], entry: float, stop: float, direction: Direction, fallback_r: float) -> float:
        structural = {
            "PRIOR_DAY_HIGH", "PRIOR_DAY_LOW", "OVERNIGHT_HIGH", "OVERNIGHT_LOW",
            "OPENING_RANGE_HIGH", "OPENING_RANGE_LOW", "SWING_HIGH", "SWING_LOW",
            "EQUAL_HIGHS", "EQUAL_LOWS", "VWAP", "VWAP_PLUS_1SD", "VWAP_MINUS_1SD",
            "VWAP_PLUS_2SD", "VWAP_MINUS_2SD", "EMA_1m_9", "EMA_1m_21",
        }
        target_level = nearest_level(levels, entry, direction=direction, allowed_types=structural)
        risk = abs(entry - stop)
        if target_level and abs(target_level.price - entry) >= self.minimum_target_room_r * risk:
            return target_level.price
        return entry + risk * fallback_r * (1 if direction == Direction.LONG else -1)


class MomentumBreakoutStrategy(BaseStrategy):
    strategy_id = "momentum_breakout"

    def evaluate(self, features: dict[str, Any], levels: list[Level], quality: DataQuality) -> StrategyEvaluation:
        bars = features["bars_1m"]
        latest = features["latest"]
        count = int(parameter(self.config, "compression_bars", 8))
        if len(bars) < count + 2:
            return self._finish(features=features, quality=quality, direction=Direction.NONE, reference=None,
                conditions=[Condition("warmup", "Compression window complete", False, len(bars))],
                invalidation="Wait for more bars", entry=None, stop=None, target=None, trigger="No trigger",
                explanation="Momentum Breakout Continuation: waiting for a completed compression window.")
        compression = bars[-count - 1:-1]
        range_high = max(bar.high for bar in compression)
        range_low = min(bar.low for bar in compression)
        atr_value = latest["atr"] or 1.0
        width = range_high - range_low
        compression_ok = width <= float(parameter(self.config, "compression_width_atr", 2.2)) * atr_value
        buffer = 0.25 * float(parameter(self.config, "breakout_buffer_ticks", 1))
        direction = Direction.LONG if bars[-1].close > range_high + buffer else Direction.SHORT if bars[-1].close < range_low - buffer else Direction.NONE
        reference_price = range_high if direction != Direction.SHORT else range_low
        reference = Level(
            level_id=f"rolling-range-{bars[-1].market_time.isoformat()}", level_type="ROLLING_RANGE_HIGH" if direction != Direction.SHORT else "ROLLING_RANGE_LOW",
            price=reference_price, created_time=compression[-1].market_time, last_updated_time=compression[-1].market_time,
            source_timeframe="1m", tests=sum(abs(bar.high-reference_price) <= 0.5 for bar in compression) if direction != Direction.SHORT else sum(abs(bar.low-reference_price) <= 0.5 for bar in compression), dynamic=False,
        )
        broke = direction != Direction.NONE
        body_ok = latest["body_to_range"] >= float(parameter(self.config, "min_body_ratio", 0.58))
        close_loc = latest["close_location"] if direction != Direction.SHORT else 1.0 - latest["close_location"]
        close_ok = close_loc >= float(parameter(self.config, "close_location", 0.70))
        rel_volume = latest.get("same_minute_relative_volume")
        volume_ok = rel_volume is not None and rel_volume >= float(parameter(self.config, "relative_volume", 1.05))
        entry = bars[-1].close
        stop = bars[-1].low - 0.25 if direction == Direction.LONG else bars[-1].high + 0.25 if direction == Direction.SHORT else None
        target = self._target(levels, entry, stop, direction, float(parameter(self.config, "fallback_target_r", 1.5))) if stop is not None else None
        room = target is not None and abs(target-entry) >= self.minimum_target_room_r * abs(entry-stop)
        conditions = [
            Condition("compression", "Local range is compressed relative to ATR", compression_ok, width / atr_value),
            Condition("breakout", "Completed bar closes beyond the level and buffer", broke, bars[-1].close-reference_price),
            Condition("body", "Breakout body-to-range efficiency passes", body_ok, latest["body_to_range"]),
            Condition("close", "Breakout closes near its directional extreme", close_ok, close_loc),
            Condition("volume", "Same-minute normalized volume passes", volume_ok, rel_volume),
            Condition("target_room", "Complete target has minimum room", room, abs(target-entry)/abs(entry-stop) if target is not None and stop != entry else None),
        ]
        state = StrategyState.APPROACHING_LEVEL if direction == Direction.NONE and min(abs(bars[-1].close-range_high), abs(bars[-1].close-range_low)) <= atr_value else StrategyState.DEVELOPING
        explanation = (
            f"Momentum Breakout Continuation {direction.value.lower()}: {count}-bar range width is {width/atr_value:.2f} ATR; "
            f"breakout depth is {abs(bars[-1].close-reference_price):.2f} points and same-minute relative volume is {rel_volume if rel_volume is not None else 'unavailable'}."
        )
        return self._finish(features=features, quality=quality, direction=direction, reference=reference,
            conditions=conditions, invalidation="Breakout level is reclaimed", entry=entry, stop=stop,
            target=target, trigger="Completed close beyond compressed range", explanation=explanation, developing_state=state)


class FailedBreakReclaimStrategy(BaseStrategy):
    strategy_id = "failed_break_reclaim"
    eligible_types = {"PRIOR_DAY_HIGH", "PRIOR_DAY_LOW", "OVERNIGHT_HIGH", "OVERNIGHT_LOW", "OPENING_RANGE_HIGH", "OPENING_RANGE_LOW", "VWAP", "VWAP_PLUS_1SD", "VWAP_MINUS_1SD", "VWAP_PLUS_2SD", "VWAP_MINUS_2SD", "SWING_HIGH", "SWING_LOW", "EQUAL_HIGHS", "EQUAL_LOWS"}

    def evaluate(self, features: dict[str, Any], levels: list[Level], quality: DataQuality) -> StrategyEvaluation:
        bars = features["bars_1m"]
        latest = features["latest"]
        atr_value = latest["atr"] or 1.0
        window_count = int(parameter(self.config, "reclaim_window_bars", 2))
        recent = bars[-(window_count + 1):]
        candidates = [level for level in levels if level.level_type in self.eligible_types and abs(level.price-bars[-1].close) <= float(parameter(self.config, "level_proximity_atr", 0.6)) * atr_value]
        reference = None
        direction = Direction.NONE
        excursion = None
        time_beyond = 0
        for level in sorted(candidates, key=lambda item: abs(item.price-bars[-1].close)):
            lows = [bar.low for bar in recent[:-1]]
            highs = [bar.high for bar in recent[:-1]]
            if lows and min(lows) < level.price and bars[-1].close > level.price:
                reference, direction, excursion = level, Direction.LONG, min(lows)
                time_beyond = sum(bar.close < level.price for bar in recent[:-1])
                break
            if highs and max(highs) > level.price and bars[-1].close < level.price:
                reference, direction, excursion = level, Direction.SHORT, max(highs)
                time_beyond = sum(bar.close > level.price for bar in recent[:-1])
                break
        failure_seen = reference is not None
        penetration = abs(reference.price-excursion) if reference and excursion is not None else None
        penetration_ok = penetration is not None and penetration <= float(parameter(self.config, "max_penetration_atr", 0.75)) * atr_value
        reclaimed = reference is not None and ((direction == Direction.LONG and bars[-1].close > reference.price) or (direction == Direction.SHORT and bars[-1].close < reference.price))
        entry = bars[-1].close
        buffer = 0.25 * float(parameter(self.config, "stop_buffer_ticks", 1))
        stop = excursion-buffer if direction == Direction.LONG and excursion is not None else excursion+buffer if direction == Direction.SHORT and excursion is not None else None
        target = self._target(levels, entry, stop, direction, float(parameter(self.config, "fallback_target_r", 1.4))) if stop is not None else None
        room = target is not None and abs(target-entry) >= self.minimum_target_room_r * abs(entry-stop)
        conditions = [
            Condition("eligible_level", "A pre-existing eligible reference level is active", reference is not None, reference.level_type if reference else None),
            Condition("failure", "Price traded beyond the reference level", failure_seen, penetration),
            Condition("penetration", "Penetration remains inside the invalidation depth", penetration_ok, penetration/atr_value if penetration is not None else None),
            Condition("reclaim", "Completed bar closes back inside the level", reclaimed, bars[-1].close),
            Condition("target_room", "Complete target has minimum room", room, abs(target-entry)/abs(entry-stop) if target is not None and stop != entry else None),
        ]
        explanation = (
            f"Failed Break & Reclaim {direction.value.lower()}: price spent {time_beyond} completed bars beyond "
            f"{reference.level_type if reference else 'an eligible level'}; penetration was {penetration if penetration is not None else 0:.2f} points."
        )
        return self._finish(features=features, quality=quality, direction=direction, reference=reference,
            conditions=conditions, invalidation="Excursion exceeds maximum depth or reclaim fails", entry=entry,
            stop=stop, target=target, trigger="Completed close reclaimed the predefined level", explanation=explanation)


class BalanceRotationStrategy(BaseStrategy):
    strategy_id = "balance_rotation"

    def evaluate(self, features: dict[str, Any], levels: list[Level], quality: DataQuality) -> StrategyEvaluation:
        bars = features["bars_1m"]
        latest = features["latest"]
        crosses = int(latest.get("vwap_crossing_count", 0))
        efficiency = float(latest.get("directional_efficiency", 1.0))
        slope = abs(float(latest.get("vwap_slope_atr", 0.0)))
        min_crosses = int(parameter(self.config, "minimum_vwap_crosses", 3))
        balance_cross = crosses >= min_crosses
        balance_efficiency = efficiency <= float(parameter(self.config, "maximum_efficiency", 0.35))
        balance_slope = slope <= float(parameter(self.config, "maximum_vwap_slope_atr", 0.12))
        band_sd = float(parameter(self.config, "band_sd", 1.0))
        upper = latest.get("vwap_plus_1sd" if band_sd == 1.0 else "vwap_plus_2sd")
        lower = latest.get("vwap_minus_1sd" if band_sd == 1.0 else "vwap_minus_2sd")
        direction = Direction.LONG if lower is not None and bars[-1].low <= lower and bars[-1].close > lower else Direction.SHORT if upper is not None and bars[-1].high >= upper and bars[-1].close < upper else Direction.NONE
        boundary_price = lower if direction == Direction.LONG else upper if direction == Direction.SHORT else min((value for value in (upper, lower) if value is not None), key=lambda value: abs(value-bars[-1].close), default=None)
        reference = Level(
            level_id=f"vwap-band-{bars[-1].market_time.isoformat()}", level_type=f"VWAP_{'MINUS' if boundary_price == lower else 'PLUS'}_{int(band_sd)}SD",
            price=boundary_price, created_time=bars[-1].market_time, last_updated_time=bars[-1].market_time,
            source_timeframe="SESSION", dynamic=True,
        ) if boundary_price is not None else None
        recent_outside = 0
        for item in reversed(features["series"][-8:]):
            band = item["plus1"] if bars[-1].close > (latest.get("vwap") or bars[-1].close) else item["minus1"]
            if band is None:
                break
            outside = item["close"] > band if bars[-1].close > (latest.get("vwap") or bars[-1].close) else item["close"] < band
            if outside:
                recent_outside += 1
            else:
                break
        not_band_ride = recent_outside <= int(parameter(self.config, "maximum_band_persistence", 3))
        rejection = direction != Direction.NONE
        entry = bars[-1].close
        buffer = 0.25 * float(parameter(self.config, "stop_buffer_ticks", 2))
        stop = bars[-1].low-buffer if direction == Direction.LONG else bars[-1].high+buffer if direction == Direction.SHORT else None
        target = latest.get("vwap") if direction != Direction.NONE else None
        room = target is not None and stop is not None and abs(target-entry) >= self.minimum_target_room_r * abs(entry-stop)
        conditions = [
            Condition("crosses", "Recent VWAP crossing count supports balance", balance_cross, crosses),
            Condition("efficiency", "Directional efficiency is below the balance maximum", balance_efficiency, efficiency),
            Condition("slope", "Normalized VWAP slope is limited", balance_slope, slope),
            Condition("band_ride", "Price is not persistently riding the same band", not_band_ride, recent_outside),
            Condition("rejection", "Completed bar rejects and closes inside the boundary", rejection, boundary_price),
            Condition("target_room", "VWAP/interior target has minimum room", room, abs(target-entry)/abs(entry-stop) if target is not None and stop != entry else None),
        ]
        score = sum((balance_cross, balance_efficiency, balance_slope, not_band_ride))
        explanation = (
            f"Balanced-Market Rotation {direction.value.lower()}: transparent balance score {score}/4 "
            f"(crosses {crosses}, efficiency {efficiency:.2f}, |VWAP slope| {slope:.2f} ATR); "
            f"the {'boundary rejected' if rejection else 'boundary rejection is incomplete'}."
        )
        return self._finish(features=features, quality=quality, direction=direction, reference=reference,
            conditions=conditions, invalidation="Balance classifier fails or price resumes a band ride", entry=entry,
            stop=stop, target=target, trigger="Completed rejection/reclaim at balance boundary", explanation=explanation)


class StrategySuite:
    classes = {
        "momentum_breakout": MomentumBreakoutStrategy,
        "failed_break_reclaim": FailedBreakReclaimStrategy,
        "balance_rotation": BalanceRotationStrategy,
    }

    def __init__(self, configs: dict[str, dict[str, Any]], risk_guard: RiskGuard, minimum_target_room_r: float = 1.0):
        self.risk_guard = risk_guard
        self.strategies = [
            self.classes[strategy_id](config, risk_guard, minimum_target_room_r)
            for strategy_id, config in configs.items()
        ]

    def evaluate_all(self, features: dict[str, Any], levels: list[Level], quality: DataQuality) -> list[StrategyEvaluation]:
        return [strategy.evaluate(features, levels, quality) for strategy in self.strategies]
