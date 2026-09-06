"""Content-hash strategy registry (STRAT-06)."""

from __future__ import annotations

import uuid
from datetime import datetime

from atlas.db.engine import Database
from atlas.models import StrategyStatus, utcnow
from atlas.strategy.spec import StrategySpec


class StrategyRegistry:
    """Persists immutable specs and the mutable lifecycle state that points at them."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def register(self, spec: StrategySpec) -> str:
        """Store a spec and create a CANDIDATE strategy. Returns the strategy id.

        Registering the same spec twice returns the existing strategy: identical rules
        are the same strategy, and re-registering must not fork its evidence.
        """
        spec_hash = spec.content_hash()
        existing = self._db.connection.execute(
            "SELECT id FROM strategies WHERE spec_hash = ?", (spec_hash,)
        ).fetchone()
        if existing is not None:
            return str(existing["id"])

        strategy_id = str(uuid.uuid4())
        now = utcnow().isoformat()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO strategy_specs(spec_hash, payload, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(spec_hash) DO NOTHING",
                (spec_hash, spec.to_json(), now),
            )
            conn.execute(
                "INSERT INTO strategies(id, spec_hash, symbol, timeframe, status, "
                "created_at, status_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    strategy_id,
                    spec_hash,
                    spec.symbol,
                    str(spec.timeframe),
                    str(StrategyStatus.CANDIDATE),
                    now,
                    now,
                ),
            )
        return strategy_id

    def load_spec(self, strategy_id: str) -> StrategySpec | None:
        row = self._db.connection.execute(
            "SELECT s.payload FROM strategy_specs s "
            "JOIN strategies g ON g.spec_hash = s.spec_hash WHERE g.id = ?",
            (strategy_id,),
        ).fetchone()
        return StrategySpec.from_json(str(row["payload"])) if row else None

    def status(self, strategy_id: str) -> StrategyStatus | None:
        row = self._db.connection.execute(
            "SELECT status FROM strategies WHERE id = ?", (strategy_id,)
        ).fetchone()
        return StrategyStatus(row["status"]) if row else None

    def set_status(
        self,
        strategy_id: str,
        status: StrategyStatus,
        *,
        reason: str = "",
        at: datetime | None = None,
    ) -> None:
        """Update lifecycle state. RETIRED is terminal (MON-06)."""
        current = self.status(strategy_id)
        if current is StrategyStatus.RETIRED and status is not StrategyStatus.RETIRED:
            raise ValueError(
                f"strategy {strategy_id} is RETIRED; reactivation is a human action "
                "taken outside the trading loop (MON-06)"
            )
        stamp = (at or utcnow()).isoformat()
        with self._db.transaction() as conn:
            if status is StrategyStatus.RETIRED:
                conn.execute(
                    "UPDATE strategies SET status = ?, status_at = ?, retired_at = ?, "
                    "retire_reason = ? WHERE id = ?",
                    (str(status), stamp, stamp, reason, strategy_id),
                )
            else:
                conn.execute(
                    "UPDATE strategies SET status = ?, status_at = ? WHERE id = ?",
                    (str(status), stamp, strategy_id),
                )

    def list_by_status(self, status: StrategyStatus) -> list[str]:
        rows = self._db.connection.execute(
            "SELECT id FROM strategies WHERE status = ? ORDER BY created_at", (str(status),)
        ).fetchall()
        return [str(r["id"]) for r in rows]
