"""STRAT-03/STRAT-05 and BT-01: pure, deterministic, no forward reads."""

from __future__ import annotations

from decimal import Decimal

import pytest
from tests.test_data_models import make_bar

from atlas.data.models import Timeframe
from atlas.errors import ValidationError
from atlas.models import PositionSide
from atlas.strategy.evaluator import evaluate, require_evaluable
from atlas.strategy.spec import (
    Comparator,
    Condition,
    EntryRule,
    IndicatorSpec,
    Operand,
    OperandKind,
    StopKind,
    StopRule,
    StrategySpec,
    TargetKind,
    TargetRule,
)

D = Decimal


def flat_bars(closes: list[str]) -> list:
    out = []
    for i, c in enumerate(closes):
        price = D(c)
        out.append(make_bar(i, open=price, high=price + 2, low=price - 2, close=price))
    return out


def percent_spec(side: PositionSide = PositionSide.LONG, threshold: str = "100") -> StrategySpec:
    """Enter whenever close crosses a constant. Stop 2%, target 2R."""
    op = Comparator.CROSS_ABOVE if side is PositionSide.LONG else Comparator.CROSS_BELOW
    return StrategySpec(
        name="threshold cross",
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        indicators=(),
        entries=(
            EntryRule(
                side=side,
                conditions=(
                    Condition(
                        left=Operand(kind=OperandKind.PRICE, ref="close"),
                        op=op,
                        right=Operand(kind=OperandKind.CONSTANT, value=D(threshold)),
                    ),
                ),
            ),
        ),
        stop=StopRule(kind=StopKind.PERCENT, value=D("0.02")),
        target=TargetRule(kind=TargetKind.RISK_MULTIPLE, value=D("2")),
    )


def test_no_signal_without_crossing() -> None:
    signals = evaluate(percent_spec(), flat_bars(["90", "91", "92"]))
    assert signals == []


def test_long_signal_on_cross_above() -> None:
    bars = flat_bars(["95", "98", "105", "107"])
    signals = evaluate(percent_spec(), bars)
    assert len(signals) == 1
    assert signals[0].bar_index == 2
    assert signals[0].side is PositionSide.LONG


def test_short_signal_on_cross_below() -> None:
    bars = flat_bars(["110", "105", "95", "90"])
    signals = evaluate(percent_spec(PositionSide.SHORT), bars)
    assert len(signals) == 1
    assert signals[0].bar_index == 2
    assert signals[0].side is PositionSide.SHORT


def test_stop_and_target_computed_at_signal_time() -> None:
    """STRAT-03: levels are fixed before the position exists."""
    bars = flat_bars(["95", "98", "200"])
    signal = evaluate(percent_spec(), bars)[0]
    assert signal.reference_price == D("200")
    assert signal.stop_price == D("196")  # 200 * (1 - 0.02)
    assert signal.target_price == D("208")  # 2R above entry
    assert signal.stop_distance == D("4")
    assert signal.stop_distance_fraction == D("0.02")
    assert signal.reward_risk == D("2")


def test_short_levels_are_inverted() -> None:
    bars = flat_bars(["110", "105", "50"])
    signal = evaluate(percent_spec(PositionSide.SHORT), bars)[0]
    assert signal.stop_price > signal.reference_price
    assert signal.target_price < signal.reference_price


def test_first_bar_cannot_cross() -> None:
    """A crossing needs a predecessor; bar 0 has none."""
    assert evaluate(percent_spec(), flat_bars(["200"])) == []


def test_evaluation_is_deterministic() -> None:
    """STRAT-05."""
    bars = flat_bars([str(90 + (i * 13) % 30) for i in range(200)])
    spec = percent_spec()
    first = evaluate(spec, bars)
    for _ in range(5):
        assert evaluate(spec, bars) == first


def test_evaluation_does_not_read_forward() -> None:
    """BT-01: signals in the first N bars must not change when later bars are added."""
    bars = flat_bars([str(90 + (i * 7) % 25) for i in range(120)])
    spec = percent_spec()
    truncated = evaluate(spec, bars[:60])
    full = [s for s in evaluate(spec, bars) if s.bar_index < 60]
    assert truncated == full


def test_atr_stop_requires_warmup() -> None:
    """No signal before the ATR has enough history to produce a stop."""
    spec = StrategySpec(
        name="atr stop",
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        indicators=(IndicatorSpec(id="atr5", name="atr", period=5),),
        entries=(
            EntryRule(
                side=PositionSide.LONG,
                conditions=(
                    Condition(
                        left=Operand(kind=OperandKind.PRICE, ref="close"),
                        op=Comparator.GT,
                        right=Operand(kind=OperandKind.CONSTANT, value=D("1")),
                    ),
                ),
            ),
        ),
        stop=StopRule(kind=StopKind.ATR_MULTIPLE, value=D("2"), indicator_ref="atr5"),
        target=TargetRule(kind=TargetKind.RISK_MULTIPLE, value=D("2")),
    )
    signals = evaluate(spec, flat_bars([str(100 + i) for i in range(20)]))
    assert all(s.bar_index >= 4 for s in signals)
    assert signals


def test_empty_bars_yield_no_signals() -> None:
    assert evaluate(percent_spec(), []) == []


def test_require_evaluable_rejects_short_history() -> None:
    spec = StrategySpec(
        name="long warmup",
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        indicators=(IndicatorSpec(id="slow", name="sma", period=200),),
        entries=(
            EntryRule(
                side=PositionSide.LONG,
                conditions=(
                    Condition(
                        left=Operand(kind=OperandKind.PRICE, ref="close"),
                        op=Comparator.GT,
                        right=Operand(kind=OperandKind.INDICATOR, ref="slow"),
                    ),
                ),
            ),
        ),
        stop=StopRule(kind=StopKind.PERCENT, value=D("0.03")),
        target=TargetRule(kind=TargetKind.RISK_MULTIPLE, value=D("2")),
    )
    with pytest.raises(ValidationError, match="insufficient history"):
        require_evaluable(spec, flat_bars(["100"] * 50))


def test_stop_distance_fraction_is_what_sizing_uses() -> None:
    """SEL-07 gates on this quantity, so it must be a clean fraction of entry."""
    bars = flat_bars(["95", "98", "150"])
    signal = evaluate(percent_spec(), bars)[0]
    assert signal.stop_distance_fraction == D("0.02")
    assert D(0) < signal.stop_distance_fraction < D(1)
