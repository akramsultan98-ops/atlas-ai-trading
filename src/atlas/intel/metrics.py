"""Accuracy measurement, including the parts that flatter a system if omitted (INTEL-07).

Three rules decide whether these numbers mean anything.

Abstentions are never counted as correct. A system that declines 900 of 1000 chances and
wins 80 of the remaining 100 is 80% accurate on 10% of the opportunities — reporting 98%
by counting the abstentions as "not wrong" is the easiest way to fake a headline number,
so abstention rate is reported alongside and never folded in.

Accuracy is reported per regime, per event category and per confidence bucket. An
aggregate hides that a system is excellent in trending markets and a coin flip in ranging
ones, which is exactly the thing an operator needs to know.

Calibration is reported as claimed-versus-observed. A 90% bucket that wins 67% of the
time is not 90% accurate with noise; it is badly calibrated, and it says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from atlas.intel.calibration import BucketOutcome, bucket_of
from atlas.intel.confluence import Decision
from atlas.intel.evidence import Direction

ZERO = Decimal(0)
ONE = Decimal(1)


@dataclass(frozen=True)
class DecisionOutcome:
    """One decision and what actually happened to it."""

    decision: Decision
    direction: Direction
    stated_confidence: Decimal | None = None
    regime: str = "UNKNOWN"
    event_category: str = "NONE"
    realised_direction: Direction | None = None
    realised_return: Decimal | None = None

    @property
    def acted(self) -> bool:
        return self.decision.is_trade

    @property
    def resolved(self) -> bool:
        return self.acted and self.realised_direction is not None

    @property
    def correct(self) -> bool:
        return self.resolved and self.realised_direction is self.direction


def _ratio(numerator: int, denominator: int) -> Decimal | None:
    if denominator <= 0:
        return None
    return Decimal(numerator) / Decimal(denominator)


@dataclass(frozen=True)
class AccuracyReport:
    """Deterministic. Every rate carries the denominator it was computed over."""

    total_decisions: int
    abstentions: int
    acted: int
    resolved: int
    correct: int
    directional_accuracy: Decimal | None
    precision: Decimal | None
    recall: Decimal | None
    f1: Decimal | None
    false_positive_rate: Decimal | None
    abstention_rate: Decimal | None
    win_rate: Decimal | None
    profit_factor: Decimal | None
    expectancy: Decimal | None
    max_drawdown: Decimal
    by_regime: dict[str, Decimal | None] = field(default_factory=dict)
    by_event_category: dict[str, Decimal | None] = field(default_factory=dict)
    by_confidence_bucket: dict[str, BucketOutcome] = field(default_factory=dict)
    abstention_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def is_reportable(self) -> bool:
        """Whether an accuracy claim may be made at all."""
        return self.resolved > 0

    def as_dict(self) -> dict[str, Any]:
        def num(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {
            "total_decisions": self.total_decisions,
            "abstentions": self.abstentions,
            "acted": self.acted,
            "resolved": self.resolved,
            "correct": self.correct,
            "directional_accuracy": num(self.directional_accuracy),
            "precision": num(self.precision),
            "recall": num(self.recall),
            "f1": num(self.f1),
            "false_positive_rate": num(self.false_positive_rate),
            "abstention_rate": num(self.abstention_rate),
            "win_rate": num(self.win_rate),
            "profit_factor": num(self.profit_factor),
            "expectancy": num(self.expectancy),
            "max_drawdown": str(self.max_drawdown),
            "by_regime": {k: num(v) for k, v in self.by_regime.items()},
            "by_event_category": {k: num(v) for k, v in self.by_event_category.items()},
            "by_confidence_bucket": {k: v.as_dict() for k, v in self.by_confidence_bucket.items()},
            "abstention_reasons": dict(self.abstention_reasons),
            "reportable": self.is_reportable,
        }


def _accuracy_over(outcomes: list[DecisionOutcome]) -> Decimal | None:
    resolved = [o for o in outcomes if o.resolved]
    if not resolved:
        return None
    return _ratio(sum(1 for o in resolved if o.correct), len(resolved))


def _max_drawdown(returns: list[Decimal]) -> Decimal:
    equity = ONE
    peak = ONE
    worst = ZERO
    for value in returns:
        equity *= ONE + value
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst


def evaluate(outcomes: list[DecisionOutcome]) -> AccuracyReport:
    """Compute every metric from resolved decisions only.

    `precision` and `recall` are defined against the LONG call as the positive class:
    precision is how often acting long was right, recall is how many of the genuinely
    long moves were caught - including the ones abstained on, which is where a
    conservative system pays for its caution.
    """
    abstentions = [o for o in outcomes if o.decision.is_abstention]
    acted = [o for o in outcomes if o.acted]
    resolved = [o for o in acted if o.resolved]
    correct = [o for o in resolved if o.correct]

    predicted_long = [o for o in resolved if o.direction is Direction.LONG]
    true_positive = sum(1 for o in predicted_long if o.correct)
    false_positive = len(predicted_long) - true_positive
    actually_long = [o for o in outcomes if o.realised_direction is Direction.LONG]
    false_negative = len(actually_long) - true_positive

    precision = _ratio(true_positive, len(predicted_long))
    recall = _ratio(true_positive, true_positive + false_negative)
    f1: Decimal | None = None
    if precision is not None and recall is not None and (precision + recall) > 0:
        f1 = (Decimal(2) * precision * recall) / (precision + recall)

    predicted_short = [o for o in resolved if o.direction is Direction.SHORT]
    negatives = len(predicted_short) + false_positive
    fpr = _ratio(false_positive, negatives) if negatives else None

    returns = [o.realised_return for o in resolved if o.realised_return is not None]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    gross_profit = sum(wins, ZERO)
    gross_loss = abs(sum(losses, ZERO))

    by_regime: dict[str, Decimal | None] = {}
    for regime in sorted({o.regime for o in outcomes}):
        by_regime[regime] = _accuracy_over([o for o in outcomes if o.regime == regime])

    by_category: dict[str, Decimal | None] = {}
    for category in sorted({o.event_category for o in outcomes}):
        by_category[category] = _accuracy_over(
            [o for o in outcomes if o.event_category == category]
        )

    buckets: dict[str, BucketOutcome] = {}
    for outcome in resolved:
        if outcome.stated_confidence is None:
            continue
        name = bucket_of(outcome.stated_confidence)
        existing = buckets.get(name, BucketOutcome(bucket=name, predictions=0, correct=0))
        buckets[name] = BucketOutcome(
            bucket=name,
            predictions=existing.predictions + 1,
            correct=existing.correct + (1 if outcome.correct else 0),
        )

    reasons: dict[str, int] = {}
    for outcome in abstentions:
        key = str(outcome.decision)
        reasons[key] = reasons.get(key, 0) + 1

    return AccuracyReport(
        total_decisions=len(outcomes),
        abstentions=len(abstentions),
        acted=len(acted),
        resolved=len(resolved),
        correct=len(correct),
        directional_accuracy=_ratio(len(correct), len(resolved)),
        precision=precision,
        recall=recall,
        f1=f1,
        false_positive_rate=fpr,
        # Reported beside accuracy, never folded into it.
        abstention_rate=_ratio(len(abstentions), len(outcomes)),
        win_rate=_ratio(len(wins), len(returns)) if returns else None,
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else None,
        expectancy=(sum(returns, ZERO) / len(returns)) if returns else None,
        max_drawdown=_max_drawdown(returns),
        by_regime=by_regime,
        by_event_category=by_category,
        by_confidence_bucket=buckets,
        abstention_reasons=reasons,
    )


def calibration_report(report: AccuracyReport) -> list[dict[str, Any]]:
    """Claimed confidence against observed frequency, bucket by bucket.

    A bucket claiming 0.9 that resolves at 0.67 is reported as poorly calibrated with
    both numbers shown. Neither is discarded in favour of the other.
    """
    rows: list[dict[str, Any]] = []
    for name, outcome in sorted(report.by_confidence_bucket.items()):
        observed = outcome.observed_rate
        low = Decimal(name.split("-")[0])
        gap = None if observed is None else observed - low
        rows.append(
            {
                "bucket": name,
                "claimed_at_least": str(low),
                "observed_rate": None if observed is None else str(observed),
                "predictions": outcome.predictions,
                "sufficient_samples": outcome.has_enough_samples,
                "well_calibrated": (
                    None
                    if observed is None or not outcome.has_enough_samples
                    else gap is not None and gap >= Decimal("-0.10")
                ),
            }
        )
    return rows


__all__ = [
    "AccuracyReport",
    "DecisionOutcome",
    "calibration_report",
    "evaluate",
]
