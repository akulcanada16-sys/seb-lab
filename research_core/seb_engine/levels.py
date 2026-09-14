from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import replace
from datetime import datetime, time, timedelta
from typing import Any
from uuid import uuid5, NAMESPACE_URL

from .data import aggregate_bars
from .models import Bar, Direction, Level


def _id(level_type: str, price: float, created: datetime) -> str:
    return str(uuid5(NAMESPACE_URL, f"seb:{level_type}:{price:.8f}:{created.isoformat()}"))


def _level(
    level_type: str,
    price: float,
    created: datetime,
    timeframe: str,
    *,
    dynamic: bool = False,
    tests: int = 0,
) -> Level:
    return Level(
        level_id=_id(level_type, price, created),
        level_type=level_type,
        price=price,
        created_time=created,
        last_updated_time=created,
        source_timeframe=timeframe,
        tests=tests,
        dynamic=dynamic,
    )


class LevelEngine:
    def __init__(
        self,
        opening_range_minutes: int = 30,
        swing_confirmation_bars: int = 2,
        equal_tolerance_ticks: int = 2,
        tick_size: float = 0.25,
        round_number_size: float = 50.0,
    ):
        self.opening_range_minutes = opening_range_minutes
        self.swing_confirmation_bars = swing_confirmation_bars
        self.equal_tolerance = equal_tolerance_ticks * tick_size
        self.round_number_size = round_number_size

    def build(self, features: dict[str, Any]) -> list[Level]:
        bars_1m: list[Bar] = features.get("bars_1m", [])
        bars_5m: list[Bar] = features.get("bars_5m", [])
        if not bars_1m:
            return []
        now = bars_1m[-1].market_time
        levels: list[Level] = []
        levels.extend(self._prior_day_levels(bars_1m, now))
        levels.extend(self._overnight_levels(bars_1m, now))
        levels.extend(self._opening_range(bars_1m, now))
        levels.extend(self._confirmed_swings(bars_1m, "1m"))
        levels.extend(self._confirmed_swings(bars_5m, "5m"))
        levels.extend(self._equal_clusters(levels, now))
        levels.extend(self._round_numbers(bars_1m[-1].close, now))
        levels.extend(self._dynamic_features(features, now))
        unique: dict[tuple[str, int], Level] = {}
        for item in levels:
            unique[(item.level_type, round(item.price / 0.25))] = item
        return sorted(unique.values(), key=lambda item: (item.price, item.level_type))

    def prepare_causal_levels(
        self,
        bars_1m: list[Bar],
        feature_frames: list[tuple[int, dict[str, Any]]],
    ) -> dict[int, list[Level]]:
        """Build replay levels incrementally with confirmation-time causality."""
        bars = sorted((bar for bar in bars_1m if bar.is_final), key=lambda bar: bar.timestamp_utc)
        if not feature_frames:
            return {}
        all_5m = aggregate_bars(bars, 5)
        rth_by_day: dict[Any, list[Bar]] = defaultdict(list)
        overnight_by_day: dict[Any, list[Bar]] = defaultdict(list)
        opening_by_day: dict[Any, list[Bar]] = defaultdict(list)
        for bar in bars:
            local = bar.market_time
            if time(9, 30) < local.time() <= time(16, 0):
                rth_by_day[local.date()].append(bar)
            if time(9, 30) < local.time() <= time(10, 0):
                opening_by_day[local.date()].append(bar)
            overnight_day = (
                local.date() + timedelta(days=1)
                if local.time() > time(18, 0) else local.date()
            )
            overnight_start = datetime.combine(
                overnight_day - timedelta(days=1), time(18, 0), local.tzinfo
            )
            overnight_end = datetime.combine(overnight_day, time(9, 30), local.tzinfo)
            if overnight_start < local <= overnight_end:
                overnight_by_day[overnight_day].append(bar)
        rth_days = sorted(rth_by_day)
        confirmation = self.swing_confirmation_bars
        swings_1m: deque[Level] = deque(maxlen=24)
        swings_5m: deque[Level] = deque(maxlen=24)
        last_1m_confirmation = -1
        last_5m_confirmation = -1
        available_5m = 0
        result: dict[int, list[Level]] = {}

        def add_confirmed(source: list[Bar], confirmed_index: int, timeframe: str, target: deque[Level]) -> None:
            center = confirmed_index - confirmation
            if center < confirmation:
                return
            window = source[center - confirmation: center + confirmation + 1]
            if len(window) != confirmation * 2 + 1:
                return
            pivot = source[center]
            created = source[confirmed_index].market_time
            if pivot.high == max(item.high for item in window) and sum(item.high == pivot.high for item in window) == 1:
                target.append(_level("SWING_HIGH", pivot.high, created, timeframe))
            if pivot.low == min(item.low for item in window) and sum(item.low == pivot.low for item in window) == 1:
                target.append(_level("SWING_LOW", pivot.low, created, timeframe))

        for index, features in feature_frames:
            for confirmed in range(last_1m_confirmation + 1, index + 1):
                add_confirmed(bars, confirmed, "1m", swings_1m)
            last_1m_confirmation = index
            now = bars[index].market_time
            while available_5m < len(all_5m) and all_5m[available_5m].market_time <= now:
                available_5m += 1
            upto_5m = available_5m - 1
            for confirmed in range(last_5m_confirmation + 1, upto_5m + 1):
                add_confirmed(all_5m, confirmed, "5m", swings_5m)
            last_5m_confirmation = max(last_5m_confirmation, upto_5m)

            levels: list[Level] = []
            session_open = datetime.combine(now.date(), time(9, 30), now.tzinfo)
            if now >= session_open:
                prior_days = [day for day in rth_days if day < now.date()]
                if prior_days:
                    session = rth_by_day[prior_days[-1]]
                    levels.extend([
                        _level("PRIOR_DAY_HIGH", max(item.high for item in session), session_open, "RTH"),
                        _level("PRIOR_DAY_LOW", min(item.low for item in session), session_open, "RTH"),
                        _level("PRIOR_DAY_CLOSE", session[-1].close, session_open, "RTH"),
                    ])
                overnight = overnight_by_day.get(now.date(), [])
                if overnight:
                    levels.extend([
                        _level("OVERNIGHT_HIGH", max(item.high for item in overnight), session_open, "GLOBEX"),
                        _level("OVERNIGHT_LOW", min(item.low for item in overnight), session_open, "GLOBEX"),
                    ])
            opening_end = session_open + timedelta(minutes=self.opening_range_minutes)
            opening = opening_by_day.get(now.date(), [])
            if now >= opening_end and opening:
                levels.extend([
                    _level("OPENING_RANGE_HIGH", max(item.high for item in opening), opening_end, f"OR{self.opening_range_minutes}"),
                    _level("OPENING_RANGE_LOW", min(item.low for item in opening), opening_end, f"OR{self.opening_range_minutes}"),
                ])
            levels.extend(swings_1m)
            levels.extend(swings_5m)
            levels.extend(self._equal_clusters(levels, now))
            levels.extend(self._round_numbers(bars[index].close, now))
            levels.extend(self._dynamic_features(features, now))
            unique: dict[tuple[str, int], Level] = {}
            for item in levels:
                unique[(item.level_type, round(item.price / 0.25))] = item
            result[index] = sorted(unique.values(), key=lambda item: (item.price, item.level_type))
        return result

    def _prior_day_levels(self, bars: list[Bar], now: datetime) -> list[Level]:
        by_day: dict[Any, list[Bar]] = defaultdict(list)
        for bar in bars:
            if time(9, 30) < bar.market_time.time() <= time(16, 0):
                by_day[bar.market_time.date()].append(bar)
        prior_days = sorted(day for day in by_day if day < now.date())
        if not prior_days:
            return []
        prior_day = prior_days[-1]
        session = by_day[prior_day]
        created = datetime.combine(now.date(), time(9, 30), now.tzinfo)
        if now < created:
            return []
        return [
            _level("PRIOR_DAY_HIGH", max(bar.high for bar in session), created, "RTH"),
            _level("PRIOR_DAY_LOW", min(bar.low for bar in session), created, "RTH"),
            _level("PRIOR_DAY_CLOSE", session[-1].close, created, "RTH"),
        ]

    def _overnight_levels(self, bars: list[Bar], now: datetime) -> list[Level]:
        start = datetime.combine(now.date() - timedelta(days=1), time(18, 0), now.tzinfo)
        end = datetime.combine(now.date(), time(9, 30), now.tzinfo)
        if now < end:
            return []
        session = [bar for bar in bars if start < bar.market_time <= end]
        if not session:
            return []
        return [
            _level("OVERNIGHT_HIGH", max(bar.high for bar in session), end, "GLOBEX"),
            _level("OVERNIGHT_LOW", min(bar.low for bar in session), end, "GLOBEX"),
        ]

    def _opening_range(self, bars: list[Bar], now: datetime) -> list[Level]:
        start = datetime.combine(now.date(), time(9, 30), now.tzinfo)
        end = start + timedelta(minutes=self.opening_range_minutes)
        if now < end:
            return []
        session = [bar for bar in bars if start < bar.market_time <= end]
        if not session:
            return []
        return [
            _level("OPENING_RANGE_HIGH", max(bar.high for bar in session), end, f"OR{self.opening_range_minutes}"),
            _level("OPENING_RANGE_LOW", min(bar.low for bar in session), end, f"OR{self.opening_range_minutes}"),
        ]

    def _confirmed_swings(self, bars: list[Bar], timeframe: str) -> list[Level]:
        confirmation = self.swing_confirmation_bars
        if len(bars) < confirmation * 2 + 1:
            return []
        result: list[Level] = []
        for center in range(confirmation, len(bars) - confirmation):
            pivot = bars[center]
            window = bars[center - confirmation : center + confirmation + 1]
            created = bars[center + confirmation].market_time
            if pivot.high == max(bar.high for bar in window) and sum(bar.high == pivot.high for bar in window) == 1:
                tests = sum(abs(bar.high - pivot.high) <= self.equal_tolerance for bar in bars[center + 1 :])
                result.append(_level("SWING_HIGH", pivot.high, created, timeframe, tests=tests))
            if pivot.low == min(bar.low for bar in window) and sum(bar.low == pivot.low for bar in window) == 1:
                tests = sum(abs(bar.low - pivot.low) <= self.equal_tolerance for bar in bars[center + 1 :])
                result.append(_level("SWING_LOW", pivot.low, created, timeframe, tests=tests))
        return result[-24:]

    def _equal_clusters(self, levels: list[Level], now: datetime) -> list[Level]:
        result: list[Level] = []
        for kind, target in (("SWING_HIGH", "EQUAL_HIGHS"), ("SWING_LOW", "EQUAL_LOWS")):
            candidates = [level for level in levels if level.level_type == kind]
            for index, left in enumerate(candidates):
                cluster = [right for right in candidates[index + 1 :] if abs(right.price - left.price) <= self.equal_tolerance]
                if cluster:
                    prices = [left.price, *(item.price for item in cluster)]
                    created = max([left.created_time, *(item.created_time for item in cluster)])
                    result.append(_level(target, sum(prices) / len(prices), created, "MIXED", tests=len(prices)))
                    break
        return result

    def _round_numbers(self, price: float, now: datetime) -> list[Level]:
        base = round(price / self.round_number_size) * self.round_number_size
        return [
            _level("ROUND_NUMBER", base + offset * self.round_number_size, now, "PRICE", dynamic=False)
            for offset in (-1, 0, 1)
        ]

    def _dynamic_features(self, features: dict[str, Any], now: datetime) -> list[Level]:
        latest = features.get("latest", {})
        result: list[Level] = []
        for key, kind in (
            ("vwap", "VWAP"),
            ("vwap_plus_1sd", "VWAP_PLUS_1SD"),
            ("vwap_minus_1sd", "VWAP_MINUS_1SD"),
            ("vwap_plus_2sd", "VWAP_PLUS_2SD"),
            ("vwap_minus_2sd", "VWAP_MINUS_2SD"),
        ):
            if latest.get(key) is not None:
                result.append(_level(kind, float(latest[key]), now, "SESSION", dynamic=True))
        for timeframe in ("1m", "5m", "15m"):
            values = features.get("timeframes", {}).get(timeframe, {}).get("latest", {})
            for ema_name in ("ema9", "ema21"):
                if values.get(ema_name) is not None:
                    result.append(_level(f"EMA_{timeframe}_{ema_name[3:]}", float(values[ema_name]), now, timeframe, dynamic=True))
        return result

    @staticmethod
    def expire_stale(levels: list[Level], now: datetime, maximum_age: timedelta) -> list[Level]:
        """Return new immutable level records with causally expired validity state."""
        return [
            replace(level, validity_state="EXPIRED", last_updated_time=now)
            if not level.dynamic and now - level.last_updated_time > maximum_age
            else level
            for level in levels
        ]


def nearest_level(
    levels: list[Level],
    price: float,
    *,
    direction: Direction | None = None,
    allowed_types: set[str] | None = None,
) -> Level | None:
    eligible = [
        level
        for level in levels
        if level.validity_state == "ACTIVE"
        and (not allowed_types or level.level_type in allowed_types)
        and (direction != Direction.LONG or level.price > price)
        and (direction != Direction.SHORT or level.price < price)
    ]
    return min(eligible, key=lambda item: abs(item.price - price)) if eligible else None


def closest_reference(levels: list[Level], price: float, allowed_types: set[str] | None = None) -> Level | None:
    eligible = [
        level for level in levels if level.validity_state == "ACTIVE" and (not allowed_types or level.level_type in allowed_types)
    ]
    return min(eligible, key=lambda item: abs(item.price - price)) if eligible else None
