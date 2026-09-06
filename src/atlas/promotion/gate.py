"""Promotion to live capital (PROM-01..04).

The video promotes on David's judgement. ATLAS requires an explicit, recorded human
approval (PROM-01) — stricter than the source, because an unattended system has nobody
to exercise judgement at 3am.

PROM-02 is what makes RISK-02 mean anything: three strategies that are all
long-BTC-momentum on the 1h are one strategy wearing three hats, and they draw down
together. David calls diversification "the holy grail" [23:47] without defining it; the
correlation gate is our definition.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from atlas.audit import AuditLog
from atlas.db.engine import Database
from atlas.errors import SafetyError
from atlas.incubation.divergence import DivergenceCheck
from atlas.incubation.tracker import IncubationSignal
from atlas.models import AuditEventType, StrategyStatus, utcnow
from atlas.strategy.registry import StrategyRegistry

ZERO = Decimal(0)


class PromotionRefused(SafetyError):
    """Promotion was attempted without satisfying every gate."""


@dataclass(frozen=True)
class PromotionThresholds:
    """ATLAS decisions."""

    max_correlation: Decimal = Decimal("0.6")
    initial_risk_multiplier: Decimal = Decimal("0.5")
    reduced_risk_trades: int = 20


@dataclass(frozen=True)
class PromotionDecision:
    eligible: bool
    reasons: list[str]
    max_observed_correlation: Decimal


def pearson_correlation(a: list[Decimal], b: list[Decimal]) -> Decimal:
    """Pearson correlation over paired returns. Zero when either series is flat."""
    n = min(len(a), len(b))
    if n < 2:
        return ZERO
    xs, ys = a[:n], b[:n]
    mean_x = sum(xs, ZERO) / n
    mean_y = sum(ys, ZERO) / n
    cov = sum(((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)), ZERO)
    var_x = sum(((x - mean_x) ** 2 for x in xs), ZERO)
    var_y = sum(((y - mean_y) ** 2 for y in ys), ZERO)
    if var_x <= 0 or var_y <= 0:
        return ZERO
    return cov / (var_x.sqrt() * var_y.sqrt())


def _returns(signals: list[IncubationSignal]) -> list[Decimal]:
    return [s.return_pct for s in signals if s.return_pct is not None]


def evaluate_promotion(
    divergence: DivergenceCheck,
    candidate_signals: list[IncubationSignal],
    live_signals_by_strategy: dict[str, list[IncubationSignal]],
    thresholds: PromotionThresholds | None = None,
) -> PromotionDecision:
    """Check incubation evidence and portfolio correlation. Human approval is separate."""
    t = thresholds or PromotionThresholds()
    reasons = list(divergence.reasons)
    candidate = _returns(candidate_signals)
    worst = ZERO

    for strategy_id, signals in live_signals_by_strategy.items():
        correlation = abs(pearson_correlation(candidate, _returns(signals)))
        worst = max(worst, correlation)
        if correlation > t.max_correlation:
            reasons.append(
                f"correlation {correlation:.2f} with live strategy {strategy_id} "
                f"exceeds {t.max_correlation} (PROM-02)"
            )

    return PromotionDecision(eligible=not reasons, reasons=reasons, max_observed_correlation=worst)


class PromotionGate:
    """Records the human approval that moves a strategy to live capital."""

    def __init__(self, db: Database, audit: AuditLog) -> None:
        self._db = db
        self._audit = audit
        self._registry = StrategyRegistry(db)

    def promote(
        self,
        strategy_id: str,
        decision: PromotionDecision,
        *,
        approved_by: str,
        human_confirmed: bool = False,
        evidence: dict[str, Any] | None = None,
        thresholds: PromotionThresholds | None = None,
    ) -> str:
        """Promote to LIVE. Requires eligibility *and* explicit human approval.

        There is deliberately no automated caller: `promote_strategy` is on the
        advisory plane's denylist (AI-02), so the AI cannot reach this method.
        """
        t = thresholds or PromotionThresholds()

        if not human_confirmed:
            raise PromotionRefused(
                "promotion requires explicit human approval (PROM-01); no automated or "
                "AI-initiated path exists"
            )
        if not approved_by.strip():
            raise PromotionRefused("promotion requires a named human approver")
        # Lifecycle position is checked before evidence quality: "this was never
        # incubated" is the more fundamental failure and the more actionable message.
        # Judging incubation evidence for a strategy that has none would otherwise
        # report weak evidence and hide the real problem.
        status = self._registry.status(strategy_id)
        if status is not StrategyStatus.INCUBATING:
            raise PromotionRefused(
                f"strategy {strategy_id} is {status}, not INCUBATING; only an incubated "
                "strategy can be promoted (AI-05)"
            )

        if not decision.eligible:
            raise PromotionRefused("promotion gates not satisfied: " + "; ".join(decision.reasons))

        promotion_id = str(uuid.uuid4())
        now = utcnow().isoformat()
        snapshot = json.dumps(
            {
                "decision_reasons": decision.reasons,
                "max_observed_correlation": str(decision.max_observed_correlation),
                "evidence": evidence or {},
                "approved_at": now,
            },
            sort_keys=True,
            default=str,
        )

        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO promotions(id, strategy_id, approved_by, approved_at, "
                "evidence, initial_risk_multiplier) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    promotion_id,
                    strategy_id,
                    approved_by,
                    now,
                    snapshot,
                    str(t.initial_risk_multiplier),
                ),
            )
        self._registry.set_status(strategy_id, StrategyStatus.LIVE)
        self._audit.append(
            AuditEventType.PROMOTION_APPROVED,
            {
                "promotion_id": promotion_id,
                "strategy_id": strategy_id,
                "approved_by": approved_by,
                "initial_risk_multiplier": str(t.initial_risk_multiplier),
            },
            approved_by,
        )
        return promotion_id

    def risk_multiplier_for(
        self,
        strategy_id: str,
        completed_trades: int,
        thresholds: PromotionThresholds | None = None,
    ) -> Decimal:
        """PROM-03: reduced size for a newly promoted strategy's first trades."""
        t = thresholds or PromotionThresholds()
        row = self._db.connection.execute(
            "SELECT initial_risk_multiplier FROM promotions WHERE strategy_id = ? "
            "ORDER BY approved_at DESC LIMIT 1",
            (strategy_id,),
        ).fetchone()
        if row is None:
            return Decimal(1)
        if completed_trades >= t.reduced_risk_trades:
            return Decimal(1)
        return Decimal(str(row["initial_risk_multiplier"]))
