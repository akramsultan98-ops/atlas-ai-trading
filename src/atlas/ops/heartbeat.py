"""Process heartbeat (OPS).

A log line proves a process wrote something once. A heartbeat row proves it was alive
at a known instant, and an external health check can read it without parsing logs.

Deliberately a single row: the question "is ATLAS alive right now" has one answer, and
an append-only heartbeat would grow without bound for no benefit. The audit log is
where history belongs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from atlas.db.engine import Database
from atlas.models import ExchangeEnv, utcnow


@dataclass(frozen=True)
class Heartbeat:
    at: datetime
    tick_count: int
    last_error: str | None
    exchange_env: str
    exchange_reachable: bool

    def age(self, now: datetime | None = None) -> timedelta:
        return (now or utcnow()) - self.at

    def is_stale(self, tolerance: timedelta, now: datetime | None = None) -> bool:
        return self.age(now) > tolerance


class HeartbeatStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def beat(
        self,
        *,
        tick_count: int,
        exchange_env: ExchangeEnv,
        exchange_reachable: bool,
        last_error: str | None = None,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO heartbeat(id, at, tick_count, last_error, exchange_env, "
                "exchange_reachable) VALUES (1, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET at=excluded.at, tick_count=excluded.tick_count, "
                "last_error=excluded.last_error, exchange_env=excluded.exchange_env, "
                "exchange_reachable=excluded.exchange_reachable",
                (
                    utcnow().isoformat(),
                    tick_count,
                    last_error,
                    str(exchange_env),
                    1 if exchange_reachable else 0,
                ),
            )

    def read(self) -> Heartbeat | None:
        row = self._db.connection.execute("SELECT * FROM heartbeat WHERE id = 1").fetchone()
        if row is None:
            return None
        return Heartbeat(
            at=datetime.fromisoformat(str(row["at"])),
            tick_count=int(row["tick_count"]),
            last_error=str(row["last_error"]) if row["last_error"] else None,
            exchange_env=str(row["exchange_env"]),
            exchange_reachable=bool(row["exchange_reachable"]),
        )
