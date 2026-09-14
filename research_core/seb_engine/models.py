from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class StrategyState(StrEnum):
    IDLE = "IDLE"
    CONTEXT_FOUND = "CONTEXT_FOUND"
    APPROACHING_LEVEL = "APPROACHING_LEVEL"
    DEVELOPING = "DEVELOPING"
    READY = "READY"
    EXPIRED = "EXPIRED"
    BLOCKED = "BLOCKED"
    STALE_DATA = "STALE_DATA"
    IN_POSITION = "IN_POSITION"
    COMPLETED = "COMPLETED"
    COOLDOWN = "COOLDOWN"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


class EvidenceStatus(StrEnum):
    DEFINITIONAL = "DEFINITIONAL"
    EVIDENCE_SUPPORTED = "EVIDENCE_SUPPORTED"
    HYPOTHESIS_DEFAULT = "HYPOTHESIS_DEFAULT"
    PROMOTED = "PROMOTED"


@dataclass(frozen=True, slots=True)
class Bar:
    timestamp_utc: datetime
    market_time: datetime
    symbol: str
    timeframe_minutes: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str
    is_final: bool = True

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["timestamp_utc"] = self.timestamp_utc.isoformat()
        data["market_time"] = self.market_time.isoformat()
        return data


@dataclass(frozen=True, slots=True)
class Quote:
    timestamp_utc: datetime
    symbol: str
    last: float | None
    bid: float | None
    ask: float | None
    last_volume: float | None
    source: str


@dataclass(slots=True)
class DataQuality:
    source: str
    active_symbol: str
    source_type: str = "UNKNOWN"
    last_event_timestamp: datetime | None = None
    feed_age_seconds: float | None = None
    last_finalized_bar_timestamp: datetime | None = None
    finalized_bar_age_seconds: float | None = None
    missing_bars: int = 0
    duplicate_events: int = 0
    out_of_order_events: int = 0
    rollover_state: str = "CURRENT"
    vwap_data_basis: str = "1m typical price x contract volume"
    warmup_complete: bool = False
    is_stale: bool = False
    recorder_running: bool | None = None
    recorder_version: str | None = None
    dropped_records: int = 0
    write_errors: int = 0
    queue_depth: int | None = None
    blocking_reasons: list[str] = field(default_factory=list)
    advisory_reasons: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return self.warmup_complete and not self.is_stale and not self.blocking_reasons

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["last_event_timestamp"] = (
            self.last_event_timestamp.isoformat() if self.last_event_timestamp else None
        )
        data["last_finalized_bar_timestamp"] = (
            self.last_finalized_bar_timestamp.isoformat()
            if self.last_finalized_bar_timestamp else None
        )
        data["actionable"] = self.actionable
        return data


@dataclass(frozen=True, slots=True)
class Level:
    level_id: str
    level_type: str
    price: float
    created_time: datetime
    last_updated_time: datetime
    source_timeframe: str
    validity_state: str = "ACTIVE"
    tests: int = 0
    dynamic: bool = False
    direction: str = "BOTH"

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_time"] = self.created_time.isoformat()
        data["last_updated_time"] = self.last_updated_time.isoformat()
        return data


@dataclass(frozen=True, slots=True)
class Condition:
    key: str
    label: str
    satisfied: bool
    value: Any = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TradeTicket:
    strategy_name: str
    strategy_version: str
    symbol: str
    direction: Direction
    timestamp: datetime
    entry: float
    stop: float
    target: float
    quantity: int
    expiration_time: datetime
    reference_level: dict[str, Any]
    trigger_description: str
    rule_checklist: list[Condition]
    data_source: str
    freshness_state: str
    signal_id: str = field(default_factory=lambda: str(uuid4()))

    @property
    def stop_distance(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def target_distance(self) -> float:
        return abs(self.target - self.entry)

    @property
    def reward_risk_ratio(self) -> float:
        return self.target_distance / self.stop_distance if self.stop_distance else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "symbol": self.symbol,
            "direction": self.direction.value,
            "timestamp": self.timestamp.isoformat(),
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "quantity": self.quantity,
            "stop_distance": self.stop_distance,
            "target_distance": self.target_distance,
            "reward_risk_ratio": self.reward_risk_ratio,
            "expiration_time": self.expiration_time.isoformat(),
            "reference_level": self.reference_level,
            "trigger_description": self.trigger_description,
            "rule_checklist": [condition.as_dict() for condition in self.rule_checklist],
            "data_source": self.data_source,
            "freshness_state": self.freshness_state,
        }


@dataclass(slots=True)
class StrategyEvaluation:
    strategy_id: str
    strategy_name: str
    strategy_version: str
    state: StrategyState
    direction: Direction
    timestamp: datetime
    conditions: list[Condition]
    missing_conditions: list[str]
    invalidation: str
    reference_level: Level | None
    relevant_features: dict[str, Any]
    explanation: str
    ticket: TradeTicket | None = None
    paper_candidate: TradeTicket | None = None

    @property
    def completion_ratio(self) -> float:
        if not self.conditions:
            return 0.0
        return sum(condition.satisfied for condition in self.conditions) / len(self.conditions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "state": self.state.value,
            "direction": self.direction.value,
            "timestamp": self.timestamp.isoformat(),
            "conditions": [condition.as_dict() for condition in self.conditions],
            "complete_conditions": [c.label for c in self.conditions if c.satisfied],
            "missing_conditions": self.missing_conditions,
            "invalidation": self.invalidation,
            "reference_level": self.reference_level.as_dict() if self.reference_level else None,
            "relevant_features": self.relevant_features,
            "explanation": self.explanation,
            "ticket": self.ticket.as_dict() if self.ticket else None,
            "paper_candidate": self.paper_candidate.as_dict() if self.paper_candidate else None,
            "completion_ratio": self.completion_ratio,
            "completion_percent": round(self.completion_ratio * 100),
        }
