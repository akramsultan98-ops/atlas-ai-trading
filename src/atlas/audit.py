"""Hash-chained append-only audit log (AI-07, EXEC-10, ADR-005).

"Append-only by convention" is a hope, not a property. Each event's hash covers the
previous event's hash, so modifying, reordering or deleting any historical event breaks
every subsequent link and is detectable by walking the chain.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from atlas.db.engine import Database
from atlas.errors import AuditIntegrityError
from atlas.models import AuditEvent, AuditEventType, utcnow

GENESIS_HASH = "0" * 64


def compute_event_hash(
    seq: int,
    event_type: str,
    payload: dict[str, Any],
    actor: str,
    at: str,
    prev_hash: str,
) -> str:
    """Hash one event. Deterministic: sorted keys, compact separators, UTF-8."""
    canonical = json.dumps(
        {
            "seq": seq,
            "event_type": event_type,
            "payload": payload,
            "actor": actor,
            "at": at,
            "prev_hash": prev_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuditLog:
    """Append-only event log over the `audit_events` table."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def _head(self, conn: sqlite3.Connection) -> tuple[int, str]:
        """Return `(next_seq, prev_hash)` for the next append."""
        row = conn.execute(
            "SELECT seq, event_hash FROM audit_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return 0, GENESIS_HASH
        return int(row["seq"]) + 1, str(row["event_hash"])

    def append(
        self,
        event_type: AuditEventType,
        payload: dict[str, Any],
        actor: str,
    ) -> AuditEvent:
        """Append an event and return it.

        Sequence allocation and insertion share one transaction, so concurrent appends
        cannot produce a forked chain.
        """
        at = utcnow()
        at_str = at.isoformat()
        with self._db.transaction() as conn:
            seq, prev_hash = self._head(conn)
            event_hash = compute_event_hash(seq, str(event_type), payload, actor, at_str, prev_hash)
            conn.execute(
                "INSERT INTO audit_events(seq, event_type, payload, actor, at, prev_hash, event_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    seq,
                    str(event_type),
                    json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
                    actor,
                    at_str,
                    prev_hash,
                    event_hash,
                ),
            )
        return AuditEvent(
            seq=seq,
            event_type=event_type,
            payload=payload,
            actor=actor,
            at=at,
            prev_hash=prev_hash,
            event_hash=event_hash,
        )

    def verify_chain(self) -> int:
        """Walk the whole chain. Return the number of events verified.

        Raises `AuditIntegrityError` on the first inconsistency: a broken link, a
        recomputed hash mismatch, or a sequence gap.
        """
        conn = self._db.connection
        rows = conn.execute(
            "SELECT seq, event_type, payload, actor, at, prev_hash, event_hash "
            "FROM audit_events ORDER BY seq ASC"
        ).fetchall()

        expected_prev = GENESIS_HASH
        for index, row in enumerate(rows):
            seq = int(row["seq"])
            if seq != index:
                raise AuditIntegrityError(f"audit sequence gap: expected seq {index}, found {seq}")
            if row["prev_hash"] != expected_prev:
                raise AuditIntegrityError(
                    f"audit chain broken at seq {seq}: prev_hash does not match "
                    f"the preceding event's hash"
                )
            recomputed = compute_event_hash(
                seq,
                str(row["event_type"]),
                json.loads(row["payload"]),
                str(row["actor"]),
                str(row["at"]),
                str(row["prev_hash"]),
            )
            if recomputed != row["event_hash"]:
                raise AuditIntegrityError(
                    f"audit event {seq} was modified: stored hash does not match its content"
                )
            expected_prev = str(row["event_hash"])
        return len(rows)

    def tail(self, limit: int = 50) -> list[AuditEvent]:
        """Most recent events, oldest first."""
        rows = self._db.connection.execute(
            "SELECT seq, event_type, payload, actor, at, prev_hash, event_hash "
            "FROM audit_events ORDER BY seq DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            AuditEvent(
                seq=int(r["seq"]),
                event_type=AuditEventType(r["event_type"]),
                payload=json.loads(r["payload"]),
                actor=str(r["actor"]),
                at=r["at"],
                prev_hash=str(r["prev_hash"]),
                event_hash=str(r["event_hash"]),
            )
            for r in reversed(rows)
        ]

    def count(self) -> int:
        row = self._db.connection.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()
        return int(row["n"])
