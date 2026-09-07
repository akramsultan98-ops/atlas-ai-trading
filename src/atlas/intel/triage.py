"""Deciding when a model call is worth making (INTEL-08).

Most bars deserve no research at all. Deterministic analysis answers the ordinary case,
and spending a deep model call on a candle that resolved itself is how a research budget
is exhausted before the day that mattered.

Three tiers: deterministic (free), shallow triage (cheap), deep research (expensive).
A tier is only reached when the case is both uncertain and consequential, and the budget
is enforced by a counter rather than by intention.

If research is unavailable, every path returns DETERMINISTIC and ATLAS carries on: the
control plane must never depend on the advisory plane (AI-08).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from atlas.intel.events import CorroboratedEvent, Severity

ZERO = Decimal(0)
ONE = Decimal(1)


class ResearchTier(StrEnum):
    DETERMINISTIC = "DETERMINISTIC"
    SHALLOW = "SHALLOW"
    DEEP = "DEEP"


@dataclass(frozen=True)
class TriagePolicy:
    """ATLAS decisions."""

    deep_calls_per_day: int = 20
    shallow_calls_per_day: int = 200
    min_severity_for_deep: Severity = Severity.HIGH
    # Below this, the deterministic layer is confident enough that a model would only
    # add latency and an opinion.
    uncertainty_floor: Decimal = Decimal("0.30")


@dataclass(frozen=True)
class TriageDecision:
    tier: ResearchTier
    reason: str
    novelty: Decimal
    uncertainty: Decimal

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": str(self.tier),
            "reason": self.reason,
            "novelty": str(self.novelty),
            "uncertainty": str(self.uncertainty),
        }


class ResearchBudget:
    """A spent-call counter. Exhaustion degrades the tier; it never fails the tick."""

    def __init__(self, policy: TriagePolicy | None = None) -> None:
        self._policy = policy or TriagePolicy()
        self.deep_spent = 0
        self.shallow_spent = 0

    @property
    def deep_available(self) -> bool:
        return self.deep_spent < self._policy.deep_calls_per_day

    @property
    def shallow_available(self) -> bool:
        return self.shallow_spent < self._policy.shallow_calls_per_day

    def spend(self, tier: ResearchTier) -> None:
        if tier is ResearchTier.DEEP:
            self.deep_spent += 1
        elif tier is ResearchTier.SHALLOW:
            self.shallow_spent += 1

    def reset(self) -> None:
        self.deep_spent = 0
        self.shallow_spent = 0


def novelty_of(event: CorroboratedEvent, seen_keys: set[str]) -> Decimal:
    """1 for a claim never seen before, 0 for one already processed."""
    return ZERO if event.content_key in seen_keys else ONE


def triage(
    *,
    uncertainty: Decimal,
    event: CorroboratedEvent | None,
    seen_keys: set[str],
    budget: ResearchBudget,
    policy: TriagePolicy | None = None,
    research_available: bool = True,
) -> TriageDecision:
    """Choose the cheapest tier that can answer the question.

    Note the ordering: an event that is severe but already-seen is not novel, and
    re-researching it buys a second opinion on a settled question.
    """
    p = policy or TriagePolicy()
    novelty = novelty_of(event, seen_keys) if event is not None else ZERO

    if not research_available:
        return TriageDecision(
            ResearchTier.DETERMINISTIC,
            "no research provider configured; deterministic analysis only (AI-08)",
            novelty,
            uncertainty,
        )

    if uncertainty < p.uncertainty_floor:
        return TriageDecision(
            ResearchTier.DETERMINISTIC,
            f"deterministic analysis is decisive (uncertainty {uncertainty} < "
            f"{p.uncertainty_floor}); a model call would add an opinion, not information",
            novelty,
            uncertainty,
        )

    severe = event is not None and event.representative.severity.rank >= (
        p.min_severity_for_deep.rank
    )
    if severe and novelty > ZERO:
        if budget.deep_available:
            return TriageDecision(
                ResearchTier.DEEP,
                f"novel {event.representative.severity} event with unresolved "
                "uncertainty justifies a deep call"
                if event is not None
                else "deep call justified",
                novelty,
                uncertainty,
            )
        if budget.shallow_available:
            return TriageDecision(
                ResearchTier.SHALLOW,
                "deep budget exhausted; degrading to shallow triage rather than skipping the event",
                novelty,
                uncertainty,
            )
        return TriageDecision(
            ResearchTier.DETERMINISTIC,
            "research budget exhausted; continuing on deterministic evidence alone",
            novelty,
            uncertainty,
        )

    if budget.shallow_available:
        return TriageDecision(
            ResearchTier.SHALLOW,
            "ambiguous but not severe; shallow triage is proportionate",
            novelty,
            uncertainty,
        )

    return TriageDecision(
        ResearchTier.DETERMINISTIC,
        "shallow budget exhausted; continuing on deterministic evidence alone",
        novelty,
        uncertainty,
    )


__all__ = [
    "ResearchBudget",
    "ResearchTier",
    "TriageDecision",
    "TriagePolicy",
    "novelty_of",
    "triage",
]
