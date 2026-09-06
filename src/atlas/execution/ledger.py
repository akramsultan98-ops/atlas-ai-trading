"""Order, fill and position ledger (EXEC-04, EXEC-10).

The schema has carried `orders`, `fills` and `positions` since Phase 1; this is what
writes to them. Without it "full order and fill reconciliation" is only half true: order
state was reconciled against the exchange, but nothing recorded what actually filled or
what the resulting position was.

Fills are idempotent on the exchange trade id. Exchange APIs re-deliver trades on
reconnect and on a polled fetch overlapping a previous window, so recording the same
trade twice would double a position that never doubled.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from atlas.audit import AuditLog
from atlas.db.engine import Database
from atlas.execution.broker import OrderRequest, OrderRole
from atlas.models import AuditEventType, ExchangeEnv, OrderSide, PositionSide, utcnow

LEDGER_ACTOR = "execution"
ZERO = Decimal(0)


@dataclass(frozen=True)
class Fill:
    order_client_id: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_asset: str
    filled_at: datetime
    exchange_trade_id: str


@dataclass(frozen=True)
class Position:
    id: str
    strategy_id: str
    symbol: str
    side: PositionSide
    quantity: Decimal
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    opened_at: datetime
    closed_at: datetime | None = None
    exit_price: Decimal | None = None
    realised_pnl: Decimal | None = None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


class Ledger:
    """Records orders, fills and the positions they produce."""

    def __init__(self, db: Database, audit: AuditLog) -> None:
        self._db = db
        self._audit = audit

    # ------------------------------------------------------------------ orders

    def record_order(
        self,
        request: OrderRequest,
        strategy_id: str,
        exchange_env: ExchangeEnv,
        *,
        status: str = "SUBMITTED",
        exchange_order_id: str | None = None,
    ) -> str:
        """Persist a submitted order. Idempotent on the client order id (EXEC-03)."""
        existing = self._db.connection.execute(
            "SELECT id FROM orders WHERE client_order_id = ?", (request.client_order_id,)
        ).fetchone()
        if existing is not None:
            return str(existing["id"])

        order_id = str(uuid.uuid4())
        now = utcnow().isoformat()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO orders(id, client_order_id, strategy_id, exchange_order_id, "
                "symbol, side, order_type, role, quantity, price, status, exchange_env, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    order_id,
                    request.client_order_id,
                    strategy_id,
                    exchange_order_id,
                    request.symbol.upper(),
                    str(request.side),
                    request.order_type,
                    str(request.role),
                    str(request.quantity),
                    str(request.price) if request.price is not None else None,
                    status,
                    str(exchange_env),
                    now,
                    now,
                ),
            )
        return order_id

    def update_order_status(self, client_order_id: str, status: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE orders SET status = ?, updated_at = ? WHERE client_order_id = ?",
                (status, utcnow().isoformat(), client_order_id),
            )

    # ------------------------------------------------------------------- fills

    def record_fill(self, fill: Fill) -> bool:
        """Record a fill. Returns False if this exchange trade was already seen.

        Idempotent because exchange APIs re-deliver trades on reconnect and on
        overlapping polled windows.
        """
        order = self._db.connection.execute(
            "SELECT id FROM orders WHERE client_order_id = ?", (fill.order_client_id,)
        ).fetchone()
        if order is None:
            raise ValueError(f"fill references unknown order {fill.order_client_id}")

        seen = self._db.connection.execute(
            "SELECT 1 FROM fills WHERE exchange_trade_id = ?", (fill.exchange_trade_id,)
        ).fetchone()
        if seen is not None:
            return False

        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO fills(id, order_id, quantity, price, fee, fee_asset, "
                "filled_at, exchange_trade_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    str(order["id"]),
                    str(fill.quantity),
                    str(fill.price),
                    str(fill.fee),
                    fill.fee_asset,
                    fill.filled_at.isoformat(),
                    fill.exchange_trade_id,
                ),
            )
        self._audit.append(
            AuditEventType.FILL_RECORDED,
            {
                "client_order_id": fill.order_client_id,
                "exchange_trade_id": fill.exchange_trade_id,
                "quantity": str(fill.quantity),
                "price": str(fill.price),
            },
            LEDGER_ACTOR,
        )
        return True

    def filled_quantity(self, client_order_id: str) -> Decimal:
        # Summed in Python rather than with SQL SUM(): SQLite would aggregate through
        # REAL, and money must never round-trip via a float (ADR-003).
        rows = self._db.connection.execute(
            "SELECT f.quantity FROM fills f JOIN orders o ON o.id = f.order_id "
            "WHERE o.client_order_id = ?",
            (client_order_id,),
        ).fetchall()
        return sum((Decimal(str(r["quantity"])) for r in rows), ZERO) if rows else ZERO

    # --------------------------------------------------------------- positions

    def open_position(
        self,
        strategy_id: str,
        symbol: str,
        side: PositionSide,
        quantity: Decimal,
        entry_price: Decimal,
        stop_price: Decimal,
        target_price: Decimal,
    ) -> str:
        position_id = str(uuid.uuid4())
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO positions(id, strategy_id, symbol, side, quantity, "
                "entry_price, stop_price, target_price, opened_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    position_id,
                    strategy_id,
                    symbol.upper(),
                    str(side),
                    str(quantity),
                    str(entry_price),
                    str(stop_price),
                    str(target_price),
                    utcnow().isoformat(),
                ),
            )
        return position_id

    def close_position(self, position_id: str, exit_price: Decimal) -> Decimal:
        """Close a position and return realised PnL, gross of fees."""
        row = self._db.connection.execute(
            "SELECT side, quantity, entry_price, closed_at FROM positions WHERE id = ?",
            (position_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown position {position_id}")
        if row["closed_at"] is not None:
            raise ValueError(f"position {position_id} is already closed")

        side = PositionSide(row["side"])
        quantity = Decimal(str(row["quantity"]))
        entry = Decimal(str(row["entry_price"]))
        direction = Decimal(1) if side is PositionSide.LONG else Decimal(-1)
        pnl = (exit_price - entry) * quantity * direction

        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE positions SET closed_at = ?, exit_price = ?, realised_pnl = ? WHERE id = ?",
                (utcnow().isoformat(), str(exit_price), str(pnl), position_id),
            )
        return pnl

    def open_positions(self, strategy_id: str | None = None) -> list[Position]:
        query = "SELECT * FROM positions WHERE closed_at IS NULL"
        params: tuple[str, ...] = ()
        if strategy_id is not None:
            query += " AND strategy_id = ?"
            params = (strategy_id,)
        return [
            self._row_to_position(r) for r in self._db.connection.execute(query, params).fetchall()
        ]

    def open_symbols(self) -> frozenset[str]:
        """RISK-08 input: symbols currently holding a position."""
        return frozenset(p.symbol for p in self.open_positions())

    def realised_returns(self, strategy_id: str) -> list[Decimal]:
        """Closed-trade returns for the monitor (MON-01..04)."""
        rows = self._db.connection.execute(
            "SELECT entry_price, realised_pnl, quantity FROM positions "
            "WHERE strategy_id = ? AND closed_at IS NOT NULL ORDER BY closed_at",
            (strategy_id,),
        ).fetchall()
        returns: list[Decimal] = []
        for r in rows:
            entry = Decimal(str(r["entry_price"]))
            quantity = Decimal(str(r["quantity"]))
            notional = entry * quantity
            if notional > 0 and r["realised_pnl"] is not None:
                returns.append(Decimal(str(r["realised_pnl"])) / notional)
        return returns

    @staticmethod
    def _row_to_position(row: object) -> Position:
        r: dict[str, object] = dict(row)  # type: ignore[call-overload]
        return Position(
            id=str(r["id"]),
            strategy_id=str(r["strategy_id"]),
            symbol=str(r["symbol"]),
            side=PositionSide(str(r["side"])),
            quantity=Decimal(str(r["quantity"])),
            entry_price=Decimal(str(r["entry_price"])),
            stop_price=Decimal(str(r["stop_price"])),
            target_price=Decimal(str(r["target_price"])),
            opened_at=datetime.fromisoformat(str(r["opened_at"])),
            closed_at=datetime.fromisoformat(str(r["closed_at"])) if r["closed_at"] else None,
            exit_price=Decimal(str(r["exit_price"])) if r["exit_price"] else None,
            realised_pnl=Decimal(str(r["realised_pnl"])) if r["realised_pnl"] else None,
        )


__all__ = ["Fill", "Ledger", "OrderRole", "OrderSide", "Position"]
