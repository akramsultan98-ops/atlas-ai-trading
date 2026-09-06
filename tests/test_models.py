"""ADR-003: money is Decimal, never float."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from atlas.models import (
    AuditEventType,
    KillSwitchRecord,
    KillSwitchState,
    KillSwitchTrigger,
    Money,
    utcnow,
)


class _Wallet(BaseModel):
    balance: Money


def test_money_accepts_decimal() -> None:
    assert _Wallet(balance=Decimal("100.25")).balance == Decimal("100.25")


def test_money_accepts_str_and_int() -> None:
    assert _Wallet(balance="0.1").balance == Decimal("0.1")
    assert _Wallet(balance=100).balance == Decimal(100)


def test_money_rejects_float() -> None:
    """ADR-003. 0.1 + 0.2 != 0.3 in binary floating point; sizing must not inherit that."""
    with pytest.raises(ValidationError, match="float is not permitted"):
        _Wallet(balance=100.25)


def test_money_rejects_bool() -> None:
    """bool subclasses int and would otherwise coerce to 0 or 1 silently."""
    with pytest.raises(ValidationError, match="bool is not a valid monetary value"):
        _Wallet(balance=True)


def test_money_rejects_garbage_string() -> None:
    with pytest.raises(ValidationError):
        _Wallet(balance="not-a-number")


def test_money_preserves_exact_decimal_arithmetic() -> None:
    total = _Wallet(balance="0.1").balance + _Wallet(balance="0.2").balance
    assert total == Decimal("0.3")


def test_utcnow_is_timezone_aware() -> None:
    assert utcnow().tzinfo is UTC


def test_killswitch_record_requires_reason() -> None:
    with pytest.raises(ValidationError):
        KillSwitchRecord(
            state=KillSwitchState.ARMED,
            trigger=KillSwitchTrigger.MANUAL,
            reason="",
            actor="operator",
            at=datetime.now(UTC),
        )


def test_models_are_frozen() -> None:
    record = KillSwitchRecord(
        state=KillSwitchState.ARMED,
        trigger=KillSwitchTrigger.MANUAL,
        reason="test",
        actor="operator",
        at=utcnow(),
    )
    with pytest.raises(ValidationError):
        record.state = KillSwitchState.DISARMED  # type: ignore[misc]


def test_all_kill_switch_triggers_are_documented() -> None:
    """KILL-02: every trigger in the spec's list exists as an enum member."""
    expected = {
        "MANUAL",
        "DAILY_LOSS_LIMIT",
        "MAX_ACCOUNT_DD",
        "RECONCILIATION_FAILURE",
        "DATA_STALENESS",
        "API_ERROR_RATE",
        "STATE_DISAGREEMENT",
        "STATE_UNREADABLE",
    }
    assert {t.value for t in KillSwitchTrigger} == expected


def test_audit_event_types_cover_order_lifecycle() -> None:
    """EXEC-10: intent, transmission and result are each distinguishable."""
    for name in ("ORDER_INTENT", "ORDER_SUBMITTED", "ORDER_RESULT", "FILL_RECORDED"):
        assert hasattr(AuditEventType, name)
