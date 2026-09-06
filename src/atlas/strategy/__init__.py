"""Strategy representation (specification section 4, STRAT-01..07)."""

from atlas.strategy.evaluator import Signal, evaluate, require_evaluable
from atlas.strategy.registry import StrategyRegistry
from atlas.strategy.spec import (
    Comparator,
    Condition,
    EntryRule,
    IndicatorSpec,
    Operand,
    OperandKind,
    PriceField,
    StopKind,
    StopRule,
    StrategySpec,
    TargetKind,
    TargetRule,
)

__all__ = [
    "Comparator",
    "Condition",
    "EntryRule",
    "IndicatorSpec",
    "Operand",
    "OperandKind",
    "PriceField",
    "Signal",
    "StopKind",
    "StopRule",
    "StrategyRegistry",
    "StrategySpec",
    "TargetKind",
    "TargetRule",
    "evaluate",
    "require_evaluable",
]
