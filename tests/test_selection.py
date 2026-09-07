"""SEL-01..07. Criteria are David's; every threshold is an ATLAS decision."""

from __future__ import annotations

from decimal import Decimal

import pytest

from atlas.backtest.stats import BacktestStats
from atlas.selection.gates import SelectionThresholds, evaluate_gates

D = Decimal


def stats(**kw: object) -> BacktestStats:
    defaults: dict[str, object] = {
        "trade_count": 150,
        "wins": 60,
        "losses": 90,
        "win_rate": D("0.4"),
        "net_return": D("0.35"),
        "profit_factor": D("1.6"),
        "expectancy": D("0.25"),
        "max_drawdown": D("0.18"),
        "avg_bars_held": D("12"),
        "gross_profit": D("100"),
        "gross_loss": D("62.5"),
        "total_fees": D("9"),
        "final_equity": D("135"),
        "buy_and_hold_return": D("0.10"),
        "beats_buy_and_hold": True,
        "capped_trade_count": 0,
    }
    defaults.update(kw)
    return BacktestStats(**defaults)  # type: ignore[arg-type]


def complete(**kw: object) -> object:
    """Every gate supplied with evidence. A PASS is only meaningful this way."""
    return evaluate_gates(
        stats(**kw),
        out_of_sample=stats(),
        median_stop_distance=D("0.05"),
    )


def test_healthy_strategy_passes_every_gate() -> None:
    outcome = complete()
    assert outcome.passed, outcome.failures  # type: ignore[attr-defined]


def test_too_few_trades_rejected() -> None:
    """SEL-01: the law of large numbers, per [23:18-23:42]."""
    outcome = evaluate_gates(stats(trade_count=40))
    assert not outcome.passed
    assert any("SEL-01" in g.gate for g in outcome.failures)


def test_excessive_drawdown_rejected() -> None:
    """SEL-02: the one rejection David performs on camera [11:41-11:54]."""
    outcome = evaluate_gates(stats(max_drawdown=D("0.40")))
    assert not outcome.passed
    assert any("SEL-02" in g.gate for g in outcome.failures)


def test_weak_profit_factor_rejected() -> None:
    outcome = evaluate_gates(stats(profit_factor=D("1.05")))
    assert any("SEL-03" in g.gate for g in outcome.failures)


def test_negative_expectancy_rejected() -> None:
    outcome = evaluate_gates(stats(expectancy=D("-0.10")))
    assert any("SEL-04" in g.gate for g in outcome.failures)


def test_failing_to_beat_buy_and_hold_rejected() -> None:
    """SEL-05: 'otherwise you might as well just hold that asset instead' [23:00]."""
    outcome = evaluate_gates(
        stats(net_return=D("0.05"), buy_and_hold_return=D("0.30"), beats_buy_and_hold=False)
    )
    assert any("SEL-05" in g.gate for g in outcome.failures)


def test_out_of_sample_collapse_rejected() -> None:
    """SEL-06: in-sample 1.6, out-of-sample 0.6 is curve fitting."""
    outcome = evaluate_gates(stats(), out_of_sample=stats(profit_factor=D("0.6")))
    assert any("SEL-06" in g.gate for g in outcome.failures)


def test_out_of_sample_retention_enforced() -> None:
    """OOS above the floor but far below in-sample still fails retention."""
    outcome = evaluate_gates(
        stats(profit_factor=D("3.0")), out_of_sample=stats(profit_factor=D("1.15"))
    )
    assert any("SEL-06" in g.gate for g in outcome.failures)


def test_out_of_sample_holding_up_passes() -> None:
    outcome = evaluate_gates(
        stats(), out_of_sample=stats(profit_factor=D("1.4")), median_stop_distance=D("0.05")
    )
    assert outcome.passed, outcome.failures


@pytest.mark.parametrize("stop", ["0.005", "0.01", "0.02", "0.029"])
def test_stop_below_feasible_band_rejected(stop: str) -> None:
    """SEL-07: a stop this tight is structurally under-risked at any equity."""
    outcome = evaluate_gates(stats(), median_stop_distance=D(stop))
    assert any("SEL-07" in g.gate for g in outcome.failures)


@pytest.mark.parametrize("stop", ["0.21", "0.30", "0.50"])
def test_stop_above_feasible_band_rejected(stop: str) -> None:
    """Above the band the position falls under minNotional and cannot be sized."""
    outcome = evaluate_gates(stats(), median_stop_distance=D(stop))
    assert any("SEL-07" in g.gate for g in outcome.failures)


@pytest.mark.parametrize("stop", ["0.03", "0.05", "0.10", "0.20"])
def test_stop_inside_feasible_band_accepted(stop: str) -> None:
    outcome = evaluate_gates(stats(), out_of_sample=stats(), median_stop_distance=D(stop))
    assert outcome.passed, outcome.failures


def test_all_gates_evaluated_not_short_circuited() -> None:
    """Reporting every failure at once keeps rejection reasons analysable in aggregate."""
    outcome = evaluate_gates(
        stats(
            trade_count=10,
            max_drawdown=D("0.9"),
            profit_factor=D("0.5"),
            expectancy=D("-1"),
            beats_buy_and_hold=False,
        ),
        median_stop_distance=D("0.90"),
    )
    assert len(outcome.failures) >= 5


def test_thresholds_are_configurable_not_hardcoded() -> None:
    """The video gives no numbers, so ours must be tunable."""
    lenient = SelectionThresholds(min_trades=10, min_profit_factor=D("1.0"))
    assert evaluate_gates(
        stats(trade_count=15, profit_factor=D("1.1")),
        out_of_sample=stats(trade_count=15, profit_factor=D("1.1")),
        thresholds=lenient,
        median_stop_distance=D("0.05"),
    ).passed


def test_outcome_serialises_for_persistence() -> None:
    payload = evaluate_gates(
        stats(), out_of_sample=stats(), median_stop_distance=D("0.05")
    ).as_dict()
    assert payload["passed"] is True
    assert all("gate" in g and "verdict" in g for g in payload["gates"])
    assert payload["undefined"] == []
