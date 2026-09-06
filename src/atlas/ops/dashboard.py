"""Control panel data (OPS).

David builds a dashboard because "you'll end up with hundreds of strategies and no way
to actually see the backtest results easily" [09:22-09:39]. This produces the data; the
rendering is left to the operator's tool of choice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from atlas.db.engine import Database
from atlas.models import StrategyStatus


@dataclass(frozen=True)
class StrategyRow:
    strategy_id: str
    symbol: str
    timeframe: str
    status: StrategyStatus
    created_at: str
    retired_at: str | None
    retire_reason: str | None
    backtest_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "status": str(self.status),
            "created_at": self.created_at,
            "retired_at": self.retired_at,
            "retire_reason": self.retire_reason,
            "backtest_count": self.backtest_count,
        }


class Dashboard:
    """Read-only views over the factory's state."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def strategies(self, status: StrategyStatus | None = None) -> list[StrategyRow]:
        query = (
            "SELECT s.id, s.symbol, s.timeframe, s.status, s.created_at, s.retired_at, "
            "s.retire_reason, (SELECT COUNT(*) FROM backtests b WHERE b.strategy_id = s.id) "
            "AS backtest_count FROM strategies s"
        )
        params: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE s.status = ?"
            params = (str(status),)
        query += " ORDER BY s.created_at DESC"

        return [
            StrategyRow(
                strategy_id=str(r["id"]),
                symbol=str(r["symbol"]),
                timeframe=str(r["timeframe"]),
                status=StrategyStatus(r["status"]),
                created_at=str(r["created_at"]),
                retired_at=str(r["retired_at"]) if r["retired_at"] else None,
                retire_reason=str(r["retire_reason"]) if r["retire_reason"] else None,
                backtest_count=int(r["backtest_count"]),
            )
            for r in self._db.connection.execute(query, params).fetchall()
        ]

    def funnel(self) -> dict[str, int]:
        """Counts per lifecycle stage — the factory's throughput at a glance."""
        rows = self._db.connection.execute(
            "SELECT status, COUNT(*) AS n FROM strategies GROUP BY status"
        ).fetchall()
        counts = {str(s): 0 for s in StrategyStatus}
        for row in rows:
            counts[str(row["status"])] = int(row["n"])
        return counts

    def summary(self) -> dict[str, Any]:
        funnel = self.funnel()
        total = sum(funnel.values())
        reached_live = funnel.get(str(StrategyStatus.LIVE), 0)
        return {
            "total_candidates": total,
            "live": reached_live,
            "retired": funnel.get(str(StrategyStatus.RETIRED), 0),
            "survival_rate": (reached_live / total) if total else 0.0,
            "funnel": funnel,
        }
