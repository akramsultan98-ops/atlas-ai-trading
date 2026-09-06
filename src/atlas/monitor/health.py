"""Live strategy health computation."""

from __future__ import annotations

from decimal import Decimal

from atlas.incubation.tracker import IncubationSignal
from atlas.monitor.rules import MonitorThresholds, StrategyHealth

ZERO = Decimal(0)


def _sqrt(value: Decimal) -> Decimal:
    return value.sqrt() if value > 0 else ZERO


def compute_health(
    closed_returns: list[Decimal],
    *,
    backtest_mean_return: Decimal,
    backtest_return_sigma: Decimal,
    bars_since_last_signal: int = 0,
    mean_inter_trade_bars: Decimal = ZERO,
    thresholds: MonitorThresholds | None = None,
) -> StrategyHealth:
    """Build a health snapshot from a strategy's closed live returns.

    Expected equity is projected from the backtest's per-trade return distribution over
    the same number of trades, and the band width scales with sqrt(n) — the spread of a
    sum of n independent draws, so the band does not tighten artificially as trades
    accumulate.
    """
    t = thresholds or MonitorThresholds()
    n = len(closed_returns)
    window = closed_returns[-t.window :] if n else []

    wins = [r for r in window if r > 0]
    losses = [r for r in window if r <= 0]
    gross_win = sum(wins, ZERO)
    gross_loss = abs(sum(losses, ZERO))

    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    elif gross_win > 0:
        profit_factor = Decimal(999)
    else:
        profit_factor = ZERO

    streak = 0
    for value in reversed(closed_returns):
        if value <= 0:
            streak += 1
        else:
            break

    realised = sum(closed_returns, ZERO)
    expected = backtest_mean_return * n
    sigma = backtest_return_sigma * _sqrt(Decimal(n))

    return StrategyHealth(
        trade_count=n,
        rolling_win_rate=(Decimal(len(wins)) / len(window)) if window else ZERO,
        rolling_profit_factor=profit_factor,
        consecutive_losses=streak,
        realised_equity=realised,
        expected_equity=expected,
        equity_sigma=sigma,
        bars_since_last_signal=bars_since_last_signal,
        mean_inter_trade_bars=mean_inter_trade_bars,
    )


def returns_from_signals(signals: list[IncubationSignal]) -> list[Decimal]:
    return [s.return_pct for s in signals if s.is_closed and s.return_pct is not None]


def return_distribution(returns: list[Decimal]) -> tuple[Decimal, Decimal]:
    """Return `(mean, population sigma)` of a return series."""
    if not returns:
        return ZERO, ZERO
    n = Decimal(len(returns))
    mean = sum(returns, ZERO) / n
    variance = sum(((r - mean) ** 2 for r in returns), ZERO) / n
    return mean, _sqrt(variance)
