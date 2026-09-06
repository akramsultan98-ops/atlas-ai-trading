"""BT-01..08. The engine is the highest-risk component: if it is wrong, every
downstream gate measures fiction."""

from __future__ import annotations

from decimal import Decimal

import pytest
from tests.test_data_models import make_bar
from tests.test_evaluator import percent_spec

from atlas.backtest.costs import CostModel
from atlas.backtest.engine import run_backtest
from atlas.backtest.guard import GuardedBars, LookaheadError
from atlas.backtest.stats import max_drawdown
from atlas.backtest.trade import ExitReason
from atlas.data.models import KlineSeries, Timeframe
from atlas.risk.sizing import ExchangeFilters, SizingPolicy

D = Decimal
POLICY = SizingPolicy(
    risk_pct=D("0.01"),
    max_position_pct=D("0.3333"),
    max_deployed_pct=D("0.75"),
    max_concurrent=3,
)
FILTERS = ExchangeFilters(
    step_size=D("0.00000001"),
    min_qty=D("0.00000001"),
    min_notional=D("1"),
    tick_size=D("0.01"),
)
NO_COSTS = CostModel(fee_rate=D(0), slippage_rate=D(0))


def series_from(rows: list[tuple[str, str, str, str]]) -> KlineSeries:
    """rows are (open, high, low, close)."""
    bars = tuple(
        make_bar(i, open=D(o), high=D(h), low=D(low), close=D(c))
        for i, (o, h, low, c) in enumerate(rows)
    )
    return KlineSeries(symbol="BTCUSDT", timeframe=Timeframe.H1, bars=bars)


# ----------------------------------------------------------------- guard (BT-02)


def test_guard_allows_reads_up_to_frontier() -> None:
    bars = series_from([("100", "101", "99", "100")] * 5).bars
    guarded = GuardedBars(bars)
    guarded.advance_to(2)
    assert guarded[0] is bars[0]
    assert guarded[2] is bars[2]


def test_guard_raises_on_forward_read() -> None:
    """A deliberately peeking strategy must fail loudly, not merely score well."""
    bars = series_from([("100", "101", "99", "100")] * 5).bars
    guarded = GuardedBars(bars)
    guarded.advance_to(1)
    with pytest.raises(LookaheadError, match="look-ahead"):
        _ = guarded[2]


def test_guard_raises_on_last_bar_peek() -> None:
    bars = series_from([("100", "101", "99", "100")] * 5).bars
    guarded = GuardedBars(bars)
    guarded.advance_to(0)
    with pytest.raises(LookaheadError):
        _ = guarded[-1]


def test_guard_refuses_slicing() -> None:
    guarded = GuardedBars(series_from([("100", "101", "99", "100")] * 3).bars)
    guarded.advance_to(2)
    with pytest.raises(LookaheadError, match="slicing"):
        _ = guarded[0:2]


# --------------------------------------------------------- hand-computed fixture


def test_hand_computed_winning_trade() -> None:
    """Signal on bar 2 (cross above 100), fill bar 3 open, target hit bar 4.

    Stop 2% -> 196; target 2R -> 208. Sizing: 2% stop at $100 equity and 1% risk
    gives $50 by risk, capped to 33.33 by position cap -> qty = 33.33/200.
    """
    series = series_from(
        [
            ("95", "96", "94", "95"),  # 0
            ("95", "99", "94", "98"),  # 1
            ("98", "201", "97", "200"),  # 2 close 200 crosses above 100 -> signal
            ("200", "202", "199", "201"),  # 3 fill at open 200
            ("201", "210", "200", "209"),  # 4 high 210 >= target 208 -> exit
        ]
    )
    result = run_backtest(
        percent_spec(),
        series,
        policy=POLICY,
        filters=FILTERS,
        starting_equity=D("100"),
        costs=NO_COSTS,
    )
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_index == 3
    assert trade.entry_price == D("200")
    assert trade.exit_index == 4
    assert trade.exit_reason is ExitReason.TARGET
    assert trade.exit_price == D("208")
    assert trade.stop_price == D("196")

    expected_qty = (D("100") * D("0.3333")) / D("200")
    assert trade.quantity == pytest.approx(expected_qty, rel=D("0.0001"))
    assert trade.net_pnl == pytest.approx(trade.quantity * D("8"), rel=D("0.0001"))
    assert result.stats.trade_count == 1
    assert result.stats.wins == 1


