"""Pure strategy evaluation (STRAT-05, BT-01).

`evaluate` is a pure function of (spec, bars). Same inputs, same signals, always.

Every value used at bar `i` comes from bars `<= i`. The evaluator emits a signal on a
closed bar; the backtester fills it at the *next* bar's open (BT-03). Stop and target are
absolute prices computed at signal time from that bar's close (STRAT-03), so they are
fixed before the position exists and cannot drift with later data.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from atlas.data.models import Kline
from atlas.errors import ValidationError
from atlas.models import PositionSide, StrictModel
from atlas.strategy import indicators as ind
from atlas.strategy.spec import (
    Comparator,
    Condition,
    Operand,
    OperandKind,
    PriceField,
    StopKind,
    StrategySpec,
    TargetKind,
)


class Signal(StrictModel):
    """An entry intent produced on a closed bar."""

    bar_index: int
    side: PositionSide
    reference_price: Decimal
    stop_price: Decimal
    target_price: Decimal

    @property
    def stop_distance(self) -> Decimal:
        """Absolute distance from reference to stop."""
        return abs(self.reference_price - self.stop_price)

    @property
    def stop_distance_fraction(self) -> Decimal:
        """Stop distance as a fraction of the reference price.

        This is the quantity the risk engine sizes from, and the one SEL-07 gates on.
        """
        return self.stop_distance / self.reference_price

    @property
    def reward_risk(self) -> Decimal:
        risk = self.stop_distance
        if risk == 0:
            return Decimal(0)
        return abs(self.target_price - self.reference_price) / risk


def _compute_indicators(
    spec: StrategySpec, bars: Sequence[Kline]
) -> dict[str, list[Decimal | None]]:
    return {item.id: ind.compute(item.name, bars, item.period) for item in spec.indicators}


def _operand_value(
    operand: Operand,
    index: int,
    bars: Sequence[Kline],
    computed: dict[str, list[Decimal | None]],
) -> Decimal | None:
    if operand.kind is OperandKind.CONSTANT:
        return operand.value
    if operand.kind is OperandKind.PRICE:
        if operand.ref is None:  # unreachable: Operand validates this at construction
            raise ValueError("price operand without a ref")
        bar = bars[index]
        return {
            PriceField.OPEN: bar.open,
            PriceField.HIGH: bar.high,
            PriceField.LOW: bar.low,
            PriceField.CLOSE: bar.close,
            PriceField.VOLUME: bar.volume,
        }[PriceField(operand.ref)]
    series = computed[str(operand.ref)]
    return series[index] if index < len(series) else None


def _evaluate_condition(
    condition: Condition,
    index: int,
    bars: Sequence[Kline],
    computed: dict[str, list[Decimal | None]],
) -> bool:
    left = _operand_value(condition.left, index, bars, computed)
    right = _operand_value(condition.right, index, bars, computed)
    if left is None or right is None:
        return False

    if condition.op is Comparator.GT:
        return left > right
    if condition.op is Comparator.LT:
        return left < right
    if condition.op is Comparator.GTE:
        return left >= right
    if condition.op is Comparator.LTE:
        return left <= right

    # Crossings need the previous bar; bar 0 has no predecessor so it cannot cross.
    if index == 0:
        return False
    prev_left = _operand_value(condition.left, index - 1, bars, computed)
    prev_right = _operand_value(condition.right, index - 1, bars, computed)
    if prev_left is None or prev_right is None:
        return False

    if condition.op is Comparator.CROSS_ABOVE:
        return prev_left <= prev_right and left > right
    return prev_left >= prev_right and left < right


def _stop_price(
    spec: StrategySpec,
    side: PositionSide,
    reference: Decimal,
    index: int,
    computed: dict[str, list[Decimal | None]],
) -> Decimal | None:
    rule = spec.stop
    if rule.kind is StopKind.PERCENT:
        distance = reference * rule.value
    else:
        series = computed[str(rule.indicator_ref)]
        atr_value = series[index] if index < len(series) else None
        if atr_value is None or atr_value <= 0:
            return None
        distance = atr_value * rule.value

    if distance <= 0:
        return None
    stop = reference - distance if side is PositionSide.LONG else reference + distance
    return stop if stop > 0 else None


def _target_price(
    spec: StrategySpec,
    side: PositionSide,
    reference: Decimal,
    stop: Decimal,
    index: int,
    computed: dict[str, list[Decimal | None]],
) -> Decimal | None:
    rule = spec.target
    if rule.kind is TargetKind.RISK_MULTIPLE:
        distance = abs(reference - stop) * rule.value
    elif rule.kind is TargetKind.PERCENT:
        distance = reference * rule.value
    else:
        series = computed[str(rule.indicator_ref)]
        atr_value = series[index] if index < len(series) else None
        if atr_value is None or atr_value <= 0:
            return None
        distance = atr_value * rule.value

    if distance <= 0:
        return None
    target = reference + distance if side is PositionSide.LONG else reference - distance
    return target if target > 0 else None


def _render_operand(operand: Operand) -> str:
    """Name an operand the way the spec author wrote it."""
    if operand.kind is OperandKind.CONSTANT:
        return str(operand.value)
    return str(operand.ref)


@dataclass(frozen=True)
class ConditionResult:
    """One condition on one bar, with the numbers it was actually decided on."""

    left: str
    op: str
    right: str
    left_value: Decimal | None
    right_value: Decimal | None
    passed: bool

    def render(self) -> str:
        left = "n/a" if self.left_value is None else str(self.left_value)
        right = "n/a" if self.right_value is None else str(self.right_value)
        mark = "PASS" if self.passed else "FAIL"
        return f"{mark} {self.left}({left}) {self.op} {self.right}({right})"

    def as_dict(self) -> dict[str, Any]:
        return {
            "left": self.left,
            "op": self.op,
            "right": self.right,
            "left_value": None if self.left_value is None else str(self.left_value),
            "right_value": None if self.right_value is None else str(self.right_value),
            "passed": self.passed,
        }


@dataclass(frozen=True)
class RuleResult:
    """One entry rule on one bar. A rule fires only when every condition holds."""

    side: PositionSide
    conditions: tuple[ConditionResult, ...]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.conditions)

    @property
    def failed_conditions(self) -> tuple[ConditionResult, ...]:
        return tuple(c for c in self.conditions if not c.passed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "side": str(self.side),
            "passed": self.passed,
            "conditions": [c.as_dict() for c in self.conditions],
        }


def explain(
    spec: StrategySpec, bars: Sequence[Kline], index: int | None = None
) -> list[RuleResult]:
    """Evaluate the entry rules on one bar and report *why* each did or did not fire.

    ATLAS strategies are rule-based, not scored: a spec is a set of boolean conditions
    (STRAT-02), so there is no single number to report as a score or a threshold. The
    honest equivalent - and the more useful one for diagnosing a quiet tick - is the
    condition-by-condition truth table with the operand values it was decided on.

    This is a read-only view of the same operands and comparators `evaluate` uses. It
    decides nothing and changes nothing.
    """
    if not bars:
        return []

    at = len(bars) - 1 if index is None else index
    computed = _compute_indicators(spec, bars)

    results: list[RuleResult] = []
    for rule in spec.entries:
        conditions = tuple(
            ConditionResult(
                left=_render_operand(condition.left),
                op=str(condition.op),
                right=_render_operand(condition.right),
                left_value=_operand_value(condition.left, at, bars, computed),
                right_value=_operand_value(condition.right, at, bars, computed),
                passed=_evaluate_condition(condition, at, bars, computed),
            )
            for condition in rule.conditions
        )
        results.append(RuleResult(side=rule.side, conditions=conditions))
    return results


def evaluate(spec: StrategySpec, bars: Sequence[Kline]) -> list[Signal]:
    """Return every entry signal the spec produces over `bars`.

    Signals are emitted per bar without position state; the backtester decides which can
    actually be taken given an open position and the risk engine. Separating them keeps
    this function pure and independently testable.
    """
    if not bars:
        return []

    computed = _compute_indicators(spec, bars)
    signals: list[Signal] = []

    for index in range(len(bars)):
        reference = bars[index].close
        for rule in spec.entries:
            if not all(
                _evaluate_condition(condition, index, bars, computed)
                for condition in rule.conditions
            ):
                continue

            stop = _stop_price(spec, rule.side, reference, index, computed)
            if stop is None:
                continue
            target = _target_price(spec, rule.side, reference, stop, index, computed)
            if target is None:
                continue

            # A stop on the wrong side of entry is not a stop.
            if rule.side is PositionSide.LONG and not (stop < reference < target):
                continue
            if rule.side is PositionSide.SHORT and not (target < reference < stop):
                continue

            signals.append(
                Signal(
                    bar_index=index,
                    side=rule.side,
                    reference_price=reference,
                    stop_price=stop,
                    target_price=target,
                )
            )
    return signals


def require_evaluable(spec: StrategySpec, bars: Sequence[Kline]) -> None:
    """Raise if the spec cannot be evaluated over this data at all."""
    longest = max((item.period for item in spec.indicators), default=1)
    if len(bars) <= longest:
        raise ValidationError(
            f"insufficient history: {len(bars)} bars for an indicator of period {longest}"
        )
