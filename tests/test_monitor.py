"""MON-01..07, including the ARB-shaped regression test.

The video's documented failure was a present rule overridden by its author because the
strategy briefly recovered. These tests assert the rules cannot be talked out of.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tests.test_strategy_spec import make_spec

from atlas.audit import AuditLog
from atlas.db.engine import Database
from atlas.models import StrategyStatus
from atlas.monitor.health import compute_health, return_distribution
from atlas.monitor.rules import MonitorThresholds, RuleAction, StrategyHealth, evaluate_all
from atlas.monitor.supervisor import Supervisor
from atlas.strategy.registry import StrategyRegistry

D = Decimal


def health(**kw: object) -> StrategyHealth:
    defaults: dict[str, object] = {
        "trade_count": 40,
        "rolling_win_rate": D("0.45"),
        "rolling_profit_factor": D("1.5"),
        "consecutive_losses": 1,
        "realised_equity": D("0.30"),
        "expected_equity": D("0.28"),
        "equity_sigma": D("0.10"),
        "bars_since_last_signal": 5,
        "mean_inter_trade_bars": D("10"),
    }
    defaults.update(kw)
    return StrategyHealth(**defaults)  # type: ignore[arg-type]


def live_strategy(db: Database) -> str:
    registry = StrategyRegistry(db)
    strategy_id = registry.register(make_spec())
    registry.set_status(strategy_id, StrategyStatus.LIVE)
    return strategy_id


# ------------------------------------------------------------------------- rules


def test_healthy_strategy_fires_nothing() -> None:
    assert not [v for v in evaluate_all(health(), D("0.45")) if v.fired]


def test_equity_band_breach_retires() -> None:
    """MON-01, David's 'ultimate stop' [21:12-21:33]."""
    verdicts = evaluate_all(
        health(realised_equity=D("0.02"), expected_equity=D("0.30"), equity_sigma=D("0.10")),
        D("0.45"),
    )
    breach = next(v for v in verdicts if v.rule.startswith("MON-01"))
    assert breach.fired
    assert breach.action is RuleAction.RETIRE


def test_equity_inside_the_band_does_not_fire() -> None:
    verdicts = evaluate_all(
        health(realised_equity=D("0.15"), expected_equity=D("0.30"), equity_sigma=D("0.10")),
        D("0.45"),
    )
    assert not next(v for v in verdicts if v.rule.startswith("MON-01")).fired


def test_zero_sigma_cannot_fire_the_band_rule() -> None:
    """Without a backtest distribution there is no band to breach."""
    verdicts = evaluate_all(
        health(realised_equity=D("-5"), expected_equity=D("0"), equity_sigma=D("0")), D("0.45")
    )
    assert not next(v for v in verdicts if v.rule.startswith("MON-01")).fired


def test_win_rate_collapse_retires() -> None:
    """MON-02."""
    verdicts = evaluate_all(health(rolling_win_rate=D("0.15")), D("0.50"))
    rule = next(v for v in verdicts if v.rule.startswith("MON-02"))
    assert rule.fired
    assert rule.action is RuleAction.RETIRE


def test_win_rate_rule_needs_a_full_window() -> None:
    """A handful of trades is not evidence of collapse."""
    verdicts = evaluate_all(health(trade_count=5, rolling_win_rate=D("0.0")), D("0.50"))
    assert not next(v for v in verdicts if v.rule.startswith("MON-02")).fired


def test_profit_factor_collapse_retires() -> None:
    """MON-03."""
    verdicts = evaluate_all(health(rolling_profit_factor=D("0.7")), D("0.45"))
    rule = next(v for v in verdicts if v.rule.startswith("MON-03"))
    assert rule.fired
    assert rule.action is RuleAction.RETIRE


def test_consecutive_losses_suspend() -> None:
    """MON-04, an ATLAS addition: faster than a 30-trade window."""
    verdicts = evaluate_all(health(consecutive_losses=9), D("0.45"))
    rule = next(v for v in verdicts if v.rule.startswith("MON-04"))
    assert rule.fired
    assert rule.action is RuleAction.SUSPEND


