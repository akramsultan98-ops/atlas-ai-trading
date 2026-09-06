"""INC-01..05 and PROM-01..04."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from tests.test_backtest import series_from
from tests.test_evaluator import percent_spec
from tests.test_selection import stats
from tests.test_strategy_spec import make_spec

from atlas.audit import AuditLog
from atlas.db.engine import Database
from atlas.incubation.divergence import (
    IncubationThresholds,
    check_divergence,
    compute_metrics,
)
from atlas.incubation.tracker import IncubationSignal, IncubationTracker
from atlas.models import PositionSide, StrategyStatus, utcnow
from atlas.promotion.gate import (
    PromotionGate,
    PromotionRefused,
    PromotionThresholds,
    evaluate_promotion,
    pearson_correlation,
)
from atlas.strategy.registry import StrategyRegistry

D = Decimal


def signal(ret: str | None, outcome: str = "WIN", days_ago: int = 30) -> IncubationSignal:
    return IncubationSignal(
        id="s",
        strategy_id="strat",
        signal_at=utcnow() - timedelta(days=days_ago),
        side=PositionSide.LONG,
        entry_price=D("100"),
        stop_price=D("95"),
        target_price=D("110"),
        exit_at=utcnow(),
        exit_price=D("105"),
        outcome=outcome,
        return_pct=D(ret) if ret is not None else None,
    )


def winners_and_losers(wins: int, losses: int) -> list[IncubationSignal]:
    """Interleave outcomes.

    Stacking every win before every loss produces a loss streak no real strategy
    generates, and the resulting drawdown would trip INC-05 on otherwise healthy
    fixtures. Round-robin keeps the equity path realistic.
    """
    out: list[IncubationSignal] = []
    remaining_wins, remaining_losses = wins, losses
    while remaining_wins or remaining_losses:
        if remaining_wins and (remaining_wins >= remaining_losses or not remaining_losses):
            out.append(signal("0.10", "WIN"))
            remaining_wins -= 1
        if remaining_losses:
            out.append(signal("-0.05", "LOSS"))
            remaining_losses -= 1
    return out


# ------------------------------------------------------------------------- tracker


def test_tracker_records_paper_signals(db: Database) -> None:
    """INC-03: capital during incubation is zero. Nothing here places an order."""
    rows = [
        ("95", "96", "94", "95"),
        ("95", "99", "94", "98"),
        ("98", "201", "97", "200"),
        ("200", "202", "199", "201"),
        ("201", "212", "200", "209"),
    ]
    tracker = IncubationTracker(db)
    strategy_id = StrategyRegistry(db).register(make_spec())
    recorded = tracker.record_signals(strategy_id, percent_spec(), series_from(rows))

    assert recorded == 1
    signals = tracker.signals_for(strategy_id)
    assert len(signals) == 1
    assert signals[0].entry_price == D("200")


def test_tracker_resolves_pessimistically(db: Database) -> None:
    """BT-06 applies here too: incubation must not flatter the backtest."""
    rows = [
        ("95", "96", "94", "95"),
        ("95", "99", "94", "98"),
        ("98", "201", "97", "200"),
        ("200", "202", "199", "201"),
        ("201", "215", "190", "200"),
    ]
    tracker = IncubationTracker(db)
    strategy_id = StrategyRegistry(db).register(make_spec())
    tracker.record_signals(strategy_id, percent_spec(), series_from(rows))
    assert tracker.signals_for(strategy_id)[0].outcome == "LOSS"


def test_tracker_marks_unresolved_signals_open(db: Database) -> None:
    rows = [
        ("95", "96", "94", "95"),
        ("95", "99", "94", "98"),
        ("98", "201", "97", "200"),
        ("200", "202", "199", "201"),
    ]
    tracker = IncubationTracker(db)
    strategy_id = StrategyRegistry(db).register(make_spec())
    tracker.record_signals(strategy_id, percent_spec(), series_from(rows))
    assert tracker.signals_for(strategy_id)[0].outcome == "OPEN"


# ------------------------------------------------------------------------- metrics


def test_metrics_over_closed_trades() -> None:
    metrics = compute_metrics(winners_and_losers(6, 4))
    assert metrics.trade_count == 10
    assert metrics.wins == 6
    assert metrics.win_rate == D("0.6")
    assert metrics.profit_factor == D("3")  # 0.60 gains / 0.20 losses


def test_open_signals_excluded_from_metrics() -> None:
    signals = [*winners_and_losers(3, 2), signal(None, "OPEN")]
    assert compute_metrics(signals).trade_count == 5


def test_metrics_of_empty_set_are_zero() -> None:
    metrics = compute_metrics([])
    assert metrics.trade_count == 0
    assert metrics.profit_factor == 0


# --------------------------------------------------------------------- divergence


def test_healthy_incubation_is_eligible() -> None:
    check = check_divergence(winners_and_losers(20, 15), stats(profit_factor=D("1.6")), 70)
    assert check.eligible, check.reasons


def test_too_few_days_blocks_promotion() -> None:
    """INC-01: 'a couple of months' [12:27]."""
    check = check_divergence(winners_and_losers(20, 15), stats(profit_factor=D("1.6")), 30)
    assert not check.eligible
    assert any("INC-01" in r for r in check.reasons)


def test_too_few_trades_blocks_promotion() -> None:
    check = check_divergence(winners_and_losers(5, 3), stats(profit_factor=D("1.2")), 90)
    assert any("INC-02" in r for r in check.reasons)


def test_profit_factor_collapse_blocks_promotion() -> None:
    """INC-04: live must retain 70% of the backtest's edge."""
    check = check_divergence(winners_and_losers(10, 25), stats(profit_factor=D("2.0")), 90)
    assert any("INC-04" in r for r in check.reasons)


