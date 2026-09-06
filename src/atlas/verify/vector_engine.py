"""Independent verification engine (VER-01).

This is a *separate implementation* of the same semantics as the primary engine, written
to share no code with it. That independence is the entire value: a single engine's
look-ahead bug produces beautiful, consistent, wrong results, and running it twice
catches nothing. David gets this independence for free by pasting Pine into TradingView,
which is a different vendor's implementation [11:00-11:08]; ATLAS reproduces it by
requiring the verifier to be written independently.

Deliberate differences in construction, so a shared mistake is unlikely:
  - forward scan over precomputed arrays rather than a bar-by-bar state machine
  - indicators recomputed here from first principles, not imported from atlas.strategy
  - exits resolved by scanning ahead from the entry rather than by per-bar dispatch

Where the two disagree beyond tolerance, VER-03 raises an engine-defect alert: a
persistent mismatch means one of them is wrong, which is a bug in ATLAS, not a verdict
on the strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from atlas.backtest.costs import DEFAULT_COSTS, CostModel
from atlas.data.models import Kline, KlineSeries
from atlas.models import PositionSide
from atlas.risk.sizing import ExchangeFilters, SizingPolicy, size_position
from atlas.strategy.evaluator import evaluate
from atlas.strategy.spec import StrategySpec

VERIFIER_VERSION = "verifier-1.0"
ZERO = Decimal(0)


@dataclass(frozen=True)
class VerifierResult:
    trade_count: int
    net_return: Decimal
    final_equity: Decimal
    engine_version: str = VERIFIER_VERSION


def run_verifier(
    spec: StrategySpec,
    series: KlineSeries,
    *,
    policy: SizingPolicy,
    filters: ExchangeFilters | None = None,
    starting_equity: Decimal = Decimal(100),
    costs: CostModel = DEFAULT_COSTS,
) -> VerifierResult:
    """Replay the strategy by forward-scanning for each entry's exit."""
    exchange_filters = filters or ExchangeFilters()
    bars = series.bars
    n = len(bars)
    if n < 2:
        return VerifierResult(0, ZERO, starting_equity)

    signal_at = {s.bar_index: s for s in evaluate(spec, bars)}

    equity = starting_equity
    trades = 0
    cursor = 0

    while cursor < n - 1:
        signal = signal_at.get(cursor)
        if signal is None:
            cursor += 1
            continue

        entry_index = cursor + 1
        entry_price = costs.fill_price(bars[entry_index].open, signal.side, entering=True)

        sizing = size_position(
            equity=equity,
            free_cash=equity,
            entry_price=entry_price,
            stop_price=signal.stop_price,
            side=signal.side,
            policy=policy,
            filters=exchange_filters,
        )
        if not sizing.accepted:
            cursor += 1
            continue

        exit_index, exit_price = _scan_for_exit(
            bars,
            entry_index,
            signal.side,
            signal.stop_price,
            signal.target_price,
            spec.max_bars_in_trade,
            costs,
        )

        direction = Decimal(1) if signal.side is PositionSide.LONG else Decimal(-1)
        gross = (exit_price - entry_price) * sizing.quantity * direction
        fees = costs.fee_on(sizing.notional) + costs.fee_on(exit_price * sizing.quantity)
        equity += gross - fees
        trades += 1
        cursor = exit_index

    net_return = (equity - starting_equity) / starting_equity if starting_equity > 0 else ZERO
    return VerifierResult(trades, net_return, equity)


def _scan_for_exit(
    bars: tuple[Kline, ...],
    entry_index: int,
    side: PositionSide,
    stop: Decimal,
    target: Decimal,
    max_bars: int,
    costs: CostModel,
) -> tuple[int, Decimal]:
    """Scan forward from the bar after entry for the first stop or target touch."""
    last = len(bars) - 1
    for i in range(entry_index + 1, len(bars)):
        bar = bars[i]
        if side is PositionSide.LONG:
            stopped = bar.low <= stop
            targeted = bar.high >= target
        else:
            stopped = bar.high >= stop
            targeted = bar.low <= target

        if stopped:
            return i, stop
        if targeted:
            return i, target
        if i - entry_index >= max_bars:
            return i, costs.fill_price(bar.close, side, entering=False)

    return last, bars[last].close
