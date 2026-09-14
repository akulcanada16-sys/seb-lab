from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Iterable

from .data import aggregate_bars
from .models import Bar


def ema(values: Iterable[float], window: int) -> list[float]:
    values = list(values)
    if not values:
        return []
    alpha = 2.0 / (window + 1.0)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * float(value) + (1.0 - alpha) * result[-1])
    return result


def true_ranges(bars: list[Bar]) -> list[float]:
    if not bars:
        return []
    result: list[float] = []
    prior_close: float | None = None
    for bar in bars:
        if prior_close is None:
            result.append(bar.high - bar.low)
        else:
            result.append(max(bar.high - bar.low, abs(bar.high - prior_close), abs(bar.low - prior_close)))
        prior_close = bar.close
    return result


def atr(bars: list[Bar], window: int = 14) -> list[float]:
    ranges = true_ranges(bars)
    if not ranges:
        return []
    result = [ranges[0]]
    for index in range(1, len(ranges)):
        if index < window:
            result.append(sum(ranges[: index + 1]) / (index + 1))
        else:
            result.append((result[-1] * (window - 1) + ranges[index]) / window)
    return result


def candle_geometry(bar: Bar, prior: Bar | None = None, atr_value: float | None = None) -> dict[str, Any]:
    full_range = max(0.0, bar.high - bar.low)
    body = abs(bar.close - bar.open)
    upper = max(0.0, bar.high - max(bar.open, bar.close))
    lower = max(0.0, min(bar.open, bar.close) - bar.low)
    safe_range = full_range if full_range else 1.0
    overlap = 0.0
    engulfing = 0.0
    inside = outside = False
    gap = 0.0
    if prior:
        overlap = max(0.0, min(bar.high, prior.high) - max(bar.low, prior.low))
        engulfing = max(0.0, bar.high - prior.high) + max(0.0, prior.low - bar.low)
        inside = bar.high <= prior.high and bar.low >= prior.low
        outside = bar.high > prior.high and bar.low < prior.low
        gap = bar.open - prior.close
    return {
        "direction": "UP" if bar.close > bar.open else "DOWN" if bar.close < bar.open else "FLAT",
        "body_size": body,
        "full_range": full_range,
        "upper_wick": upper,
        "lower_wick": lower,
        "body_to_range": body / safe_range if full_range else 0.0,
        "upper_wick_to_range": upper / safe_range if full_range else 0.0,
        "lower_wick_to_range": lower / safe_range if full_range else 0.0,
        "close_location": (bar.close - bar.low) / safe_range if full_range else 0.5,
        "range_div_atr": full_range / atr_value if atr_value else 0.0,
        "body_div_atr": body / atr_value if atr_value else 0.0,
        "gap_from_prior_close": gap,
        "overlap_with_prior": overlap,
        "engulfing_amount": engulfing,
        "inside_bar": inside,
        "outside_bar": outside,
    }


def directional_efficiency(bars: list[Bar]) -> float:
    if len(bars) < 2:
        return 0.0
    path = sum(abs(right.close - left.close) for left, right in zip(bars, bars[1:]))
    return abs(bars[-1].close - bars[0].close) / path if path else 0.0


def range_position(price: float, low: float, high: float) -> float | None:
    width = high - low
    if width <= 0:
        return None
    return min(1.0, max(0.0, (price - low) / width))


def range_zone(position: float | None) -> str:
    if position is None:
        return "UNAVAILABLE"
    if position <= 0.20:
        return "BOTTOM_20"
    if position <= 0.40:
        return "LOWER_MIDDLE"
    if position < 0.60:
        return "MIDDLE_20"
    if position < 0.80:
        return "UPPER_MIDDLE"
    return "TOP_20"


def trading_session_anchor(at: datetime) -> datetime:
    day = at.date() if at.time() >= time(18, 0) else at.date() - timedelta(days=1)
    return datetime.combine(day, time(18, 0), at.tzinfo)


def direction_changes(bars: list[Bar]) -> int:
    changes = 0
    prior_sign = 0
    for left, right in zip(bars, bars[1:]):
        sign = 1 if right.close > left.close else -1 if right.close < left.close else 0
        if sign and prior_sign and sign != prior_sign:
            changes += 1
        if sign:
            prior_sign = sign
    return changes