def test_all_divergence_reasons_reported_together() -> None:
    check = check_divergence(winners_and_losers(2, 8), stats(profit_factor=D("3.0")), 5)
    assert len(check.reasons) >= 3


def test_thresholds_are_configurable() -> None:
    lenient = IncubationThresholds(min_days=1, min_trades=2, min_profit_factor_retention=D("0.1"))
    assert check_divergence(winners_and_losers(2, 1), stats(), 2, lenient).eligible


# -------------------------------------------------------------------- correlation


def test_identical_series_correlate_perfectly() -> None:
    values = [D("0.1"), D("-0.05"), D("0.2"), D("-0.1")]
    assert pearson_correlation(values, values) == pytest.approx(D(1), rel=D("0.001"))


def test_opposite_series_correlate_negatively() -> None:
    a = [D("0.1"), D("-0.05"), D("0.2")]
    b = [D("-0.1"), D("0.05"), D("-0.2")]
    assert pearson_correlation(a, b) < D("-0.9")


def test_flat_series_correlates_zero() -> None:
    assert pearson_correlation([D(0), D(0), D(0)], [D("0.1"), D("0.2"), D("0.3")]) == 0


def test_correlated_candidate_is_refused() -> None:
    """PROM-02: three copies of one idea is not diversification."""
    identical = winners_and_losers(20, 15)
    check = check_divergence(identical, stats(profit_factor=D("1.6")), 90)
    decision = evaluate_promotion(check, identical, {"live-1": identical})
    assert not decision.eligible
    assert any("PROM-02" in r for r in decision.reasons)
    assert decision.max_observed_correlation > D("0.9")


def test_uncorrelated_candidate_is_eligible() -> None:
    candidate = [signal("0.10") if i % 2 else signal("-0.05", "LOSS") for i in range(40)]
    other = [signal("-0.05", "LOSS") if i % 3 else signal("0.10") for i in range(40)]
    check = check_divergence(candidate, stats(profit_factor=D("1.0")), 90)
    decision = evaluate_promotion(check, candidate, {"live-1": other})
    assert decision.max_observed_correlation <= D("0.6")


# ---------------------------------------------------------------------- promotion


def _incubating(db: Database) -> str:
    registry = StrategyRegistry(db)
    strategy_id = registry.register(make_spec())
    registry.set_status(strategy_id, StrategyStatus.INCUBATING)
    return strategy_id


def test_promotion_requires_human_confirmation(db: Database, audit: AuditLog) -> None:
    """PROM-01: stricter than the source, which promotes on judgement."""
    strategy_id = _incubating(db)
    signals = winners_and_losers(20, 15)
    decision = evaluate_promotion(
        check_divergence(signals, stats(profit_factor=D("1.6")), 90), signals, {}
    )
    with pytest.raises(PromotionRefused, match="explicit human approval"):
        PromotionGate(db, audit).promote(strategy_id, decision, approved_by="automation")


