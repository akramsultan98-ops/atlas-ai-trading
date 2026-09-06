"""Interval scheduler.

David runs generation on a 15-minute loop [08:55]. This is the same shape, with one
addition: a tick that raises must never kill the loop. An unattended system that exits
on the first transient error is not unattended.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from atlas.models import utcnow


class Clock(Protocol):
    def now(self) -> datetime: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return utcnow()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass
class TickReport:
    ticks: int = 0
    failures: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def consecutive_failure_limit_reached(self) -> bool:
        return self.failures > 0 and self.failures == self.ticks


class IntervalScheduler:
    """Runs a callable on a fixed interval for a bounded number of ticks.

    Bounded rather than infinite so it is testable and so a supervisor process, not this
    class, owns the "run forever" decision.
    """

    def __init__(self, interval_seconds: float, clock: Clock | None = None) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.interval_seconds = interval_seconds
        self._clock = clock or SystemClock()

    def run(
        self,
        tick: Callable[[], None],
        *,
        max_ticks: int,
        stop_after_consecutive_failures: int = 10,
    ) -> TickReport:
        """Run `tick` up to `max_ticks` times.

        A failing tick is recorded and the loop continues. Only a sustained run of
        consecutive failures stops it — at that point the fault is structural and
        continuing just fills the log.
        """
        report = TickReport()
        consecutive = 0

        for _ in range(max_ticks):
            report.ticks += 1
            try:
                tick()
                consecutive = 0
            except Exception as exc:
                report.failures += 1
                consecutive += 1
                report.errors.append(f"{type(exc).__name__}: {exc}")
                if consecutive >= stop_after_consecutive_failures:
                    break
            self._clock.sleep(self.interval_seconds)

        return report
