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
from atlas.errors import KillSwitchArmedError, SafetyError
from atlas.execution.broker import (
    BinanceSpotBroker,
    OrderAck,
    OrderRejection,
    OrderRequest,
    OrderRole,
)
from atlas.execution.idempotency import client_order_id
from atlas.execution.ledger import Ledger
from atlas.models import AuditEventType, OrderSide, PositionSide


class UnprotectedPositionError(SafetyError):
    """A position was opened and could not be protected, and reversal also failed."""


class EntryDidNotFillError(OrderRejection):
    """The exchange accepted the entry but filled none of it.

    Placing the protective stop anyway would sell an asset the account does not hold,
    which the exchange refuses for insufficient balance and which leaves a stop order
    attached to nothing.
    """


@dataclass(frozen=True)
class BracketResult:
    entry: OrderAck
    stop: OrderAck
    position_id: str
    entry_price: Decimal
    reversed_entry: bool = False


def _record_failure(ledger: Ledger, client_order_id_: str, error: Exception) -> None:
    """Record what a failed placement tells us about where the order actually is.

    The three cases are not the same, and treating them alike is how exposure goes
    unrecorded:

      - the kill switch refused it before any network call, so it certainly does not
        exist on the exchange
      - the exchange answered with a terminal status, so it certainly does not exist
      - retries were exhausted against a transport fault, so it *may* exist. That row
        stays SUBMITTED: reconciliation must go and ask, and marking it rejected here
        would delete the only local trace of a possible live order.
    """
    if isinstance(error, KillSwitchArmedError):
        ledger.update_order_status(client_order_id_, "NOT_SENT")
    elif isinstance(error, OrderRejection) and error.status is not None:
        ledger.update_order_status(client_order_id_, "REJECTED")


def _fill_price(ack: OrderAck, fallback: Decimal) -> Decimal:
    """The price the entry actually filled at, not the price that was signalled.

    Binance reports `cummulativeQuoteQty` alongside `executedQty` for a filled market
    order, and their ratio is the volume-weighted average the account actually paid.
    The signalled reference price is a bar close: using it would record a position at a
    price that never traded, and every realised return computed from it would be wrong
    by the slippage.
    """
    quote = ack.raw.get("cummulativeQuoteQty")
    if quote is not None and ack.executed_qty > 0:
        try:
            filled_value = Decimal(str(quote))
        except (ArithmeticError, ValueError, TypeError):
            return fallback
        if filled_value > 0:
            return filled_value / ack.executed_qty
    return fallback


def _entry_side(side: PositionSide) -> OrderSide:
    return OrderSide.BUY if side is PositionSide.LONG else OrderSide.SELL


def _exit_side(side: PositionSide) -> OrderSide:
    return OrderSide.SELL if side is PositionSide.LONG else OrderSide.BUY


def open_bracketed_position(
    broker: BinanceSpotBroker,
    audit: AuditLog,
    ledger: Ledger,
    *,
    strategy_id: str,
    signal_bar_time: datetime,
    symbol: str,
    side: PositionSide,
    quantity: Decimal,
    stop_price: Decimal,
    reference_price: Decimal,
    target_price: Decimal,
) -> BracketResult:
    """Place an entry and its protective stop as one operation (EXEC-02).

    Every order is written to the ledger *before* it is sent. An order that is accepted
    by the exchange but whose response never arrives would otherwise exist on the
    exchange with no local record, which reconciliation correctly reads as unrecorded
    exposure and arms the kill switch for (EXEC-05). Recording first costs a row that
    reconciliation will close; recording after would cost a halt.
    """
    entry_request = OrderRequest(
        client_order_id=client_order_id(
            strategy_id, signal_bar_time, _entry_side(side), str(OrderRole.ENTRY)
        ),
        symbol=symbol,
        side=_entry_side(side),
        role=OrderRole.ENTRY,
        quantity=quantity,
    )
    ledger.record_order(entry_request, strategy_id, broker.exchange_env)
    try:
        entry = broker.place(entry_request)
    except (OrderRejection, KillSwitchArmedError) as exc:
        _record_failure(ledger, entry_request.client_order_id, exc)
        raise
    ledger.update_order_status(entry_request.client_order_id, entry.status)

    if entry.executed_qty <= 0:
        # Nothing was bought. There is no position to protect and nothing to reverse.
        audit.append(
            AuditEventType.RISK_REJECTION,
            {
                "event": "entry_did_not_fill",
                "strategy_id": strategy_id,
                "symbol": symbol,
                "status": entry.status,
            },
            "execution",
        )
        raise EntryDidNotFillError(
            f"entry on {symbol} was accepted with status {entry.status} but filled "
            "nothing; no stop placed",
            retryable=False,
        )

    entry_price = _fill_price(entry, reference_price)
    position_id = ledger.open_position(
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        quantity=entry.executed_qty,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
    )
    ledger.link_order_to_position(entry_request.client_order_id, position_id)

    stop_request = OrderRequest(
        client_order_id=client_order_id(
            strategy_id, signal_bar_time, _exit_side(side), str(OrderRole.STOP)
        ),
        symbol=symbol,
        side=_exit_side(side),
        role=OrderRole.STOP,
        quantity=entry.executed_qty,
        price=stop_price,
        stop_price=stop_price,
    )
    ledger.record_order(stop_request, strategy_id, broker.exchange_env)
    ledger.link_order_to_position(stop_request.client_order_id, position_id)

    try:
        stop = broker.place(stop_request)
    except OrderRejection as stop_error:
        _record_failure(ledger, stop_request.client_order_id, stop_error)
        # The entry filled but is unprotected. Reverse it now.
        reversal = OrderRequest(
            client_order_id=client_order_id(
                strategy_id, signal_bar_time, _exit_side(side), "REVERSAL"
            ),
            symbol=symbol,
            side=_exit_side(side),
            role=OrderRole.ENTRY,  # market out
            quantity=entry.executed_qty,
        )
        ledger.record_order(reversal, strategy_id, broker.exchange_env)
        ledger.link_order_to_position(reversal.client_order_id, position_id)
        try:
            reversal_ack = broker.place(reversal)
        except OrderRejection as reversal_error:
            _record_failure(ledger, reversal.client_order_id, reversal_error)
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

        # The reversal closed the position. Record that, or the ledger will hold a
        # position the account no longer has - which blocks the symbol under RISK-08
        # and overstates deployed capital for as long as the row survives.
        ledger.update_order_status(reversal.client_order_id, reversal_ack.status)
        ledger.close_position(position_id, _fill_price(reversal_ack, entry_price))

        audit.append(
            AuditEventType.RISK_REJECTION,
            {
                "event": "entry_reversed",
                "strategy_id": strategy_id,
                "symbol": symbol,
                "position_id": position_id,
                "reason": str(stop_error),
            },
            "execution",
        )
        raise OrderRejection(
            f"stop placement failed on {symbol} ({stop_error}); entry reversed",
            retryable=False,
        ) from stop_error

    ledger.update_order_status(stop_request.client_order_id, stop.status)
    return BracketResult(entry=entry, stop=stop, position_id=position_id, entry_price=entry_price)
