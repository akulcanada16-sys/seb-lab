from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TrailingStopUpdate:
    active: bool
    activated: bool
    prior_stop: float
    new_stop: float
    reference_price: float
    distance_points: float
    entry_atr: float | None
    initial_risk_points: float
    mfe_r: float
    reason: str
    required_activation_r: float = 0.0

    @property
    def changed(self) -> bool:
        return not math.isclose(self.prior_stop, self.new_stop, abs_tol=1e-12)


class VolatilityTrailingStop:
    """Completed-bar, volatility-aware trailing stop for local paper positions.

    The initial strategy stop remains authoritative until the position reaches
    the configured MFE activation threshold. Once active, the stop ratchets
    from the most favorable completed-bar extreme. A new level only applies to
    later bars; callers must always evaluate the current bar against the prior
    stop before applying an update returned here.
    """

    def __init__(self, config: dict[str, Any], tick_size: float):
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", False))
        self.policy_version = str(
            self.config.get("policy_version", "atr-chandelier-paper-v1")
        )
        self.eligible_strategies = {
            str(item) for item in self.config.get("eligible_strategies", [])
        }
        self.activation_r = float(self.config.get("activation_mfe_r", 1.0))
        self.minimum_bars_held = max(1, int(self.config.get("minimum_bars_held", 1)))
        self.atr_multiple = float(self.config.get("atr_multiple", 1.5))
        self.minimum_distance_r = float(self.config.get("minimum_distance_r", 1.0))
        self.fallback_distance_r = float(self.config.get("fallback_distance_r", 1.25))
        self.lock_in_r = float(self.config.get("lock_in_r", 0.10))
        self.target_handoff = bool(self.config.get("target_handoff", True))
        # A chandelier trail cannot act as a stop until the move exceeds the trail
        # distance. Arming earlier leaves the raw trail below entry, so the lock-in floor
        # silently becomes the exit. Measured 2026-08-07: the median trail distance was
        # 1.92R against a 1.0R activation, so for 60% of armed trades the +0.10R floor WAS
        # the stop and multi-R winners banked ~+0.09R. Set false to restore v1 behaviour.
        self.require_trail_clearance = bool(
            self.config.get("activation_requires_trail_clearance", True)
        )
        self.tick_size = float(tick_size)
        self._validate()

    def _validate(self) -> None:
        if self.activation_r <= 0:
            raise ValueError("trailing_stop.activation_mfe_r must be positive")
        if self.atr_multiple <= 0:
            raise ValueError("trailing_stop.atr_multiple must be positive")
        if self.minimum_distance_r <= 0 or self.fallback_distance_r <= 0:
            raise ValueError("trailing-stop distances must be positive")
        if not 0 <= self.lock_in_r < self.activation_r:
            raise ValueError("trailing_stop.lock_in_r must be below activation_mfe_r")
        if self.tick_size <= 0:
            raise ValueError("tick_size must be positive")

    def applies_to(self, strategy_id: str) -> bool:
        return self.enabled and (
            not self.eligible_strategies or strategy_id in self.eligible_strategies
        )

    @staticmethod
    def state(context: dict[str, Any]) -> dict[str, Any]:
        value = context.get("trailing_stop")
        return dict(value) if isinstance(value, dict) else {}

    def target_is_milestone(self, strategy_id: str, context: dict[str, Any]) -> bool:
        return (
            self.applies_to(strategy_id)
            and self.target_handoff
            and bool(self.state(context).get("active"))
        )

    def update(
        self,
        *,
        strategy_id: str,
        direction_sign: int,
        entry_price: float,
        initial_stop: float,
        current_stop: float,
        favorable_high: float,
        favorable_low: float,
        bars_held: int,
        context: dict[str, Any],
    ) -> TrailingStopUpdate | None:
        if not self.applies_to(strategy_id):
            return None
        initial_risk = abs(float(entry_price) - float(initial_stop))
        if initial_risk <= 0:
            return None
        reference = float(favorable_high if direction_sign == 1 else favorable_low)
        mfe_points = (
            reference - float(entry_price)
            if direction_sign == 1
            else float(entry_price) - reference
        )
        mfe_r = max(0.0, mfe_points / initial_risk)
        prior_state = self.state(context)
        was_active = bool(prior_state.get("active"))

        entry_atr = self._entry_atr(context)
        if entry_atr is not None:
            distance = max(
                self.atr_multiple * entry_atr,
                self.minimum_distance_r * initial_risk,
            )
        else:
            distance = self.fallback_distance_r * initial_risk

        # Require the move to clear the trail distance plus the lock-in floor before the
        # trail arms, so the chandelier is genuinely above the floor the moment it engages.
        # Until then the strategy's own stop and target stay in force. This scales per
        # trade: a quiet setup arms near 1.1R, a volatile one waits much longer.
        required_activation_r = self.activation_r
        if self.require_trail_clearance:
            required_activation_r = max(
                required_activation_r, distance / initial_risk + self.lock_in_r
            )

        active = was_active or (
            bars_held >= self.minimum_bars_held and mfe_r >= required_activation_r
        )
        if not active:
            return TrailingStopUpdate(
                active=False,
                activated=False,
                prior_stop=float(current_stop),
                new_stop=float(current_stop),
                reference_price=reference,
                distance_points=0.0,
                entry_atr=entry_atr,
                initial_risk_points=initial_risk,
                mfe_r=mfe_r,
                reason="WAITING_FOR_MFE_ACTIVATION",
                required_activation_r=required_activation_r,
            )

        raw_chandelier = reference - direction_sign * distance
        profit_floor = float(entry_price) + direction_sign * self.lock_in_r * initial_risk
        if direction_sign == 1:
            candidate = max(raw_chandelier, profit_floor)
            candidate = self._round_down(candidate)
            new_stop = max(float(current_stop), candidate)
        else:
            candidate = min(raw_chandelier, profit_floor)
            candidate = self._round_up(candidate)
            new_stop = min(float(current_stop), candidate)

        return TrailingStopUpdate(
            active=True,
            activated=not was_active,
            prior_stop=float(current_stop),
            new_stop=new_stop,
            reference_price=reference,
            distance_points=distance,
            entry_atr=entry_atr,
            initial_risk_points=initial_risk,
            mfe_r=mfe_r,
            reason="ACTIVATED_AT_MFE" if not was_active else "RATCHETED_FROM_FAVORABLE_EXTREME",
            required_activation_r=required_activation_r,
        )

    @staticmethod
    def _entry_atr(context: dict[str, Any]) -> float | None:
        features = context.get("features")
        raw = features.get("atr") if isinstance(features, dict) else None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value > 0 else None

    def _round_down(self, value: float) -> float:
        return math.floor((value + 1e-12) / self.tick_size) * self.tick_size

    def _round_up(self, value: float) -> float:
        return math.ceil((value - 1e-12) / self.tick_size) * self.tick_size