class SameMinuteVolumeProfile:
    """Expected volume computed strictly from earlier calendar sessions."""

    def __init__(self, lookback_sessions: int = 20, estimator: str = "median", ewm_alpha: float = 0.25):
        self.lookback_sessions = lookback_sessions
        self.estimator = estimator
        self.ewm_alpha = ewm_alpha

    def values_before(self, bars: list[Bar], index: int) -> list[float]:
        current = bars[index]
        current_day = current.market_time.date()
        minute_key = (current.market_time.hour, current.market_time.minute)
        by_day: dict[Any, float] = {}
        for prior in bars[:index]:
            if prior.market_time.date() >= current_day:
                continue
            if (prior.market_time.hour, prior.market_time.minute) == minute_key:
                by_day[prior.market_time.date()] = prior.volume
        days = sorted(by_day)[-self.lookback_sessions :]
        return [by_day[day] for day in days]

    def expected_at(self, bars: list[Bar], index: int) -> float | None:
        values = self.values_before(bars, index)
        if not values:
            return None
        if self.estimator == "mean":
            return statistics.fmean(values)
        if self.estimator == "ewm":
            value = values[0]
            for observation in values[1:]:
                value = self.ewm_alpha * observation + (1 - self.ewm_alpha) * value
            return value
        return statistics.median(values)

    def relative_at(self, bars: list[Bar], index: int) -> float | None:
        expected = self.expected_at(bars, index)
        return bars[index].volume / expected if expected and expected > 0 else None

    def zscore_at(self, bars: list[Bar], index: int) -> float | None:
        values = self.values_before(bars, index)
        if len(values) < 2:
            return None
        expected = self.expected_at(bars, index)
        deviation = statistics.stdev(values)
        return (bars[index].volume - expected) / deviation if expected is not None and deviation else 0.0


@dataclass(slots=True)
class OnlineVWAP:
    rth_only: bool = True
    sum_volume: float = 0.0
    sum_price_volume: float = 0.0
    sum_price2_volume: float = 0.0
    session_date: Any = None

    def update(self, bar: Bar) -> tuple[float | None, float | None]:
        local = bar.market_time
        in_rth = time(9, 30) < local.time() <= time(16, 0)
        if self.rth_only and not in_rth:
            return None, None
        if self.session_date != local.date():
            self.session_date = local.date()
            self.sum_volume = self.sum_price_volume = self.sum_price2_volume = 0.0
        typical = (bar.high + bar.low + bar.close) / 3.0
        volume = max(0.0, bar.volume)
        self.sum_volume += volume
        self.sum_price_volume += typical * volume
        self.sum_price2_volume += typical * typical * volume
        if self.sum_volume <= 0:
            return None, None
        value = self.sum_price_volume / self.sum_volume
        variance = max(0.0, self.sum_price2_volume / self.sum_volume - value * value)
        return value, math.sqrt(variance)


def session_vwap(bars: list[Bar], rth_only: bool = True) -> list[tuple[float | None, float | None]]:
    calculator = OnlineVWAP(rth_only=rth_only)
    return [calculator.update(bar) for bar in bars]


def _timeframe_features(bars: list[Bar], atr_window: int) -> dict[str, Any]:
    if not bars:
        return {"bars": [], "ema9": [], "ema21": [], "atr": []}
    closes = [bar.close for bar in bars]
    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    atr_values = atr(bars, atr_window)
    latest_atr = atr_values[-1] or 1.0
    touches9 = sum(1 for bar, value in zip(bars[-20:], ema9[-20:]) if bar.low <= value <= bar.high)
    touches21 = sum(1 for bar, value in zip(bars[-20:], ema21[-20:]) if bar.low <= value <= bar.high)
    return {
        "bars": bars,
        "ema9": ema9,
        "ema21": ema21,
        "atr": atr_values,
        "latest": {
            "ema9": ema9[-1],
            "ema21": ema21[-1],
            "ema9_slope": ema9[-1] - ema9[-4] if len(ema9) >= 4 else 0.0,
            "ema21_slope": ema21[-1] - ema21[-4] if len(ema21) >= 4 else 0.0,
            "ema_separation": ema9[-1] - ema21[-1],
            "price_distance_ema9": closes[-1] - ema9[-1],
            "price_distance_ema21": closes[-1] - ema21[-1],
            "price_distance_ema9_atr": (closes[-1] - ema9[-1]) / latest_atr,
            "price_distance_ema21_atr": (closes[-1] - ema21[-1]) / latest_atr,
            "ema_order": "BULL" if ema9[-1] > ema21[-1] else "BEAR" if ema9[-1] < ema21[-1] else "FLAT",
            "recent_touches_ema9": touches9,
            "recent_touches_ema21": touches21,
        },
    }


