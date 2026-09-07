"""Adversarial tests for the analysis layer.

The property under test throughout: **bad research cannot become a trade.** Every path
from a model's output to an order runs through deterministic code that can refuse it,
and these tests are the refusals.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tests.test_backtest import series_from

from atlas.intel.calibration import (
    MIN_SAMPLES_FOR_CALIBRATION,
    BucketOutcome,
    CalibrationStatus,
    bucket_of,
    calibrate,
)
from atlas.intel.confluence import (
    ConfluenceThresholds,
    Decision,
    GroupStatus,
    GroupVerdict,
    decide,
    summarise_groups,
)
from atlas.intel.events import (
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
from atlas.intel.metrics import DecisionOutcome, calibration_report, evaluate
from atlas.intel.regime import RiskState, TrendState, VolatilityState, classify_regime
from atlas.intel.splits import (
    EvaluationPlan,
    LeakageError,
    Phase,
    PhaseWindow,
    assert_no_lookahead,
)
from atlas.intel.triage import ResearchBudget, ResearchTier, TriagePolicy, triage

D = Decimal
NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _event(
    event_id: str,
    source: str,
    headline: str = "SEC approves spot ETF",
    *,
    published: datetime | None = None,
    severity: Severity = Severity.HIGH,
    direction: Direction = Direction.LONG,
    assets: tuple[str, ...] = ("BTCUSDT",),
) -> MarketEvent:
    at = published or NOW - timedelta(minutes=5)
    return MarketEvent(
        event_id=event_id,
        source=source,
        published_at=at,
        observed_at=at + timedelta(seconds=30),
        category=EventCategory.REGULATORY,
        severity=severity,
        headline=headline,
        entities=("SEC",),
        affected_assets=assets,
        direction=direction,
    )


def _record(
    group: EvidenceGroup,
    direction: Direction,
    *,
    record_id: str = "r",
    confidence: Decimal | None = None,
    with_model: bool = False,
) -> AnalysisRecord:
    interpretation = None
    fact = None
    if with_model:
        fact = ObservedFact("ETF approved", "reuters", NOW)
        interpretation = Interpretation("bullish for BTC", "some-model", NOW)
    return AnalysisRecord(
        record_id=record_id,
        group=group,
        fact=fact,
        interpretation=interpretation,
        hypothesis=ImpactHypothesis("BTCUSDT", direction, Horizon.DAYS),
        model_confidence=confidence,
    )


# --------------------------------------------------- the seven parts stay separate


def test_an_interpretation_without_a_fact_is_refused() -> None:
    """A model talking about nothing is not analysis."""
    with pytest.raises(ValueError, match="no observed fact"):
        AnalysisRecord(
            record_id="r1",
            group=EvidenceGroup.NEWS_EVENT,
            fact=None,
            interpretation=Interpretation("very bullish", "some-model", NOW),
            hypothesis=ImpactHypothesis("BTCUSDT", Direction.LONG, Horizon.DAYS),
        )


def test_a_record_carries_no_decision() -> None:
    """The decision is made downstream by deterministic code, from many records."""
    record = _record(EvidenceGroup.NEWS_EVENT, Direction.LONG, with_model=True)
    assert not hasattr(record, "decision")
    assert record.is_model_derived
    assert record.as_dict()["interpretation"]["model_id"] == "some-model"


def test_confidence_outside_zero_to_one_is_refused() -> None:
    with pytest.raises(ValueError, match="fraction"):
        _record(EvidenceGroup.TECHNICAL, Direction.LONG, confidence=D("1.5"))


# ------------------------------------------------------------ event engine


def test_the_same_story_from_five_aggregators_is_one_observation() -> None:
    """Repetition is not corroboration."""
    events = [_event(f"e{i}", f"aggregator-{i}") for i in range(5)]
    # The same wire story republished by an aggregator that already carried it.
    events.append(_event("e9", "aggregator-1"))

    grouped = deduplicate(events)
    assert len(grouped) == 1
    assert len(grouped[0].reports) == 6
    assert grouped[0].source_count == 5, "distinct sources, not article count"


def test_a_single_source_claim_is_marked_as_such() -> None:
    grouped = deduplicate([_event("e1", "one-blog")])
    assert grouped[0].is_single_source


def test_sources_disagreeing_is_recorded_not_resolved() -> None:
    """Picking the newest or loudest is how a system becomes confidently wrong."""
    grouped = deduplicate(
        [
            _event("e1", "reuters", direction=Direction.LONG),
            _event("e2", "bloomberg", direction=Direction.SHORT),
        ]
    )
    assert len(grouped) == 1
    assert grouped[0].conflicting_directions


def test_an_event_observed_before_publication_is_refused() -> None:
    """One of the two clocks is wrong, and a leakage guard trusting either lets the
    future in."""
    with pytest.raises(ValueError, match="before it was published"):
        MarketEvent(
            event_id="e1",
            source="s",
            published_at=NOW,
            observed_at=NOW - timedelta(hours=1),
            category=EventCategory.MACRO,
            severity=Severity.LOW,
            headline="x",
        )


def test_stale_intelligence_is_detectable() -> None:
    old = deduplicate([_event("e1", "reuters", published=NOW - timedelta(days=3))])[0]
    assert old.is_stale(NOW, timedelta(hours=6))
    assert not old.is_stale(NOW, timedelta(days=7))


def test_future_events_are_invisible_to_a_past_decision() -> None:
    """The most dangerous leak in event-driven research."""
    past = _event("e1", "reuters", published=NOW - timedelta(hours=2))
    future = _event("e2", "reuters", published=NOW + timedelta(hours=2))

    visible = visible_at([past, future], NOW)
    assert [e.event_id for e in visible] == ["e1"]


# ------------------------------------------------------------------ regime


def _trending_bars(count: int = 60):
    rows = [(str(100 + i), str(101 + i), str(99 + i), str(100 + i + 1)) for i in range(count)]
    return series_from(rows).bars


def _ranging_bars(count: int = 60):
    rows = []
    for i in range(count):
        base = 100 + (i % 2) * 2
        rows.append((str(base), str(base + 1), str(base - 1), str(base)))
    return series_from(rows).bars


def test_a_trending_market_is_classified_as_trending() -> None:
    regime = classify_regime(_trending_bars())
    assert regime.trend is TrendState.TRENDING_UP
    assert regime.is_measurable


def test_a_round_trip_is_ranging_not_trending() -> None:
    """Directional efficiency, not the sign of a return: a market that ends where it
    started after a large journey is ranging."""
    assert classify_regime(_ranging_bars()).trend is TrendState.RANGING


def test_too_little_data_is_unclear_not_calm() -> None:
    regime = classify_regime(_trending_bars(5))
    assert regime.trend is TrendState.UNCLEAR
    assert regime.volatility is VolatilityState.UNCLEAR
    assert not regime.is_measurable


def test_risk_on_off_is_unavailable_from_one_symbol() -> None:
    """It is a claim about capital rotating between asset classes. One crypto pair
    cannot observe that, so it must not label it."""
    assert classify_regime(_trending_bars()).risk is RiskState.UNAVAILABLE


def test_regime_is_deterministic() -> None:
    bars = _trending_bars()
    assert classify_regime(bars).as_dict() == classify_regime(bars).as_dict()


# ------------------------------------------------------- confluence and abstention


def _market_support(n: int) -> list[GroupVerdict]:
    groups = [
        EvidenceGroup.TECHNICAL,
        EvidenceGroup.VOLUME,
        EvidenceGroup.MARKET_STRUCTURE,
        EvidenceGroup.VOLATILITY,
    ][:n]
    return [
        GroupVerdict(g, GroupStatus.SUPPORTS, Direction.LONG, D("0.6"), [f"r-{g}"]) for g in groups
    ]


def test_agreement_across_independent_groups_can_trade() -> None:
    outcome = decide(_market_support(3), Direction.LONG)
    assert outcome.decision is Decision.TRADE
    assert outcome.direction is Direction.LONG


def test_a_confident_model_alone_cannot_trade() -> None:
    """The central rule: conviction is not evidence.

    Three narrative groups all agreeing, each at 0.95 stated confidence, with nothing
    measured from the market behind them.
    """
    # Enough groups to clear the count, but only one of them measured from the market.
    verdicts = [
        GroupVerdict(g, GroupStatus.SUPPORTS, Direction.LONG, D("0.95"), ["r"])
        for g in (EvidenceGroup.NEWS_EVENT, EvidenceGroup.MACRO, EvidenceGroup.TECHNICAL)
    ]
    outcome = decide(verdicts, Direction.LONG)

    assert len(outcome.supporting) == 3, "the count threshold is satisfied"
    assert outcome.decision is Decision.INSUFFICIENT_EVIDENCE
    assert "not a substitute for observable evidence" in outcome.reason


def test_one_opposing_group_abstains() -> None:
    verdicts = [
        *_market_support(3),
        GroupVerdict(EvidenceGroup.MACRO, GroupStatus.OPPOSES, Direction.SHORT, D("0.4"), ["r"]),
    ]
    outcome = decide(verdicts, Direction.LONG)
    assert outcome.decision is Decision.CONFLICTING_EVIDENCE


def test_stale_intelligence_abstains() -> None:
    outcome = decide(_market_support(4), Direction.LONG, stale=True)
    assert outcome.decision is Decision.STALE_INTELLIGENCE


def test_an_uncorroborated_event_abstains() -> None:
    outcome = decide(_market_support(4), Direction.LONG, unconfirmed_event=True)
    assert outcome.decision is Decision.UNCONFIRMED_EVENT


def test_an_unmeasurable_regime_abstains() -> None:
    outcome = decide(_market_support(4), Direction.LONG, regime_measurable=False)
    assert outcome.decision is Decision.REGIME_UNCLEAR


def test_thin_evidence_abstains() -> None:
    outcome = decide(_market_support(1), Direction.LONG)
    assert outcome.decision is Decision.INSUFFICIENT_EVIDENCE


def test_a_group_disagreeing_with_itself_is_neutral_not_weak_support() -> None:
    records = [
        _record(EvidenceGroup.TECHNICAL, Direction.LONG, record_id="a"),
        _record(EvidenceGroup.TECHNICAL, Direction.SHORT, record_id="b"),
    ]
    verdict = summarise_groups(records, Direction.LONG)[0]
    assert verdict.status is GroupStatus.NEUTRAL


def test_every_abstention_reason_is_distinguishable() -> None:
    reasons = {d for d in Decision if d.is_abstention}
    assert reasons == {
        Decision.NO_TRADE,
        Decision.INSUFFICIENT_EVIDENCE,
        Decision.CONFLICTING_EVIDENCE,
        Decision.STALE_INTELLIGENCE,
        Decision.UNCONFIRMED_EVENT,
        Decision.REGIME_UNCLEAR,
    }


def test_thresholds_can_be_tightened_not_bypassed() -> None:
    strict = ConfluenceThresholds(min_supporting_groups=4, min_market_measured_groups=4)
    assert decide(_market_support(3), Direction.LONG, thresholds=strict).decision is (
        Decision.INSUFFICIENT_EVIDENCE
    )


# ------------------------------------------------------------------ calibration


def test_a_model_claiming_ninety_percent_is_uncalibrated_without_history() -> None:
    """The headline failure this prevents: a token sequence read as a probability."""
    confidence = calibrate(model_confidence=D("0.9"), evidence_confidence=D("0.5"))

    assert confidence.status is CalibrationStatus.UNCALIBRATED
    assert confidence.calibrated_probability is None
    assert confidence.model_confidence == D("0.9"), "the claim is kept, not laundered"
    assert str(MIN_SAMPLES_FOR_CALIBRATION) in confidence.notes[0]


def test_a_thin_sample_is_still_uncalibrated() -> None:
    history = {bucket_of(D("0.9")): BucketOutcome(bucket_of(D("0.9")), predictions=10, correct=10)}
    confidence = calibrate(model_confidence=D("0.9"), evidence_confidence=D("0.5"), history=history)
    assert confidence.status is CalibrationStatus.UNCALIBRATED
    assert confidence.calibrated_probability is None, "ten out of ten is not a probability"


def test_a_poorly_calibrated_bucket_says_so() -> None:
    """A 90% bucket winning 67% is badly calibrated, not 90% with noise."""
    name = bucket_of(D("0.9"))
    history = {name: BucketOutcome(name, predictions=300, correct=200)}
    confidence = calibrate(model_confidence=D("0.9"), evidence_confidence=D("0.7"), history=history)

    assert confidence.status is CalibrationStatus.CALIBRATED
    assert confidence.calibrated_probability == Decimal(200) / Decimal(300)
    assert any("poorly calibrated" in note for note in confidence.notes)


def test_the_four_quantities_stay_separate() -> None:
    name = bucket_of(D("0.8"))
    history = {name: BucketOutcome(name, predictions=100, correct=75)}
    payload = calibrate(
        model_confidence=D("0.8"), evidence_confidence=D("0.55"), history=history
    ).as_dict()

    assert payload["model_confidence"] == "0.8"
    assert payload["evidence_confidence"] == "0.55"
    assert payload["historical_accuracy"] == "0.75"
    assert payload["calibrated_probability"] == "0.75"


# ------------------------------------------------------------ leakage and phases


def test_information_after_the_decision_raises() -> None:
    with pytest.raises(LeakageError, match="postdate the decision"):
        assert_no_lookahead(NOW, [NOW - timedelta(hours=1), NOW + timedelta(minutes=1)])


def test_information_before_the_decision_passes() -> None:
    assert_no_lookahead(NOW, [NOW - timedelta(hours=1), NOW])


def test_overlapping_phases_are_refused() -> None:
    """An out-of-sample window that overlaps research was already seen."""
    with pytest.raises(ValueError, match="overlapping phases"):
        EvaluationPlan(
            (
                PhaseWindow(Phase.RESEARCH, NOW - timedelta(days=100), NOW - timedelta(days=10)),
                PhaseWindow(Phase.OUT_OF_SAMPLE, NOW - timedelta(days=20), NOW),
            )
        )


def test_only_held_out_phases_are_evidence_of_skill() -> None:
    assert not Phase.RESEARCH.is_evidence_of_skill
    assert not Phase.VALIDATION.is_evidence_of_skill
    assert Phase.OUT_OF_SAMPLE.is_evidence_of_skill
    assert Phase.FORWARD_INCUBATION.is_evidence_of_skill
    assert Phase.LIVE.is_evidence_of_skill


# --------------------------------------------------------------------- metrics


def _resolved(direction: Direction, correct: bool, ret: str, **kw: object) -> DecisionOutcome:
    realised = (
        direction
        if correct
        else (Direction.SHORT if direction is Direction.LONG else Direction.LONG)
    )
    return DecisionOutcome(
        decision=Decision.TRADE,
        direction=direction,
        realised_direction=realised,
        realised_return=D(ret),
        **kw,  # type: ignore[arg-type]
    )


def test_abstentions_are_never_counted_as_correct() -> None:
    """The easiest way to fake a headline accuracy number."""
    outcomes = [
        *[DecisionOutcome(Decision.INSUFFICIENT_EVIDENCE, Direction.NEUTRAL) for _ in range(90)],
        *[_resolved(Direction.LONG, True, "0.02") for _ in range(8)],
        *[_resolved(Direction.LONG, False, "-0.01") for _ in range(2)],
    ]
    report = evaluate(outcomes)

    assert report.total_decisions == 100
    assert report.abstentions == 90
    assert report.resolved == 10
    assert report.directional_accuracy == D("0.8"), "measured on decisions actually made"
    assert report.abstention_rate == D("0.9"), "reported beside accuracy, never folded in"


def test_accuracy_is_none_when_nothing_resolved() -> None:
    report = evaluate([DecisionOutcome(Decision.NO_TRADE, Direction.NEUTRAL)])
    assert report.directional_accuracy is None
    assert not report.is_reportable, "no accuracy claim may be made"


def test_accuracy_is_broken_out_by_regime() -> None:
    """An aggregate hides being excellent trending and a coin flip ranging."""
    outcomes = [
        *[_resolved(Direction.LONG, True, "0.02", regime="TRENDING_UP") for _ in range(9)],
        _resolved(Direction.LONG, False, "-0.01", regime="TRENDING_UP"),
        *[_resolved(Direction.LONG, True, "0.01", regime="RANGING") for _ in range(5)],
        *[_resolved(Direction.LONG, False, "-0.01", regime="RANGING") for _ in range(5)],
    ]
    report = evaluate(outcomes)

    assert report.by_regime["TRENDING_UP"] == D("0.9")
    assert report.by_regime["RANGING"] == D("0.5")


def test_profit_factor_and_expectancy_are_reported() -> None:
    report = evaluate(
        [
            *[_resolved(Direction.LONG, True, "0.02") for _ in range(6)],
            *[_resolved(Direction.LONG, False, "-0.01") for _ in range(4)],
        ]
    )
    assert report.profit_factor == D("0.12") / D("0.04")
    assert report.expectancy is not None and report.expectancy > 0
    assert report.max_drawdown >= 0


def test_the_calibration_report_shows_claimed_against_observed() -> None:
    outcomes = [
        *[_resolved(Direction.LONG, True, "0.01", stated_confidence=D("0.9")) for _ in range(67)],
        *[_resolved(Direction.LONG, False, "-0.01", stated_confidence=D("0.9")) for _ in range(33)],
    ]
    rows = calibration_report(evaluate(outcomes))
    row = next(r for r in rows if r["bucket"].startswith("0.9"))

    assert row["claimed_at_least"] == "0.9"
    assert row["observed_rate"] == "0.67"
    assert row["well_calibrated"] is False, "0.9 claimed, 0.67 observed"


# ----------------------------------------------------------------- token control


def test_an_ordinary_candle_does_not_spend_a_model_call() -> None:
    decision = triage(uncertainty=D("0.05"), event=None, seen_keys=set(), budget=ResearchBudget())
    assert decision.tier is ResearchTier.DETERMINISTIC


def test_a_novel_severe_event_justifies_a_deep_call() -> None:
    event = deduplicate([_event("e1", "reuters", severity=Severity.CRITICAL)])[0]
    decision = triage(uncertainty=D("0.8"), event=event, seen_keys=set(), budget=ResearchBudget())
    assert decision.tier is ResearchTier.DEEP


def test_an_already_seen_event_does_not_buy_a_second_opinion() -> None:
    event = deduplicate([_event("e1", "reuters", severity=Severity.CRITICAL)])[0]
    decision = triage(
        uncertainty=D("0.8"),
        event=event,
        seen_keys={event.content_key},
        budget=ResearchBudget(),
    )
    assert decision.tier is ResearchTier.SHALLOW


def test_budget_exhaustion_degrades_and_never_fails() -> None:
    event = deduplicate([_event("e1", "reuters", severity=Severity.CRITICAL)])[0]
    budget = ResearchBudget(TriagePolicy(deep_calls_per_day=0, shallow_calls_per_day=0))

    decision = triage(
        uncertainty=D("0.9"),
        event=event,
        seen_keys=set(),
        budget=budget,
        policy=TriagePolicy(deep_calls_per_day=0, shallow_calls_per_day=0),
    )
    assert decision.tier is ResearchTier.DETERMINISTIC
    assert "exhausted" in decision.reason


def test_a_research_outage_leaves_atlas_running() -> None:
    """AI-08: the control plane never depends on the advisory plane."""
    decision = triage(
        uncertainty=D("0.99"),
        event=None,
        seen_keys=set(),
        budget=ResearchBudget(),
        research_available=False,
    )
    assert decision.tier is ResearchTier.DETERMINISTIC
    assert "AI-08" in decision.reason
