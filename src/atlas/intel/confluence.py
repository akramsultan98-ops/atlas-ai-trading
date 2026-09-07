"""Signal confluence and abstention (INTEL-05).

Not an average. Averaging unrelated scores lets one very loud opinion outvote three
quiet measurements, and lets three indicators computed from the same closing prices
count as three confirmations.

Instead, evidence is grouped into independent families, each family produces one verdict,
and the decision needs agreement *across* families. Two rules carry most of the weight:

  - a hypothesis supported only by narrative groups (news, macro, a model's reading)
    has nothing measured behind it, and cannot trade however confident the model is
  - any family pointing the other way is a conflict, and conflicts abstain rather than
    resolve

Abstention is a first-class outcome. A system that must always answer will answer badly
on the days it should have been quiet, and those are the expensive days.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from atlas.intel.calibration import Confidence
from atlas.intel.evidence import AnalysisRecord, Direction, EvidenceGroup

ZERO = Decimal(0)
ONE = Decimal(1)


class GroupStatus(StrEnum):
    SUPPORTS = "SUPPORTS"
    OPPOSES = "OPPOSES"
    NEUTRAL = "NEUTRAL"
    ABSENT = "ABSENT"


class Decision(StrEnum):
    TRADE = "TRADE"
    NO_TRADE = "NO_TRADE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    STALE_INTELLIGENCE = "STALE_INTELLIGENCE"
    UNCONFIRMED_EVENT = "UNCONFIRMED_EVENT"
    REGIME_UNCLEAR = "REGIME_UNCLEAR"

    @property
    def is_trade(self) -> bool:
        return self is Decision.TRADE

    @property
    def is_abstention(self) -> bool:
        """Everything that is not a trade. Abstentions are counted separately from
        wrong answers: declining to predict is not a failed prediction."""
        return self is not Decision.TRADE


@dataclass(frozen=True)
class ConfluenceThresholds:
    """ATLAS decisions."""

    min_supporting_groups: int = 3
    min_market_measured_groups: int = 2
    allow_opposing_groups: int = 0


@dataclass(frozen=True)
class GroupVerdict:
    group: EvidenceGroup
    status: GroupStatus
    direction: Direction
    confidence: Decimal
    references: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "group": str(self.group),
            "status": str(self.status),
            "direction": str(self.direction),
            "confidence": str(self.confidence),
            "references": list(self.references),
        }


@dataclass(frozen=True)
class ConfluenceOutcome:
    decision: Decision
    direction: Direction
    reason: str
    verdicts: list[GroupVerdict]
    confidence: Confidence | None = None

    @property
    def supporting(self) -> list[GroupVerdict]:
        return [v for v in self.verdicts if v.status is GroupStatus.SUPPORTS]

    @property
    def opposing(self) -> list[GroupVerdict]:
        return [v for v in self.verdicts if v.status is GroupStatus.OPPOSES]

    @property
    def market_measured_support(self) -> list[GroupVerdict]:
        return [v for v in self.supporting if v.group.is_market_measured]

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": str(self.decision),
            "direction": str(self.direction),
            "reason": self.reason,
            "verdicts": [v.as_dict() for v in self.verdicts],
            "confidence": self.confidence.as_dict() if self.confidence else None,
            "supporting_groups": len(self.supporting),
            "market_measured_groups": len(self.market_measured_support),
        }


def summarise_groups(records: list[AnalysisRecord], proposed: Direction) -> list[GroupVerdict]:
    """Collapse records into one verdict per family.

    Within a family, records are not averaged either: a family that contains records
    pointing both ways is NEUTRAL, because internal disagreement is not weak support.
    """
    by_group: dict[EvidenceGroup, list[AnalysisRecord]] = {}
    for record in records:
        by_group.setdefault(record.group, []).append(record)

    verdicts: list[GroupVerdict] = []
    for group, items in sorted(by_group.items(), key=lambda kv: str(kv[0])):
        directions = {
            r.hypothesis.direction for r in items if r.hypothesis.direction.is_directional
        }
        confidences = [r.model_confidence for r in items if r.model_confidence is not None]
        confidence = sum(confidences, ZERO) / len(confidences) if confidences else ZERO
        references = [r.record_id for r in items]

        if len(directions) > 1 or not directions:
            status, direction = GroupStatus.NEUTRAL, Direction.NEUTRAL
        else:
            direction = next(iter(directions))
            if direction is proposed:
                status = GroupStatus.SUPPORTS
            elif direction.opposes(proposed):
                status = GroupStatus.OPPOSES
            else:
                status = GroupStatus.NEUTRAL

        verdicts.append(
            GroupVerdict(
                group=group,
                status=status,
                direction=direction,
                confidence=confidence,
                references=references,
            )
        )
    return verdicts


def decide(
    verdicts: list[GroupVerdict],
    proposed: Direction,
    *,
    confidence: Confidence | None = None,
    thresholds: ConfluenceThresholds | None = None,
    regime_measurable: bool = True,
    stale: bool = False,
    unconfirmed_event: bool = False,
) -> ConfluenceOutcome:
    """Decide from group verdicts. Deterministic; no model participates.

    The order of the checks is the order of severity: a conflict is worse than thin
    evidence, and stale intelligence is worse than either because it looks fresh.
    """
    supporting = [v for v in verdicts if v.status is GroupStatus.SUPPORTS]
    opposing = [v for v in verdicts if v.status is GroupStatus.OPPOSES]
    measured = [v for v in supporting if v.group.is_market_measured]
    t = thresholds or ConfluenceThresholds()

    def outcome(
        decision: Decision, reason: str, direction: Direction = Direction.NEUTRAL
    ) -> ConfluenceOutcome:
        return ConfluenceOutcome(
            decision=decision,
            direction=direction,
            reason=reason,
            verdicts=verdicts,
            confidence=confidence,
        )

    if not proposed.is_directional:
        return outcome(Decision.NO_TRADE, "no direction proposed")

    if stale:
        return outcome(
            Decision.STALE_INTELLIGENCE,
            "the supporting intelligence is older than its usable life",
        )

    if len(opposing) > t.allow_opposing_groups:
        return outcome(
            Decision.CONFLICTING_EVIDENCE,
            f"{len(opposing)} evidence group(s) point the other way: "
            f"{[str(v.group) for v in opposing]}",
        )

    if unconfirmed_event:
        return outcome(
            Decision.UNCONFIRMED_EVENT,
            "the driving event has not been corroborated by an independent source",
        )

    if not regime_measurable:
        return outcome(
            Decision.REGIME_UNCLEAR,
            "the market regime could not be measured from the available data",
        )

    if len(supporting) < t.min_supporting_groups:
        return outcome(
            Decision.INSUFFICIENT_EVIDENCE,
            f"{len(supporting)} supporting group(s), {t.min_supporting_groups} required",
        )

    if len(measured) < t.min_market_measured_groups:
        # The rule that stops a confident narrative from trading on its own.
        return outcome(
            Decision.INSUFFICIENT_EVIDENCE,
            f"only {len(measured)} of the supporting group(s) are measured from market "
            f"data ({t.min_market_measured_groups} required); a model's conviction is "
            "not a substitute for observable evidence",
        )

    return outcome(
        Decision.TRADE,
        f"{len(supporting)} independent groups agree, {len(measured)} of them measured "
        "from market data",
        proposed,
    )


__all__ = [
    "ConfluenceOutcome",
    "ConfluenceThresholds",
    "Decision",
    "GroupStatus",
    "GroupVerdict",
    "decide",
    "summarise_groups",
]
