"""Selection gates (SEL-01..07). Criteria from the source; thresholds are ATLAS."""

from atlas.selection.gates import (
    GateResult,
    GateVerdict,
    SelectionOutcome,
    SelectionThresholds,
    evaluate_gates,
)

__all__ = ["GateResult", "GateVerdict", "SelectionOutcome", "SelectionThresholds", "evaluate_gates"]