def test_promotion_requires_named_approver(db: Database, audit: AuditLog) -> None:
    strategy_id = _incubating(db)
    signals = winners_and_losers(20, 15)
    decision = evaluate_promotion(
        check_divergence(signals, stats(profit_factor=D("1.6")), 90), signals, {}
    )
    with pytest.raises(PromotionRefused, match="named human approver"):
        PromotionGate(db, audit).promote(
            strategy_id, decision, approved_by="   ", human_confirmed=True
        )


def test_ineligible_strategy_cannot_be_promoted(db: Database, audit: AuditLog) -> None:
    strategy_id = _incubating(db)
    signals = winners_and_losers(2, 2)
    decision = evaluate_promotion(check_divergence(signals, stats(), 3), signals, {})
    with pytest.raises(PromotionRefused, match="gates not satisfied"):
        PromotionGate(db, audit).promote(
            strategy_id, decision, approved_by="akram", human_confirmed=True
        )


def test_only_incubating_strategies_can_be_promoted(db: Database, audit: AuditLog) -> None:
    """AI-05: no path shortens the pipeline."""
    registry = StrategyRegistry(db)
    strategy_id = registry.register(make_spec())  # CANDIDATE, never incubated
    signals = winners_and_losers(20, 15)
    decision = evaluate_promotion(
        check_divergence(signals, stats(profit_factor=D("1.6")), 90), signals, {}
    )
    with pytest.raises(PromotionRefused, match="not INCUBATING"):
        PromotionGate(db, audit).promote(
            strategy_id, decision, approved_by="akram", human_confirmed=True
        )


def test_successful_promotion_records_evidence(db: Database, audit: AuditLog) -> None:
    """PROM-04: an immutable snapshot of what justified the decision."""
    strategy_id = _incubating(db)
    signals = winners_and_losers(20, 15)
    decision = evaluate_promotion(
        check_divergence(signals, stats(profit_factor=D("1.6")), 90), signals, {}
    )
    gate = PromotionGate(db, audit)
    promotion_id = gate.promote(
        strategy_id,
        decision,
        approved_by="akram",
        human_confirmed=True,
        evidence={"backtest_pf": "1.6"},
    )

    assert StrategyRegistry(db).status(strategy_id) is StrategyStatus.LIVE
    row = db.connection.execute(
        "SELECT approved_by, evidence FROM promotions WHERE id = ?", (promotion_id,)
    ).fetchone()
    assert row["approved_by"] == "akram"
    assert "backtest_pf" in row["evidence"]
    assert any(e.event_type.value == "PROMOTION_APPROVED" for e in audit.tail(5))


def test_newly_promoted_strategy_trades_at_reduced_size(db: Database, audit: AuditLog) -> None:
    """PROM-03."""
    strategy_id = _incubating(db)
    signals = winners_and_losers(20, 15)
    decision = evaluate_promotion(
        check_divergence(signals, stats(profit_factor=D("1.6")), 90), signals, {}
    )
    gate = PromotionGate(db, audit)
    gate.promote(strategy_id, decision, approved_by="akram", human_confirmed=True)

    assert gate.risk_multiplier_for(strategy_id, completed_trades=0) == D("0.5")
    assert gate.risk_multiplier_for(strategy_id, completed_trades=19) == D("0.5")
    assert gate.risk_multiplier_for(strategy_id, completed_trades=20) == D("1")


def test_unpromoted_strategy_has_full_multiplier(db: Database, audit: AuditLog) -> None:
    assert PromotionGate(db, audit).risk_multiplier_for("unknown", 0) == D("1")


def test_promotion_thresholds_configurable(db: Database, audit: AuditLog) -> None:
    strategy_id = _incubating(db)
    signals = winners_and_losers(20, 15)
    decision = evaluate_promotion(
        check_divergence(signals, stats(profit_factor=D("1.6")), 90), signals, {}
    )
    gate = PromotionGate(db, audit)
    gate.promote(
        strategy_id,
        decision,
        approved_by="akram",
        human_confirmed=True,
        thresholds=PromotionThresholds(initial_risk_multiplier=D("0.25")),
    )
    assert gate.risk_multiplier_for(strategy_id, 0) == D("0.25")
