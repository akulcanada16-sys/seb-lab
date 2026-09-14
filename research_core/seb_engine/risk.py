from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class SizingResult:
    quantity: int
    risk_per_contract: float
    total_risk: float
    blocked: bool
    reason: str
    reason_code: str = "OK"
    account_risk_cap: float | None = None


@dataclass(frozen=True, slots=True)
class ExecutionRiskAssessment:
    """Deterministic execution-time bracket, sizing, and account-safety result."""

    quantity: int
    risk_per_contract: float
    total_risk: float
    reward_risk_ratio: float | None
    blocked: bool
    reason_code: str
    reason: str


class RiskGuard:
    def __init__(self, config: dict[str, Any], account_state_path: str | Path | None = None):
        self.config = config
        self.account_state_path = Path(account_state_path) if account_state_path else None

    def account_state(self) -> dict[str, Any]:
        if not self.account_state_path or not self.account_state_path.exists():
            return {}
        try:
            return json.loads(self.account_state_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {}

    def account_status(self) -> dict[str, str]:
        account = self.account_state()
        if not self.account_state_path:
            return {"status": "NOT_CONFIGURED", "source": "none"}
        if not account:
            return {"status": "UNAVAILABLE", "source": str(self.account_state_path)}
        if account.get("account_locked") or account.get("daily_circuit_breaker_locked"):
            return {"status": "LOCKED_READ_ONLY", "source": str(self.account_state_path)}
        declared = str(account.get("account_status") or "CONNECTED")
        return {"status": f"{declared}_READ_ONLY", "source": str(self.account_state_path)}

    def _account_risk_cap(self, account: dict[str, Any]) -> float | None:
        def remaining_capacity(value: Any) -> float:
            """Treat an explicitly supplied but unusable limit as no capacity."""
            try:
                number = float(value)
            except (TypeError, ValueError):
                return 0.0
            return max(0.0, number) if math.isfinite(number) else 0.0

        caps: list[float] = []
        for key in self.config.get("account_hard_limit_fields", []):
            value = account.get(key)
            if value is not None:
                caps.append(remaining_capacity(value))
        if account.get("current_balance") is not None and account.get("trailing_floor") is not None:
            try:
                trailing_capacity = float(account["current_balance"]) - float(account["trailing_floor"])
            except (TypeError, ValueError):
                trailing_capacity = 0.0
            caps.append(remaining_capacity(trailing_capacity))
        daily_remaining = account.get("daily_loss_remaining")
        if daily_remaining is not None:
            caps.append(remaining_capacity(daily_remaining))
        elif account.get("daily_loss_limit") is not None:
            try:
                limit = abs(float(account["daily_loss_limit"]))
                daily_capacity = limit + min(0.0, float(account.get("daily_pnl", 0.0)))
            except (TypeError, ValueError):
                daily_capacity = 0.0
            caps.append(remaining_capacity(daily_capacity))
        return min(caps) if caps else None

    def size(
        self,
        entry: float,
        stop: float,
        instrument: str = "MNQ",
        *,
        apply_account_constraints: bool = True,
    ) -> SizingResult:
        risk = abs(entry - stop)
        if not math.isfinite(risk) or risk <= 0:
            return SizingResult(
                0, 0.0, 0.0, True, "Stop distance must be positive", "INVALID_STOP"
            )
        point_value = float(self.config.get("point_values", {}).get(instrument, 2.0 if instrument == "MNQ" else 20.0))
        tick_size = float(self.config.get("tick_size", 0.25))
        commission = float(self.config.get("estimated_round_trip_commission", 0.0))
        slippage_ticks = float(self.config.get("slippage_ticks_each_side", 0.0))
        slippage_cost = slippage_ticks * 2.0 * tick_size * point_value
        per_contract = risk * point_value + commission + slippage_cost
        mode = self.config.get("sizing_mode", "FIXED_RISK_DOLLARS")
        if mode == "FIXED_CONTRACTS":
            quantity = int(self.config.get("fixed_contracts", 1))
        else:
            allowed = float(self.config.get("fixed_risk_dollars", 100.0))
            quantity = math.floor(allowed / per_contract)
        quantity = min(quantity, int(self.config.get("maximum_contracts", 1)))
        account_cap: float | None = None
        if apply_account_constraints:
            account = self.account_state()
            if account.get("account_locked"):
                return SizingResult(
                    0, per_contract, 0.0, True,
                    "Read-only account state reports account_locked",
                    "ACCOUNT_LOCKED",
                )
            if account.get("daily_circuit_breaker_locked"):
                return SizingResult(
                    0, per_contract, 0.0, True,
                    "Read-only account state reports daily_circuit_breaker_locked",
                    "DAILY_CIRCUIT_BREAKER_LOCKED",
                )
            account_cap = self._account_risk_cap(account)
            if account_cap is not None:
                quantity = min(quantity, math.floor(account_cap / per_contract))
        if quantity < 1:
            return SizingResult(
                0, per_contract, 0.0, True,
                "Minimum one-contract risk exceeds the global/account limit",
                "ACCOUNT_RISK_CAP" if account_cap is not None else "GLOBAL_RISK_CAP",
                account_cap,
            )
        return SizingResult(
            quantity, per_contract, quantity * per_contract, False,
            "Global sizing method and read-only account caps applied",
            "OK",
            account_cap,
        )

    def assess_execution(
        self,
        *,
        entry: float,
        stop: float,
        target: float,
        direction: str,
        requested_quantity: int,
        instrument: str = "MNQ",
        minimum_reward_risk: float | None = None,
    ) -> ExecutionRiskAssessment:
        """Revalidate a planned order at its actual fill price.

        Quantity can only stay the same or decrease. This method is shared by
        signal-time paper preflight and next-open fill validation so external
        account locks and caps cannot be bypassed between those two moments.
        """
        values = (entry, stop, target)
        if not all(math.isfinite(float(value)) for value in values):
            return ExecutionRiskAssessment(
                0, 0.0, 0.0, None, True, "INVALID_BRACKET",
                "Entry, stop, and target must all be finite",
            )
        normalized_direction = str(getattr(direction, "value", direction)).upper()
        valid_bracket = (
            normalized_direction == "LONG" and stop < entry < target
        ) or (
            normalized_direction == "SHORT" and target < entry < stop
        )
        if not valid_bracket:
            return ExecutionRiskAssessment(
                0, 0.0, 0.0, None, True, "INVALID_BRACKET",
                "Actual next-bar open is outside the planned stop/target bracket",
            )
        tick_size = float(self.config.get("tick_size", 0.25))
        if tick_size <= 0 or any(
            abs(float(value) / tick_size - round(float(value) / tick_size)) > 1e-7
            for value in values
        ):
            return ExecutionRiskAssessment(
                0, 0.0, 0.0, None, True, "TICK_SIZE_COMPLIANCE",
                "Actual entry, stop, and target must align to the configured tick",
            )
        stop_distance = abs(entry - stop)
        reward_distance = abs(target - entry)
        reward_risk = reward_distance / stop_distance if stop_distance > 0 else None
        minimum = float(
            self.config.get("minimum_target_room_r", 1.0)
            if minimum_reward_risk is None else minimum_reward_risk
        )
        if reward_risk is None or reward_risk + 1e-9 < minimum:
            return ExecutionRiskAssessment(
                0, 0.0, 0.0, reward_risk, True, "MINIMUM_REWARD_TO_RISK",
                f"Actual next-bar open leaves less than {minimum:.2f}R of target room",
            )
        if int(requested_quantity) < 1:
            return ExecutionRiskAssessment(
                0, 0.0, 0.0, reward_risk, True, "INVALID_POSITION_SIZE",
                "Planned quantity must be at least one contract",
            )
        sizing = self.size(entry, stop, instrument, apply_account_constraints=True)
        if sizing.blocked:
            return ExecutionRiskAssessment(
                0, sizing.risk_per_contract, 0.0, reward_risk, True,
                sizing.reason_code, sizing.reason,
            )
        quantity = min(int(requested_quantity), int(sizing.quantity))
        fixed_risk = float(self.config.get("fixed_risk_dollars", math.inf))
        if math.isfinite(fixed_risk):
            quantity = min(quantity, math.floor(fixed_risk / sizing.risk_per_contract))
        if quantity < 1:
            return ExecutionRiskAssessment(
                0, sizing.risk_per_contract, 0.0, reward_risk, True,
                "GLOBAL_RISK_CAP",
                "Actual next-bar stop risk exceeds the per-trade risk cap for one contract",
            )
        total_risk = sizing.risk_per_contract * quantity
        return ExecutionRiskAssessment(
            quantity, sizing.risk_per_contract, total_risk, reward_risk, False, "OK",
            "Actual fill bracket, risk, and read-only account limits passed",
        )
