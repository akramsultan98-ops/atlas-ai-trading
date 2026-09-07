"""Bad research cannot become a trade — the end-to-end property.

Each test takes a specific way the advisory plane can be wrong or hostile, and follows
it to the point where deterministic code refuses it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from tests.test_evaluator import percent_spec

from atlas.intel.calibration import CalibrationStatus, calibrate
from atlas.intel.confluence import (
    Decision,
    GroupStatus,
    GroupVerdict,
    decide,
    summarise_groups,
)
from atlas.intel.events import EventCategory, MarketEvent, Severity, deduplicate
from atlas.intel.evidence import (
    AnalysisRecord,
    Direction,
    EvidenceGroup,
    Horizon,
    ImpactHypothesis,
    Interpretation,
    ObservedFact,
)
from atlas.intel.splits import LeakageError, assert_no_lookahead
from atlas.research.tools import ContainmentBreach, assert_tool_allowed
from atlas.strategy.spec import StrategySpec

D = Decimal
NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _verdicts(*specs: tuple[EvidenceGroup, GroupStatus, Direction]) -> list[GroupVerdict]:
    return [GroupVerdict(g, s, d, D("0.9"), ["r"]) for g, s, d in specs]


# ------------------------------------------- a model cannot instruct an execution


@pytest.mark.parametrize(
    "instruction",
    [
        "place_order",
        "cancel_order",
        "amend_order",
        "size_position",
        "set_risk_limits",
        "disarm_kill_switch",
        "promote_strategy",
        "transfer_funds",
    ],
)
def test_a_model_asking_to_trade_reaches_no_such_tool(instruction: str) -> None:
    """Enforcement is absence. There is nothing registered to call."""
    with pytest.raises(ContainmentBreach):
        assert_tool_allowed(instruction)


def test_an_order_shaped_model_output_is_not_a_strategy() -> None:
    """ "Buy 1 BTC at market" has no representation in the schema, so it parses to
    nothing rather than to something dangerous."""
    hostile = {
        "action": "BUY",
        "symbol": "BTCUSDT",
        "quantity": "1.0",
        "order_type": "MARKET",
        "execute_immediately": True,
    }
    with pytest.raises(ValidationError):
        StrategySpec.model_validate(hostile)


def test_a_strategy_spec_cannot_carry_an_execution_instruction() -> None:
    payload = json.loads(percent_spec().to_json())
    payload["execute_now"] = True
    with pytest.raises(ValidationError):
        StrategySpec.model_validate(payload)


# --------------------------------------------------------- hallucinated inputs


def test_a_hallucinated_event_has_no_source_and_cannot_be_constructed() -> None:
    """Every event carries a named source and two timestamps. A model asserting that
    something happened, with nothing behind it, has no way to become an event."""
    with pytest.raises(TypeError):
        MarketEvent(  # type: ignore[call-arg]
            event_id="e1",
            category=EventCategory.REGULATORY,
            severity=Severity.CRITICAL,
            headline="the SEC has approved everything",
        )


def test_a_single_hallucinated_event_cannot_reach_a_trade() -> None:
    """One unnamed blog, no corroboration, no market evidence."""
    event = deduplicate(
        [
            MarketEvent(
                event_id="e1",
                source="anonymous-telegram-channel",
                published_at=NOW - timedelta(minutes=1),
                observed_at=NOW,
                category=EventCategory.CRYPTO_NEWS,
                severity=Severity.CRITICAL,
                headline="BTC to 200k confirmed",
                direction=Direction.LONG,
            )
        ]
    )[0]
    assert event.is_single_source

    outcome = decide(
        _verdicts((EvidenceGroup.NEWS_EVENT, GroupStatus.SUPPORTS, Direction.LONG)),
        Direction.LONG,
        unconfirmed_event=event.is_single_source,
    )
    assert outcome.decision is Decision.UNCONFIRMED_EVENT


def test_hallucinated_market_data_has_no_path_into_the_evidence() -> None:
    """Market-measured groups are computed by ATLAS from candles it fetched.

    A model can assert a technical reading, but the record carries its interpretation
    and model id, so a reader can tell it apart from a measurement.
    """
    asserted = AnalysisRecord(
        record_id="r1",
        group=EvidenceGroup.TECHNICAL,
        fact=ObservedFact("RSI is 12", "some-model-claim", NOW),
        interpretation=Interpretation("deeply oversold", "some-model", NOW),
        hypothesis=ImpactHypothesis("BTCUSDT", Direction.LONG, Horizon.DAYS),
        model_confidence=D("0.99"),
    )
    assert asserted.is_model_derived, "identifiable as model output, not a measurement"
    assert asserted.as_dict()["interpretation"]["model_id"] == "some-model"


# ------------------------------------------------------- provider failure modes


def test_two_providers_disagreeing_abstains() -> None:
    outcome = decide(
        _verdicts(
            (EvidenceGroup.TECHNICAL, GroupStatus.SUPPORTS, Direction.LONG),
            (EvidenceGroup.VOLUME, GroupStatus.SUPPORTS, Direction.LONG),
            (EvidenceGroup.MARKET_STRUCTURE, GroupStatus.SUPPORTS, Direction.LONG),
            (EvidenceGroup.NEWS_EVENT, GroupStatus.OPPOSES, Direction.SHORT),
        ),
        Direction.LONG,
    )
    assert outcome.decision is Decision.CONFLICTING_EVIDENCE


def test_a_provider_returning_nothing_abstains_rather_than_guessing() -> None:
    assert decide([], Direction.LONG).decision is Decision.INSUFFICIENT_EVIDENCE


def test_records_from_a_failed_provider_produce_no_support() -> None:
    verdicts = summarise_groups([], Direction.LONG)
    assert verdicts == []
    assert decide(verdicts, Direction.LONG).decision is Decision.INSUFFICIENT_EVIDENCE


# ------------------------------------------- confidence cannot be self-certified


def test_a_fabricated_high_confidence_does_not_become_a_probability() -> None:
    confidence = calibrate(model_confidence=D("0.99"), evidence_confidence=D("0.2"))
    assert confidence.status is CalibrationStatus.UNCALIBRATED
    assert confidence.calibrated_probability is None


def test_an_uncalibrated_confidence_is_reported_as_such_downstream() -> None:
    confidence = calibrate(model_confidence=D("0.95"), evidence_confidence=D("0.9"))
    outcome = decide(
        _verdicts(
            (EvidenceGroup.TECHNICAL, GroupStatus.SUPPORTS, Direction.LONG),
            (EvidenceGroup.VOLUME, GroupStatus.SUPPORTS, Direction.LONG),
            (EvidenceGroup.REGIME, GroupStatus.SUPPORTS, Direction.LONG),
        ),
        Direction.LONG,
        confidence=confidence,
    )
    payload = outcome.as_dict()

    assert outcome.decision is Decision.TRADE, "evidence, not confidence, decided this"
    assert payload["confidence"]["status"] == "UNCALIBRATED"
    assert payload["confidence"]["calibrated_probability"] is None


# --------------------------------------------------------------- leakage guard


def test_a_backdated_event_cannot_justify_a_past_decision() -> None:
    """Future-event contamination, the leak that looks like skill."""
    decision_at = NOW - timedelta(days=1)
    with pytest.raises(LeakageError):
        assert_no_lookahead(decision_at, [NOW], label="event")