def test_signal_starvation_alerts() -> None:
    """MON-05: a silently broken strategy looks identical to a quiet one."""
    verdicts = evaluate_all(
        health(bars_since_last_signal=50, mean_inter_trade_bars=D("10")), D("0.45")
    )
    rule = next(v for v in verdicts if v.rule.startswith("MON-05"))
    assert rule.fired
    assert rule.action is RuleAction.ALERT


def test_thresholds_are_configurable() -> None:
    strict = MonitorThresholds(max_consecutive_losses=2)
    verdicts = evaluate_all(health(consecutive_losses=3), D("0.45"), strict)
    assert next(v for v in verdicts if v.rule.startswith("MON-04")).fired


# ---------------------------------------------------------------------- health


def test_compute_health_from_returns() -> None:
    returns = [D("0.10"), D("-0.05")] * 20
    snapshot = compute_health(
        returns, backtest_mean_return=D("0.02"), backtest_return_sigma=D("0.08")
    )
    assert snapshot.trade_count == 40
    assert snapshot.rolling_win_rate == D("0.5")
    assert snapshot.rolling_profit_factor == D("2")
    assert snapshot.realised_equity == D("1.00")


def test_consecutive_losses_counted_from_the_end() -> None:
    returns = [D("0.1"), D("0.1"), D("-0.05"), D("-0.05"), D("-0.05")]
    snapshot = compute_health(
        returns, backtest_mean_return=D("0.01"), backtest_return_sigma=D("0.05")
    )
    assert snapshot.consecutive_losses == 3


def test_band_widens_with_trade_count() -> None:
    """Sigma scales with sqrt(n), so the band does not tighten as trades accumulate."""
    small = compute_health(
        [D("0.01")] * 4, backtest_mean_return=D("0.01"), backtest_return_sigma=D("0.05")
    )
    large = compute_health(
        [D("0.01")] * 100, backtest_mean_return=D("0.01"), backtest_return_sigma=D("0.05")
    )
    assert large.equity_sigma > small.equity_sigma


def test_return_distribution() -> None:
    mean, sigma = return_distribution([D("0.1"), D("-0.1"), D("0.1"), D("-0.1")])
    assert mean == 0
    assert sigma == pytest.approx(D("0.1"), rel=D("0.001"))


def test_empty_returns_are_safe() -> None:
    snapshot = compute_health([], backtest_mean_return=D("0.01"), backtest_return_sigma=D("0.05"))
    assert snapshot.trade_count == 0
    assert snapshot.rolling_profit_factor == 0


# ------------------------------------------------------------------ supervisor


def test_healthy_strategy_is_left_alone(db: Database, audit: AuditLog) -> None:
    strategy_id = live_strategy(db)
    result = Supervisor(db, audit).supervise(strategy_id, health(), D("0.45"))
    assert result.action_taken is None
    assert StrategyRegistry(db).status(strategy_id) is StrategyStatus.LIVE


def test_single_rule_retires_without_a_quorum(db: Database, audit: AuditLog) -> None:
    """MON-07: rules are not averaged, weighted or voted on."""
    strategy_id = live_strategy(db)
    result = Supervisor(db, audit).supervise(
        strategy_id, health(rolling_profit_factor=D("0.5")), D("0.45")
    )
    assert result.retired
    assert len(result.fired) == 1
    assert StrategyRegistry(db).status(strategy_id) is StrategyStatus.RETIRED


def test_retire_outranks_suspend(db: Database, audit: AuditLog) -> None:
    strategy_id = live_strategy(db)
    result = Supervisor(db, audit).supervise(
        strategy_id, health(rolling_profit_factor=D("0.5"), consecutive_losses=10), D("0.45")
    )
    assert result.action_taken is RuleAction.RETIRE


