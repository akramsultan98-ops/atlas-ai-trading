"""EXEC-01..10. No network: the broker transport is a stub throughout."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from atlas.audit import AuditLog
from atlas.db.engine import Database
from atlas.errors import AtlasError, KillSwitchArmedError
from atlas.execution.brackets import UnprotectedPositionError, open_bracketed_position
from atlas.execution.broker import (
    SPOT_MAINNET,
    SPOT_TESTNET,
    BinanceSpotBroker,
    OrderRejection,
    OrderRequest,
    OrderRole,
    classify_rejection,
)
from atlas.execution.idempotency import client_order_id
from atlas.execution.ledger import Ledger
from atlas.execution.reconcile import Reconciler
from atlas.killswitch import KillSwitch
from atlas.models import (
    ExchangeEnv,
    KillSwitchState,
    KillSwitchTrigger,
    OrderSide,
    PositionSide,
)
from atlas.risk.sizing import ExchangeFilters

D = Decimal
FILTERS = ExchangeFilters(
    step_size=D("0.00000001"), min_qty=D("0.00000001"), min_notional=D("1"), tick_size=D("0.01")
)


def _strategy(db: Database) -> str:
    """A registered strategy. Orders carry a foreign key to one, so it must exist."""
    from tests.test_evaluator import percent_spec

    from atlas.strategy.registry import StrategyRegistry

    return StrategyRegistry(db).register(percent_spec())


BAR_TIME = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


class StubTransport:
    """Records requests and replays queued responses or errors."""

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.posts: list[str] = []
        self.deletes: list[str] = []
        self.gets: list[str] = []

    def _next(self, url: str = "") -> Any:
        item = self.responses.pop(0) if self.responses else {}
        if isinstance(item, Exception):
            raise item
        # A callable response is handed the request URL so it can answer the way the
        # exchange does - echoing back the client order ids it was actually given,
        # rather than hardcoding ids a test would have to keep in sync.
        if callable(item):
            return item(url)
        return item

    def post(self, url: str, headers: dict[str, str]) -> Any:
        self.posts.append(url)
        return self._next(url)

    def get(self, url: str, headers: dict[str, str]) -> Any:
        self.gets.append(url)
        return self._next()

    def delete(self, url: str, headers: dict[str, str]) -> Any:
        self.deletes.append(url)
        return self._next()


def ack(client_id: str = "atlasX", status: str = "FILLED") -> dict[str, Any]:
    return {
        "clientOrderId": client_id,
        "orderId": 12345,
        "symbol": "BTCUSDT",
        "status": status,
        "executedQty": "0.001",
    }


def oco_reply(
    stop_status: str = "NEW", target_status: str = "NEW"
) -> Callable[[str], dict[str, Any]]:
    """Answer an OCO request with both legs, keyed by the ids the request carried."""

    def reply(url: str) -> dict[str, Any]:
        query = parse_qs(urlparse(url).query)
        stop_id = query["stopClientOrderId"][0]
        target_id = query["limitClientOrderId"][0]
        return {
            "orderListId": 5150,
            "listClientOrderId": query["listClientOrderId"][0],
            "orderReports": [
                {
                    "clientOrderId": stop_id,
                    "orderId": 201,
                    "symbol": "BTCUSDT",
                    "status": stop_status,
                    "executedQty": "0",
                },
                {
                    "clientOrderId": target_id,
                    "orderId": 202,
                    "symbol": "BTCUSDT",
                    "status": target_status,
                    "executedQty": "0",
                },
            ],
        }

    return reply


def make_broker(
    db: Database, audit: AuditLog, killswitch: KillSwitch, transport: StubTransport
) -> BinanceSpotBroker:
    return BinanceSpotBroker(
        "test-key",
        "test-secret",
        killswitch,
        audit,
        exchange_env=ExchangeEnv.TESTNET,
        transport=transport,
    )


# --------------------------------------------------------------- EXEC-03 idempotency


def test_client_order_id_is_deterministic() -> None:
    a = client_order_id("strat-1", BAR_TIME, OrderSide.BUY, "ENTRY")
    b = client_order_id("strat-1", BAR_TIME, OrderSide.BUY, "ENTRY")
    assert a == b


def test_client_order_id_varies_by_decision() -> None:
    base = client_order_id("strat-1", BAR_TIME, OrderSide.BUY, "ENTRY")
    assert base != client_order_id("strat-2", BAR_TIME, OrderSide.BUY, "ENTRY")
    assert base != client_order_id("strat-1", BAR_TIME, OrderSide.SELL, "ENTRY")
    assert base != client_order_id("strat-1", BAR_TIME, OrderSide.BUY, "STOP")


def test_client_order_id_fits_exchange_limit() -> None:
    assert len(client_order_id("a" * 200, BAR_TIME, OrderSide.BUY, "ENTRY")) <= 36


def test_replayed_signal_produces_the_same_order_id() -> None:
    """A crash-and-replay collides with the original order rather than doubling up."""
    ids = {client_order_id("s", BAR_TIME, OrderSide.BUY, "ENTRY") for _ in range(50)}
    assert len(ids) == 1


# ------------------------------------------------------------------ EXEC-01, EXEC-06


def test_testnet_is_the_default_environment(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    assert make_broker(db, audit, killswitch, StubTransport()).base_url == SPOT_TESTNET


def test_live_environment_is_explicit(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    broker = BinanceSpotBroker(
        "k",
        "s",
        killswitch,
        audit,
        exchange_env=ExchangeEnv.LIVE,
        transport=StubTransport(),
    )
    assert broker.base_url == SPOT_MAINNET


def test_broker_requires_credentials(db: Database, audit: AuditLog, killswitch: KillSwitch) -> None:
    with pytest.raises(AtlasError, match="API key and secret"):
        BinanceSpotBroker("", "", killswitch, audit)


def test_no_transport_refuses_to_build_one_implicitly(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    killswitch.initialise()
    broker = BinanceSpotBroker("k", "s", killswitch, audit)
    with pytest.raises(AtlasError, match="no broker transport"):
        broker.place(OrderRequest("cid", "BTCUSDT", OrderSide.BUY, OrderRole.ENTRY, D("0.001")))


def test_armed_kill_switch_blocks_entry(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """EXEC-06: checked immediately before transmission, not at signal time."""
    killswitch.initialise()
    killswitch.arm(KillSwitchTrigger.DAILY_LOSS_LIMIT, "down 5%")
    transport = StubTransport(ack())
    broker = make_broker(db, audit, killswitch, transport)

    with pytest.raises(KillSwitchArmedError):
        broker.place(OrderRequest("cid", "BTCUSDT", OrderSide.BUY, OrderRole.ENTRY, D("0.001")))
    assert transport.posts == [], "no request may reach the exchange while armed"


def test_armed_kill_switch_still_allows_protective_orders(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """KILL-03: refusing stops while armed would leave positions naked."""
    killswitch.initialise()
    killswitch.arm(KillSwitchTrigger.MAX_ACCOUNT_DD, "down 20%")
    transport = StubTransport(ack(status="NEW"))
    broker = make_broker(db, audit, killswitch, transport)

    broker.place(
        OrderRequest(
            "cid",
            "BTCUSDT",
            OrderSide.SELL,
            OrderRole.STOP,
            D("0.001"),
            price=D("95"),
            stop_price=D("95"),
        )
    )
    assert len(transport.posts) == 1


def test_uninitialised_kill_switch_blocks_entry(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """A system never deliberately released to trade must not trade."""
    transport = StubTransport(ack())
    broker = make_broker(db, audit, killswitch, transport)
    with pytest.raises(KillSwitchArmedError):
        broker.place(OrderRequest("cid", "BTCUSDT", OrderSide.BUY, OrderRole.ENTRY, D("0.001")))


# ------------------------------------------------------------------------- EXEC-10


def test_intent_is_logged_before_the_network_call(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """An order that vanishes mid-flight still leaves a record it was attempted."""
    killswitch.initialise()
    broker = make_broker(
        db, audit, killswitch, StubTransport(OrderRejection("network died", retryable=True))
    )
    with pytest.raises(OrderRejection):
        broker.place(OrderRequest("cid", "BTCUSDT", OrderSide.BUY, OrderRole.ENTRY, D("0.001")))

    kinds = [e.event_type.value for e in audit.tail(10)]
    assert "ORDER_INTENT" in kinds
    assert "ORDER_RESULT" in kinds


def test_successful_order_logs_intent_and_result(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    killswitch.initialise()
    broker = make_broker(db, audit, killswitch, StubTransport(ack()))
    broker.place(OrderRequest("cid", "BTCUSDT", OrderSide.BUY, OrderRole.ENTRY, D("0.001")))
    kinds = [e.event_type.value for e in audit.tail(10)]
    assert kinds.count("ORDER_INTENT") == 1
    assert kinds.count("ORDER_RESULT") == 1
    assert audit.verify_chain() > 0


def test_request_is_signed(db: Database, audit: AuditLog, killswitch: KillSwitch) -> None:
    killswitch.initialise()
    transport = StubTransport(ack())
    make_broker(db, audit, killswitch, transport).place(
        OrderRequest("cid", "BTCUSDT", OrderSide.BUY, OrderRole.ENTRY, D("0.001"))
    )
    assert "signature=" in transport.posts[0]
    assert "timestamp=" in transport.posts[0]


def test_credentials_never_appear_in_the_url(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    killswitch.initialise()
    transport = StubTransport(ack())
    make_broker(db, audit, killswitch, transport).place(
        OrderRequest("cid", "BTCUSDT", OrderSide.BUY, OrderRole.ENTRY, D("0.001"))
    )
    assert "test-secret" not in transport.posts[0]
    assert "test-key" not in transport.posts[0]


# ------------------------------------------------------------------------- EXEC-09


def test_rate_limit_is_retryable() -> None:
    assert classify_rejection(429, '{"msg":"Too many requests"}').retryable


def test_server_error_is_retryable() -> None:
    assert classify_rejection(503, "unavailable").retryable


def test_insufficient_balance_is_terminal() -> None:
    """Resending an order refused on its merits just burns rate limit."""
    rejection = classify_rejection(400, '{"code":-2010,"msg":"Account has insufficient balance"}')
    assert not rejection.retryable
    assert "insufficient balance" in str(rejection)


def test_filter_failure_is_terminal() -> None:
    assert not classify_rejection(400, '{"msg":"Filter failure: MIN_NOTIONAL"}').retryable


# ------------------------------------------------------------- EXEC-02 brackets


def test_entry_and_stop_placed_together(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    killswitch.initialise()
    transport = StubTransport(ack("entry"), oco_reply())
    broker = make_broker(db, audit, killswitch, transport)

    result = open_bracketed_position(
        broker,
        audit,
        Ledger(db, audit),
        strategy_id=_strategy(db),
        signal_bar_time=BAR_TIME,
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        quantity=D("0.001"),
        stop_price=D("95"),
        reference_price=D("100"),
        target_price=D("110"),
        filters=FILTERS,
    )
    assert len(transport.posts) == 2, "the entry, then one OCO carrying both exits"
    assert result.entry.client_order_id == "entry"

    # The exit is a single order list: two independent sells cannot both stand against
    # one holding, because the first locks the base asset.
    assert "order/oco" in transport.posts[1]
    assert result.stop.client_order_id != result.take_profit.client_order_id
    assert result.exit_orders.order_list_id == "5150"
    assert result.stop_price == D("95")
    assert result.target_price == D("110")


def test_failed_stop_reverses_the_entry(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """A position must never exist unprotected."""
    killswitch.initialise()
    transport = StubTransport(
        ack("entry"),
        OrderRejection("stop rejected", retryable=False),
        ack("reversal"),
    )
    broker = make_broker(db, audit, killswitch, transport)

    with pytest.raises(OrderRejection, match="entry reversed"):
        open_bracketed_position(
            broker,
            audit,
            Ledger(db, audit),
            strategy_id=_strategy(db),
            signal_bar_time=BAR_TIME,
            symbol="BTCUSDT",
            side=PositionSide.LONG,
            quantity=D("0.001"),
            stop_price=D("95"),
            reference_price=D("100"),
            target_price=D("110"),
            filters=FILTERS,
        )
    assert len(transport.posts) == 3, "entry, failed stop, reversal"


def test_failed_reversal_raises_unprotected_position(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """The one case needing a human: open, unprotected, and cannot be closed."""
    killswitch.initialise()
    transport = StubTransport(
        ack("entry"),
        OrderRejection("stop rejected", retryable=False),
        OrderRejection("reversal rejected", retryable=False),
    )
    broker = make_broker(db, audit, killswitch, transport)

    with pytest.raises(UnprotectedPositionError, match="manual intervention"):
        open_bracketed_position(
            broker,
            audit,
            Ledger(db, audit),
            strategy_id=_strategy(db),
            signal_bar_time=BAR_TIME,
            symbol="BTCUSDT",
            side=PositionSide.LONG,
            quantity=D("0.001"),
            stop_price=D("95"),
            reference_price=D("100"),
            target_price=D("110"),
            filters=FILTERS,
        )


def test_short_bracket_inverts_sides(db: Database, audit: AuditLog, killswitch: KillSwitch) -> None:
    killswitch.initialise()
    transport = StubTransport(ack("entry"), oco_reply())
    broker = make_broker(db, audit, killswitch, transport)
    open_bracketed_position(
        broker,
        audit,
        Ledger(db, audit),
        strategy_id=_strategy(db),
        signal_bar_time=BAR_TIME,
        symbol="BTCUSDT",
        side=PositionSide.SHORT,
        quantity=D("0.001"),
        stop_price=D("105"),
        reference_price=D("100"),
        target_price=D("90"),
        filters=FILTERS,
    )
    assert "side=SELL" in transport.posts[0]
    assert "side=BUY" in transport.posts[1]


# ----------------------------------------------------------- EXEC-04, EXEC-05


def _insert_order(db: Database, client_id: str, status: str = "NEW") -> None:
    db.connection.execute(
        "INSERT INTO strategy_specs(spec_hash, payload, created_at) VALUES (?, '{}', 'now') "
        "ON CONFLICT DO NOTHING",
        ("a" * 64,),
    )
    db.connection.execute(
        "INSERT INTO strategies(id, spec_hash, symbol, timeframe, status, created_at, "
        "status_at) VALUES ('s1', ?, 'BTCUSDT', '1h', 'LIVE', 'now', 'now') "
        "ON CONFLICT DO NOTHING",
        ("a" * 64,),
    )
    db.connection.execute(
        "INSERT INTO orders(id, client_order_id, strategy_id, symbol, side, order_type, "
        "role, quantity, status, exchange_env, created_at, updated_at) "
        "VALUES (?, ?, 's1', 'BTCUSDT', 'BUY', 'MARKET', 'ENTRY', '0.001', ?, "
        "'testnet', 'now', 'now')",
        (client_id, client_id, status),
    )


def test_matching_state_reconciles_clean(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    killswitch.initialise()
    _insert_order(db, "atlas-a")
    report = Reconciler(db, audit, killswitch).reconcile(
        [{"clientOrderId": "atlas-a", "status": "NEW"}]
    )
    assert report.clean
    assert report.matched == 1
    assert not killswitch.is_armed()


def test_status_drift_is_repaired_from_the_exchange(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """EXEC-04: exchange state is truth."""
    killswitch.initialise()
    _insert_order(db, "atlas-b", status="NEW")
    report = Reconciler(db, audit, killswitch).reconcile(
        [{"clientOrderId": "atlas-b", "status": "FILLED"}]
    )
    assert "atlas-b" in report.repaired
    row = db.connection.execute(
        "SELECT status FROM orders WHERE client_order_id = 'atlas-b'"
    ).fetchone()
    assert row["status"] == "FILLED"


def test_locally_open_order_missing_remotely_is_closed(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    killswitch.initialise()
    _insert_order(db, "atlas-c")
    report = Reconciler(db, audit, killswitch).reconcile([])
    assert "atlas-c" in report.repaired
    assert report.clean


def test_unknown_remote_order_arms_the_kill_switch(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """EXEC-05: unrecorded exposure is the dangerous direction. Stop, don't guess."""
    killswitch.initialise()
    report = Reconciler(db, audit, killswitch).reconcile(
        [{"clientOrderId": "atlasGHOST", "status": "NEW"}]
    )
    assert not report.clean
    assert "atlasGHOST" in report.unreconcilable
    state = killswitch.read_state()
    assert state.state is KillSwitchState.ARMED
    assert state.trigger is KillSwitchTrigger.RECONCILIATION_FAILURE


def test_foreign_orders_are_ignored(db: Database, audit: AuditLog, killswitch: KillSwitch) -> None:
    """Manual orders placed by a human are not ATLAS's to reconcile."""
    killswitch.initialise()
    report = Reconciler(db, audit, killswitch).reconcile(
        [{"clientOrderId": "web_manual_order", "status": "NEW"}]
    )
    assert report.clean
    assert not killswitch.is_armed()


def test_balance_drift_arms_the_kill_switch(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    killswitch.initialise()
    reconciler = Reconciler(db, audit, killswitch)
    assert reconciler.reconcile_balance(D("100"), D("99.99"), D("0.05"))
    assert not killswitch.is_armed()
    assert not reconciler.reconcile_balance(D("100"), D("80"), D("0.05"))
    assert killswitch.is_armed()


def test_reconciliation_is_audited(db: Database, audit: AuditLog, killswitch: KillSwitch) -> None:
    killswitch.initialise()
    Reconciler(db, audit, killswitch).reconcile([])
    assert any(e.event_type.value == "RECONCILIATION" for e in audit.tail(5))
