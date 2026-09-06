"""Declarative strategy specification (STRAT-01..07).

A strategy is data, not code. The LLM emits one of these and a fixed engine interprets
it; ATLAS never executes model-generated code. This is the single largest deviation from
the source video, which emits Pine Script, and it exists because executing generated code
would place arbitrary logic inside the control plane (specification section 2).

There is deliberately no trailing-stop field (STRAT-04). David enforces that rule by
prompt instruction; ATLAS enforces it by making the concept unrepresentable.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from atlas.data.models import Timeframe
from atlas.models import Money, PositionSide, StrictModel
from atlas.strategy.indicators import INDICATOR_NAMES

MAX_INDICATORS = 6
MAX_CONDITIONS = 6


class OperandKind(StrEnum):
    INDICATOR = "indicator"
    PRICE = "price"
    CONSTANT = "constant"


class PriceField(StrEnum):
    OPEN = "open"
    HIGH = "high"
    LOW = "low"
    CLOSE = "close"
    VOLUME = "volume"


class Comparator(StrEnum):
    GT = "gt"
    LT = "lt"
    GTE = "gte"
    LTE = "lte"
    CROSS_ABOVE = "cross_above"
    CROSS_BELOW = "cross_below"


class StopKind(StrEnum):
    """How the protective stop is derived at signal time (STRAT-03)."""

    ATR_MULTIPLE = "atr_multiple"
    PERCENT = "percent"


class TargetKind(StrEnum):
    RISK_MULTIPLE = "risk_multiple"
    ATR_MULTIPLE = "atr_multiple"
    PERCENT = "percent"


class IndicatorSpec(StrictModel):
    id: str = Field(min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    period: int = Field(ge=1, le=500)

    @model_validator(mode="after")
    def _known_indicator(self) -> Self:
        if self.name not in INDICATOR_NAMES:
            raise ValueError(
                f"unknown indicator {self.name!r}; permitted: {sorted(INDICATOR_NAMES)}"
            )
        return self


class Operand(StrictModel):
    kind: OperandKind
    ref: str | None = None
    value: Money | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.kind is OperandKind.CONSTANT:
            if self.value is None:
                raise ValueError("constant operand requires a value")
            if self.ref is not None:
                raise ValueError("constant operand must not carry a ref")
        else:
            if self.ref is None:
                raise ValueError(f"{self.kind} operand requires a ref")
            if self.value is not None:
                raise ValueError(f"{self.kind} operand must not carry a value")
            if self.kind is OperandKind.PRICE and self.ref not in set(PriceField):
                raise ValueError(
                    f"unknown price field {self.ref!r}; permitted: {sorted(PriceField)}"
                )
        return self


class Condition(StrictModel):
    left: Operand
    op: Comparator
    right: Operand


class EntryRule(StrictModel):
    """Conditions are ANDed. All must hold on the same closed bar."""

    side: PositionSide
    conditions: tuple[Condition, ...] = Field(min_length=1, max_length=MAX_CONDITIONS)


class StopRule(StrictModel):
    kind: StopKind
    value: Money = Field(gt=0)
    indicator_ref: str | None = None

    @model_validator(mode="after")
    def _atr_needs_ref(self) -> Self:
        if self.kind is StopKind.ATR_MULTIPLE and not self.indicator_ref:
            raise ValueError("atr_multiple stop requires indicator_ref naming an ATR indicator")
        if self.kind is StopKind.PERCENT and not (Decimal(0) < self.value < Decimal(1)):
            raise ValueError("percent stop value must be a fraction in (0, 1)")
        return self


class TargetRule(StrictModel):
    kind: TargetKind
    value: Money = Field(gt=0)
    indicator_ref: str | None = None

    @model_validator(mode="after")
    def _atr_needs_ref(self) -> Self:
        if self.kind is TargetKind.ATR_MULTIPLE and not self.indicator_ref:
            raise ValueError("atr_multiple target requires indicator_ref")
        if self.kind is TargetKind.PERCENT and not (Decimal(0) < self.value < Decimal(1)):
            raise ValueError("percent target value must be a fraction in (0, 1)")
        return self


class StrategySpec(StrictModel):
    """A complete, executable strategy definition.

    Immutable and content-hash versioned (STRAT-06): editing a spec produces a new
    strategy with its own backtest and incubation history, never a mutation of an
    existing one whose evidence was gathered under different rules.
    """

    name: str = Field(min_length=1, max_length=120)
    symbol: str = Field(min_length=1, max_length=20)
    timeframe: Timeframe
    indicators: tuple[IndicatorSpec, ...] = Field(max_length=MAX_INDICATORS)
    entries: tuple[EntryRule, ...] = Field(min_length=1, max_length=2)
    stop: StopRule
    target: TargetRule
    max_bars_in_trade: int = Field(default=200, ge=1, le=5000)
    rationale: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _validate_references(self) -> Self:
        ids = [ind.id for ind in self.indicators]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate indicator id")
        known = set(ids)

        for rule in self.entries:
            for condition in rule.conditions:
                for operand in (condition.left, condition.right):
                    if operand.kind is OperandKind.INDICATOR and operand.ref not in known:
                        raise ValueError(f"condition references unknown indicator {operand.ref!r}")

        sides = [rule.side for rule in self.entries]
        if len(sides) != len(set(sides)):
            raise ValueError("at most one entry rule per side")

        for rule_ref, label in (
            (self.stop.indicator_ref, "stop"),
            (self.target.indicator_ref, "target"),
        ):
            if rule_ref is not None and rule_ref not in known:
                raise ValueError(f"{label} references unknown indicator {rule_ref!r}")
        return self

    @property
    def sides(self) -> tuple[PositionSide, ...]:
        return tuple(rule.side for rule in self.entries)

    def content_hash(self) -> str:
        """Stable identity (STRAT-06). Field order and formatting cannot change it."""
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, payload: str) -> StrategySpec:
        return cls.model_validate_json(payload)
