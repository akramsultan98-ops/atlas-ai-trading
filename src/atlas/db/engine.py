"""SQLite/WAL persistence (ADR-001, ADR-002).

The execution process is the sole writer. The research plane connects read-only, which
is how AI-03 is enforced at the database level rather than by convention.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from atlas.errors import PersistenceError

SCHEMA_VERSION = "2"
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA busy_timeout = 5000")


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to the current shape.

    `CREATE TABLE IF NOT EXISTS` silently leaves an older table alone, so a column
    added to schema.sql never reaches a database that already exists. Each step is
    idempotent and checks for its own effect rather than trusting a version number.
    """
    columns = {str(r["name"]) for r in conn.execute("PRAGMA table_info(orders)")}
    if columns and "position_id" not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN position_id TEXT")


def connect(path: Path) -> sqlite3.Connection:
    """Open a read-write connection, creating the schema if absent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    _configure(conn)
    _migrate(conn)
    conn.executescript(_SCHEMA_PATH.read_text())
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SCHEMA_VERSION,),
    )
    return conn


def connect_readonly(path: Path) -> sqlite3.Connection:
    """Open a read-only connection for the advisory plane (AI-03).

    Writes raise `sqlite3.OperationalError`. The restriction is enforced by SQLite, not
    by the caller remembering to behave.
    """
    if not path.exists():
        raise PersistenceError(f"database does not exist: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


class Database:
    """Owns a read-write connection and its transaction boundaries."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn = connect(path)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Atomic unit of work. Rolls back on any exception."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