def test_hand_computed_losing_trade() -> None:
    series = series_from(
        [
            ("95", "96", "94", "95"),
            ("95", "99", "94", "98"),
            ("98", "201", "97", "200"),
            ("200", "202", "199", "201"),
            ("201", "202", "195", "196"),  # low 195 <= stop 196 -> stop out
        ]
    )
    result = run_backtest(
        percent_spec(),
        series,
        policy=POLICY,
        filters=FILTERS,
        starting_equity=D("100"),
        costs=NO_COSTS,
    )
    trade = result.trades[0]
    assert trade.exit_reason is ExitReason.STOP
    assert trade.exit_price == D("196")
    assert trade.net_pnl < 0
    assert result.stats.losses == 1


# ------------------------------------------------------------------ BT-03, BT-06


def test_fill_is_at_next_bar_open_not_signal_close() -> None:
    """BT-03: a signal produced by bar i's close cannot fill at that same close."""
    series = series_from(
        [
            ("95", "96", "94", "95"),
            ("95", "99", "94", "98"),
            ("98", "201", "97", "200"),  # signal here, close 200
            ("198", "205", "197", "204"),  # next open is 198, deliberately != close 200
            ("204", "220", "203", "219"),
        ]
    )
    result = run_backtest(
        percent_spec(),
        series,
        policy=POLICY,
        filters=FILTERS,
        starting_equity=D("100"),
        costs=NO_COSTS,
    )
    assert result.trades[0].entry_price == D("198")
    assert result.trades[0].entry_index == 3


def test_both_touched_resolves_to_the_stop() -> None:
    """BT-06: bar data cannot say which came first, so resolve pessimistically."""
    series = series_from(
        [
            ("95", "96", "94", "95"),
            ("95", "99", "94", "98"),
            ("98", "201", "97", "200"),
            ("200", "202", "199", "201"),
            ("201", "215", "190", "200"),  # touches target 208 AND stop 196
        ]
    )
    result = run_backtest(
        percent_spec(),
        series,
        policy=POLICY,
        filters=FILTERS,
        starting_equity=D("100"),
        costs=NO_COSTS,
    )
    assert result.trades[0].exit_reason is ExitReason.STOP


def test_entry_bar_cannot_also_exit() -> None:
    """The fill happened at this bar's open; using its range as an exit would assume
    intrabar ordering the data does not contain."""
    series = series_from(
        [
            ("95", "96", "94", "95"),
            ("95", "99", "94", "98"),
            ("98", "201", "97", "200"),
            ("200", "230", "150", "201"),  # entry bar; range spans both levels
            ("201", "202", "200", "201"),
        ]
    )
    result = run_backtest(
        percent_spec(),
        series,
        policy=POLICY,
        filters=FILTERS,
        starting_equity=D("100"),
        costs=NO_COSTS,
    )
    assert result.trades[0].exit_index > 3


# ------------------------------------------------------------------------ costs


def test_costs_reduce_net_pnl() -> None:
    series = series_from(
        [
            ("95", "96", "94", "95"),
            ("95", "99", "94", "98"),
            ("98", "201", "97", "200"),
            ("200", "202", "199", "201"),
            ("201", "210", "200", "209"),
        ]
    )
    free = run_backtest(
        percent_spec(),
        series,
        policy=POLICY,
        filters=FILTERS,
        starting_equity=D("100"),
        costs=NO_COSTS,
    )
    costed = run_backtest(
        percent_spec(), series, policy=POLICY, filters=FILTERS, starting_equity=D("100")
    )
    assert costed.trades[0].fees > 0
    assert costed.trades[0].net_pnl < free.trades[0].net_pnl


def test_slippage_worsens_entry_for_a_long() -> None:
    series = series_from(
        [
            ("95", "96", "94", "95"),
            ("95", "99", "94", "98"),
            ("98", "201", "97", "200"),
            ("200", "202", "199", "201"),
            ("201", "210", "200", "209"),
        ]
    )
    result = run_backtest(
        percent_spec(), series, policy=POLICY, filters=FILTERS, starting_equity=D("100")
    )
    assert result.trades[0].entry_price > D("200")


# ------------------------------------------------------------------- BT-05, stats


def test_unsizeable_signal_is_rejected_not_forced() -> None:
    """BT-05: a strategy that cannot be sized at this capital takes no trade."""
    strict = ExchangeFilters(
        step_size=D("0.00001"),
        min_qty=D("0.00001"),
        min_notional=D("500"),
        tick_size=D("0.01"),
    )
    series = series_from(
        [
            ("95", "96", "94", "95"),
            ("95", "99", "94", "98"),
            ("98", "201", "97", "200"),
            ("200", "202", "199", "201"),
            ("201", "210", "200", "209"),
        ]
    )
    result = run_backtest(
        percent_spec(),
        series,
        policy=POLICY,
        filters=strict,
        starting_equity=D("100"),
        costs=NO_COSTS,
    )
    assert result.trades == []
    assert result.rejected_signals == 1


