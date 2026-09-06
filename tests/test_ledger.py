"""Order, fill and position ledger."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tests.test_strategy_spec import make_spec

from atlas.audit import AuditLog
from atlas.db.engine import Database
from atlas.execution.broker import OrderRequest, OrderRole
from atlas.execution.ledger import Fill, Ledger
from atlas.models import ExchangeEnv, OrderSide, PositionSide, StrategyStatus
from atlas.strategy.registry import StrategyRegistry

D = Decimal
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def strategy_id(db: Database) -> str:
    registry = StrategyRegistry(db)
    sid = registry.register(make_spec())
    registry.set_status(sid, StrategyStatus.LIVE)
    return sid


def entry_request(cid: str = "atlas-entry") -> OrderRequest:
    return OrderRequest(cid, "BTCUSDT", OrderSide.BUY, OrderRole.ENTRY, D("0.001"))


# --------------------------------------------------------------------- orders


def test_record_order(db: Database, audit: AuditLog, strategy_id: str) -> None:
    ledger = Ledger(db, audit)
    order_id = ledger.record_order(entry_request(), strategy_id, ExchangeEnv.TESTNET)
    row = db.connection.execute(
        "SELECT client_order_id, exchange_env, status FROM orders WHERE id = ?", (order_id,)
    ).fetchone()
    assert row["client_order_id"] == "atlas-entry"
    assert row["exchange_env"] == "testnet"


def test_record_order_is_idempotent(db: Database, audit: AuditLog, strategy_id: str) -> None:
    """EXEC-03: a replayed submission must not create a second row."""
    ledger = Ledger(db, audit)
    first = ledger.record_order(entry_request(), strategy_id, ExchangeEnv.TESTNET)
    second = ledger.record_order(entry_request(), strategy_id, ExchangeEnv.TESTNET)
    assert first == second
    assert db.connection.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"] == 1


def test_update_order_status(db: Database, audit: AuditLog, strategy_id: str) -> None:
    ledger = Ledger(db, audit)
    ledger.record_order(entry_request(), strategy_id, ExchangeEnv.TESTNET)
    ledger.update_order_status("atlas-entry", "FILLED")
    row = db.connection.execute(
        "SELECT status FROM orders WHERE client_order_id = 'atlas-entry'"
    ).fetchone()
    assert row["status"] == "FILLED"


# ---------------------------------------------------------------------- fills


def test_record_fill(db: Database, audit: AuditLog, strategy_id: str) -> None:
    ledger = Ledger(db, audit)
    ledger.record_order(entry_request(), strategy_id, ExchangeEnv.TESTNET)
    assert ledger.record_fill(
        Fill("atlas-entry", D("0.001"), D("100"), D("0.0001"), "BNB", NOW, "trade-1")
    )
    assert ledger.filled_quantity("atlas-entry") == D("0.001")


def test_duplicate_trade_id_is_ignored(db: Database, audit: AuditLog, strategy_id: str) -> None:
    """Exchanges re-deliver trades on reconnect; recording twice would double a position."""
    ledger = Ledger(db, audit)
    ledger.record_order(entry_request(), strategy_id, ExchangeEnv.TESTNET)
    fill = Fill("atlas-entry", D("0.001"), D("100"), D("0.0001"), "BNB", NOW, "trade-1")

    assert ledger.record_fill(fill) is True
    assert ledger.record_fill(fill) is False
    assert ledger.filled_quantity("atlas-entry") == D("0.001")


def test_partial_fills_accumulate(db: Database, audit: AuditLog, strategy_id: str) -> None:
    ledger = Ledger(db, audit)
    ledger.record_order(entry_request(), strategy_id, ExchangeEnv.TESTNET)
    ledger.record_fill(Fill("atlas-entry", D("0.0004"), D("100"), D("0"), "BNB", NOW, "t1"))
    ledger.record_fill(Fill("atlas-entry", D("0.0006"), D("101"), D("0"), "BNB", NOW, "t2"))
    assert ledger.filled_quantity("atlas-entry") == D("0.001")


def test_fill_quantities_stay_exact(db: Database, audit: AuditLog, strategy_id: str) -> None:
    """ADR-003: summing through a float would drift."""
    ledger = Ledger(db, audit)
    ledger.record_order(entry_request(), strategy_id, ExchangeEnv.TESTNET)
    for i in range(10):
        ledger.record_fill(Fill("atlas-entry", D("0.1"), D("100"), D("0"), "BNB", NOW, f"t{i}"))
    assert ledger.filled_quantity("atlas-entry") == D("1.0")


def test_fill_for_unknown_order_raises(db: Database, audit: AuditLog) -> None:
    with pytest.raises(ValueError, match="unknown order"):
        Ledger(db, audit).record_fill(Fill("nope", D("1"), D("1"), D("0"), "BNB", NOW, "t1"))


def test_fill_is_audited(db: Database, audit: AuditLog, strategy_id: str) -> None:
    ledger = Ledger(db, audit)
    ledger.record_order(entry_request(), strategy_id, ExchangeEnv.TESTNET)
    ledger.record_fill(Fill("atlas-entry", D("0.001"), D("100"), D("0"), "BNB", NOW, "t1"))
    assert any(e.event_type.value == "FILL_RECORDED" for e in audit.tail(5))
    assert audit.verify_chain() > 0


# ------------------------------------------------------------------ positions


def test_open_and_close_a_long(db: Database, audit: AuditLog, strategy_id: str) -> None:
    ledger = Ledger(db, audit)
    position_id = ledger.open_position(
        strategy_id, "BTCUSDT", PositionSide.LONG, D("2"), D("100"), D("95"), D("110")
    )
    assert len(ledger.open_positions()) == 1
    assert ledger.close_position(position_id, D("110")) == D("20")
    assert ledger.open_positions() == []


def test_short_pnl_is_inverted(db: Database, audit: AuditLog, strategy_id: str) -> None:
    ledger = Ledger(db, audit)
    position_id = ledger.open_position(
        strategy_id, "BTCUSDT", PositionSide.SHORT, D("2"), D("100"), D("105"), D("90")
    )
    assert ledger.close_position(position_id, D("90")) == D("20")


def test_losing_trade_has_negative_pnl(db: Database, audit: AuditLog, strategy_id: str) -> None:
    ledger = Ledger(db, audit)
    position_id = ledger.open_position(
        strategy_id, "BTCUSDT", PositionSide.LONG, D("2"), D("100"), D("95"), D("110")
    )
    assert ledger.close_position(position_id, D("95")) == D("-10")


def test_double_close_is_refused(db: Database, audit: AuditLog, strategy_id: str) -> None:
    ledger = Ledger(db, audit)
    position_id = ledger.open_position(
        strategy_id, "BTCUSDT", PositionSide.LONG, D("1"), D("100"), D("95"), D("110")
    )
    ledger.close_position(position_id, D("110"))
    with pytest.raises(ValueError, match="already closed"):
        ledger.close_position(position_id, D("110"))


def test_closing_unknown_position_raises(db: Database, audit: AuditLog) -> None:
    with pytest.raises(ValueError, match="unknown position"):
        Ledger(db, audit).close_position("nope", D("100"))


def test_open_symbols_feeds_the_risk_engine(
    db: Database, audit: AuditLog, strategy_id: str
) -> None:
    """RISK-08 input."""
    ledger = Ledger(db, audit)
    ledger.open_position(
        strategy_id, "BTCUSDT", PositionSide.LONG, D("1"), D("100"), D("95"), D("110")
    )
    assert ledger.open_symbols() == frozenset({"BTCUSDT"})


def test_realised_returns_feed_the_monitor(db: Database, audit: AuditLog, strategy_id: str) -> None:
    """MON-01..04 input: closed-trade returns as fractions of notional."""
    ledger = Ledger(db, audit)
    for exit_price, expected_pnl in ((D("110"), D("10")), (D("95"), D("-5"))):
        pid = ledger.open_position(
            strategy_id, "BTCUSDT", PositionSide.LONG, D("1"), D("100"), D("95"), D("110")
        )
        assert ledger.close_position(pid, exit_price) == expected_pnl

    returns = ledger.realised_returns(strategy_id)
    assert returns == [D("0.1"), D("-0.05")]


def test_open_positions_excluded_from_returns(
    db: Database, audit: AuditLog, strategy_id: str
) -> None:
    ledger = Ledger(db, audit)
    ledger.open_position(
        strategy_id, "BTCUSDT", PositionSide.LONG, D("1"), D("100"), D("95"), D("110")
    )
    assert ledger.realised_returns(strategy_id) == []
