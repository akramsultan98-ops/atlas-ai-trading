"""Evaluation phases and the leakage guards between them (INTEL-06).

Five phases, in strict chronological order, with no overlap:

    RESEARCH            the data a methodology was designed on
    VALIDATION          tuning, still in-sample for honesty purposes
    OUT_OF_SAMPLE       held out, touched once
    FORWARD_INCUBATION  observed forward in real time
    LIVE                real capital

The rule that makes them worth having: a decision made at time T may only use
information published at or before T. That is easy to state and easy to violate, because
the violation looks like excellent performance rather than like a bug.

Reporting accuracy from RESEARCH or VALIDATION as though it were predictive is the
second failure mode, so a report carries the phase it came from and phases have an
explicit `is_evidence_of_skill` property.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from itertools import pairwise
from typing import Any

from atlas.errors import AtlasError


class LeakageError(AtlasError):
    """Information from after the decision point reached the decision."""


class Phase(StrEnum):
    RESEARCH = "RESEARCH"
    VALIDATION = "VALIDATION"
    OUT_OF_SAMPLE = "OUT_OF_SAMPLE"
    FORWARD_INCUBATION = "FORWARD_INCUBATION"
    LIVE = "LIVE"

    @property
    def is_evidence_of_skill(self) -> bool:
        """Whether performance here says anything about future performance.

        RESEARCH and VALIDATION do not: the methodology was shaped by that data, so
        good numbers there are a description of the fitting, not a prediction.
        """
        return self in {
            Phase.OUT_OF_SAMPLE,
            Phase.FORWARD_INCUBATION,
            Phase.LIVE,
        }


@dataclass(frozen=True)
class PhaseWindow:
    phase: Phase
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(f"{self.phase} window ends at or before it starts")

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": str(self.phase),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }


@dataclass(frozen=True)
class EvaluationPlan:
    """Non-overlapping windows in chronological order."""

    windows: tuple[PhaseWindow, ...]

    def __post_init__(self) -> None:
        ordered = sorted(self.windows, key=lambda w: w.start)
        for earlier, later in pairwise(ordered):
            if later.start < earlier.end:
                raise ValueError(
                    f"{later.phase} starts at {later.start.isoformat()}, inside "
                    f"{earlier.phase} which runs to {earlier.end.isoformat()}; "
                    "overlapping phases mean the held-out set was already seen"
                )

    def phase_at(self, moment: datetime) -> Phase | None:
        for window in self.windows:
            if window.contains(moment):
                return window.phase
        return None

    def window_for(self, phase: Phase) -> PhaseWindow | None:
        for window in self.windows:
            if window.phase is phase:
                return window
        return None

    def as_dict(self) -> dict[str, Any]:
        return {"windows": [w.as_dict() for w in self.windows]}


def assert_no_lookahead(
    decision_at: datetime,
    information_timestamps: list[datetime],
    *,
    label: str = "information",
) -> None:
    """Raise if any input postdates the decision it fed.

    Deliberately an exception rather than a filter. Silently dropping future data hides
    that the caller assembled a leaking dataset, and the same caller will assemble
    another one tomorrow.
    """
    future = [t for t in information_timestamps if t > decision_at]
    if future:
        newest = max(future).isoformat()
        raise LeakageError(
            f"{len(future)} {label} item(s) postdate the decision at "
            f"{decision_at.isoformat()} (newest {newest}); a historical decision cannot "
            "use information that did not exist when it was made"
        )


__all__ = [
    "EvaluationPlan",
    "LeakageError",
    "Phase",
    "PhaseWindow",
    "assert_no_lookahead",
]
