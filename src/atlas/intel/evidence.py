"""The seven-part analysis record (INTEL-01).

The failure this prevents: a single sentence that reads "Bitcoin will rally because the
ETF was approved, 90% confident" and collapses seven different kinds of claim into one
unfalsifiable statement. Each part fails differently and each is checkable on its own.

    OBSERVED FACT   what happened, as reported by a source
    SOURCE          who reported it, and when
    INTERPRETATION  what a model thinks it means - opinion, not observation
    HYPOTHESIS      the market-impact claim, which is the falsifiable part
    CONFIDENCE      how sure, and on what basis
    HORIZON         over what period the hypothesis is meant to hold
    DECISION        what ATLAS did, which is decided by deterministic code

A model may supply the interpretation, the hypothesis and its own confidence. It never
supplies the fact, the source, or the decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

ZERO = Decimal(0)
ONE = Decimal(1)


class EvidenceGroup(StrEnum):
    """Independent families of evidence. Confluence is measured across these.

    Averaging scores within one family and calling it agreement is how three
    indicators derived from the same closing prices become "three confirmations".
    """

    TECHNICAL = "TECHNICAL"
    MARKET_STRUCTURE = "MARKET_STRUCTURE"
    VOLUME = "VOLUME"
    VOLATILITY = "VOLATILITY"
    MACRO = "MACRO"
    NEWS_EVENT = "NEWS_EVENT"
    CROSS_ASSET = "CROSS_ASSET"
    REGIME = "REGIME"

    @property
    def is_market_measured(self) -> bool:
        """Groups computed from market data ATLAS observed itself.

        The distinction matters at the decision: a hypothesis supported only by
        narrative groups has nothing measured behind it.
        """
        return self in {
            EvidenceGroup.TECHNICAL,
            EvidenceGroup.MARKET_STRUCTURE,
            EvidenceGroup.VOLUME,
            EvidenceGroup.VOLATILITY,
            EvidenceGroup.REGIME,
            EvidenceGroup.CROSS_ASSET,
        }


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"

    @property
    def is_directional(self) -> bool:
        return self is not Direction.NEUTRAL

    def opposes(self, other: Direction) -> bool:
        return self.is_directional and other.is_directional and self is not other


class Horizon(StrEnum):
    """How long a hypothesis is meant to hold. A claim with no horizon is unfalsifiable."""

    INTRADAY = "INTRADAY"
    DAYS = "DAYS"
    WEEKS = "WEEKS"
    UNSPECIFIED = "UNSPECIFIED"


class ConfidenceBasis(StrEnum):
    """Where a confidence number came from. These are not interchangeable.

    A model asserting 0.9 and a measured 0.9 hit-rate over 400 resolved cases are
    different objects that happen to share a numeral.
    """

    MODEL_ASSERTED = "MODEL_ASSERTED"
    EVIDENCE_COUNTED = "EVIDENCE_COUNTED"
    HISTORICALLY_MEASURED = "HISTORICALLY_MEASURED"


@dataclass(frozen=True)
class ObservedFact:
    """Something a named source reported at a known time. Never model output."""

    statement: str
    source_id: str
    observed_at: datetime
    published_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "statement": self.statement,
            "source_id": self.source_id,
            "observed_at": self.observed_at.isoformat(),
            "published_at": self.published_at.isoformat() if self.published_at else None,
        }


@dataclass(frozen=True)
class Interpretation:
    """A model's reading of a fact. Opinion, labelled as such, with its author."""

    text: str
    model_id: str
    produced_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "model_id": self.model_id,
            "produced_at": self.produced_at.isoformat(),
        }


@dataclass(frozen=True)
class ImpactHypothesis:
    """The falsifiable claim: this direction, over this horizon, for this asset."""

    symbol: str
    direction: Direction
    horizon: Horizon
    rationale: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": str(self.direction),
            "horizon": str(self.horizon),
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class AnalysisRecord:
    """One complete, separable analysis. The parts are never merged.

    `decision` is deliberately absent: what ATLAS does is decided downstream by
    deterministic code from many records, and a record that carried its own decision
    would be a model deciding.
    """

    record_id: str
    group: EvidenceGroup
    fact: ObservedFact | None
    interpretation: Interpretation | None
    hypothesis: ImpactHypothesis
    model_confidence: Decimal | None = None
    references: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.model_confidence is not None and not (ZERO <= self.model_confidence <= ONE):
            raise ValueError(
                f"model_confidence must be a fraction in [0, 1], got {self.model_confidence}"
            )
        if self.interpretation is not None and self.fact is None:
            raise ValueError(
                "an interpretation with no observed fact is a model talking about "
                "nothing; supply the fact it interprets"
            )

    @property
    def is_model_derived(self) -> bool:
        return self.interpretation is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "group": str(self.group),
            "fact": self.fact.as_dict() if self.fact else None,
            "interpretation": self.interpretation.as_dict() if self.interpretation else None,
            "hypothesis": self.hypothesis.as_dict(),
            "model_confidence": (
                None if self.model_confidence is None else str(self.model_confidence)
            ),
            "references": list(self.references),
        }


__all__ = [
    "AnalysisRecord",
    "ConfidenceBasis",
    "Direction",
    "EvidenceGroup",
    "Horizon",
    "ImpactHypothesis",
    "Interpretation",
    "ObservedFact",
]
