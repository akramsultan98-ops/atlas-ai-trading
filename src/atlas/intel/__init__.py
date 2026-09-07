"""Evidence-driven analysis: events, regime, confluence, calibration, accuracy.

The LLM is an analyst here, never the decision maker. Every path from a model's output
to an order passes through deterministic code that can refuse it.
"""

from atlas.intel.calibration import BucketOutcome, CalibrationStatus, Confidence, calibrate
from atlas.intel.confluence import (
    ConfluenceOutcome,
    ConfluenceThresholds,
    Decision,
    GroupStatus,
    GroupVerdict,
    decide,
    summarise_groups,
)
from atlas.intel.events import (
    CorroboratedEvent,
    EventCategory,
    MarketEvent,
    Severity,
    deduplicate,
    visible_at,
)
from atlas.intel.evidence import (
    AnalysisRecord,
    Direction,
    EvidenceGroup,
    Horizon,
    ImpactHypothesis,
    Interpretation,
    ObservedFact,
)
from atlas.intel.metrics import AccuracyReport, DecisionOutcome, calibration_report, evaluate
from atlas.intel.regime import MarketRegime, RegimeThresholds, classify_regime
from atlas.intel.splits import EvaluationPlan, LeakageError, Phase, PhaseWindow, assert_no_lookahead
from atlas.intel.triage import ResearchBudget, ResearchTier, TriagePolicy, triage

__all__ = [
    "AccuracyReport",
    "AnalysisRecord",
    "BucketOutcome",
    "CalibrationStatus",
    "Confidence",
    "ConfluenceOutcome",
    "ConfluenceThresholds",
    "CorroboratedEvent",
    "Decision",
    "DecisionOutcome",
    "Direction",
    "EvaluationPlan",
    "EventCategory",
    "EvidenceGroup",
    "GroupStatus",
    "GroupVerdict",
    "Horizon",
    "ImpactHypothesis",
    "Interpretation",
    "LeakageError",
    "MarketEvent",
    "MarketRegime",
    "ObservedFact",
    "Phase",
    "PhaseWindow",
    "RegimeThresholds",
    "ResearchBudget",
    "ResearchTier",
    "Severity",
    "TriagePolicy",
    "assert_no_lookahead",
    "calibrate",
    "calibration_report",
    "classify_regime",
    "decide",
    "deduplicate",
    "evaluate",
    "summarise_groups",
    "triage",
    "visible_at",
]
