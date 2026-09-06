"""Fill ingestion (Phase F) and its failure modes (Phase P)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tests.test_strategy_spec import make_spec

from atlas.audit import AuditLog
from atlas.db.engine import Database
from atlas.execution.broker import BinanceSpotBroker, OrderRequest, OrderRole
from atlas.execution.ingest import FillIngestor
from atlas.execution.ledger import Ledger
from atlas.killswitch import KillSwitch
from atlas.models import ExchangeEnv, OrderSide, StrategyStatus
from atlas.strategy.registry import StrategyRegistry

D = Decimal


class TradeTransport:
    """Serves a fixed myTrades payload."""

    def __init__(self, *pages: list[dict[str, Any]]) -> None:
        self.pages = list(pages)
        self.gets: list[str] = []

    def get(self, url: str, headers: dict[str, str]) -> Any:
        self.gets.append(url)
        return self.pages.pop(0) if self.pages else []

    def post(self, url: str, headers: dict[str, str]) -> Any:
        return {}

    def delete(self, url: str, headers: dict[str, str]) -> Any:
        return {}


def trade(tid: int, cid: str, qty: str = "0.001", price: str = "100") -> dict[str, Any]:
    return {
        "id": tid,
        "clientOrderId": cid,
        "qty": qty,
        "price": price,
        "commission": "0.0001",
        "commissionAsset": "BNB",
        "time": 1_770_000_000_000,
    }


def setup(db: Database, audit: AuditLog, ks: KillSwitch, transport: TradeTransport):
    ks.initialise()
    registry = StrategyRegistry(db)
    sid = registry.register(make_spec())
    registry.set_status(sid, StrategyStatus.LIVE)
    broker = BinanceSpotBroker(
        "k", "s", ks, audit, exchange_env=ExchangeEnv.TESTNET, transport=transport
    )
    ledger = Ledger(db, audit)
    ledger.record_order(
        OrderRequest("atlas-e1", "BTCUSDT", OrderSide.BUY, OrderRole.ENTRY, D("0.001")),
        sid,
        ExchangeEnv.TESTNET,
    )
    return FillIngestor(db, audit, broker), ledger, sid


def test_ingests_new_fills(db: Database, audit: AuditLog, killswitch: KillSwitch) -> None:
    ingestor, ledger, _ = setup(db, audit, killswitch, TradeTransport([trade(1, "atlas-e1")]))
    report = ingestor.ingest("BTCUSDT")
    assert report.recorded == 1
    assert ledger.filled_quantity("atlas-e1") == D("0.001")


def test_duplicate_delivery_is_not_double_counted(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """The load-bearing property: overlapping polls must not double a position."""
    transport = TradeTransport([trade(1, "atlas-e1")], [trade(1, "atlas-e1")])
    ingestor, ledger, _ = setup(db, audit, killswitch, transport)

    first = ingestor.ingest("BTCUSDT")
    assert first.recorded == 1

    # Force a re-read of the same trade by replaying the identical page.
    second = ingestor.ingest("BTCUSDT")
    assert second.recorded == 0
    assert ledger.filled_quantity("atlas-e1") == D("0.001"), "quantity must not double"


def test_partial_fills_accumulate(db: Database, audit: AuditLog, killswitch: KillSwitch) -> None:
    transport = TradeTransport(
        [trade(1, "atlas-e1", qty="0.0004"), trade(2, "atlas-e1", qty="0.0006")]
    )
    ingestor, ledger, _ = setup(db, audit, killswitch, transport)
    assert ingestor.ingest("BTCUSDT").recorded == 2
    assert ledger.filled_quantity("atlas-e1") == D("0.001")


def test_cursor_resumes_from_the_last_recorded_trade(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    transport = TradeTransport([trade(5, "atlas-e1")], [trade(6, "atlas-e1")])
    ingestor, _, _ = setup(db, audit, killswitch, transport)
    ingestor.ingest("BTCUSDT")
    ingestor.ingest("BTCUSDT")
    assert "fromId=6" in transport.gets[1], "second poll must resume after trade 5"


def test_unknown_order_is_reported_as_an_orphan(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """An unrecognised trade is unrecorded exposure, not something to invent a row for."""
    transport = TradeTransport([trade(9, "not-an-atlas-order")])
    ingestor, _, _ = setup(db, audit, killswitch, transport)
    report = ingestor.ingest("BTCUSDT")
    assert report.recorded == 0
    assert report.had_orphans


def test_malformed_trade_is_skipped_not_fatal(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    bad = {
        "id": 3,
        "clientOrderId": "atlas-e1",
        "qty": "not-a-number",
        "price": "1",
        "time": 1_770_000_000_000,
    }
    transport = TradeTransport([bad, trade(4, "atlas-e1")])
    ingestor, _, _ = setup(db, audit, killswitch, transport)
    report = ingestor.ingest("BTCUSDT")
    assert report.recorded == 1
    assert report.had_orphans


def test_trade_without_client_order_id_is_an_orphan(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    transport = TradeTransport([{"id": 11, "qty": "1", "price": "1", "time": 1}])
    ingestor, _, _ = setup(db, audit, killswitch, transport)
    assert ingestor.ingest("BTCUSDT").had_orphans


def test_one_symbol_failure_does_not_stop_the_others(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """Phase N: a single symbol must not kill the service."""

    class Exploding(TradeTransport):
        def get(self, url: str, headers: dict[str, str]) -> Any:
            if "ETHUSDT" in url:
                raise RuntimeError("exchange unreachable")
            return super().get(url, headers)

    ingestor, _, _ = setup(db, audit, killswitch, Exploding([trade(1, "atlas-e1")]))
    reports = ingestor.ingest_all(["BTCUSDT", "ETHUSDT"])
    assert len(reports) == 2
    assert reports[0].recorded == 1
    assert reports[1].had_orphans


def test_ingestion_is_audited(db: Database, audit: AuditLog, killswitch: KillSwitch) -> None:
    ingestor, _, _ = setup(db, audit, killswitch, TradeTransport([trade(1, "atlas-e1")]))
    ingestor.ingest("BTCUSDT")
    assert any(e.payload.get("event") == "fill_ingest" for e in audit.tail(10))
    assert audit.verify_chain() > 0
