"""Reconciliation (EXEC-04, EXEC-05).

Exchange state is the source of truth; local state is repaired to match. Where the two
cannot be reconciled, the kill switch is armed rather than guessing — a system that
resolves ambiguity about its own positions by picking the more convenient answer is how
an account quietly ends up with exposure nobody recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from atlas.audit import AuditLog
from atlas.db.engine import Database
from atlas.killswitch import KillSwitch
from atlas.models import AuditEventType, KillSwitchTrigger

RECONCILE_ACTOR = "execution"


@dataclass(frozen=True)
class ReconciliationReport:
    matched: int
    repaired: list[str] = field(default_factory=list)
    unreconcilable: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.unreconcilable


class Reconciler:
    """Compares local open orders against the exchange and repairs the difference."""

    def __init__(self, db: Database, audit: AuditLog, killswitch: KillSwitch) -> None:
        self._db = db
        self._audit = audit
        self._killswitch = killswitch

    def _local_open_orders(self) -> dict[str, dict[str, str]]:
        rows = self._db.connection.execute(
            "SELECT client_order_id, symbol, status, quantity FROM orders "
            "WHERE status IN ('NEW', 'PARTIALLY_FILLED', 'SUBMITTED')"
        ).fetchall()
        return {
            str(r["client_order_id"]): {
                "symbol": str(r["symbol"]),
                "status": str(r["status"]),
                "quantity": str(r["quantity"]),
            }
            for r in rows
        }

    def reconcile(self, exchange_orders: list[dict[str, object]]) -> ReconciliationReport:
        """Repair local state from exchange state. Arms the kill switch on divergence."""
        remote = {str(o.get("clientOrderId")): o for o in exchange_orders if o.get("clientOrderId")}
        local = self._local_open_orders()

        matched = 0
        repaired: list[str] = []
        unreconcilable: list[str] = []

        for client_id, local_row in local.items():
            remote_row = remote.get(client_id)
            if remote_row is None:
                # The exchange does not have it. It filled or was cancelled while we
                # were not looking; mark it closed locally rather than assuming.
                with self._db.transaction() as conn:
                    conn.execute(
                        "UPDATE orders SET status = 'RECONCILED_CLOSED', updated_at = "
                        "datetime('now') WHERE client_order_id = ?",
                        (client_id,),
                    )
                repaired.append(client_id)
                continue

            remote_status = str(remote_row.get("status", ""))
            if remote_status != local_row["status"]:
                with self._db.transaction() as conn:
                    conn.execute(
                        "UPDATE orders SET status = ?, updated_at = datetime('now') "
                        "WHERE client_order_id = ?",
                        (remote_status, client_id),
                    )
                repaired.append(client_id)
            else:
                matched += 1

        # An order the exchange knows about and we do not is the dangerous direction:
        # unrecorded exposure. We cannot safely adopt it, so we stop.
        for client_id in remote.keys() - local.keys():
            if str(client_id).startswith("atlas"):
                unreconcilable.append(str(client_id))

        report = ReconciliationReport(
            matched=matched, repaired=repaired, unreconcilable=unreconcilable
        )
        self._audit.append(
            AuditEventType.RECONCILIATION,
            {
                "matched": matched,
                "repaired": repaired,
                "unreconcilable": unreconcilable,
            },
            RECONCILE_ACTOR,
        )

        if not report.clean:
            self._killswitch.arm(
                KillSwitchTrigger.RECONCILIATION_FAILURE,
                f"exchange reports {len(unreconcilable)} ATLAS order(s) with no local "
                f"record: {unreconcilable}",
                RECONCILE_ACTOR,
            )
        return report

    def reconcile_balance(
        self, expected_quote: Decimal, actual_quote: Decimal, tolerance: Decimal
    ) -> bool:
        """Compare expected and actual quote balance. Arms the kill switch on drift."""
        drift = abs(expected_quote - actual_quote)
        if drift <= tolerance:
            return True
        self._killswitch.arm(
            KillSwitchTrigger.RECONCILIATION_FAILURE,
            f"quote balance drift {drift} exceeds tolerance {tolerance} "
            f"(expected {expected_quote}, exchange reports {actual_quote})",
            RECONCILE_ACTOR,
        )
        return False
