"""Monitoring and automatic retirement (MON-01..07)."""

from atlas.monitor.health import compute_health, return_distribution, returns_from_signals
from atlas.monitor.rules import (
    MonitorThresholds,
    RuleAction,
    RuleVerdict,
    StrategyHealth,
    evaluate_all,
)
from atlas.monitor.supervisor import SupervisionResult, Supervisor

__all__ = [
    "MonitorThresholds",
    "RuleAction",
    "RuleVerdict",
    "StrategyHealth",
    "SupervisionResult",
    "Supervisor",
    "compute_health",
    "evaluate_all",
    "return_distribution",
    "returns_from_signals",
]
