"""AI-07 / ADR-005: hash-chained append-only audit log."""

from __future__ import annotations

import pytest

from atlas.audit import GENESIS_HASH, AuditLog
from atlas.db.engine import Database
from atlas.errors import AuditIntegrityError
from atlas.models import AuditEventType


def test_first_event_links_to_genesis(audit: AuditLog) -> None:
    event = audit.append(AuditEventType.SYSTEM, {"msg": "start"}, "system")
    assert event.seq == 0
    assert event.prev_hash == GENESIS_HASH


def test_events_chain_together(audit: AuditLog) -> None:
    first = audit.append(AuditEventType.SYSTEM, {"n": 1}, "system")
    second = audit.append(AuditEventType.SYSTEM, {"n": 2}, "system")
    assert second.prev_hash == first.event_hash
    assert second.seq == first.seq + 1


def test_chain_verifies(audit: AuditLog) -> None:
    for i in range(25):
        audit.append(AuditEventType.SYSTEM, {"n": i}, "system")
    assert audit.verify_chain() == 25


def test_empty_chain_verifies(audit: AuditLog) -> None:
    assert audit.verify_chain() == 0


def test_tamper_detected_on_payload_edit(audit: AuditLog, db: Database) -> None:
    """The core property: editing history is detectable."""
    for i in range(5):
        audit.append(AuditEventType.SYSTEM, {"n": i}, "system")

    db.connection.execute("UPDATE audit_events SET payload = ? WHERE seq = 2", ('{"n":999}',))
    with pytest.raises(AuditIntegrityError, match="was modified"):
        audit.verify_chain()


def test_tamper_detected_on_actor_edit(audit: AuditLog, db: Database) -> None:
    audit.append(AuditEventType.AI_ACTION, {"action": "propose"}, "research")
    db.connection.execute("UPDATE audit_events SET actor = 'operator' WHERE seq = 0")
    with pytest.raises(AuditIntegrityError, match="was modified"):
        audit.verify_chain()


def test_deletion_detected(audit: AuditLog, db: Database) -> None:
    """Removing an event leaves a sequence gap."""
    for i in range(5):
        audit.append(AuditEventType.SYSTEM, {"n": i}, "system")
    db.connection.execute("DELETE FROM audit_events WHERE seq = 2")
    with pytest.raises(AuditIntegrityError, match="sequence gap"):
        audit.verify_chain()


def test_broken_link_detected(audit: AuditLog, db: Database) -> None:
    for i in range(4):
        audit.append(AuditEventType.SYSTEM, {"n": i}, "system")
    db.connection.execute("UPDATE audit_events SET prev_hash = ? WHERE seq = 2", ("f" * 64,))
    with pytest.raises(AuditIntegrityError, match="chain broken"):
        audit.verify_chain()


def test_tail_returns_oldest_first(audit: AuditLog) -> None:
    for i in range(10):
        audit.append(AuditEventType.SYSTEM, {"n": i}, "system")
    tail = audit.tail(3)
    assert [e.seq for e in tail] == [7, 8, 9]


def test_hash_is_deterministic_for_same_content(audit: AuditLog) -> None:
    from atlas.audit import compute_event_hash

    args = (0, "SYSTEM", {"b": 2, "a": 1}, "system", "2026-01-01T00:00:00+00:00", GENESIS_HASH)
    assert compute_event_hash(*args) == compute_event_hash(*args)


def test_hash_ignores_dict_ordering(audit: AuditLog) -> None:
    """Canonical JSON: key order must not change the hash."""
    from atlas.audit import compute_event_hash

    at, prev = "2026-01-01T00:00:00+00:00", GENESIS_HASH
    a = compute_event_hash(0, "SYSTEM", {"a": 1, "b": 2}, "system", at, prev)
    b = compute_event_hash(0, "SYSTEM", {"b": 2, "a": 1}, "system", at, prev)
    assert a == b
