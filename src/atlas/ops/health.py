"""System health checks (OPS, DATA-05, KILL-02)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from atlas.data.models import Timeframe
from atlas.data.validate import check_staleness
from atlas.models import KillSwitchTrigger, utcnow


@dataclass(frozen=True)
class HealthIssue:
    check: str
    detail: str
    trigger: KillSwitchTrigger | None = None


@dataclass(frozen=True)
class HealthReport:
    healthy: bool
    issues: list[HealthIssue]

    @property
    def kill_switch_triggers(self) -> list[KillSwitchTrigger]:
        return [i.trigger for i in self.issues if i.trigger is not None]


def check_system_health(
    *,
    last_bar_close: datetime | None,
    timeframe: Timeframe,
    last_heartbeat: datetime | None,
    api_error_rate: Decimal,
    stuck_order_count: int,
    now: datetime | None = None,
    heartbeat_tolerance: timedelta = timedelta(minutes=15),
    max_api_error_rate: Decimal = Decimal("0.25"),
) -> HealthReport:
    """Run every check and report all failures together."""
    reference = now or utcnow()
    issues: list[HealthIssue] = []

    if last_bar_close is None:
        issues.append(
            HealthIssue(
                "market_data", "no market data received yet", KillSwitchTrigger.DATA_STALENESS
            )
        )
    else:
        stale, age = check_staleness(last_bar_close, timeframe, now=reference)
        if stale:
            issues.append(
                HealthIssue(
                    "market_data",
                    f"last bar closed {age:.0f}s ago, beyond 2x the {timeframe} interval",
                    KillSwitchTrigger.DATA_STALENESS,
                )
            )

    if last_heartbeat is None or reference - last_heartbeat > heartbeat_tolerance:
        issues.append(HealthIssue("heartbeat", "no heartbeat within tolerance"))

    if api_error_rate > max_api_error_rate:
        issues.append(
            HealthIssue(
                "api_errors",
                f"error rate {api_error_rate:.0%} exceeds {max_api_error_rate:.0%}",
                KillSwitchTrigger.API_ERROR_RATE,
            )
        )

    if stuck_order_count > 0:
        issues.append(
            HealthIssue("orders", f"{stuck_order_count} order(s) stuck in a pending state")
        )

    return HealthReport(healthy=not issues, issues=issues)
