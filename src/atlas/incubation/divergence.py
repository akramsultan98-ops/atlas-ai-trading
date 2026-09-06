"""Backtest-vs-live divergence scoring (INC-04, INC-05).

The gate the source describes but never quantifies: David leaves strategies incubating
"to see whether or not these strategies actually perform as well on live data as
back-tested data" [12:27-12:36] without stating what counts as passing. Every threshold
here is an ATLAS decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from atlas.backtest.stats import BacktestStats
from atlas.incubation.tracker import IncubationSignal

ZERO = Decimal(0)


@dataclass(frozen=True)
class IncubationThresholds:
    """ATLAS decisions."""

    min_days: int = 60
    min_trades: int = 30
    min_profit_factor_retention: Decimal = Decimal("0.70")
    max_drawdown_multiple: Decimal = Decimal("1.5")


@dataclass(frozen=True)
class IncubationMetrics:
    trade_count: int
    wins: int
    losses: int
    win_rate: Decimal
    profit_factor: Decimal
    total_return: Decimal
    max_drawdown: Decimal


@dataclass(frozen=True)
class DivergenceCheck:
    eligible: bool
    reasons: list[str]
    metrics: IncubationMetrics


def compute_metrics(signals: list[IncubationSignal]) -> IncubationMetrics:
    """Aggregate closed paper trades. Open signals are excluded from every statistic."""
    closed = [s for s in signals if s.is_closed and s.return_pct is not None]
    if not closed:
        return IncubationMetrics(0, 0, 0, ZERO, ZERO, ZERO, ZERO)

    returns = [s.return_pct for s in closed if s.return_pct is not None]
    gains = sum((r for r in returns if r > 0), ZERO)
    losses = abs(sum((r for r in returns if r <= 0), ZERO))
    wins = sum(1 for r in returns if r > 0)

    if losses > 0:
        profit_factor = gains / losses
    elif gains > 0:
        profit_factor = Decimal(999)
    else:
        profit_factor = ZERO

    equity = Decimal(1)
    peak = equity
    worst = ZERO
    for r in returns:
        equity *= Decimal(1) + r
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)

    return IncubationMetrics(
        trade_count=len(closed),
        wins=wins,
        losses=len(closed) - wins,
        win_rate=Decimal(wins) / len(closed),
        profit_factor=profit_factor,
        total_return=equity - Decimal(1),
        max_drawdown=worst,
    )


def check_divergence(
    signals: list[IncubationSignal],
    backtest: BacktestStats,
    elapsed_days: int,
    thresholds: IncubationThresholds | None = None,
) -> DivergenceCheck:
    """Decide whether incubation evidence supports promotion.

    All conditions are evaluated so the operator sees every reason at once.
    """
    t = thresholds or IncubationThresholds()
    metrics = compute_metrics(signals)
    reasons: list[str] = []

    if elapsed_days < t.min_days:
        reasons.append(f"incubated {elapsed_days} days, need {t.min_days} (INC-01)")
    if metrics.trade_count < t.min_trades:
        reasons.append(f"{metrics.trade_count} closed trades, need {t.min_trades} (INC-02)")

    required_pf = backtest.profit_factor * t.min_profit_factor_retention
    if metrics.profit_factor < required_pf:
        reasons.append(
            f"profit factor {metrics.profit_factor:.2f} below {t.min_profit_factor_retention} "
            f"x backtest {backtest.profit_factor:.2f} = {required_pf:.2f} (INC-04)"
        )

    max_allowed_dd = backtest.max_drawdown * t.max_drawdown_multiple
    if backtest.max_drawdown > 0 and metrics.max_drawdown > max_allowed_dd:
        reasons.append(
            f"drawdown {metrics.max_drawdown:.2%} exceeds {t.max_drawdown_multiple} x "
            f"backtest {backtest.max_drawdown:.2%} (INC-05)"
        )

    return DivergenceCheck(eligible=not reasons, reasons=reasons, metrics=metrics)
