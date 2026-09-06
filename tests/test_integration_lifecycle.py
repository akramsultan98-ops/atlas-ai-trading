"""Integration: startup, reconciliation and failure scenarios (Phases G, M, P).

These drive the assembled service, not individual components. They are deterministic
fixtures standing in for exchange behaviour, NOT evidence about Binance: the exchange is
unreachable from this environment, so every response here is scripted. What they do
establish is that ATLAS's own reaction to each scenario is correct and safe.

Scenario coverage, per Phase G:
  clean startup · restart with an open position · stop filled while offline ·
  unknown exchange order · missing local order · duplicate fill · partial fill ·
  cancelled order · rejected order
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_strategy_spec import make_spec

from atlas.audit import AuditLog
from atlas.db.engine import Database
from atlas.execution.broker import (
    BinanceSpotBroker,
    OrderRejection,
    OrderRequest,
    OrderRole,
)
from atlas.execution.ingest import FillIngestor
from atlas.execution.ledger import Fill, Ledger
from atlas.execution.reconcile import Reconciler
from atlas.killswitch import KillSwitch
from atlas.models import (
    ExchangeEnv,
    KillSwitchTrigger,
    OrderSide,
    PositionSide,
    StrategyStatus,
)
from atlas.runtime.recovery import recover
from atlas.strategy.registry import StrategyRegistry

D = Decimal
TS = 1_770_000_000_000


class ScriptedExchange:
    """A scripted Binance. Every response here is fiction, by construction."""

    def __init__(self) -> None:
        self.open_orders: list[dict[str, Any]] = []
        self.trades: list[dict[str, Any]] = []
        self.reject_next: OrderRejection | None = None
        self.posts = 0

    def post(self, url: str, headers: dict[str, str]) -> Any:
        self.posts += 1
        if self.reject_next is not None:
            rejection, self.reject_next = self.reject_next, None
            raise rejection
        return {
            "clientOrderId": "atlas-x",
            "orderId": 1,
            "symbol": "BTCUSDT",
            "status": "FILLED",
            "executedQty": "0.001",
        }

    def get(self, url: str, headers: dict[str, str]) -> Any:
        if "myTrades" in url:
            return self.trades
        if "openOrders" in url:
            return self.open_orders
        if "/time" in url:
            return {"serverTime": TS}
        return {}

    def delete(self, url: str, headers: dict[str, str]) -> Any:
        return {"status": "CANCELED"}


@pytest.fixture
def rig(db: Database, audit: AuditLog, tmp_path: Path):
    exchange = ScriptedExchange()
    killswitch = KillSwitch(db, tmp_path / "ks.json", audit)
    broker = BinanceSpotBroker(
        "k", "s", killswitch, audit, exchange_env=ExchangeEnv.TESTNET, transport=exchange
    )
    registry = StrategyRegistry(db)
    strategy_id = registry.register(make_spec())
    registry.set_status(strategy_id, StrategyStatus.LIVE)
    return {
        "db": db,
        "audit": audit,
        "ks": killswitch,
        "broker": broker,
        "exchange": exchange,
        "ledger": Ledger(db, audit),
        "ingestor": FillIngestor(db, audit, broker),
        "reconciler": Reconciler(db, audit, killswitch),
        "strategy_id": strategy_id,
        "registry": registry,
    }


def place(rig: dict[str, Any], cid: str, role: OrderRole = OrderRole.ENTRY) -> None:
    rig["ledger"].record_order(
        OrderRequest(cid, "BTCUSDT", OrderSide.BUY, role, D("0.001")),
        rig["strategy_id"],
        ExchangeEnv.TESTNET,
    )


# ------------------------------------------------------------------ clean startup


def test_clean_startup_permits_entries(rig: dict[str, Any]) -> None:
    rig["ks"].initialise()
    report = recover(rig["db"], rig["audit"], rig["ks"], [])
    assert report.may_resume_entries
    assert report.reconciled


def test_startup_without_deliberate_release_stays_halted(rig: dict[str, Any]) -> None:
    """KILL-06: a system never released to trade does not start trading."""
    report = recover(rig["db"], rig["audit"], rig["ks"], [])
    assert not report.may_resume_entries
    assert report.kill_switch_armed


# ------------------------------------------------- restart with an open position


def test_restart_with_open_position_preserves_it(rig: dict[str, Any]) -> None:
    rig["ks"].initialise()
    position_id = rig["ledger"].open_position(
        rig["strategy_id"],
        "BTCUSDT",
        PositionSide.LONG,
        D("0.001"),
        D("100"),
        D("95"),
        D("110"),
    )
    place(rig, "atlas-stop", OrderRole.STOP)
    rig["exchange"].open_orders = [{"clientOrderId": "atlas-stop", "status": "NEW"}]

    report = recover(rig["db"], rig["audit"], rig["ks"], rig["exchange"].open_orders)

    assert report.may_resume_entries
    open_positions = rig["ledger"].open_positions()
    assert len(open_positions) == 1
    assert open_positions[0].id == position_id
    assert rig["ledger"].open_symbols() == frozenset({"BTCUSDT"})


def test_stop_filled_while_offline_is_discovered(rig: dict[str, Any]) -> None:
    """The exchange executed the protective stop while ATLAS was down.

    The order is gone from openOrders and a trade exists. Reconciliation must close
    the local order and ingestion must record the fill - neither may be missed.
    """
    rig["ks"].initialise()
    place(rig, "atlas-stop", OrderRole.STOP)
    rig["exchange"].open_orders = []  # exchange no longer holds it
    rig["exchange"].trades = [
        {
            "id": 55,
            "clientOrderId": "atlas-stop",
            "qty": "0.001",
            "price": "95",
            "commission": "0.0001",
            "commissionAsset": "BNB",
            "time": TS,
        }
    ]

    report = recover(rig["db"], rig["audit"], rig["ks"], rig["exchange"].open_orders)
    assert "atlas-stop" in report.issues[0] if report.issues else True

    ingest = rig["ingestor"].ingest("BTCUSDT")
    assert ingest.recorded == 1
    assert rig["ledger"].filled_quantity("atlas-stop") == D("0.001")

    row = (
        rig["db"]
        .connection.execute("SELECT status FROM orders WHERE client_order_id = 'atlas-stop'")
        .fetchone()
    )
    assert row["status"] == "RECONCILED_CLOSED"


# ------------------------------------------------------------ divergence handling


def test_unknown_exchange_order_halts_the_system(rig: dict[str, Any]) -> None:
    """EXEC-05: unrecorded exposure is the dangerous direction. Stop, do not adopt."""
    rig["ks"].initialise()
    report = recover(
        rig["db"],
        rig["audit"],
        rig["ks"],
        [{"clientOrderId": "atlasGHOST", "status": "NEW"}],
    )
    assert not report.may_resume_entries
    assert rig["ks"].read_state().trigger is KillSwitchTrigger.RECONCILIATION_FAILURE


def test_missing_local_order_is_repaired_not_fatal(rig: dict[str, Any]) -> None:
    rig["ks"].initialise()
    place(rig, "atlas-gone")
    report = recover(rig["db"], rig["audit"], rig["ks"], [])
    assert report.may_resume_entries, "a vanished order is repairable, not a halt"


def test_foreign_manual_order_is_ignored(rig: dict[str, Any]) -> None:
    """A human's own order on the same account is not ATLAS's to reconcile."""
    rig["ks"].initialise()
    report = recover(
        rig["db"],
        rig["audit"],
        rig["ks"],
        [{"clientOrderId": "web_manual_1", "status": "NEW"}],
    )
    assert report.may_resume_entries


# ----------------------------------------------------------------- fill scenarios


def test_duplicate_fill_event_does_not_double_the_position(rig: dict[str, Any]) -> None:
    rig["ks"].initialise()
    place(rig, "atlas-e1")
    trade = {
        "id": 1,
        "clientOrderId": "atlas-e1",
        "qty": "0.001",
        "price": "100",
        "commission": "0",
        "commissionAsset": "BNB",
        "time": TS,
    }
    rig["exchange"].trades = [trade]

    rig["ingestor"].ingest("BTCUSDT")
    rig["exchange"].trades = [trade]  # exchange re-delivers on reconnect
    second = rig["ingestor"].ingest("BTCUSDT")

    assert second.recorded == 0
    assert rig["ledger"].filled_quantity("atlas-e1") == D("0.001")


def test_partial_fills_accumulate_to_the_full_quantity(rig: dict[str, Any]) -> None:
    rig["ks"].initialise()
    place(rig, "atlas-e1")
    rig["exchange"].trades = [
        {
            "id": 1,
            "clientOrderId": "atlas-e1",
            "qty": "0.0003",
            "price": "100",
            "commission": "0",
            "commissionAsset": "BNB",
            "time": TS,
        },
        {
            "id": 2,
            "clientOrderId": "atlas-e1",
            "qty": "0.0007",
            "price": "101",
            "commission": "0",
            "commissionAsset": "BNB",
            "time": TS,
        },
    ]
    assert rig["ingestor"].ingest("BTCUSDT").recorded == 2
    assert rig["ledger"].filled_quantity("atlas-e1") == D("0.001")


def test_cancelled_order_is_closed_locally(rig: dict[str, Any]) -> None:
    rig["ks"].initialise()
    place(rig, "atlas-c1")
    rig["reconciler"].reconcile([{"clientOrderId": "atlas-c1", "status": "CANCELED"}])
    row = (
        rig["db"]
        .connection.execute("SELECT status FROM orders WHERE client_order_id = 'atlas-c1'")
        .fetchone()
    )
    assert row["status"] == "CANCELED"


def test_rejected_order_does_not_create_a_position(rig: dict[str, Any]) -> None:
    """Phase H: an ambiguous or failed entry must never leave phantom state."""
    rig["ks"].initialise()
    rig["exchange"].reject_next = OrderRejection(
        "insufficient balance", retryable=False, status=400
    )
    with pytest.raises(OrderRejection):
        rig["broker"].place(
            OrderRequest("atlas-r1", "BTCUSDT", OrderSide.BUY, OrderRole.ENTRY, D("0.001"))
        )
    assert rig["ledger"].open_positions() == []


# ------------------------------------------------------------------ kill switch


def test_armed_kill_switch_blocks_entries_but_not_stops(rig: dict[str, Any]) -> None:
    rig["ks"].initialise()
    rig["ks"].arm(KillSwitchTrigger.DAILY_LOSS_LIMIT, "down 5%")

    from atlas.errors import KillSwitchArmedError

    with pytest.raises(KillSwitchArmedError):
        rig["broker"].place(
            OrderRequest("atlas-e2", "BTCUSDT", OrderSide.BUY, OrderRole.ENTRY, D("0.001"))
        )
    before = rig["exchange"].posts
    rig["broker"].place(
        OrderRequest(
            "atlas-s2",
            "BTCUSDT",
            OrderSide.SELL,
            OrderRole.STOP,
            D("0.001"),
            price=D("95"),
            stop_price=D("95"),
        )
    )
    assert rig["exchange"].posts == before + 1, "protective orders still pass"


def test_audit_chain_survives_the_whole_scenario(rig: dict[str, Any]) -> None:
    rig["ks"].initialise()
    place(rig, "atlas-e1")
    rig["exchange"].trades = [
        {
            "id": 1,
            "clientOrderId": "atlas-e1",
            "qty": "0.001",
            "price": "100",
            "commission": "0",
            "commissionAsset": "BNB",
            "time": TS,
        }
    ]
    rig["ingestor"].ingest("BTCUSDT")
    recover(rig["db"], rig["audit"], rig["ks"], [])
    assert rig["audit"].verify_chain() > 0


def test_ledger_feeds_the_monitor_after_a_closed_trade(rig: dict[str, Any]) -> None:
    """The wiring the audit found missing: fills -> ledger -> monitor input."""
    rig["ks"].initialise()
    place(rig, "atlas-e1")
    rig["ledger"].record_fill(
        Fill(
            "atlas-e1",
            D("0.001"),
            D("100"),
            D("0"),
            "BNB",
            __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            "t1",
        )
    )
    pid = rig["ledger"].open_position(
        rig["strategy_id"],
        "BTCUSDT",
        PositionSide.LONG,
        D("1"),
        D("100"),
        D("95"),
        D("110"),
    )
    rig["ledger"].close_position(pid, D("110"))
    assert rig["ledger"].realised_returns(rig["strategy_id"]) == [D("0.1")]
