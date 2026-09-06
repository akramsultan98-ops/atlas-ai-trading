"""ADR-001, ADR-002, AI-03."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from atlas.db.engine import Database, connect_readonly
from atlas.errors import PersistenceError


def test_schema_creates_all_tables(db: Database) -> None:
    rows = db.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    names = {r["name"] for r in rows}
    for expected in (
        "audit_events",
        "kill_switch_state",
        "strategy_specs",
        "strategies",
        "backtests",
        "verifications",
        "selection_results",
        "incubation_signals",
        "promotions",
        "orders",
        "fills",
        "positions",
        "equity_snapshots",
    ):
        assert expected in names, f"missing table: {expected}"


def test_foreign_keys_enforced(db: Database) -> None:
    """ADR-002: a strategy cannot reference a spec that does not exist."""
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO strategies(id, spec_hash, symbol, timeframe, status, "
            "created_at, status_at) VALUES ('s1', 'missing', 'BTCUSDT', '1h', "
            "'CANDIDATE', '2026-01-01', '2026-01-01')"
        )


def test_status_check_constraint(db: Database) -> None:
    db.connection.execute(
        "INSERT INTO strategy_specs(spec_hash, payload, created_at) VALUES (?, '{}', '2026-01-01')",
        ("a" * 64,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO strategies(id, spec_hash, symbol, timeframe, status, "
            "created_at, status_at) VALUES ('s1', ?, 'BTCUSDT', '1h', 'BOGUS', "
            "'2026-01-01', '2026-01-01')",
            ("a" * 64,),
        )


def test_retired_strategy_must_have_retired_at(db: Database) -> None:
    """The schema enforces the invariant, not just the Python layer."""
    db.connection.execute(
        "INSERT INTO strategy_specs(spec_hash, payload, created_at) VALUES (?, '{}', '2026-01-01')",
        ("b" * 64,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO strategies(id, spec_hash, symbol, timeframe, status, "
            "created_at, status_at) VALUES ('s2', ?, 'BTCUSDT', '1h', 'RETIRED', "
            "'2026-01-01', '2026-01-01')",
            ("b" * 64,),
        )


def test_kill_switch_state_is_single_row(db: Database) -> None:
    db.connection.execute(
        "INSERT INTO kill_switch_state(id, state, reason, actor, at) "
        "VALUES (1, 'ARMED', 'r', 'a', '2026-01-01')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO kill_switch_state(id, state, reason, actor, at) "
            "VALUES (2, 'ARMED', 'r', 'a', '2026-01-01')"
        )


def test_audit_hash_length_constrained(db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO audit_events(seq, event_type, payload, actor, at, prev_hash, event_hash) "
            "VALUES (0, 'SYSTEM', '{}', 'a', '2026-01-01', 'short', ?)",
            ("c" * 64,),
        )


def test_transaction_rolls_back_on_error(db: Database) -> None:
    with pytest.raises(RuntimeError), db.transaction() as conn:
        conn.execute(
            "INSERT INTO strategy_specs(spec_hash, payload, created_at) "
            "VALUES (?, '{}', '2026-01-01')",
            ("d" * 64,),
        )
        raise RuntimeError("boom")

    row = db.connection.execute("SELECT COUNT(*) AS n FROM strategy_specs").fetchone()
    assert row["n"] == 0


def test_readonly_connection_cannot_write(db: Database, tmp_path: Path) -> None:
    """AI-03: enforced by SQLite, not by the caller remembering to behave."""
    ro = connect_readonly(db.path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute(
                "INSERT INTO strategy_specs(spec_hash, payload, created_at) "
                "VALUES (?, '{}', '2026-01-01')",
                ("e" * 64,),
            )
    finally:
        ro.close()


def test_readonly_connection_can_read(db: Database) -> None:
    db.connection.execute(
        "INSERT INTO strategy_specs(spec_hash, payload, created_at) VALUES (?, '{}', '2026-01-01')",
        ("f" * 64,),
    )
    ro = connect_readonly(db.path)
    try:
        row = ro.execute("SELECT COUNT(*) AS n FROM strategy_specs").fetchone()
        assert row["n"] == 1
    finally:
        ro.close()


def test_readonly_on_missing_database_raises(tmp_path: Path) -> None:
    with pytest.raises(PersistenceError):
        connect_readonly(tmp_path / "nope.db")


def test_wal_mode_enabled(db: Database) -> None:
    mode = db.connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"
