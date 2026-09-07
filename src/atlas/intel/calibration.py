"""Confidence that means something (INTEL-04).

A model saying "90% confident" is a token sequence, not a probability. It becomes a
probability only when a bucket of past claims made at that stated confidence is checked
against what actually happened. Until then the honest label is UNCALIBRATED, and this
module refuses to produce a number that would be read as a probability.

Four separate quantities, never merged:

    model_confidence       what the model asserted. An opinion.
    evidence_confidence    how much independent evidence agrees. Counted, not asserted.
    historical_accuracy    the measured hit rate of this bucket. Requires resolved cases.
    calibrated_probability derived from the above, and only when there are enough cases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Any

ZERO = Decimal(0)
ONE = Decimal(1)

# Below this many resolved outcomes a hit rate is noise. Ten wins out of ten says almost
# nothing about the next hundred, and reporting it as 100% would be a lie with a decimal
# point in it.
MIN_SAMPLES_FOR_CALIBRATION = 30

BUCKET_EDGES: tuple[Decimal, ...] = (
    Decimal("0.5"),
    Decimal("0.6"),
    Decimal("0.7"),
    Decimal("0.8"),
    Decimal("0.9"),
)


class CalibrationStatus(StrEnum):
    CALIBRATED = "CALIBRATED"
    UNCALIBRATED = "UNCALIBRATED"


def bucket_of(confidence: Decimal) -> str:
    """The reporting bucket a stated confidence falls into."""
    edges = [ZERO, *BUCKET_EDGES, ONE]
    for low, high in pairwise(edges):
        if low <= confidence < high:
            return f"{low}-{high}"
    return f"{BUCKET_EDGES[-1]}-1"


@dataclass(frozen=True)
class BucketOutcome:
    """Resolved history for one confidence bucket."""

    bucket: str
    predictions: int
    correct: int

    @property
    def observed_rate(self) -> Decimal | None:
        if self.predictions <= 0:
            return None
        return Decimal(self.correct) / Decimal(self.predictions)

    @property
    def has_enough_samples(self) -> bool:
        return self.predictions >= MIN_SAMPLES_FOR_CALIBRATION

    def as_dict(self) -> dict[str, Any]:
        rate = self.observed_rate
        return {
            "bucket": self.bucket,
            "predictions": self.predictions,
            "correct": self.correct,
            "observed_rate": None if rate is None else str(rate),
            "sufficient_samples": self.has_enough_samples,
        }


@dataclass(frozen=True)
class Confidence:
    """The four quantities, kept apart.

    `calibrated_probability` is None whenever the status is UNCALIBRATED. There is no
    fallback value, because any number placed here would be read as a probability by
    everything downstream.
    """

    model_confidence: Decimal | None
    evidence_confidence: Decimal
    historical_accuracy: Decimal | None
    status: CalibrationStatus
    samples: int
    bucket: str
    notes: list[str] = field(default_factory=list)

    @property
    def calibrated_probability(self) -> Decimal | None:
        if self.status is not CalibrationStatus.CALIBRATED:
            return None
        return self.historical_accuracy

    @property
    def is_calibrated(self) -> bool:
        return self.status is CalibrationStatus.CALIBRATED

    def as_dict(self) -> dict[str, Any]:
        probability = self.calibrated_probability
        return {
            "model_confidence": (
                None if self.model_confidence is None else str(self.model_confidence)
            ),
            "evidence_confidence": str(self.evidence_confidence),
            "historical_accuracy": (
                None if self.historical_accuracy is None else str(self.historical_accuracy)
            ),
            "calibrated_probability": None if probability is None else str(probability),
            "status": str(self.status),
            "samples": self.samples,
            "bucket": self.bucket,
            "notes": list(self.notes),
        }


def calibrate(
    *,
    model_confidence: Decimal | None,
    evidence_confidence: Decimal,
    history: dict[str, BucketOutcome] | None = None,
) -> Confidence:
    """Turn a stated confidence into a calibrated one, or refuse to.

    The bucket is chosen by *evidence* confidence when a model asserted nothing, and by
    the model's own claim when it did - because that is the claim being checked against
    history. A model that says 0.9 is judged on how often its 0.9s have come true.
    """
    notes: list[str] = []
    stated = model_confidence if model_confidence is not None else evidence_confidence
    bucket = bucket_of(stated)
    outcome = (history or {}).get(bucket)

    if outcome is None or not outcome.has_enough_samples:
        have = 0 if outcome is None else outcome.predictions
        notes.append(
            f"only {have} resolved outcomes in bucket {bucket}; "
            f"{MIN_SAMPLES_FOR_CALIBRATION} required before a stated confidence may be "
            "reported as a probability"
        )
        return Confidence(
            model_confidence=model_confidence,
            evidence_confidence=evidence_confidence,
            historical_accuracy=outcome.observed_rate if outcome else None,
            status=CalibrationStatus.UNCALIBRATED,
            samples=have,
            bucket=bucket,
            notes=notes,
        )

    observed = outcome.observed_rate
    assert observed is not None  # has_enough_samples implies predictions > 0
    if model_confidence is not None and abs(model_confidence - observed) >= Decimal("0.15"):
        notes.append(
            f"poorly calibrated: claims at {model_confidence} resolve correctly "
            f"{observed:.0%} of the time over {outcome.predictions} cases"
        )

    return Confidence(
        model_confidence=model_confidence,
        evidence_confidence=evidence_confidence,
        historical_accuracy=observed,
        status=CalibrationStatus.CALIBRATED,
        samples=outcome.predictions,
        bucket=bucket,
        notes=notes,
    )


__all__ = [
    "BUCKET_EDGES",
    "MIN_SAMPLES_FOR_CALIBRATION",
    "BucketOutcome",
    "CalibrationStatus",
    "Confidence",
    "bucket_of",
    "calibrate",
]
