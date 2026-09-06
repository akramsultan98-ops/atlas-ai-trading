"""Domain models.

Money and quantity are `Decimal` everywhere and `float` is rejected at construction
(ADR-003). Binary floating point cannot represent decimal fractions exactly, and
accumulated error in sizing or PnL is a correctness bug with financial consequence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _reject_float(value: Any) -> Any:
    """Reject `float` (and `bool`) for monetary values.

    `bool` is a subclass of `int`, so it would otherwise coerce silently.
    """
    # ValueError, not TypeError: pydantic wraps ValueError into ValidationError, so a
    # rejected float surfaces the same way as every other validation failure. The
    # rejection itself is unconditional either way.
    if isinstance(value, bool):
        raise ValueError("bool is not a valid monetary value")
    if isinstance(value, float):
        raise ValueError(
            f"float is not permitted for monetary values (got {value!r}); "
            "use Decimal or str to avoid binary floating-point error"
        )
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"not a valid decimal: {value!r}") from exc
    if isinstance(value, int):
        return Decimal(value)
    return value


Money = Annotated[Decimal, BeforeValidator(_reject_float)]
"""A monetary or quantity value. Never a float."""


def utcnow() -> datetime:
    """Timezone-aware UTC now. All timestamps in ATLAS are UTC."""
    return datetime.now(UTC)


class StrictModel(BaseModel):
    """Base model: unknown fields are an error, values are immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=False)


# --------------------------------------------------------------------------- enums


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TESTING = "testing"
    PRODUCTION = "production"


class ExchangeEnv(StrEnum):
    """Which Binance environment. `LIVE` additionally requires the final audit gate."""

    TESTNET = "testnet"
    LIVE = "live"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class PositionSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class StrategyStatus(StrEnum):
    """Lifecycle. Transitions toward RETIRED are one-way (MON-06)."""

    CANDIDATE = "CANDIDATE"
    VERIFIED = "VERIFIED"
    INCUBATING = "INCUBATING"
    PROMOTED = "PROMOTED"
    LIVE = "LIVE"
    SUSPENDED = "SUSPENDED"
    RETIRED = "RETIRED"
    REJECTED = "REJECTED"


class KillSwitchState(StrEnum):
    DISARMED = "DISARMED"
    ARMED = "ARMED"


class KillSwitchTrigger(StrEnum):
    """Why the kill switch armed (KILL-02)."""

    MANUAL = "MANUAL"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    MAX_ACCOUNT_DD = "MAX_ACCOUNT_DD"
    RECONCILIATION_FAILURE = "RECONCILIATION_FAILURE"
    DATA_STALENESS = "DATA_STALENESS"
    API_ERROR_RATE = "API_ERROR_RATE"
    STATE_DISAGREEMENT = "STATE_DISAGREEMENT"
    STATE_UNREADABLE = "STATE_UNREADABLE"


class AuditEventType(StrEnum):
    GENESIS = "GENESIS"
    KILL_SWITCH_ARMED = "KILL_SWITCH_ARMED"
    KILL_SWITCH_DISARMED = "KILL_SWITCH_DISARMED"
    STRATEGY_CREATED = "STRATEGY_CREATED"
    STRATEGY_STATUS_CHANGED = "STRATEGY_STATUS_CHANGED"
    STRATEGY_RETIRED = "STRATEGY_RETIRED"
    BACKTEST_COMPLETED = "BACKTEST_COMPLETED"
    VERIFICATION_COMPLETED = "VERIFICATION_COMPLETED"
    SELECTION_DECISION = "SELECTION_DECISION"
    PROMOTION_APPROVED = "PROMOTION_APPROVED"
    ORDER_INTENT = "ORDER_INTENT"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_RESULT = "ORDER_RESULT"
    FILL_RECORDED = "FILL_RECORDED"
    RECONCILIATION = "RECONCILIATION"
    RISK_REJECTION = "RISK_REJECTION"
    AI_ACTION = "AI_ACTION"
    SYSTEM = "SYSTEM"


# --------------------------------------------------------------------------- records


class KillSwitchRecord(StrictModel):
    """Persisted kill-switch state. Written to both a file and the database (KILL-04)."""

    state: KillSwitchState
    trigger: KillSwitchTrigger | None = None
    reason: str = Field(min_length=1, max_length=500)
    actor: str = Field(min_length=1, max_length=100)
    at: datetime

    def is_armed(self) -> bool:
        return self.state is KillSwitchState.ARMED


class AuditEvent(StrictModel):
    """One link in the hash chain (ADR-005).

    `event_hash` covers `prev_hash`, so modifying or removing any historical event
    breaks every subsequent link.
    """

    seq: int = Field(ge=0)
    event_type: AuditEventType
    payload: dict[str, Any]
    actor: str = Field(min_length=1, max_length=100)
    at: datetime
    prev_hash: str = Field(min_length=64, max_length=64)
    event_hash: str = Field(min_length=64, max_length=64)
