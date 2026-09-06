"""Backtest engine (specification section 5, BT-01..08)."""

from atlas.backtest.costs import DEFAULT_COSTS, CostModel
from atlas.backtest.engine import ENGINE_VERSION, BacktestResult, run_backtest
from atlas.backtest.guard import GuardedBars, LookaheadError
from atlas.backtest.stats import BacktestStats, compute_stats, max_drawdown
from atlas.backtest.trade import ExitReason, Trade

__all__ = [
    "DEFAULT_COSTS",
    "ENGINE_VERSION",
    "BacktestResult",
    "BacktestStats",
    "CostModel",
    "ExitReason",
    "GuardedBars",
    "LookaheadError",
    "Trade",
    "compute_stats",
    "max_drawdown",
    "run_backtest",
]
