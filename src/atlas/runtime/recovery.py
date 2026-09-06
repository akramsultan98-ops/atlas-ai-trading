"""Restart recovery (docs/RUNBOOK.md).

The order matters and is not negotiable:

  1. read kill-switch state — unreadable means armed (KILL-06)
  2. reconcile every open position against the exchange (EXEC-04)
  3. an irreconcilable divergence arms the switch and stops trading (EXEC-05)
  4. resume monitoring before resuming entries

Step 4 last: a system that starts taking entries before it can retire strategies has a
window where it can open positions it cannot close out of.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from atlas.audit import AuditLog
from atlas.db.engine import Database
from atlas.execution.reconcile import Reconciler
from atlas.killswitch import KillSwitch
from atlas.models import AuditEventType, StrategyStatus
from atlas.strategy.registry import StrategyRegistry

RECOVERY_ACTOR = "runtime"


@dataclass(frozen=True)
class RecoveryReport:
    kill_switch_armed: bool
    reconciled: bool
    live_strategies: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def may_resume_entries(self) -> bool:
        """Entries resume only when the switch is clear and state reconciles."""
        return not self.kill_switch_armed and self.reconciled and not self.issues


def recover(
    db: Database,
    audit: AuditLog,
    killswitch: KillSwitch,
    exchange_orders: list[dict[str, object]],
) -> RecoveryReport:
    """Bring a restarted process back to a known-safe state."""
    issues: list[str] = []

    state = killswitch.read_state()
    if state.is_armed():
        issues.append(f"kill switch is ARMED ({state.trigger}): {state.reason}")

    report = Reconciler(db, audit, killswitch).reconcile(exchange_orders)
    if not report.clean:
        issues.append(
            f"{len(report.unreconcilable)} unreconcilable order(s): {report.unreconcilable}"
        )

    live = StrategyRegistry(db).list_by_status(StrategyStatus.LIVE)

    # Re-read: reconciliation may have armed the switch a moment ago.
    armed_now = killswitch.is_armed()

    audit.append(
        AuditEventType.SYSTEM,
        {
            "event": "restart_recovery",
            "kill_switch_armed": armed_now,
            "reconciled": report.clean,
            "repaired": report.repaired,
            "live_strategies": len(live),
            "issues": issues,
        },
        RECOVERY_ACTOR,
    )

    return RecoveryReport(
        kill_switch_armed=armed_now,
        reconciled=report.clean,
        live_strategies=live,
        issues=issues,
    )
