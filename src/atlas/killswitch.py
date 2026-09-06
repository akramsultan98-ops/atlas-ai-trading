"""Account-level kill switch (KILL-01..06, ADR-004).

The failure mode of a kill switch that fails *open* is unbounded loss. The failure mode
of one that fails *closed* is missed trades. These are not symmetric, so every ambiguity
— unreadable state, corrupt state, disagreement between stores — resolves to ARMED.

State is persisted twice, to a file and to the database (KILL-04). Either store going
missing, unparseable, or out of step with the other is itself a trigger.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from atlas.audit import AuditLog
from atlas.db.engine import Database
from atlas.errors import KillSwitchArmedError
from atlas.models import (
    AuditEventType,
    KillSwitchRecord,
    KillSwitchState,
    KillSwitchTrigger,
    utcnow,
)

_SYSTEM_ACTOR = "system"


class KillSwitch:
    """Dual-store, fail-safe account halt.

    Reads are cheap and are performed immediately before every order transmission
    (EXEC-06), not merely at signal time.
    """

    def __init__(self, db: Database, state_path: Path, audit: AuditLog) -> None:
        self._db = db
        self._path = state_path
        self._audit = audit

    # ------------------------------------------------------------------ stores

    def _read_file(self) -> KillSwitchRecord | None:
        """Return the file record, or None if it is absent, unreadable or corrupt."""
        try:
            raw = self._path.read_text()
        except FileNotFoundError:
            return None
        except OSError:
            return None
        try:
            return KillSwitchRecord.model_validate_json(raw)
        except Exception:
            # Corrupt content is not "no state" — it is an integrity failure. The
            # caller treats None as ambiguous and resolves to ARMED either way.
            return None

    def _read_db(self) -> KillSwitchRecord | None:
        try:
            row = self._db.connection.execute(
                "SELECT state, trigger, reason, actor, at FROM kill_switch_state WHERE id = 1"
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        try:
            return KillSwitchRecord(
                state=KillSwitchState(row["state"]),
                trigger=KillSwitchTrigger(row["trigger"]) if row["trigger"] else None,
                reason=str(row["reason"]),
                actor=str(row["actor"]),
                at=datetime.fromisoformat(str(row["at"])),
            )
        except Exception:
            return None

    def _write_both(self, record: KillSwitchRecord) -> None:
        """Persist to both stores. The database write is the commit point."""
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO kill_switch_state(id, state, trigger, reason, actor, at) "
                "VALUES (1, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET state=excluded.state, trigger=excluded.trigger, "
                "reason=excluded.reason, actor=excluded.actor, at=excluded.at",
                (
                    str(record.state),
                    str(record.trigger) if record.trigger else None,
                    record.reason,
                    record.actor,
                    record.at.isoformat(),
                ),
            )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(record.model_dump_json())
        tmp.replace(self._path)  # atomic on POSIX

    # ------------------------------------------------------------------ state

    def read_state(self) -> KillSwitchRecord:
        """Return the effective state, resolving every ambiguity toward ARMED.

        Cases, all of which arm (KILL-04, KILL-06):

        - neither store readable          → armed, STATE_UNREADABLE
        - exactly one store readable      → armed, STATE_DISAGREEMENT
        - both readable but states differ → armed, STATE_DISAGREEMENT

        An uninitialised system (both stores absent) is armed: the system has not yet
        been deliberately released to trade.
        """
        file_record = self._read_file()
        db_record = self._read_db()

        if file_record is None and db_record is None:
            return KillSwitchRecord(
                state=KillSwitchState.ARMED,
                trigger=KillSwitchTrigger.STATE_UNREADABLE,
                reason="kill-switch state is absent or unreadable in both stores",
                actor=_SYSTEM_ACTOR,
                at=utcnow(),
            )

        if file_record is None or db_record is None:
            missing = "file" if file_record is None else "database"
            return KillSwitchRecord(
                state=KillSwitchState.ARMED,
                trigger=KillSwitchTrigger.STATE_DISAGREEMENT,
                reason=f"kill-switch state missing or unreadable in the {missing} store",
                actor=_SYSTEM_ACTOR,
                at=utcnow(),
            )

        if file_record.state is not db_record.state:
            return KillSwitchRecord(
                state=KillSwitchState.ARMED,
                trigger=KillSwitchTrigger.STATE_DISAGREEMENT,
                reason=(
                    f"kill-switch stores disagree: file={file_record.state}, "
                    f"database={db_record.state}"
                ),
                actor=_SYSTEM_ACTOR,
                at=utcnow(),
            )

        return db_record

    def is_armed(self) -> bool:
        return self.read_state().is_armed()

    # ------------------------------------------------------------------ actions

    def initialise(self, actor: str = _SYSTEM_ACTOR) -> KillSwitchRecord:
        """Write an initial DISARMED state if the system has never been initialised.

        Does nothing if either store already holds state. This is the deliberate
        release-to-trade step; it is not performed implicitly on startup.
        """
        if self._read_file() is not None or self._read_db() is not None:
            return self.read_state()
        record = KillSwitchRecord(
            state=KillSwitchState.DISARMED,
            trigger=None,
            reason="initialised",
            actor=actor,
            at=utcnow(),
        )
        self._write_both(record)
        self._audit.append(
            AuditEventType.KILL_SWITCH_DISARMED,
            {"reason": record.reason, "initialisation": True},
            actor,
        )
        return record

    def arm(
        self,
        trigger: KillSwitchTrigger,
        reason: str,
        actor: str = _SYSTEM_ACTOR,
    ) -> KillSwitchRecord:
        """Arm the kill switch. Always permitted, from any state, by any caller.

        Arming only ever reduces exposure, so it is never gated.
        """
        record = KillSwitchRecord(
            state=KillSwitchState.ARMED,
            trigger=trigger,
            reason=reason,
            actor=actor,
            at=utcnow(),
        )
        self._write_both(record)
        self._audit.append(
            AuditEventType.KILL_SWITCH_ARMED,
            {"trigger": str(trigger), "reason": reason},
            actor,
        )
        return record

    def disarm(
        self,
        reason: str,
        actor: str,
        human_confirmed: bool = False,
    ) -> KillSwitchRecord:
        """Disarm. Requires explicit human confirmation (KILL-05).

        There is deliberately no AI-reachable path to this method: it is not registered
        on the advisory plane's tool surface (AI-02), and `human_confirmed` must be set
        by a caller that can represent a person.
        """
        if not human_confirmed:
            raise KillSwitchArmedError(
                "disarming the kill switch requires explicit human confirmation "
                "(KILL-05); no automated or AI-initiated path exists"
            )
        if not reason.strip():
            raise KillSwitchArmedError("disarming requires a non-empty reason")

        record = KillSwitchRecord(
            state=KillSwitchState.DISARMED,
            trigger=None,
            reason=reason,
            actor=actor,
            at=utcnow(),
        )
        self._write_both(record)
        self._audit.append(
            AuditEventType.KILL_SWITCH_DISARMED,
            {"reason": reason, "human_confirmed": True},
            actor,
        )
        return record

    # ------------------------------------------------------------------ gates

    def check_can_enter(self) -> None:
        """Gate for opening or increasing exposure. Raises when armed (KILL-03).

        Call immediately before transmission, not at signal time (EXEC-06).
        """
        state = self.read_state()
        if state.is_armed():
            raise KillSwitchArmedError(f"kill switch is ARMED ({state.trigger}): {state.reason}")

    def check_can_place_protective(self) -> None:
        """Gate for protective orders. Never blocks (KILL-03).

        Stops and targets reduce risk. Cancelling or refusing them while armed would
        leave open positions naked, which is the opposite of what arming is for. This
        method exists so the call site documents which kind of order it is placing.
        """
        return None
