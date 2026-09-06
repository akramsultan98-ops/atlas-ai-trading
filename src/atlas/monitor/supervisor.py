"""Automatic retirement (MON-06, MON-07).

This is the component the source video exists to argue for. David lost a strategy that
had run to +2181% because he had exactly these rules and overrode them, twice, after it
briefly recovered [17:56-18:03].

So the supervisor is deliberately mechanical:

  - any single rule firing retires the strategy — no quorum, no weighting, no
    averaging, nothing that can be talked out of a shutdown (MON-07)
  - retirement is one-way; there is no programmatic reactivation anywhere in ATLAS,
    and `reactivate_strategy` is on the advisory denylist (MON-06, AI-02)
  - open positions are left to their existing brackets; only new entries stop
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from atlas.audit import AuditLog
from atlas.db.engine import Database
from atlas.models import AuditEventType, StrategyStatus
from atlas.monitor.rules import (
    MonitorThresholds,
    RuleAction,
    RuleVerdict,
    StrategyHealth,
    evaluate_all,
)
from atlas.strategy.registry import StrategyRegistry

SUPERVISOR_ACTOR = "monitor"


@dataclass(frozen=True)
class SupervisionResult:
    strategy_id: str
    verdicts: list[RuleVerdict]
    action_taken: RuleAction | None
    reason: str

    @property
    def fired(self) -> list[RuleVerdict]:
        return [v for v in self.verdicts if v.fired]

    @property
    def retired(self) -> bool:
        return self.action_taken is RuleAction.RETIRE


class Supervisor:
    """Evaluates the retirement rules and acts on them without discretion."""

    def __init__(self, db: Database, audit: AuditLog) -> None:
        self._db = db
        self._audit = audit
        self._registry = StrategyRegistry(db)

    def supervise(
        self,
        strategy_id: str,
        health: StrategyHealth,
        backtest_win_rate: Decimal,
        thresholds: MonitorThresholds | None = None,
    ) -> SupervisionResult:
        """Run every rule and apply the most severe action any of them demands."""
        verdicts = evaluate_all(health, backtest_win_rate, thresholds)
        fired = [v for v in verdicts if v.fired]

        status = self._registry.status(strategy_id)
        if status is StrategyStatus.RETIRED:
            return SupervisionResult(strategy_id, verdicts, None, "already retired")

        # Severity order, not a vote: RETIRE beats SUSPEND beats ALERT.
        action: RuleAction | None = None
        for candidate in (RuleAction.RETIRE, RuleAction.SUSPEND, RuleAction.ALERT):
            if any(v.action is candidate for v in fired):
                action = candidate
                break

        if action is None:
            return SupervisionResult(strategy_id, verdicts, None, "healthy")

        reason = "; ".join(f"{v.rule}: {v.detail}" for v in fired if v.action is action)

        if action is RuleAction.RETIRE:
            self._registry.set_status(strategy_id, StrategyStatus.RETIRED, reason=reason)
            self._audit.append(
                AuditEventType.STRATEGY_RETIRED,
                {
                    "strategy_id": strategy_id,
                    "reason": reason,
                    "rules_fired": [v.rule for v in fired],
                    "reactivation": "human only (MON-06)",
                },
                SUPERVISOR_ACTOR,
            )
        elif action is RuleAction.SUSPEND:
            self._registry.set_status(strategy_id, StrategyStatus.SUSPENDED, reason=reason)
            self._audit.append(
                AuditEventType.STRATEGY_STATUS_CHANGED,
                {"strategy_id": strategy_id, "status": "SUSPENDED", "reason": reason},
                SUPERVISOR_ACTOR,
            )
        else:
            self._audit.append(
                AuditEventType.SYSTEM,
                {"strategy_id": strategy_id, "alert": reason},
                SUPERVISOR_ACTOR,
            )

        return SupervisionResult(strategy_id, verdicts, action, reason)

    def can_open_new_entries(self, strategy_id: str) -> bool:
        """A retired or suspended strategy places no new entries (MON-06)."""
        return self._registry.status(strategy_id) is StrategyStatus.LIVE
