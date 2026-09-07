"""Structured market events, deduplication and source conflict (INTEL-02).

A headline is not an event. An event is a claim with a source, two timestamps, a
category, a severity, the entities it names and the assets it plausibly touches — and,
crucially, an independent count of how many distinct sources reported it.

Two failure modes drive the design.

One source repeated by five aggregators is one observation, not five. Deduplication is
therefore by content, and corroboration counts *distinct sources*, never articles.

When sources disagree, the newest or loudest is not the truth. A conflict is recorded as
a conflict and it suppresses the claim rather than resolving it, because a system that
picks a side under contradiction will confidently pick the wrong one.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from atlas.intel.evidence import Direction

ZERO = Decimal(0)
ONE = Decimal(1)


class EventCategory(StrEnum):
    MACRO = "MACRO"
    GEOPOLITICAL = "GEOPOLITICAL"
    CRYPTO_NEWS = "CRYPTO_NEWS"
    REGULATORY = "REGULATORY"
    EXCHANGE_SECURITY = "EXCHANGE_SECURITY"
    ASSET_SPECIFIC = "ASSET_SPECIFIC"
    CROSS_MARKET = "CROSS_MARKET"


class Severity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}[str(self)]


@dataclass(frozen=True)
class MarketEvent:
    """One reported event, with everything needed to judge whether to believe it."""

    event_id: str
    source: str
    published_at: datetime
    observed_at: datetime
    category: EventCategory
    severity: Severity
    headline: str
    entities: tuple[str, ...] = ()
    affected_assets: tuple[str, ...] = ()
    direction: Direction = Direction.NEUTRAL
    expected_horizon: str = "UNSPECIFIED"
    source_confidence: Decimal = ONE

    def __post_init__(self) -> None:
        if not (ZERO <= self.source_confidence <= ONE):
            raise ValueError("source_confidence must be a fraction in [0, 1]")
        if self.observed_at < self.published_at:
            # Observing something before it was published means one of the two clocks
            # is wrong, and a leakage guard that trusts either would let the future in.
            raise ValueError(
                f"event {self.event_id} was observed at {self.observed_at.isoformat()}, "
                f"before it was published at {self.published_at.isoformat()}"
            )

    @property
    def content_key(self) -> str:
        """Identity by content, so the same story from five aggregators is one event."""
        words = re.findall(r"[a-z0-9]+", self.headline.lower())
        normalised = " ".join(sorted(set(words)))
        payload = f"{self.category}|{normalised}|{','.join(sorted(self.affected_assets))}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def age_at(self, now: datetime) -> timedelta:
        return now - self.published_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "source": self.source,
            "published_at": self.published_at.isoformat(),
            "observed_at": self.observed_at.isoformat(),
            "category": str(self.category),
            "severity": str(self.severity),
            "headline": self.headline,
            "entities": list(self.entities),
            "affected_assets": list(self.affected_assets),
            "direction": str(self.direction),
            "expected_horizon": self.expected_horizon,
            "source_confidence": str(self.source_confidence),
        }


@dataclass(frozen=True)
class CorroboratedEvent:
    """One deduplicated claim and the independent sources that reported it."""

    content_key: str
    representative: MarketEvent
    sources: tuple[str, ...]
    reports: tuple[MarketEvent, ...]
    conflicting_directions: bool

    @property
    def source_count(self) -> int:
        """Distinct sources. Five articles from one wire service count once."""
        return len(self.sources)

    @property
    def is_single_source(self) -> bool:
        return self.source_count < 2

    @property
    def earliest_published(self) -> datetime:
        return min(r.published_at for r in self.reports)

    def is_stale(self, now: datetime, max_age: timedelta) -> bool:
        return (now - self.earliest_published) > max_age

    def as_dict(self) -> dict[str, Any]:
        return {
            "content_key": self.content_key,
            "headline": self.representative.headline,
            "category": str(self.representative.category),
            "severity": str(self.representative.severity),
            "sources": list(self.sources),
            "source_count": self.source_count,
            "conflicting_directions": self.conflicting_directions,
            "earliest_published": self.earliest_published.isoformat(),
        }


def deduplicate(events: list[MarketEvent]) -> list[CorroboratedEvent]:
    """Group reports of the same claim and count the independent sources.

    Direction conflict is recorded, never resolved: two credible sources disagreeing
    about which way a thing cuts is information, and picking one is how a system
    becomes confidently wrong.
    """
    grouped: dict[str, list[MarketEvent]] = defaultdict(list)
    for event in events:
        grouped[event.content_key].append(event)

    out: list[CorroboratedEvent] = []
    for key, reports in grouped.items():
        ordered = sorted(reports, key=lambda e: (e.published_at, e.event_id))
        sources = tuple(sorted({r.source for r in ordered}))
        directions = {r.direction for r in ordered if r.direction.is_directional}
        out.append(
            CorroboratedEvent(
                content_key=key,
                # The most severe report represents the claim: understating severity is
                # the more dangerous error.
                representative=max(ordered, key=lambda e: (e.severity.rank, e.published_at)),
                sources=sources,
                reports=tuple(ordered),
                conflicting_directions=len(directions) > 1,
            )
        )
    return sorted(out, key=lambda c: c.earliest_published)


def visible_at(events: list[MarketEvent], as_of: datetime) -> list[MarketEvent]:
    """Only events already published at `as_of` (INTEL-06, look-ahead guard).

    The single most dangerous leak in event-driven research: evaluating a historical
    decision with news that had not been published when the decision was made produces
    accuracy that cannot be reproduced live.
    """
    return [e for e in events if e.published_at <= as_of]


__all__ = [
    "CorroboratedEvent",
    "EventCategory",
    "MarketEvent",
    "Severity",
    "deduplicate",
    "visible_at",
]