def test_retirement_is_one_way(db: Database, audit: AuditLog) -> None:
    """MON-06: no programmatic reactivation exists anywhere in ATLAS."""
    strategy_id = live_strategy(db)
    registry = StrategyRegistry(db)
    Supervisor(db, audit).supervise(strategy_id, health(rolling_profit_factor=D("0.5")), D("0.45"))

    with pytest.raises(ValueError, match="RETIRED"):
        registry.set_status(strategy_id, StrategyStatus.LIVE)
    assert registry.status(strategy_id) is StrategyStatus.RETIRED


def test_retired_strategy_opens_no_new_entries(db: Database, audit: AuditLog) -> None:
    strategy_id = live_strategy(db)
    supervisor = Supervisor(db, audit)
    assert supervisor.can_open_new_entries(strategy_id)
    supervisor.supervise(strategy_id, health(rolling_profit_factor=D("0.5")), D("0.45"))
    assert not supervisor.can_open_new_entries(strategy_id)


def test_supervising_a_retired_strategy_is_a_no_op(db: Database, audit: AuditLog) -> None:
    strategy_id = live_strategy(db)
    supervisor = Supervisor(db, audit)
    supervisor.supervise(strategy_id, health(rolling_profit_factor=D("0.5")), D("0.45"))
    again = supervisor.supervise(strategy_id, health(), D("0.45"))
    assert again.action_taken is None
    assert again.reason == "already retired"


def test_retirement_is_audited(db: Database, audit: AuditLog) -> None:
    strategy_id = live_strategy(db)
    Supervisor(db, audit).supervise(strategy_id, health(rolling_win_rate=D("0.05")), D("0.50"))
    events = [e for e in audit.tail(10) if e.event_type.value == "STRATEGY_RETIRED"]
    assert events
    assert events[-1].payload["reactivation"] == "human only (MON-06)"
    assert audit.verify_chain() > 0


# --------------------------------------------------------- ARB regression (Phase 10)


def test_decay_curve_retires_automatically(db: Database, audit: AuditLog) -> None:
    """The regression test for the failure the video is about.

    A strategy that runs up hard, then decays, then *briefly recovers*, then collapses.
    The brief recovery is the part that fooled its author. No human is consulted here.
    """
    strategy_id = live_strategy(db)
    supervisor = Supervisor(db, audit)

    run_up = [D("0.12")] * 30
    decay = [D("-0.06")] * 12
    dead_cat = [D("0.05")] * 4  # the recovery that fooled him
    collapse = [D("-0.08")] * 20

    retired_after = None
    returns: list[Decimal] = []
    for phase in (run_up, decay, dead_cat, collapse):
        for value in phase:
            returns.append(value)
            snapshot = compute_health(
                returns, backtest_mean_return=D("0.03"), backtest_return_sigma=D("0.05")
            )
            result = supervisor.supervise(strategy_id, snapshot, D("0.55"))
            if result.retired and retired_after is None:
                retired_after = len(returns)
                break
        if retired_after:
            break

    assert retired_after is not None, "the decay curve must retire the strategy"
    assert retired_after < len(run_up) + len(decay) + len(dead_cat) + len(collapse)
    assert StrategyRegistry(db).status(strategy_id) is StrategyStatus.RETIRED

    with pytest.raises(ValueError):
        StrategyRegistry(db).set_status(strategy_id, StrategyStatus.LIVE)


def test_recovery_cannot_undo_a_retirement(db: Database, audit: AuditLog) -> None:
    """The specific failure mode: 'it looks better now' is not a reason."""
    strategy_id = live_strategy(db)
    supervisor = Supervisor(db, audit)
    supervisor.supervise(strategy_id, health(rolling_profit_factor=D("0.4")), D("0.45"))
    assert StrategyRegistry(db).status(strategy_id) is StrategyStatus.RETIRED

    for _ in range(10):
        supervisor.supervise(strategy_id, health(rolling_profit_factor=D("3.0")), D("0.45"))
    assert StrategyRegistry(db).status(strategy_id) is StrategyStatus.RETIRED
    assert not supervisor.can_open_new_entries(strategy_id)
