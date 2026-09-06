"""Series validation (DATA-03) and staleness detection (DATA-05).

A failed series is rejected, never repaired. Interpolating a missing bar invents price
history, and a backtest run over invented history is not evidence of anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise

from atlas.data.models import KlineSeries, Timeframe
from atlas.errors import ValidationError
from atlas.models import utcnow


class SeriesValidationError(ValidationError):
    """A kline series failed validation and must not be used."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


@dataclass(frozen=True)
class ValidationReport:
    valid: bool
    reasons: list[str] = field(default_factory=list)
    gap_count: int = 0
    duplicate_count: int = 0


def validate_series(series: KlineSeries, *, allow_gaps: bool = False) -> ValidationReport:
    """Check ordering, duplication, continuity and per-bar OHLC consistency.

    Per-bar OHLC consistency is already enforced by `Kline`'s own validator, so a
    constructed series cannot contain an internally inconsistent bar. What this adds is
    the cross-bar structure: order, uniqueness and continuity.

    `allow_gaps` exists for symbols with genuine listing gaps or exchange downtime; it
    records the gaps in the report rather than hiding them.
    """
    reasons: list[str] = []
    bars = series.bars

    if not bars:
        return ValidationReport(valid=False, reasons=["series is empty"])

    interval_ms = series.timeframe.milliseconds
    duplicates = 0
    gaps = 0

    for previous, current in pairwise(bars):
        prev_ms = int(previous.open_time.timestamp() * 1000)
        curr_ms = int(current.open_time.timestamp() * 1000)

        if curr_ms == prev_ms:
            duplicates += 1
            reasons.append(f"duplicate bar at {current.open_time.isoformat()}")
            continue
        if curr_ms < prev_ms:
            reasons.append(
                f"out-of-order bar at {current.open_time.isoformat()} "
                f"follows {previous.open_time.isoformat()}"
            )
            continue

        delta = curr_ms - prev_ms
        if delta != interval_ms:
            gaps += 1
            missing = (delta // interval_ms) - 1 if interval_ms else 0
            message = (
                f"gap of {missing} bar(s) between {previous.open_time.isoformat()} "
                f"and {current.open_time.isoformat()}"
            )
            if not allow_gaps:
                reasons.append(message)

    return ValidationReport(
        valid=not reasons,
        reasons=reasons,
        gap_count=gaps,
        duplicate_count=duplicates,
    )


def require_valid_series(series: KlineSeries, *, allow_gaps: bool = False) -> KlineSeries:
    """Return the series, or raise. Use at every boundary that consumes market data."""
    report = validate_series(series, allow_gaps=allow_gaps)
    if not report.valid:
        raise SeriesValidationError(report.reasons)
    return series


def check_staleness(
    last_close_time: datetime,
    timeframe: Timeframe,
    *,
    now: datetime | None = None,
    tolerance_multiple: int = 2,
) -> tuple[bool, float]:
    """Return `(is_stale, age_seconds)` for the most recent closed bar (DATA-05).

    Stale beyond `tolerance_multiple` intervals halts new entries. Two intervals allows
    for one missed publication without tripping on ordinary jitter.
    """
    reference = now or utcnow()
    age_seconds = (reference - last_close_time).total_seconds()
    limit = timeframe.duration.total_seconds() * tolerance_multiple
    return age_seconds > limit, age_seconds