class FeatureEngine:
    def __init__(
        self,
        atr_window: int = 14,
        volume_lookback_sessions: int = 20,
        volume_estimator: str = "median",
    ):
        self.atr_window = atr_window
        self.volume_profile = SameMinuteVolumeProfile(
            volume_lookback_sessions, volume_estimator
        )

    def compute(
        self,
        bars_1m: list[Bar],
        bars_5m: list[Bar] | None = None,
        bars_15m: list[Bar] | None = None,
    ) -> dict[str, Any]:
        if not bars_1m:
            return {"ready": False, "latest": {}, "series": [], "timeframes": {}}
        bars_1m = sorted((bar for bar in bars_1m if bar.is_final), key=lambda bar: bar.timestamp_utc)
        bars_5m = sorted(bars_5m or aggregate_bars(bars_1m, 5), key=lambda bar: bar.timestamp_utc)
        bars_15m = sorted(bars_15m or aggregate_bars(bars_1m, 15), key=lambda bar: bar.timestamp_utc)
        tf1 = _timeframe_features(bars_1m, self.atr_window)
        tf5 = _timeframe_features(bars_5m, self.atr_window)
        tf15 = _timeframe_features(bars_15m, self.atr_window)
        vwap_values = session_vwap(bars_1m)
        latest_vwap, latest_sd = vwap_values[-1]
        latest_atr = tf1["atr"][-1]
        latest_bar = bars_1m[-1]
        geometry = candle_geometry(
            latest_bar,
            bars_1m[-2] if len(bars_1m) > 1 else None,
            latest_atr,
        )
        relative_volume = self.volume_profile.relative_at(bars_1m, len(bars_1m) - 1)
        volume_zscore = self.volume_profile.zscore_at(bars_1m, len(bars_1m) - 1)
        window = bars_1m[-20:]
        closes = [bar.close for bar in window]
        log_returns = [math.log(right / left) for left, right in zip(closes, closes[1:]) if left > 0 and right > 0]
        realized_volatility = math.sqrt(sum(value * value for value in log_returns)) if log_returns else 0.0
        crosses = 0
        side_streak = 0
        prior_side = 0
        current_streak = 0
        for index in range(max(1, len(bars_1m) - 20), len(bars_1m)):
            left_vwap = vwap_values[index - 1][0]
            right_vwap = vwap_values[index][0]
            if left_vwap is None or right_vwap is None:
                continue
            left_side = 1 if bars_1m[index - 1].close > left_vwap else -1
            right_side = 1 if bars_1m[index].close > right_vwap else -1
            crosses += int(left_side != right_side)
            if right_side == prior_side:
                current_streak += 1
            else:
                current_streak = 1
                prior_side = right_side
            side_streak = current_streak
        rolling_volumes = [bar.volume for bar in bars_1m[-20:-1]]
        rolling_volume_ratio = latest_bar.volume / statistics.fmean(rolling_volumes) if rolling_volumes else 1.0
        recent_high = max(bar.high for bar in window)
        recent_low = min(bar.low for bar in window)
        recent_position = range_position(latest_bar.close, recent_low, recent_high)
        anchor = trading_session_anchor(latest_bar.market_time)
        session_bars = [bar for bar in bars_1m if bar.market_time >= anchor]
        session_high = max(bar.high for bar in session_bars)
        session_low = min(bar.low for bar in session_bars)
        session_position = range_position(latest_bar.close, session_low, session_high)
        rth_start = datetime.combine(
            latest_bar.market_time.date(), time(9, 30), latest_bar.market_time.tzinfo
        )
        rth_bars = [bar for bar in bars_1m if rth_start <= bar.market_time <= latest_bar.market_time]
        rth_high = max((bar.high for bar in rth_bars), default=None)
        rth_low = min((bar.low for bar in rth_bars), default=None)
        rth_position = (
            range_position(latest_bar.close, rth_low, rth_high)
            if rth_low is not None and rth_high is not None else None
        )
        prior_atrs = [value for value in tf1["atr"][-61:-1] if value and value > 0]
        atr_reference = statistics.median(prior_atrs) if prior_atrs else None
        volatility_ratio = latest_atr / atr_reference if atr_reference else None
        volatility_regime = (
            "LOW" if volatility_ratio is not None and volatility_ratio < 0.80
            else "HIGH" if volatility_ratio is not None and volatility_ratio > 1.20
            else "NORMAL" if volatility_ratio is not None else "UNAVAILABLE"
        )
        vwap_zscore = (
            (latest_bar.close - latest_vwap) / latest_sd
            if latest_vwap is not None and latest_sd is not None and latest_sd > 0 else None
        )
        vwap_zone = (
            "UNAVAILABLE" if latest_vwap is None
            else "BELOW_2SD" if vwap_zscore is not None and vwap_zscore <= -2
            else "BELOW_1SD" if vwap_zscore is not None and vwap_zscore <= -1
            else "BELOW_VWAP" if latest_bar.close < latest_vwap
            else "ABOVE_2SD" if vwap_zscore is not None and vwap_zscore >= 2
            else "ABOVE_1SD" if vwap_zscore is not None and vwap_zscore >= 1
            else "ABOVE_VWAP"
        )
        vwap_slope = 0.0
        if len(vwap_values) >= 4 and latest_vwap is not None and vwap_values[-4][0] is not None:
            vwap_slope = latest_vwap - vwap_values[-4][0]
        latest = {
            **geometry,
            "atr": latest_atr,
            "realized_volatility": realized_volatility,
            "rolling_range": recent_high - recent_low,
            "recent_range_position": recent_position,
            "recent_range_zone": range_zone(recent_position),
            "distance_to_recent_high_atr": (recent_high - latest_bar.close) / latest_atr if latest_atr else None,
            "distance_to_recent_low_atr": (latest_bar.close - recent_low) / latest_atr if latest_atr else None,
            "session_range_position": session_position,
            "session_range_zone": range_zone(session_position),
            "session_range_atr": (session_high - session_low) / latest_atr if latest_atr else None,
            "distance_to_session_high_atr": (session_high - latest_bar.close) / latest_atr if latest_atr else None,
            "distance_to_session_low_atr": (latest_bar.close - session_low) / latest_atr if latest_atr else None,
            "rth_range_position": rth_position,
            "rth_range_zone": range_zone(rth_position),
            "hour_bucket": f"H{latest_bar.market_time.hour:02d}",
            "volatility_ratio": volatility_ratio,
            "volatility_regime": volatility_regime,
            "vwap_zscore": vwap_zscore,
            "vwap_zone": vwap_zone,
            "directional_efficiency": directional_efficiency(window),
            "direction_changes": direction_changes(window),
            "consecutive_closes_vwap_side": side_streak,
            "vwap_crossing_count": crosses,
            "raw_volume": latest_bar.volume,
            "rolling_volume_ratio": rolling_volume_ratio,
            "same_minute_expected_volume": self.volume_profile.expected_at(bars_1m, len(bars_1m) - 1),
            "same_minute_relative_volume": relative_volume,
            "same_minute_volume_zscore": volume_zscore,
            "vwap": latest_vwap,
            "vwap_sd": latest_sd,
            "vwap_plus_1sd": latest_vwap + latest_sd if latest_vwap is not None and latest_sd is not None else None,
            "vwap_minus_1sd": latest_vwap - latest_sd if latest_vwap is not None and latest_sd is not None else None,
            "vwap_plus_2sd": latest_vwap + 2 * latest_sd if latest_vwap is not None and latest_sd is not None else None,
            "vwap_minus_2sd": latest_vwap - 2 * latest_sd if latest_vwap is not None and latest_sd is not None else None,
            "distance_to_vwap": latest_bar.close - latest_vwap if latest_vwap is not None else None,
            "vwap_slope": vwap_slope,
            "vwap_slope_atr": vwap_slope / latest_atr if latest_atr else 0.0,
        }
        series: list[dict[str, Any]] = []
        start = max(0, len(bars_1m) - 160)
        for index in range(start, len(bars_1m)):
            bar = bars_1m[index]
            vw, sd = vwap_values[index]
            series.append(
                {
                    "time": bar.market_time.isoformat(),
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "ema9": tf1["ema9"][index],
                    "ema21": tf1["ema21"][index],
                    "vwap": vw,
                    "plus1": vw + sd if vw is not None and sd is not None else None,
                    "minus1": vw - sd if vw is not None and sd is not None else None,
                    "plus2": vw + 2 * sd if vw is not None and sd is not None else None,
                    "minus2": vw - 2 * sd if vw is not None and sd is not None else None,
                }
            )
        return {
            "ready": len(bars_1m) >= max(21, self.atr_window),
            "latest": latest,
            "series": series,
            "timeframes": {"1m": tf1, "5m": tf5, "15m": tf15},
            "bars_1m": bars_1m,
            "bars_5m": bars_5m,
            "bars_15m": bars_15m,
            "vwap_series": vwap_values,
        }

    def prepare_causal_frames(self, bars_1m: list[Bar]) -> list[tuple[int, dict[str, Any]]]:
        """Prepare every replay frame in linear time without future leakage.

        Indicator arrays are causal recurrences, so their value at an index is
        identical whether later array values have already been calculated or
        not. Completed 5m/15m bars are selected by close timestamp, preventing
        unfinished higher-timeframe candles from entering a frame.
        """
        bars = sorted((bar for bar in bars_1m if bar.is_final), key=lambda bar: bar.timestamp_utc)
        if len(bars) < 31:
            return []
        full = self.compute(bars)
        tf1 = full["timeframes"]["1m"]
        tf5 = full["timeframes"]["5m"]
        tf15 = full["timeframes"]["15m"]
        vwap_values = full["vwap_series"]
        close_times_5 = [bar.timestamp_utc for bar in tf5["bars"]]
        close_times_15 = [bar.timestamp_utc for bar in tf15["bars"]]

        minute_history: dict[tuple[int, int], list[tuple[Any, float]]] = defaultdict(list)
        volume_context: list[tuple[float | None, float | None, float | None]] = []
        for bar in bars:
            key = (bar.market_time.hour, bar.market_time.minute)
            prior = [
                volume for day, volume in minute_history[key]
                if day < bar.market_time.date()
            ][-self.volume_profile.lookback_sessions:]
            expected: float | None = None
            if prior:
                if self.volume_profile.estimator == "mean":
                    expected = statistics.fmean(prior)
                elif self.volume_profile.estimator == "ewm":
                    expected = prior[0]
                    for observation in prior[1:]:
                        expected = (
                            self.volume_profile.ewm_alpha * observation
                            + (1 - self.volume_profile.ewm_alpha) * expected
                        )
                else:
                    expected = statistics.median(prior)
            relative = bar.volume / expected if expected and expected > 0 else None
            deviation = statistics.stdev(prior) if len(prior) >= 2 else None
            zscore = (
                (bar.volume - expected) / deviation
                if expected is not None and deviation else (0.0 if expected is not None and len(prior) >= 2 else None)
            )
            volume_context.append((expected, relative, zscore))
            minute_history[key].append((bar.market_time.date(), bar.volume))

        def timeframe_view(timeframe: dict[str, Any], upto: int) -> dict[str, Any]:
            if upto < 0:
                return {"ema9": [], "ema21": [], "atr": [], "latest": {}}
            tf_bars = timeframe["bars"]
            ema9_values = timeframe["ema9"]
            ema21_values = timeframe["ema21"]
            atr_values = timeframe["atr"]
            start = max(0, upto - 19)
            latest_atr = atr_values[upto] or 1.0
            close = tf_bars[upto].close
            return {
                "ema9": ema9_values[max(0, upto - 9): upto + 1],
                "ema21": ema21_values[max(0, upto - 9): upto + 1],
                "atr": atr_values[max(0, upto - 9): upto + 1],
                "latest": {
                    "ema9": ema9_values[upto],
                    "ema21": ema21_values[upto],
                    "ema9_slope": ema9_values[upto] - ema9_values[upto - 3] if upto >= 3 else 0.0,
                    "ema21_slope": ema21_values[upto] - ema21_values[upto - 3] if upto >= 3 else 0.0,
                    "ema_separation": ema9_values[upto] - ema21_values[upto],
                    "price_distance_ema9": close - ema9_values[upto],
                    "price_distance_ema21": close - ema21_values[upto],
                    "price_distance_ema9_atr": (close - ema9_values[upto]) / latest_atr,
                    "price_distance_ema21_atr": (close - ema21_values[upto]) / latest_atr,
                    "ema_order": (
                        "BULL" if ema9_values[upto] > ema21_values[upto]
                        else "BEAR" if ema9_values[upto] < ema21_values[upto] else "FLAT"
                    ),
                    "recent_touches_ema9": sum(
                        bar.low <= value <= bar.high
                        for bar, value in zip(tf_bars[start: upto + 1], ema9_values[start: upto + 1])
                    ),
                    "recent_touches_ema21": sum(
                        bar.low <= value <= bar.high
                        for bar, value in zip(tf_bars[start: upto + 1], ema21_values[start: upto + 1])
                    ),
                },
            }

        contextual: list[dict[str, Any]] = []
        current_anchor: datetime | None = None
        session_high = session_low = 0.0
        rth_day = None
        rth_high: float | None = None
        rth_low: float | None = None
        for index, bar in enumerate(bars):
            anchor = trading_session_anchor(bar.market_time)
            if anchor != current_anchor:
                current_anchor = anchor
                session_high, session_low = bar.high, bar.low
            else:
                session_high = max(session_high, bar.high)
                session_low = min(session_low, bar.low)
            if bar.market_time.date() != rth_day:
                rth_day, rth_high, rth_low = bar.market_time.date(), None, None
            if time(9, 30) <= bar.market_time.timetz().replace(tzinfo=None) <= time(16, 0):
                rth_high = bar.high if rth_high is None else max(rth_high, bar.high)
                rth_low = bar.low if rth_low is None else min(rth_low, bar.low)
            recent = bars[max(0, index - 19): index + 1]
            recent_high = max(item.high for item in recent)
            recent_low = min(item.low for item in recent)
            recent_position = range_position(bar.close, recent_low, recent_high)
            session_position = range_position(bar.close, session_low, session_high)
            rth_position = (
                range_position(bar.close, rth_low, rth_high)
                if rth_low is not None and rth_high is not None else None
            )
            current_atr = tf1["atr"][index]
            prior_atrs = [value for value in tf1["atr"][max(0, index - 60):index] if value and value > 0]
            atr_reference = statistics.median(prior_atrs) if prior_atrs else None
            volatility_ratio = current_atr / atr_reference if atr_reference else None
            vw, sd = vwap_values[index]
            vwap_zscore = (bar.close - vw) / sd if vw is not None and sd is not None and sd > 0 else None
            contextual.append({
                "recent_range_position": recent_position,
                "recent_range_zone": range_zone(recent_position),
                "distance_to_recent_high_atr": (recent_high - bar.close) / current_atr if current_atr else None,
                "distance_to_recent_low_atr": (bar.close - recent_low) / current_atr if current_atr else None,
                "session_range_position": session_position,
                "session_range_zone": range_zone(session_position),
                "session_range_atr": (session_high - session_low) / current_atr if current_atr else None,
                "distance_to_session_high_atr": (session_high - bar.close) / current_atr if current_atr else None,
                "distance_to_session_low_atr": (bar.close - session_low) / current_atr if current_atr else None,
                "rth_range_position": rth_position,
                "rth_range_zone": range_zone(rth_position),
                "hour_bucket": f"H{bar.market_time.hour:02d}",
                "volatility_ratio": volatility_ratio,
                "volatility_regime": (
                    "LOW" if volatility_ratio is not None and volatility_ratio < 0.80
                    else "HIGH" if volatility_ratio is not None and volatility_ratio > 1.20
                    else "NORMAL" if volatility_ratio is not None else "UNAVAILABLE"
                ),
                "vwap_zscore": vwap_zscore,
                "vwap_zone": (
                    "UNAVAILABLE" if vw is None
                    else "BELOW_2SD" if vwap_zscore is not None and vwap_zscore <= -2
                    else "BELOW_1SD" if vwap_zscore is not None and vwap_zscore <= -1
                    else "BELOW_VWAP" if bar.close < vw
                    else "ABOVE_2SD" if vwap_zscore is not None and vwap_zscore >= 2
                    else "ABOVE_1SD" if vwap_zscore is not None and vwap_zscore >= 1
                    else "ABOVE_VWAP"
                ),
            })

        frames: list[tuple[int, dict[str, Any]]] = []
        for index in range(29, len(bars) - 1):
            bar = bars[index]
            window = bars[max(0, index - 19): index + 1]
            latest_atr = tf1["atr"][index]
            latest_vwap, latest_sd = vwap_values[index]
            geometry = candle_geometry(bar, bars[index - 1], latest_atr)
            closes = [item.close for item in window]
            log_returns = [
                math.log(right / left)
                for left, right in zip(closes, closes[1:]) if left > 0 and right > 0
            ]
            crosses = 0
            prior_side = 0
            side_streak = 0
            current_streak = 0
            for cursor in range(max(1, index - 19), index + 1):
                left_vwap = vwap_values[cursor - 1][0]
                right_vwap = vwap_values[cursor][0]
                if left_vwap is None or right_vwap is None:
                    continue
                left_side = 1 if bars[cursor - 1].close > left_vwap else -1
                right_side = 1 if bars[cursor].close > right_vwap else -1
                crosses += int(left_side != right_side)
                if right_side == prior_side:
                    current_streak += 1
                else:
                    current_streak = 1
                    prior_side = right_side
                side_streak = current_streak
            rolling_volumes = [item.volume for item in bars[max(0, index - 19):index]]
            expected, relative, zscore = volume_context[index]
            vwap_slope = (
                latest_vwap - vwap_values[index - 3][0]
                if index >= 3 and latest_vwap is not None and vwap_values[index - 3][0] is not None
                else 0.0
            )
            latest = {
                **geometry,
                **contextual[index],
                "atr": latest_atr,
                "realized_volatility": math.sqrt(sum(value * value for value in log_returns)) if log_returns else 0.0,
                "rolling_range": max(item.high for item in window) - min(item.low for item in window),
                "directional_efficiency": directional_efficiency(window),
                "direction_changes": direction_changes(window),
                "consecutive_closes_vwap_side": side_streak,
                "vwap_crossing_count": crosses,
                "raw_volume": bar.volume,
                "rolling_volume_ratio": bar.volume / statistics.fmean(rolling_volumes) if rolling_volumes else 1.0,
                "same_minute_expected_volume": expected,
                "same_minute_relative_volume": relative,
                "same_minute_volume_zscore": zscore,
                "vwap": latest_vwap,
                "vwap_sd": latest_sd,
                "vwap_plus_1sd": latest_vwap + latest_sd if latest_vwap is not None and latest_sd is not None else None,
                "vwap_minus_1sd": latest_vwap - latest_sd if latest_vwap is not None and latest_sd is not None else None,
                "vwap_plus_2sd": latest_vwap + 2 * latest_sd if latest_vwap is not None and latest_sd is not None else None,
                "vwap_minus_2sd": latest_vwap - 2 * latest_sd if latest_vwap is not None and latest_sd is not None else None,
                "distance_to_vwap": bar.close - latest_vwap if latest_vwap is not None else None,
                "vwap_slope": vwap_slope,
                "vwap_slope_atr": vwap_slope / latest_atr if latest_atr else 0.0,
            }
            upto5 = bisect_right(close_times_5, bar.timestamp_utc) - 1
            upto15 = bisect_right(close_times_15, bar.timestamp_utc) - 1
            series = []
            for cursor in range(max(0, index - 7), index + 1):
                item = bars[cursor]
                vw, sd = vwap_values[cursor]
                series.append({
                    "time": item.market_time.isoformat(), "open": item.open,
                    "high": item.high, "low": item.low, "close": item.close,
                    "volume": item.volume, "ema9": tf1["ema9"][cursor],
                    "ema21": tf1["ema21"][cursor], "vwap": vw,
                    "plus1": vw + sd if vw is not None and sd is not None else None,
                    "minus1": vw - sd if vw is not None and sd is not None else None,
                    "plus2": vw + 2 * sd if vw is not None and sd is not None else None,
                    "minus2": vw - 2 * sd if vw is not None and sd is not None else None,
                })
            frames.append((index, {
                "ready": True,
                "latest": latest,
                "series": series,
                "timeframes": {
                    "1m": timeframe_view(tf1, index),
                    "5m": timeframe_view(tf5, upto5),
                    "15m": timeframe_view(tf15, upto15),
                },
                "bars_1m": bars[max(0, index - 31): index + 1],
                "bars_5m": tf5["bars"][max(0, upto5 - 9): upto5 + 1] if upto5 >= 0 else [],
                "bars_15m": tf15["bars"][max(0, upto15 - 5): upto15 + 1] if upto15 >= 0 else [],
            }))
        return frames
