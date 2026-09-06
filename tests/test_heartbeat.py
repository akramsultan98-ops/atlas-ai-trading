"""Heartbeat and health reporting (Phase O)."""

from __future__ import annotations

from datetime import timedelta

from atlas.db.engine import Database
from atlas.models import ExchangeEnv, utcnow
from atlas.ops.heartbeat import HeartbeatStore


def test_no_heartbeat_before_the_first_tick(db: Database) -> None:
    assert HeartbeatStore(db).read() is None


def test_beat_is_recorded(db: Database) -> None:
    store = HeartbeatStore(db)
    store.beat(tick_count=1, exchange_env=ExchangeEnv.TESTNET, exchange_reachable=True)
    beat = store.read()
    assert beat is not None
    assert beat.tick_count == 1
    assert beat.exchange_reachable
    assert beat.last_error is None


def test_heartbeat_is_a_single_row(db: Database) -> None:
    """The question 'is ATLAS alive now' has one answer; history belongs in the audit."""
    store = HeartbeatStore(db)
    for i in range(1, 6):
        store.beat(tick_count=i, exchange_env=ExchangeEnv.TESTNET, exchange_reachable=True)
    assert db.connection.execute("SELECT COUNT(*) AS n FROM heartbeat").fetchone()["n"] == 1
    read = store.read()
    assert read is not None and read.tick_count == 5


def test_failure_is_recorded_in_the_heartbeat(db: Database) -> None:
    """A hung process and one failing every cycle look identical without this."""
    store = HeartbeatStore(db)
    store.beat(
        tick_count=3,
        exchange_env=ExchangeEnv.TESTNET,
        exchange_reachable=False,
        last_error="OrderRejection: 403 Forbidden",
    )
    beat = store.read()
    assert beat is not None
    assert beat.last_error is not None
    assert "403" in beat.last_error
    assert not beat.exchange_reachable


def test_fresh_heartbeat_is_not_stale(db: Database) -> None:
    store = HeartbeatStore(db)
    store.beat(tick_count=1, exchange_env=ExchangeEnv.TESTNET, exchange_reachable=True)
    beat = store.read()
    assert beat is not None
    assert not beat.is_stale(timedelta(minutes=5))


def test_old_heartbeat_is_stale(db: Database) -> None:
    store = HeartbeatStore(db)
    store.beat(tick_count=1, exchange_env=ExchangeEnv.TESTNET, exchange_reachable=True)
    beat = store.read()
    assert beat is not None
    assert beat.is_stale(timedelta(seconds=1), now=utcnow() + timedelta(hours=1))


def test_beat_survives_reconstruction(db: Database) -> None:
    """Restart recovery reads it back from disk, not memory."""
    HeartbeatStore(db).beat(tick_count=9, exchange_env=ExchangeEnv.TESTNET, exchange_reachable=True)
    beat = HeartbeatStore(db).read()
    assert beat is not None and beat.tick_count == 9
