"""KILL-01..06 and ADR-004.

The governing asymmetry: failing open risks unbounded loss, failing closed risks missed
trades. Every ambiguity must resolve to ARMED.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atlas.audit import AuditLog
from atlas.db.engine import Database
from atlas.errors import KillSwitchArmedError
from atlas.killswitch import KillSwitch
from atlas.models import KillSwitchState, KillSwitchTrigger


def test_uninitialised_system_is_armed(killswitch: KillSwitch) -> None:
    """KILL-06: a system that has never been released to trade is halted."""
    state = killswitch.read_state()
    assert state.state is KillSwitchState.ARMED
    assert state.trigger is KillSwitchTrigger.STATE_UNREADABLE
    assert killswitch.is_armed()


def test_initialise_releases_to_trade(killswitch: KillSwitch) -> None:
    killswitch.initialise()
    assert not killswitch.is_armed()
    killswitch.check_can_enter()  # must not raise


def test_arm_blocks_trading(killswitch: KillSwitch) -> None:
    """KILL-01."""
    killswitch.initialise()
    killswitch.arm(KillSwitchTrigger.DAILY_LOSS_LIMIT, "lost 5% today")
    assert killswitch.is_armed()
    with pytest.raises(KillSwitchArmedError, match="DAILY_LOSS_LIMIT"):
        killswitch.check_can_enter()


def test_protective_orders_allowed_while_armed(killswitch: KillSwitch) -> None:
    """KILL-03: cancelling stops while armed would leave positions naked."""
    killswitch.initialise()
    killswitch.arm(KillSwitchTrigger.MAX_ACCOUNT_DD, "20% drawdown")
    assert killswitch.is_armed()
    killswitch.check_can_place_protective()  # must not raise


def test_disarm_requires_human_confirmation(killswitch: KillSwitch) -> None:
    """KILL-05: no automated or AI-initiated path."""
    killswitch.initialise()
    killswitch.arm(KillSwitchTrigger.MANUAL, "testing")
    with pytest.raises(KillSwitchArmedError, match="explicit human confirmation"):
        killswitch.disarm("looks fine now", actor="automation")
    assert killswitch.is_armed()


def test_disarm_requires_non_empty_reason(killswitch: KillSwitch) -> None:
    killswitch.initialise()
    killswitch.arm(KillSwitchTrigger.MANUAL, "testing")
    with pytest.raises(KillSwitchArmedError, match="non-empty reason"):
        killswitch.disarm("   ", actor="operator", human_confirmed=True)


def test_disarm_with_confirmation_succeeds(killswitch: KillSwitch) -> None:
    killswitch.initialise()
    killswitch.arm(KillSwitchTrigger.MANUAL, "testing")
    killswitch.disarm("investigated, feed restored", actor="operator", human_confirmed=True)
    assert not killswitch.is_armed()


def test_unreadable_state_is_armed(db: Database, audit: AuditLog, tmp_path: Path) -> None:
    """KILL-06."""
    ks = KillSwitch(db, tmp_path / "nonexistent" / "ks.json", audit)
    assert ks.is_armed()


def test_corrupt_state_file_is_armed(killswitch: KillSwitch, tmp_path: Path) -> None:
    """KILL-06: corrupt content is an integrity failure, not 'no state'."""
    killswitch.initialise()
    assert not killswitch.is_armed()

    (tmp_path / "killswitch.json").write_text("{ this is not valid json")
    state = killswitch.read_state()
    assert state.state is KillSwitchState.ARMED
    assert state.trigger is KillSwitchTrigger.STATE_DISAGREEMENT


def test_missing_file_store_is_armed(killswitch: KillSwitch, tmp_path: Path) -> None:
    """KILL-04: one store present is not agreement."""
    killswitch.initialise()
    (tmp_path / "killswitch.json").unlink()
    state = killswitch.read_state()
    assert state.state is KillSwitchState.ARMED
    assert state.trigger is KillSwitchTrigger.STATE_DISAGREEMENT


def test_missing_db_store_is_armed(killswitch: KillSwitch, db: Database) -> None:
    killswitch.initialise()
    db.connection.execute("DELETE FROM kill_switch_state")
    state = killswitch.read_state()
    assert state.state is KillSwitchState.ARMED
    assert state.trigger is KillSwitchTrigger.STATE_DISAGREEMENT


def test_store_disagreement_resolves_armed(killswitch: KillSwitch, db: Database) -> None:
    """KILL-04: the file says disarmed, the database says armed. Armed wins."""
    killswitch.initialise()
    assert not killswitch.is_armed()

    db.connection.execute(
        "UPDATE kill_switch_state SET state = 'ARMED', trigger = 'MANUAL' WHERE id = 1"
    )
    state = killswitch.read_state()
    assert state.state is KillSwitchState.ARMED
    assert state.trigger is KillSwitchTrigger.STATE_DISAGREEMENT
    assert "disagree" in state.reason


def test_disagreement_resolves_armed_in_either_direction(
    killswitch: KillSwitch, tmp_path: Path
) -> None:
    """The database says disarmed, the file says armed. Still armed."""
    killswitch.initialise()
    record = killswitch.read_state()
    armed_json = record.model_copy(
        update={"state": KillSwitchState.ARMED, "trigger": KillSwitchTrigger.MANUAL}
    ).model_dump_json()
    (tmp_path / "killswitch.json").write_text(armed_json)

    state = killswitch.read_state()
    assert state.state is KillSwitchState.ARMED


def test_arm_is_always_permitted(killswitch: KillSwitch) -> None:
    """Arming only reduces exposure, so it is never gated."""
    killswitch.arm(KillSwitchTrigger.API_ERROR_RATE, "exchange erroring")
    killswitch.arm(KillSwitchTrigger.DATA_STALENESS, "feed stale")
    assert killswitch.is_armed()


def test_arm_and_disarm_are_audited(killswitch: KillSwitch, audit: AuditLog) -> None:
    killswitch.initialise()
    killswitch.arm(KillSwitchTrigger.MANUAL, "operator halted")
    killswitch.disarm("resolved", actor="operator", human_confirmed=True)

    kinds = [e.event_type.value for e in audit.tail(10)]
    assert "KILL_SWITCH_ARMED" in kinds
    assert kinds.count("KILL_SWITCH_DISARMED") == 2  # initialise + disarm
    assert audit.verify_chain() > 0


def test_initialise_is_idempotent(killswitch: KillSwitch) -> None:
    killswitch.initialise()
    killswitch.arm(KillSwitchTrigger.MANUAL, "halted")
    killswitch.initialise()  # must not silently release
    assert killswitch.is_armed()


def test_state_survives_reconstruction(db: Database, audit: AuditLog, tmp_path: Path) -> None:
    """Restart recovery: state is read from durable stores, not memory."""
    path = tmp_path / "killswitch.json"
    first = KillSwitch(db, path, audit)
    first.initialise()
    first.arm(KillSwitchTrigger.RECONCILIATION_FAILURE, "state divergence")

    second = KillSwitch(db, path, audit)
    assert second.is_armed()
    assert second.read_state().trigger is KillSwitchTrigger.RECONCILIATION_FAILURE
