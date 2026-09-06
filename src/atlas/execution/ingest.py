"""Fill ingestion (EXEC-04, EXEC-10).

The ledger had no producer: `Ledger.record_fill` existed and was tested, but nothing in
production ever called it. This is the exchange-side half — it discovers trades the
exchange has executed and feeds them in.

Idempotence is load-bearing rather than incidental. Binance's `myTrades` is paged by
trade id and a poll that overlaps its previous window re-delivers trades; a restart
re-reads from the last persisted cursor and re-delivers more. Recording a trade twice
would double a position that never doubled, so identity comes from the exchange trade
id and `record_fill` returns False for anything already seen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from atlas.audit import AuditLog
from atlas.db.engine import Database
from atlas.execution.broker import BinanceSpotBroker
from atlas.execution.ledger import Fill, Ledger
from atlas.models import AuditEventType

INGEST_ACTOR = "execution"


@dataclass(frozen=True)
class IngestReport:
    symbol: str
    seen: int = 0
    recorded: int = 0
    duplicates: int = 0
    orphans: list[str] = field(default_factory=list)
    closed_positions: list[str] = field(default_factory=list)

    @property
    def had_orphans(self) -> bool:
        """Trades for orders ATLAS has no record of — a reconciliation signal."""
        return bool(self.orphans)


class FillIngestor:
    """Polls exchange trade history and records new fills in the ledger."""

    def __init__(self, db: Database, audit: AuditLog, broker: BinanceSpotBroker) -> None:
        self._db = db
        self._audit = audit
        self._broker = broker
        self._ledger = Ledger(db, audit)

    # ------------------------------------------------------------------ cursor

    def _cursor(self, symbol: str) -> int | None:
        """Highest exchange trade id already ingested for this symbol.

        Persisted implicitly in the fills table rather than in a side file, so the
        cursor cannot drift away from what was actually recorded.
        """
        row = self._db.connection.execute(
            "SELECT f.exchange_trade_id FROM fills f "
            "JOIN orders o ON o.id = f.order_id "
            "WHERE o.symbol = ? AND f.exchange_trade_id IS NOT NULL",
            (symbol.upper(),),
        ).fetchall()
        ids = [int(r["exchange_trade_id"]) for r in row if str(r["exchange_trade_id"]).isdigit()]
        return max(ids) if ids else None

    # ------------------------------------------------------------------ ingest

    def ingest(self, symbol: str) -> IngestReport:
        """Fetch and record trades for one symbol.

        Resumes from the last recorded trade id. A trade whose `clientOrderId` ATLAS
        does not know is reported as an orphan rather than invented into the ledger:
        an unrecognised trade is unrecorded exposure, which is reconciliation's problem
        (EXEC-05), not ingestion's to paper over.
        """
        cursor = self._cursor(symbol)
        from_id = cursor + 1 if cursor is not None else None
        trades = self._broker.my_trades(symbol, from_id=from_id)

        recorded = 0
        duplicates = 0
        orphans: list[str] = []
        closed_positions: list[str] = []

        for trade in trades:
            client_order_id = str(trade.get("clientOrderId", ""))
            if not client_order_id:
                orphans.append(f"trade {trade.get('id')} has no clientOrderId")
                continue
            try:
                fill = _to_fill(trade, client_order_id)
            except (KeyError, ValueError, TypeError, ArithmeticError, OverflowError) as exc:
                # ArithmeticError covers decimal.InvalidOperation, which a malformed
                # price or quantity raises and which is NOT a ValueError. Without it a
                # single bad field from the exchange would abort the whole poll.
                orphans.append(f"trade {trade.get('id')} unparseable: {exc}")
                continue

            try:
                if self._ledger.record_fill(fill):
                    recorded += 1
                    closed = self._close_if_exit_filled(client_order_id, fill.price)
                    if closed is not None:
                        closed_positions.append(closed)
                else:
                    duplicates += 1
            except ValueError:
                # record_fill raises when the order is unknown to ATLAS.
                orphans.append(client_order_id)

        report = IngestReport(
            symbol=symbol.upper(),
            seen=len(trades),
            recorded=recorded,
            duplicates=duplicates,
            orphans=orphans,
            closed_positions=closed_positions,
        )
        self._audit.append(
            AuditEventType.RECONCILIATION,
            {
                "event": "fill_ingest",
                "symbol": report.symbol,
                "seen": report.seen,
                "recorded": report.recorded,
                "duplicates": report.duplicates,
                "orphans": report.orphans,
                "closed_positions": report.closed_positions,
            },
            INGEST_ACTOR,
        )
        return report

    def _close_if_exit_filled(self, client_order_id: str, price: Decimal) -> str | None:
        """Close the position when its protective order has filled in full.

        A stop or target filling *is* the position closing, and nothing else observes
        it: the exchange sends no separate notification, and the order simply stops
        appearing in openOrders. Without this the ledger holds the position open
        forever, which blocks the symbol under RISK-08, overstates deployed capital and
        marked equity, and starves the monitor of the realised return that decides
        whether the strategy keeps trading.

        A partial fill closes nothing. The remainder of the position is still held and
        still protected by the unfilled remainder of the order.
        """
        row = self._ledger.order_row(client_order_id)
        if row is None:
            return None
        if str(row.get("role")) not in {"STOP", "TARGET"}:
            return None

        position_id = row.get("position_id")
        if not position_id:
            return None
        position = self._ledger.position(str(position_id))
        if position is None or not position.is_open:
            return None

        if self._ledger.filled_quantity(client_order_id) < position.quantity:
            return None

        self._ledger.close_position(position.id, price)
        self._audit.append(
            AuditEventType.RECONCILIATION,
            {
                "event": "position_closed_by_fill",
                "position_id": position.id,
                "symbol": position.symbol,
                "client_order_id": client_order_id,
                "exit_price": str(price),
            },
            INGEST_ACTOR,
        )
        return str(position.id)

    def ingest_all(self, symbols: list[str]) -> list[IngestReport]:
        """Ingest every symbol. One symbol's failure must not stop the others."""
        reports: list[IngestReport] = []
        for symbol in symbols:
            try:
                reports.append(self.ingest(symbol))
            except Exception as exc:
                self._audit.append(
                    AuditEventType.RECONCILIATION,
                    {"event": "fill_ingest_failed", "symbol": symbol, "error": str(exc)},
                    INGEST_ACTOR,
                )
                reports.append(IngestReport(symbol=symbol, orphans=[f"ingest failed: {exc}"]))
        return reports


def _to_fill(trade: dict[str, Any], client_order_id: str) -> Fill:
    """Map one Binance trade record onto a Fill.

    Prices and quantities are parsed via `str()` into Decimal - Binance returns them as
    strings, and going through float would discard exactness (ADR-003).
    """
    return Fill(
        order_client_id=client_order_id,
        quantity=Decimal(str(trade["qty"])),
        price=Decimal(str(trade["price"])),
        fee=Decimal(str(trade.get("commission", "0"))),
        fee_asset=str(trade.get("commissionAsset", "")),
        filled_at=datetime.fromtimestamp(int(trade["time"]) / 1000, tz=UTC),
        exchange_trade_id=str(trade["id"]),
    )
