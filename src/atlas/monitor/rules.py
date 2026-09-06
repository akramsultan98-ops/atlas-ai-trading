"""Retirement rules (MON-01..05).

The three rules are David's; every parameter is an ATLAS decision, because the video
states each rule and no number for any of them.

MON-01  equity curve below the lower standard-deviation band — his "ultimate stop"
        [21:12-21:33]
MON-02  rolling win rate falling apart [21:01-21:12]
MON-03  rolling profit factor no longer showing an edge [21:06-21:12]

MON-04 and MON-05 are ATLAS additions: a 30-trade window is slow to notice a sharp
regime break, and a strategy that silently stops signalling looks identical to one
that is merely quiet.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

ZERO = Decimal(0)


class RuleAction(StrEnum):
    RETIRE = "RETIRE"
    SUSPEND = "SUSPEND"
    ALERT = "ALERT"


@dataclass(frozen=True)
class MonitorThresholds:
    """ATLAS decisions."""

    window: int = 30
    band_sigma: Decimal = Decimal("2")
    win_rate_retention: Decimal = Decimal("0.60")
    min_profit_factor: Decimal = Decimal("1.00")
    max_consecutive_losses: int = 8
    starvation_multiple: Decimal = Decimal("3")


@dataclass(frozen=True)
class StrategyHealth:
    """Live statistics for one strategy, computed from its closed trades."""

    trade_count: int
    rolling_win_rate: Decimal
    rolling_profit_factor: Decimal
    consecutive_losses: int
    realised_equity: Decimal
    expected_equity: Decimal
    equity_sigma: Decimal
    bars_since_last_signal: int
    mean_inter_trade_bars: Decimal


@dataclass(frozen=True)
class RuleVerdict:
    rule: str
    fired: bool
    action: RuleAction
    detail: str


def equity_band_breach(health: StrategyHealth, thresholds: MonitorThresholds) -> RuleVerdict:
    """MON-01. The 'ultimate stop': realised equity below expected minus N sigma."""
    lower = health.expected_equity - (health.equity_sigma * thresholds.band_sigma)
    fired = health.equity_sigma > 0 and health.realised_equity < lower
    return RuleVerdict(
        rule="MON-01 equity_band",
        fired=fired,
        action=RuleAction.RETIRE,
        detail=(
            f"realised {health.realised_equity:.4f} vs lower band {lower:.4f} "
            f"(expected {health.expected_equity:.4f} - {thresholds.band_sigma} sigma "
            f"x {health.equity_sigma:.4f})"
        ),
    )


def win_rate_collapse(
    health: StrategyHealth, backtest_win_rate: Decimal, thresholds: MonitorThresholds
) -> RuleVerdict:
    """MON-02."""
    floor = backtest_win_rate * thresholds.win_rate_retention
    fired = health.trade_count >= thresholds.window and health.rolling_win_rate < floor
    return RuleVerdict(
        rule="MON-02 rolling_win_rate",
        fired=fired,
        action=RuleAction.RETIRE,
        detail=(
            f"rolling win rate {health.rolling_win_rate:.2%} below "
            f"{thresholds.win_rate_retention} x backtest {backtest_win_rate:.2%} "
            f"= {floor:.2%}"
        ),
    )


def profit_factor_collapse(health: StrategyHealth, thresholds: MonitorThresholds) -> RuleVerdict:
    """MON-03."""
    fired = (
        health.trade_count >= thresholds.window
        and health.rolling_profit_factor < thresholds.min_profit_factor
    )
    return RuleVerdict(
        rule="MON-03 rolling_profit_factor",
        fired=fired,
        action=RuleAction.RETIRE,
        detail=(
            f"rolling profit factor {health.rolling_profit_factor:.2f} below "
            f"{thresholds.min_profit_factor}"
        ),
    )


def consecutive_loss_streak(health: StrategyHealth, thresholds: MonitorThresholds) -> RuleVerdict:
    """MON-04, ATLAS addition. Catches a sharp break faster than a 30-trade window."""
    fired = health.consecutive_losses >= thresholds.max_consecutive_losses
    return RuleVerdict(
        rule="MON-04 consecutive_losses",
        fired=fired,
        action=RuleAction.SUSPEND,
        detail=(
            f"{health.consecutive_losses} consecutive losses "
            f"(limit {thresholds.max_consecutive_losses})"
        ),
    )


def signal_starvation(health: StrategyHealth, thresholds: MonitorThresholds) -> RuleVerdict:
    """MON-05, ATLAS addition. A silently broken strategy looks like a quiet one."""
    limit = health.mean_inter_trade_bars * thresholds.starvation_multiple
    fired = health.mean_inter_trade_bars > 0 and Decimal(health.bars_since_last_signal) > limit
    return RuleVerdict(
        rule="MON-05 signal_starvation",
        fired=fired,
        action=RuleAction.ALERT,
        detail=(
            f"{health.bars_since_last_signal} bars since the last signal "
            f"(expected around {health.mean_inter_trade_bars:.1f})"
        ),
    )


def evaluate_all(
    health: StrategyHealth,
    backtest_win_rate: Decimal,
    thresholds: MonitorThresholds | None = None,
) -> list[RuleVerdict]:
    """Evaluate every rule. No quorum, no weighting, no averaging (MON-07)."""
    t = thresholds or MonitorThresholds()
    return [
        equity_band_breach(health, t),
        win_rate_collapse(health, backtest_win_rate, t),
        profit_factor_collapse(health, t),
        consecutive_loss_streak(health, t),
        signal_starvation(health, t),
    ]
