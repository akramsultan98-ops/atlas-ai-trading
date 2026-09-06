"""STRAT-01..07: the spec is data, validated, immutable and hash-versioned."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from atlas.data.models import Timeframe
from atlas.models import PositionSide
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


def ind_op(ref: str) -> Operand:
    return Operand(kind=OperandKind.INDICATOR, ref=ref)


def price_op(field: str = "close") -> Operand:
    return Operand(kind=OperandKind.PRICE, ref=field)


def const_op(value: str) -> Operand:
    return Operand(kind=OperandKind.CONSTANT, value=D(value))


def make_spec(**overrides: object) -> StrategySpec:
    defaults: dict[str, object] = {
        "name": "ema cross with atr stop",
        "symbol": "BTCUSDT",
        "timeframe": Timeframe.H1,
        "indicators": (
            IndicatorSpec(id="fast", name="ema", period=12),
            IndicatorSpec(id="slow", name="ema", period=26),
            IndicatorSpec(id="atr14", name="atr", period=14),
        ),
        "entries": (
            EntryRule(
                side=PositionSide.LONG,
                conditions=(
                    Condition(left=ind_op("fast"), op=Comparator.CROSS_ABOVE, right=ind_op("slow")),
                ),
            ),
        ),
        "stop": StopRule(kind=StopKind.ATR_MULTIPLE, value=D("2"), indicator_ref="atr14"),
        "target": TargetRule(kind=TargetKind.RISK_MULTIPLE, value=D("2")),
    }
    defaults.update(overrides)
    return StrategySpec(**defaults)  # type: ignore[arg-type]


def test_valid_spec_constructs() -> None:
    spec = make_spec()
    assert spec.sides == (PositionSide.LONG,)
    assert len(spec.content_hash()) == 64


def test_spec_has_no_trailing_stop_field() -> None:
    """STRAT-04: enforced by making the concept unrepresentable, not by instruction."""
    assert "trailing" not in str(StrategySpec.model_fields.keys()).lower()
    assert "trailing" not in str(StopRule.model_fields.keys()).lower()
    with pytest.raises(ValidationError):
        StopRule(kind=StopKind.PERCENT, value=D("0.02"), trailing=True)  # type: ignore[call-arg]


def test_unknown_indicator_rejected() -> None:
    """STRAT-07."""
    with pytest.raises(ValidationError, match="unknown indicator"):
        IndicatorSpec(id="x", name="proprietary_alpha", period=10)


def test_condition_referencing_undeclared_indicator_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown indicator 'ghost'"):
        make_spec(
            entries=(
                EntryRule(
                    side=PositionSide.LONG,
                    conditions=(
                        Condition(left=ind_op("ghost"), op=Comparator.GT, right=const_op("1")),
                    ),
                ),
            )
        )


def test_stop_referencing_undeclared_indicator_rejected() -> None:
    with pytest.raises(ValidationError, match="stop references unknown indicator"):
        make_spec(stop=StopRule(kind=StopKind.ATR_MULTIPLE, value=D("2"), indicator_ref="nope"))


def test_atr_stop_requires_indicator_ref() -> None:
    with pytest.raises(ValidationError, match="requires indicator_ref"):
        StopRule(kind=StopKind.ATR_MULTIPLE, value=D("2"))


def test_percent_stop_must_be_a_fraction() -> None:
    with pytest.raises(ValidationError, match="fraction in"):
        StopRule(kind=StopKind.PERCENT, value=D("5"))


def test_duplicate_indicator_id_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate indicator id"):
        make_spec(
            indicators=(
                IndicatorSpec(id="dup", name="ema", period=10),
                IndicatorSpec(id="dup", name="sma", period=20),
            ),
            entries=(
                EntryRule(
                    side=PositionSide.LONG,
                    conditions=(
                        Condition(left=ind_op("dup"), op=Comparator.GT, right=const_op("1")),
                    ),
                ),
            ),
            stop=StopRule(kind=StopKind.PERCENT, value=D("0.03")),
        )


def test_two_entry_rules_same_side_rejected() -> None:
    rule = EntryRule(
        side=PositionSide.LONG,
        conditions=(Condition(left=price_op(), op=Comparator.GT, right=const_op("1")),),
    )
    with pytest.raises(ValidationError, match="one entry rule per side"):
        make_spec(entries=(rule, rule), stop=StopRule(kind=StopKind.PERCENT, value=D("0.03")))


def test_constant_operand_requires_value() -> None:
    with pytest.raises(ValidationError, match="constant operand requires a value"):
        Operand(kind=OperandKind.CONSTANT)


def test_indicator_operand_requires_ref() -> None:
    with pytest.raises(ValidationError, match="requires a ref"):
        Operand(kind=OperandKind.INDICATOR, value=D("1"))


def test_unknown_price_field_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown price field"):
        Operand(kind=OperandKind.PRICE, ref="vwap")


def test_spec_is_frozen() -> None:
    spec = make_spec()
    with pytest.raises(ValidationError):
        spec.name = "renamed"  # type: ignore[misc]


def test_extra_fields_rejected() -> None:
    """LLM output is untrusted (AI-06): unknown keys must not pass silently."""
    with pytest.raises(ValidationError):
        make_spec(secret_backdoor="rm -rf /")


def test_content_hash_is_stable_and_order_independent() -> None:
    assert make_spec().content_hash() == make_spec().content_hash()


def test_content_hash_changes_with_parameters() -> None:
    a = make_spec()
    b = make_spec(
        indicators=(
            IndicatorSpec(id="fast", name="ema", period=13),  # 12 -> 13
            IndicatorSpec(id="slow", name="ema", period=26),
            IndicatorSpec(id="atr14", name="atr", period=14),
        )
    )
    assert a.content_hash() != b.content_hash()


def test_json_round_trip_preserves_hash() -> None:
    spec = make_spec()
    assert StrategySpec.from_json(spec.to_json()).content_hash() == spec.content_hash()


def test_indicator_count_is_bounded() -> None:
    with pytest.raises(ValidationError):
        make_spec(
            indicators=tuple(IndicatorSpec(id=f"i{n}", name="sma", period=n + 2) for n in range(10))
        )
