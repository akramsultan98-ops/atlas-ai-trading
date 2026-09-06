"""Event-driven backtest engine (BT-01..08).

The highest-risk component in ATLAS: if this is wrong, every downstream gate is
measuring fiction. Four rules do most of the work.

BT-01  A decision at bar i reads only bars <= i. Enforced by GuardedBars, not by care.
BT-03  Fills execute at the next bar's open. A signal is produced by bar i's close and
       cannot be filled at that same close.
BT-05  The same sizing, position cap and minimum-notional rules as live. A backtest
       assuming unlimited capital is meaningless at $100.
BT-06  When a bar touches both stop and target, the stop fills. Bar data cannot say
       which came first, so resolve pessimistically rather than optimistically.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from atlas.backtest.costs import DEFAULT_COSTS, CostModel
from atlas.backtest.guard import GuardedBars
from atlas.backtest.stats import BacktestStats, compute_stats
from atlas.backtest.trade import ExitReason, Trade
from atlas.data.models import Kline, KlineSeries
from atlas.models import PositionSide
from atlas.risk.sizing import ExchangeFilters, SizingPolicy, size_position
from atlas.strategy.evaluator import Signal, evaluate
from atlas.strategy.spec import StrategySpec

ENGINE_VERSION = "primary-1.0"
ZERO = Decimal(0)


@dataclass
class _OpenPosition:
    entry_index: int
    side: PositionSide
    quantity: Decimal
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    entry_fee: Decimal
    was_capped: bool


@dataclass(frozen=True)
class BacktestResult:
    spec_hash: str
    data_hash: str
    engine_version: str
    cost_model: dict[str, str]
    starting_equity: Decimal
    trades: list[Trade]
    equity_curve: list[Decimal]
    stats: BacktestStats
    rejected_signals: int


def _buy_and_hold_return(series: KlineSeries, costs: CostModel) -> Decimal:
    """Benchmark for SEL-05, costed the same way a real hold would be."""
    if len(series.bars) < 2:
        return ZERO
    first, last = series.bars[0].close, series.bars[-1].close
    if first <= 0:
        return ZERO
    gross = (last - first) / first
    return gross - costs.round_trip_rate


def run_backtest(
    spec: StrategySpec,
    series: KlineSeries,
    *,
    policy: SizingPolicy,
    filters: ExchangeFilters | None = None,
    starting_equity: Decimal = Decimal(100),
    costs: CostModel = DEFAULT_COSTS,
) -> BacktestResult:
    """Run `spec` over `series` and return trades, equity curve and statistics."""
    exchange_filters = filters or ExchangeFilters()
    bars = series.bars
    guarded = GuardedBars(bars)

    signals_by_bar: dict[int, Signal] = {}
    for signal in evaluate(spec, bars):
        # One signal per bar; the first rule to fire wins. Two entries on one bar would
        # mean opening two positions from one decision point.
        signals_by_bar.setdefault(signal.bar_index, signal)

    equity = starting_equity
    equity_curve: list[Decimal] = []
    trades: list[Trade] = []
    rejected = 0
    position: _OpenPosition | None = None
    pending: Signal | None = None

    for index in range(len(bars)):
        guarded.advance_to(index)
        bar = guarded[index]

        # 1. Fill any signal raised on the previous bar, at this bar's open (BT-03).
        if position is None and pending is not None:
            fill_price = costs.fill_price(bar.open, pending.side, entering=True)
            sizing = size_position(
                equity=equity,
                free_cash=equity,
                entry_price=fill_price,
                stop_price=pending.stop_price,
                side=pending.side,
                policy=policy,
                filters=exchange_filters,
                open_positions=0,
                deployed=ZERO,
            )
            if sizing.accepted:
                entry_fee = costs.fee_on(sizing.notional)
                equity -= entry_fee
                position = _OpenPosition(
                    entry_index=index,
                    side=pending.side,
                    quantity=sizing.quantity,
                    entry_price=fill_price,
                    stop_price=pending.stop_price,
                    target_price=pending.target_price,
                    entry_fee=entry_fee,
                    was_capped=sizing.capped,
                )
            else:
                rejected += 1
            pending = None

        # 2. Manage an open position against this bar's range (BT-06).
        if position is not None:
            exit_price, reason = _resolve_exit(position, bar, index, spec.max_bars_in_trade)
            if reason is not None and exit_price is not None:
                trade, exit_fee = _close(position, index, bar, exit_price, reason, costs)
                trades.append(trade)
                equity += trade.gross_pnl - exit_fee
                position = None

        # 3. Arm a signal raised on this bar for the next bar's open.
        if position is None and pending is None and index in signals_by_bar:
            pending = signals_by_bar[index]

        equity_curve.append(equity)

    # Close anything still open at the final close, so equity is comparable.
    if position is not None:
        last_index = len(bars) - 1
        last_bar = bars[last_index]
        exit_price = last_bar.close
        trade, exit_fee = _close(
            position, last_index, last_bar, exit_price, ExitReason.END_OF_DATA, costs
        )
        trades.append(trade)
        equity += trade.gross_pnl - exit_fee
        if equity_curve:
            equity_curve[-1] = equity

    return BacktestResult(
        spec_hash=spec.content_hash(),
        data_hash=series.content_hash(),
        engine_version=ENGINE_VERSION,
        cost_model=costs.as_dict(),
        starting_equity=starting_equity,
        trades=trades,
        equity_curve=equity_curve,
        stats=compute_stats(
            trades, equity_curve, starting_equity, _buy_and_hold_return(series, costs)
        ),
        rejected_signals=rejected,
    )


def _resolve_exit(
    position: _OpenPosition, bar: Kline, index: int, max_bars: int
) -> tuple[Decimal | None, ExitReason | None]:
    """Decide whether this bar closes the position, and at what price.

    Entry and exit cannot occur on the same bar: the fill happened at this bar's open,
    and treating the same bar's range as an exit opportunity would assume intrabar
    ordering the data does not contain.
    """
    if index == position.entry_index:
        return None, None

    high = bar.high
    low = bar.low

    if position.side is PositionSide.LONG:
        hit_stop = low <= position.stop_price
        hit_target = high >= position.target_price
    else:
        hit_stop = high >= position.stop_price
        hit_target = low <= position.target_price

    # BT-06: both touched in one bar resolves to the stop. Bar data cannot say which
    # came first, and assuming the target is how a backtest flatters itself.
    if hit_stop:
        return position.stop_price, ExitReason.STOP
    if hit_target:
        return position.target_price, ExitReason.TARGET
    if index - position.entry_index >= max_bars:
        return bar.close, ExitReason.TIME
    return None, None


def _close(
    position: _OpenPosition,
    index: int,
    bar: Kline,
    raw_exit_price: Decimal,
    reason: ExitReason,
    costs: CostModel,
) -> tuple[Trade, Decimal]:
    """Close the position. Returns the trade and the exit fee to charge equity."""
    exit_price = (
        raw_exit_price
        if reason in (ExitReason.STOP, ExitReason.TARGET)
        else costs.fill_price(raw_exit_price, position.side, entering=False)
    )
    direction = Decimal(1) if position.side is PositionSide.LONG else Decimal(-1)
    gross = (exit_price - position.entry_price) * position.quantity * direction
    exit_fee = costs.fee_on(exit_price * position.quantity)

    trade = Trade(
        entry_index=position.entry_index,
        exit_index=index,
        entry_time=bar.open_time,
        exit_time=bar.close_time,
        side=position.side,
        quantity=position.quantity,
        entry_price=position.entry_price,
        exit_price=exit_price,
        stop_price=position.stop_price,
        target_price=position.target_price,
        gross_pnl=gross,
        fees=position.entry_fee + exit_fee,
        net_pnl=gross - position.entry_fee - exit_fee,
        exit_reason=reason,
        was_capped=position.was_capped,
    )
    return trade, exit_fee
