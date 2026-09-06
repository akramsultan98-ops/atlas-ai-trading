"""The position round trip: entry -> ledger -> exit fill -> closed (EXEC-02, EXEC-04).

Before this module, `Ledger.open_position` and `Ledger.close_position` were never
called by any production code path. The ledger was a fully tested, entirely unfed data
structure, and the consequences all landed on the first real trade:

  - `open_symbols` was always empty, so RISK-08 never bound and a strategy could
    re-enter the same symbol every tick
  - deployed capital and marked equity read zero however much was actually at risk
  - `realised_returns` stayed empty, so the monitor could never retire anything
  - every fill from `myTrades` was an orphan, because no order was on file
  - reconciliation found ATLAS orders on the exchange with no local record and armed
    the kill switch -- on the first restart after any order existed

Each test here fails against the previous code.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tests.test_execution import StubTransport, _strategy, ack

from atlas.audit import AuditLog
from atlas.db.engine import Database
from atlas.execution.brackets import (
    EntryDidNotFillError,
    UnprotectedPositionError,
    open_bracketed_position,
)
from atlas.execution.broker import BinanceSpotBroker, OrderRejection, OrderRole
from atlas.execution.idempotency import client_order_id
from atlas.execution.ingest import FillIngestor
from atlas.execution.ledger import Ledger
from atlas.execution.reconcile import Reconciler
from atlas.killswitch import KillSwitch
from atlas.models import ExchangeEnv, OrderSide, PositionSide

D = Decimal
BAR_TIME = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def entry_id(strategy_id: str) -> str:
    """The deterministic id the ledger stores (EXEC-03), not what a stub ack echoes."""
    return client_order_id(strategy_id, BAR_TIME, OrderSide.BUY, str(OrderRole.ENTRY))


def stop_id(strategy_id: str) -> str:
    return client_order_id(strategy_id, BAR_TIME, OrderSide.SELL, str(OrderRole.STOP))


def _broker(audit: AuditLog, killswitch: KillSwitch, transport: StubTransport) -> BinanceSpotBroker:
    return BinanceSpotBroker(
        "k", "s", killswitch, audit, exchange_env=ExchangeEnv.TESTNET, transport=transport
    )


def _entry_ack(qty: str = "0.001", quote: str = "30.15", status: str = "FILLED") -> dict[str, str]:
    """A market entry the exchange filled, priced the way Binance reports it."""
    return {
        "clientOrderId": "entry",
        "orderId": "111",
        "symbol": "BTCUSDT",
        "status": status,
        "executedQty": qty,
        "cummulativeQuoteQty": quote,
    }


def _bracket(
    db: Database,
    audit: AuditLog,
    killswitch: KillSwitch,
    *,
    entry: dict[str, str] | None = None,
    stop: object = None,
) -> tuple[object, Ledger, StubTransport, str]:
    killswitch.initialise()
    strategy_id = _strategy(db)
    transport = StubTransport(entry or _entry_ack(), stop or ack("stop", status="NEW"))
    ledger = Ledger(db, audit)
    result = open_bracketed_position(
        _broker(audit, killswitch, transport),
        audit,
        ledger,
        strategy_id=strategy_id,
        signal_bar_time=BAR_TIME,
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        quantity=D("0.001"),
        stop_price=D("29000"),
        reference_price=D("30000"),
        target_price=D("32000"),
    )
    return result, ledger, transport, strategy_id


# ------------------------------------------------------------------- opening


def test_a_bracketed_entry_opens_a_ledger_position(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    result, ledger, _, strategy_id = _bracket(db, audit, killswitch)

    positions = ledger.open_positions()
    assert len(positions) == 1
    position = positions[0]
    assert position.symbol == "BTCUSDT"
    assert position.quantity == D("0.001")
    assert position.stop_price == D("29000")
    assert position.target_price == D("32000")
    assert position.strategy_id == strategy_id
    assert position.id == result.position_id  # type: ignore[attr-defined]


def test_the_position_records_the_price_it_filled_at(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """Not the signalled price: 30.15 quote for 0.001 units is 30150, not 30000.

    Recording the bar close would book the position at a price that never traded and
    put the slippage into every realised return computed from it.
    """
    result, ledger, _, _ = _bracket(db, audit, killswitch)
    assert ledger.open_positions()[0].entry_price == D("30150")
    assert result.entry_price == D("30150")  # type: ignore[attr-defined]


def test_a_position_without_a_quote_total_falls_back_to_the_signal(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """Some responses omit `cummulativeQuoteQty`. A missing price is not a zero price."""
    entry = _entry_ack()
    del entry["cummulativeQuoteQty"]
    _, ledger, _, _ = _bracket(db, audit, killswitch, entry=entry)
    assert ledger.open_positions()[0].entry_price == D("30000")


def test_both_orders_are_on_file_and_linked_to_the_position(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """Reconciliation compares against these rows. Without them every ATLAS order on
    the exchange looks like unrecorded exposure."""
    result, ledger, _, strategy_id = _bracket(db, audit, killswitch)
    for client_id in (entry_id(strategy_id), stop_id(strategy_id)):
        row = ledger.order_row(client_id)
        assert row is not None, f"{client_id} was never recorded"
        assert row["position_id"] == result.position_id  # type: ignore[attr-defined]


def test_the_symbol_is_now_blocked_for_re_entry(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """RISK-08 reads `open_symbols`, which was permanently empty before."""
    _, ledger, _, _ = _bracket(db, audit, killswitch)
    assert ledger.open_symbols() == frozenset({"BTCUSDT"})


def test_deployed_capital_is_visible_to_risk(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    _, ledger, _, _ = _bracket(db, audit, killswitch)
    assert ledger.deployed_notional({"BTCUSDT": D("31000")}) == D("31.000")


def test_a_restart_after_an_entry_does_not_arm_the_kill_switch(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """The failure this would have caused on the first restart of a live account.

    With no local order rows, every open ATLAS order on the exchange was unrecordable
    exposure and reconciliation armed RECONCILIATION_FAILURE.
    """
    _, _, _, strategy_id = _bracket(db, audit, killswitch)

    exchange_orders = [
        {"clientOrderId": stop_id(strategy_id), "symbol": "BTCUSDT", "status": "NEW"}
    ]
    report = Reconciler(db, audit, killswitch).reconcile(exchange_orders)

    assert report.clean
    assert report.unreconcilable == []
    assert not killswitch.is_armed()


# ------------------------------------------------------------- entry that fails


def test_an_entry_that_fills_nothing_places_no_stop(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """A stop for an asset the account does not hold is rejected for balance, and a
    position that was never opened must not appear in the ledger."""
    killswitch.initialise()
    transport = StubTransport(_entry_ack(qty="0", quote="0", status="EXPIRED"))
    ledger = Ledger(db, audit)

    with pytest.raises(EntryDidNotFillError):
        open_bracketed_position(
            _broker(audit, killswitch, transport),
            audit,
            ledger,
            strategy_id=_strategy(db),
            signal_bar_time=BAR_TIME,
            symbol="BTCUSDT",
            side=PositionSide.LONG,
            quantity=D("0.001"),
            stop_price=D("29000"),
            reference_price=D("30000"),
            target_price=D("32000"),
        )

    assert len(transport.posts) == 1, "only the entry was sent"
    assert ledger.open_positions() == []


def test_a_reversed_entry_closes_its_position(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """The reversal sells the position, so the ledger must not keep holding it.

    A position row left open would block the symbol under RISK-08 and inflate deployed
    capital for as long as it survived -- which is forever, since nothing else closes it.
    """
    killswitch.initialise()
    transport = StubTransport(
        _entry_ack(),
        OrderRejection("stop rejected", retryable=False),
        ack("reversal", status="FILLED"),
    )
    ledger = Ledger(db, audit)

    with pytest.raises(OrderRejection):
        open_bracketed_position(
            _broker(audit, killswitch, transport),
            audit,
            ledger,
            strategy_id=_strategy(db),
            signal_bar_time=BAR_TIME,
            symbol="BTCUSDT",
            side=PositionSide.LONG,
            quantity=D("0.001"),
            stop_price=D("29000"),
            reference_price=D("30000"),
            target_price=D("32000"),
        )

    assert ledger.open_positions() == []
    assert ledger.open_symbols() == frozenset()


def test_an_unprotected_position_stays_on_the_books(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """When the reversal also fails the position is real, unprotected and open.

    Removing the row would hide exposure the account genuinely has. It stays, and the
    error demands a human.
    """
    killswitch.initialise()
    transport = StubTransport(
        _entry_ack(),
        OrderRejection("stop rejected", retryable=False),
        OrderRejection("reversal rejected", retryable=False),
    )
    ledger = Ledger(db, audit)

    with pytest.raises(UnprotectedPositionError):
        open_bracketed_position(
            _broker(audit, killswitch, transport),
            audit,
            ledger,
            strategy_id=_strategy(db),
            signal_bar_time=BAR_TIME,
            symbol="BTCUSDT",
            side=PositionSide.LONG,
            quantity=D("0.001"),
            stop_price=D("29000"),
            reference_price=D("30000"),
            target_price=D("32000"),
        )

    assert len(ledger.open_positions()) == 1


# ------------------------------------------------------------------- closing


def _trade(client_id: str, qty: str, price: str, trade_id: str = "9001") -> dict[str, object]:
    return {
        "id": trade_id,
        "clientOrderId": client_id,
        "qty": qty,
        "price": price,
        "commission": "0.0001",
        "commissionAsset": "USDT",
        "time": 1757160000000,
    }


def _ingest(
    db: Database, audit: AuditLog, killswitch: KillSwitch, trades: list[dict[str, object]]
) -> object:
    """Ingest with a transport of its own, so queued order acks are not consumed."""
    broker = _broker(audit, killswitch, StubTransport(trades))
    return FillIngestor(db, audit, broker).ingest("BTCUSDT")


def test_a_filled_stop_closes_the_position(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """Nothing else observes this. The exchange sends no notification -- the order
    simply stops appearing in openOrders."""
    _, ledger, _, strategy_id = _bracket(db, audit, killswitch)

    report = _ingest(db, audit, killswitch, [_trade(stop_id(strategy_id), "0.001", "29000")])

    assert report.recorded == 1  # type: ignore[attr-defined]
    assert len(report.closed_positions) == 1  # type: ignore[attr-defined]
    assert ledger.open_positions() == []
    assert ledger.open_symbols() == frozenset()

    position = ledger.position(report.closed_positions[0])  # type: ignore[attr-defined]
    assert position is not None
    assert not position.is_open
    assert position.exit_price == D("29000")
    # (29000 - 30150) * 0.001
    assert position.realised_pnl == D("-1.150")


def test_the_monitor_can_finally_see_a_realised_return(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """MON-01..06 compare live returns against the backtest. With no closed position
    the list was always empty and no strategy could ever be retired."""
    _, ledger, _, strategy_id = _bracket(db, audit, killswitch)
    _ingest(db, audit, killswitch, [_trade(stop_id(strategy_id), "0.001", "29000")])

    returns = ledger.realised_returns(strategy_id)
    assert len(returns) == 1
    assert returns[0] < 0


def test_a_partial_exit_fill_closes_nothing(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """Half the position is still held, and still protected by the rest of the order."""
    _, ledger, _, strategy_id = _bracket(db, audit, killswitch)

    report = _ingest(db, audit, killswitch, [_trade(stop_id(strategy_id), "0.0005", "29000")])

    assert report.closed_positions == []  # type: ignore[attr-defined]
    assert len(ledger.open_positions()) == 1


def test_the_rest_of_a_partial_fill_closes_it(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    _, ledger, _, strategy_id = _bracket(db, audit, killswitch)
    _ingest(db, audit, killswitch, [_trade(stop_id(strategy_id), "0.0005", "29000", trade_id="1")])
    _ingest(db, audit, killswitch, [_trade(stop_id(strategy_id), "0.0005", "28900", trade_id="2")])
    assert ledger.open_positions() == []


def test_an_entry_fill_closes_nothing(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """Only a protective order filling is an exit."""
    _, ledger, _, strategy_id = _bracket(db, audit, killswitch)

    report = _ingest(db, audit, killswitch, [_trade(entry_id(strategy_id), "0.001", "30150")])

    assert report.recorded == 1  # type: ignore[attr-defined]
    assert report.closed_positions == []  # type: ignore[attr-defined]
    assert len(ledger.open_positions()) == 1


def test_a_replayed_exit_fill_closes_the_position_once(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """Polled windows overlap and restarts re-read. Closing twice would double the
    realised loss and corrupt every statistic built on it."""
    _, ledger, _, strategy_id = _bracket(db, audit, killswitch)
    _ingest(db, audit, killswitch, [_trade(stop_id(strategy_id), "0.001", "29000")])
    report = _ingest(db, audit, killswitch, [_trade(stop_id(strategy_id), "0.001", "29000")])

    assert report.duplicates == 1  # type: ignore[attr-defined]
    assert report.closed_positions == []  # type: ignore[attr-defined]
    assert len(ledger.realised_returns(strategy_id)) == 1


def test_the_position_link_survives_a_reopened_database(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """`CREATE TABLE IF NOT EXISTS` leaves an older table alone, so the column that
    links an order to its position needs a migration to reach an existing database."""
    _, _, _, strategy_id = _bracket(db, audit, killswitch)
    path = db.path
    db.close()

    reopened = Database(path)
    try:
        columns = {str(r["name"]) for r in reopened.connection.execute("PRAGMA table_info(orders)")}
        assert "position_id" in columns
        row = Ledger(reopened, AuditLog(reopened)).order_row(stop_id(strategy_id))
        assert row is not None and row["position_id"]
    finally:
        reopened.close()


# ------------------------------------------------- what a failed placement records


def test_an_order_the_kill_switch_stopped_is_marked_not_sent(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """It never reached the network, so it certainly does not exist on the exchange."""
    from atlas.errors import KillSwitchArmedError
    from atlas.models import KillSwitchTrigger

    killswitch.initialise()
    killswitch.arm(KillSwitchTrigger.MANUAL, "halted", "test")
    strategy_id = _strategy(db)
    ledger = Ledger(db, audit)
    transport = StubTransport(_entry_ack())

    with pytest.raises(KillSwitchArmedError):
        open_bracketed_position(
            _broker(audit, killswitch, transport),
            audit,
            ledger,
            strategy_id=strategy_id,
            signal_bar_time=BAR_TIME,
            symbol="BTCUSDT",
            side=PositionSide.LONG,
            quantity=D("0.001"),
            stop_price=D("29000"),
            reference_price=D("30000"),
            target_price=D("32000"),
        )

    assert transport.posts == []
    row = ledger.order_row(entry_id(strategy_id))
    assert row is not None and row["status"] == "NOT_SENT"


def test_a_terminally_rejected_order_is_marked_rejected(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    killswitch.initialise()
    strategy_id = _strategy(db)
    ledger = Ledger(db, audit)
    transport = StubTransport(OrderRejection("insufficient balance", retryable=False, status=400))

    with pytest.raises(OrderRejection):
        open_bracketed_position(
            _broker(audit, killswitch, transport),
            audit,
            ledger,
            strategy_id=strategy_id,
            signal_bar_time=BAR_TIME,
            symbol="BTCUSDT",
            side=PositionSide.LONG,
            quantity=D("0.001"),
            stop_price=D("29000"),
            reference_price=D("30000"),
            target_price=D("32000"),
        )

    row = ledger.order_row(entry_id(strategy_id))
    assert row is not None and row["status"] == "REJECTED"


def test_an_ambiguous_send_stays_open_for_reconciliation(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """Retries exhausted against a transport fault: the order may be live.

    Marking it rejected would erase the only local trace of a possible open order, and
    reconciliation would then find it on the exchange with no local record -- the exact
    condition that arms the kill switch.
    """
    killswitch.initialise()
    strategy_id = _strategy(db)
    ledger = Ledger(db, audit)
    transport = StubTransport(OrderRejection("no response after 3 attempts", retryable=True))

    with pytest.raises(OrderRejection):
        open_bracketed_position(
            _broker(audit, killswitch, transport),
            audit,
            ledger,
            strategy_id=strategy_id,
            signal_bar_time=BAR_TIME,
            symbol="BTCUSDT",
            side=PositionSide.LONG,
            quantity=D("0.001"),
            stop_price=D("29000"),
            reference_price=D("30000"),
            target_price=D("32000"),
        )

    row = ledger.order_row(entry_id(strategy_id))
    assert row is not None and row["status"] == "SUBMITTED"

    # And reconciliation now recognises it rather than arming.
    report = Reconciler(db, audit, killswitch).reconcile(
        [{"clientOrderId": entry_id(strategy_id), "symbol": "BTCUSDT", "status": "NEW"}]
    )
    assert report.clean
    assert not killswitch.is_armed()
