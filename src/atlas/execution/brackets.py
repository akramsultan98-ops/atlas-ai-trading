"""Bracketed entry (EXEC-02, EXEC-08).

A position must never exist without its protective stop. If the stop fails to place
after the entry filled, the entry is immediately reversed — an unprotected position is
a worse outcome than a small realised loss on the reversal.

No trailing stop, no amendment after entry, no scaling (EXEC-08). Levels set at entry
stand until hit or the strategy retires.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from atlas.audit import AuditLog
from atlas.errors import SafetyError
from atlas.execution.broker import (
    BinanceSpotBroker,
    OrderAck,
    OrderRejection,
    OrderRequest,
    OrderRole,
)
from atlas.execution.idempotency import client_order_id
from atlas.models import AuditEventType, OrderSide, PositionSide


class UnprotectedPositionError(SafetyError):
    """A position was opened and could not be protected, and reversal also failed."""


@dataclass(frozen=True)
class BracketResult:
    entry: OrderAck
    stop: OrderAck
    reversed_entry: bool = False


def _entry_side(side: PositionSide) -> OrderSide:
    return OrderSide.BUY if side is PositionSide.LONG else OrderSide.SELL


def _exit_side(side: PositionSide) -> OrderSide:
    return OrderSide.SELL if side is PositionSide.LONG else OrderSide.BUY


def open_bracketed_position(
    broker: BinanceSpotBroker,
    audit: AuditLog,
    *,
    strategy_id: str,
    signal_bar_time: datetime,
    symbol: str,
    side: PositionSide,
    quantity: Decimal,
    stop_price: Decimal,
) -> BracketResult:
    """Place an entry and its protective stop as one operation (EXEC-02)."""
    entry_request = OrderRequest(
        client_order_id=client_order_id(
            strategy_id, signal_bar_time, _entry_side(side), str(OrderRole.ENTRY)
        ),
        symbol=symbol,
        side=_entry_side(side),
        role=OrderRole.ENTRY,
        quantity=quantity,
    )
    entry = broker.place(entry_request)

    stop_request = OrderRequest(
        client_order_id=client_order_id(
            strategy_id, signal_bar_time, _exit_side(side), str(OrderRole.STOP)
        ),
        symbol=symbol,
        side=_exit_side(side),
        role=OrderRole.STOP,
        quantity=quantity,
        price=stop_price,
        stop_price=stop_price,
    )

    try:
        stop = broker.place(stop_request)
    except OrderRejection as stop_error:
        # The entry filled but is unprotected. Reverse it now.
        reversal = OrderRequest(
            client_order_id=client_order_id(
                strategy_id, signal_bar_time, _exit_side(side), "REVERSAL"
            ),
            symbol=symbol,
            side=_exit_side(side),
            role=OrderRole.ENTRY,  # market out
            quantity=quantity,
        )
        try:
            broker.place(reversal)
        except OrderRejection as reversal_error:
            audit.append(
                AuditEventType.RISK_REJECTION,
                {
                    "event": "unprotected_position",
                    "strategy_id": strategy_id,
                    "symbol": symbol,
                    "stop_error": str(stop_error),
                    "reversal_error": str(reversal_error),
                },
                "execution",
            )
            raise UnprotectedPositionError(
                f"position on {symbol} is open, its stop was rejected "
                f"({stop_error}) and the reversal also failed ({reversal_error}); "
                "manual intervention required"
            ) from reversal_error

        audit.append(
            AuditEventType.RISK_REJECTION,
            {
                "event": "entry_reversed",
                "strategy_id": strategy_id,
                "symbol": symbol,
                "reason": str(stop_error),
            },
            "execution",
        )
        raise OrderRejection(
            f"stop placement failed on {symbol} ({stop_error}); entry reversed",
            retryable=False,
        ) from stop_error

    return BracketResult(entry=entry, stop=stop)