def test_no_signals_yields_empty_result() -> None:
    series = series_from([("50", "51", "49", "50")] * 10)
    result = run_backtest(
        percent_spec(),
        series,
        policy=POLICY,
        filters=FILTERS,
        starting_equity=D("100"),
        costs=NO_COSTS,
    )
    assert result.trades == []
    assert result.stats.trade_count == 0
    assert result.stats.profit_factor == 0


def test_open_position_closes_at_end_of_data() -> None:
    series = series_from(
        [
            ("95", "96", "94", "95"),
            ("95", "99", "94", "98"),
            ("98", "201", "97", "200"),
            ("200", "202", "199", "201"),
            ("201", "203", "200", "202"),
        ]
    )
    result = run_backtest(
        percent_spec(),
        series,
        policy=POLICY,
        filters=FILTERS,
        starting_equity=D("100"),
        costs=NO_COSTS,
    )
    assert result.trades[0].exit_reason is ExitReason.END_OF_DATA


def test_result_records_provenance() -> None:
    """BT-08."""
    spec = percent_spec()
    series = series_from([("100", "101", "99", "100")] * 5)
    result = run_backtest(spec, series, policy=POLICY, filters=FILTERS, starting_equity=D("100"))
    assert result.spec_hash == spec.content_hash()
    assert result.data_hash == series.content_hash()
    assert result.engine_version
    assert result.cost_model["fee_rate"] == "0.001"


def test_backtest_is_deterministic() -> None:
    series = series_from(
        [
            (str(100 + i % 7), str(103 + i % 7), str(97 + i % 7), str(100 + i % 7 + (i % 5 - 2)))
            for i in range(200)
        ]
    )
    spec = percent_spec(threshold="103")
    first = run_backtest(spec, series, policy=POLICY, filters=FILTERS, starting_equity=D("100"))
    for _ in range(3):
        again = run_backtest(spec, series, policy=POLICY, filters=FILTERS, starting_equity=D("100"))
        assert again.trades == first.trades
        assert again.equity_curve == first.equity_curve


def test_equity_curve_length_matches_bars() -> None:
    series = series_from([("100", "101", "99", "100")] * 25)
    result = run_backtest(
        percent_spec(), series, policy=POLICY, filters=FILTERS, starting_equity=D("100")
    )
    assert len(result.equity_curve) == 25


# ------------------------------------------------------------------------ stats


def test_max_drawdown_computation() -> None:
    assert max_drawdown([D(100), D(120), D(90), D(110)]) == pytest.approx(D("0.25"), rel=D("0.001"))


def test_max_drawdown_of_monotonic_curve_is_zero() -> None:
    assert max_drawdown([D(100), D(110), D(120)]) == 0


def test_max_drawdown_of_empty_curve_is_zero() -> None:
    assert max_drawdown([]) == 0


def test_buy_and_hold_benchmark_is_costed() -> None:
    """SEL-05 compares against a hold that also pays to get in and out."""
    series = series_from([("100", "101", "99", "100"), ("100", "121", "99", "120")])
    result = run_backtest(
        percent_spec(threshold="1000"),
        series,
        policy=POLICY,
        filters=FILTERS,
        starting_equity=D("100"),
    )
    assert result.stats.buy_and_hold_return < D("0.20")
    assert result.stats.buy_and_hold_return > D("0.19")


def test_gap_past_stop_before_fill_is_refused() -> None:
    """If the next open gaps beyond the stop, the trade is already stopped out.

    The stop is fixed at signal time from that bar's close (STRAT-03). When the market
    opens past it, entering would mean opening a position whose protective level is
    already breached, so sizing refuses it rather than entering and instantly exiting.
    """
    series = series_from(
        [
            ("95", "96", "94", "95"),
            ("95", "99", "94", "98"),
            ("98", "201", "97", "200"),  # signal: stop 196, target 208
            ("150", "205", "149", "204"),  # opens at 150, far below the 196 stop
            ("204", "220", "203", "219"),
        ]
    )
    result = run_backtest(
        percent_spec(),
        series,
        policy=POLICY,
        filters=FILTERS,
        starting_equity=D("100"),
        costs=NO_COSTS,
    )
    assert result.trades == []
    assert result.rejected_signals == 1
