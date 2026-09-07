"""The strategy factory end to end: generation -> gates -> incubation -> promotion.

Two defects this closes, both of the same kind as the unfed ledger: a table declared in
the schema that nothing ever wrote, and a gate that passed by omission.

  - `backtests`, `verifications` and `selection_results` were never written. The
    research loop computed all three, decided on them, and discarded the evidence, so a
    promotion could cite nothing and INC-04/05 had no stored backtest to compare against.
  - a gate whose evidence was absent was left out of the result entirely, so a candidate
    with no out-of-sample window passed selection having never been tested out of sample.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tests.test_backtest import FILTERS, POLICY, series_from
from tests.test_evaluator import percent_spec
from tests.test_selection import stats

from atlas.audit import AuditLog
from atlas.backtest.costs import DEFAULT_COSTS, CostModel
from atlas.backtest.engine import run_backtest
from atlas.backtest.stats import BacktestStats
from atlas.db.engine import Database
from atlas.factory.provenance import config_hash
from atlas.factory.store import PRIMARY, FactoryStore
from atlas.incubation.divergence import check_divergence
from atlas.incubation.tracker import IncubationTracker
from atlas.models import StrategyStatus
from atlas.promotion.gate import PromotionDecision, PromotionGate, PromotionRefused
from atlas.research.tools import ContainmentBreach, assert_tool_allowed
from atlas.risk.sizing import ExchangeFilters
from atlas.selection.gates import GateVerdict, evaluate_gates
from atlas.strategy.registry import StrategyRegistry
from atlas.verify.compare import compare
from atlas.verify.vector_engine import run_verifier

D = Decimal
NOW = datetime(2026, 9, 7, tzinfo=UTC)


def _series(rows: int = 40) -> object:
    """A deterministic oscillating series that produces threshold crossings."""
    pattern = [("100", "112", "98", "110"), ("110", "112", "95", "99")]
    return series_from([pattern[i % 2] for i in range(rows)])


def _registered(db: Database, threshold: str = "105") -> str:
    return StrategyRegistry(db).register(percent_spec(threshold=threshold))


# ---------------------------------------------------- D. deterministic hashing


def test_the_same_rules_hash_the_same(db: Database) -> None:
    """STRAT-06: a spec is identified by its content, not by when it was proposed."""
    first = percent_spec(threshold="105")
    second = percent_spec(threshold="105")
    assert first.content_hash() == second.content_hash()
    assert percent_spec(threshold="106").content_hash() != first.content_hash()


def test_registering_the_same_spec_twice_is_one_strategy(db: Database) -> None:
    """Identical rules are the same strategy; re-proposing must not fork its evidence."""
    registry = StrategyRegistry(db)
    assert registry.register(percent_spec()) == registry.register(percent_spec())


def test_a_candidate_starts_as_CANDIDATE(db: Database) -> None:
    registry = StrategyRegistry(db)
    strategy_id = registry.register(percent_spec())
    assert registry.status(strategy_id) is StrategyStatus.CANDIDATE


# ------------------------------------------------ config provenance and storage


def test_config_hash_changes_with_the_rules_a_run_was_measured_under() -> None:
    """Two results are only comparable when this matches."""
    base = {
        "policy": POLICY,
        "filters": FILTERS,
        "costs": DEFAULT_COSTS,
        "starting_equity": D("100"),
    }
    baseline = config_hash(**base)  # type: ignore[arg-type]

    assert config_hash(**{**base, "starting_equity": D("1000")}) != baseline  # type: ignore[arg-type]
    assert (
        config_hash(**{**base, "costs": CostModel(fee_rate=D("0.005"), slippage_rate=D(0))})  # type: ignore[arg-type]
        != baseline
    )
    assert (
        config_hash(  # type: ignore[arg-type]
            **{
                **base,
                "filters": ExchangeFilters(
                    step_size=D("0.001"),
                    min_qty=D("0.001"),
                    min_notional=D("50"),
                    tick_size=D("0.01"),
                ),
            }
        )
        != baseline
    )
    assert config_hash(**base) == baseline  # type: ignore[arg-type]


def test_a_backtest_is_stored_with_everything_needed_to_reproduce_it(
    db: Database,
) -> None:
    """The tables existed from the first commit and nothing ever wrote to them."""
    strategy_id = _registered(db)
    series = _series()
    result = run_backtest(
        percent_spec(threshold="105"),
        series,  # type: ignore[arg-type]
        policy=POLICY,
        filters=FILTERS,
        starting_equity=D("100"),
    )
    store = FactoryStore(db)
    store.record_backtest(
        strategy_id,
        result,
        series,
        engine=PRIMARY,
        config_hash="cfg-1",  # type: ignore[arg-type]
    )

    stored = store.primary_backtest(strategy_id)
    assert stored is not None
    assert stored.engine == PRIMARY
    assert stored.engine_version == result.engine_version
    assert stored.data_hash == result.data_hash
    assert stored.config_hash == "cfg-1"
    assert stored.cost_model == result.cost_model
    assert stored.window_start and stored.window_end
    assert stored.stats["trade_count"] == result.stats.trade_count


def test_stored_stats_round_trip(db: Database) -> None:
    """INC-04/05 compare against the stored run, so it has to be reconstructable."""
    original = stats()
    assert BacktestStats.from_dict(original.as_dict()) == original


# ---------------------------------------------- E/F/G/H/I/J. backtest behaviour


def test_backtest_is_deterministic(db: Database) -> None:
    series = _series()
    spec = percent_spec(threshold="105")
    runs = [
        run_backtest(spec, series, policy=POLICY, filters=FILTERS, starting_equity=D("100"))  # type: ignore[arg-type]
        for _ in range(3)
    ]
    assert {r.stats.as_dict()["net_return"] for r in runs} == {
        runs[0].stats.as_dict()["net_return"]
    }
    assert {r.data_hash for r in runs} == {runs[0].data_hash}


def test_fees_reduce_the_result(db: Database) -> None:
    """G: a costed run can never beat the same run with no costs."""
    series = _series()
    spec = percent_spec(threshold="105")
    free = run_backtest(
        spec,
        series,  # type: ignore[arg-type]
        policy=POLICY,
        filters=FILTERS,
        starting_equity=D("100"),
        costs=CostModel(fee_rate=D(0), slippage_rate=D(0)),
    )
    costed = run_backtest(
        spec,
        series,  # type: ignore[arg-type]
        policy=POLICY,
        filters=FILTERS,
        starting_equity=D("100"),
        costs=CostModel(fee_rate=D("0.001"), slippage_rate=D("0.0005")),
    )
    assert costed.stats.total_fees > 0
    assert costed.stats.net_return <= free.stats.net_return


def test_minimum_notional_makes_a_strategy_untradeable(db: Database) -> None:
    """J: at $100 a $500 minimum leaves nothing that can be sized."""
    series = _series()
    spec = percent_spec(threshold="105")
    result = run_backtest(
        spec,
        series,  # type: ignore[arg-type]
        policy=POLICY,
        filters=ExchangeFilters(
            step_size=D("0.00000001"),
            min_qty=D("0.00000001"),
            min_notional=D("500"),
            tick_size=D("0.01"),
        ),
        starting_equity=D("100"),
    )
    assert result.stats.trade_count == 0
    assert result.rejected_signals > 0


# ------------------------------------------------- K. independent verification


def test_the_verifier_shares_no_code_with_the_primary_engine() -> None:
    """VER-01: running the same function twice detects nothing."""
    from atlas.backtest import engine as primary_engine
    from atlas.verify import vector_engine

    source = vector_engine.__file__ or ""
    assert source
    with open(source) as handle:
        text = handle.read()
    assert "run_backtest" not in text, "the verifier must not call the primary engine"
    assert primary_engine.ENGINE_VERSION != vector_engine.VERIFIER_VERSION


def test_verification_agreement_is_recorded(db: Database) -> None:
    strategy_id = _registered(db)
    series = _series()
    spec = percent_spec(threshold="105")
    primary = run_backtest(
        spec,
        series,
        policy=POLICY,
        filters=FILTERS,
        starting_equity=D("100"),  # type: ignore[arg-type]
    )
    verifier = run_verifier(
        spec,
        series,
        policy=POLICY,
        filters=FILTERS,
        starting_equity=D("100"),  # type: ignore[arg-type]
    )
    outcome = compare(primary, verifier)

    store = FactoryStore(db)
    primary_id = store.record_backtest(strategy_id, primary, series)  # type: ignore[arg-type]
    verifier_id = store.record_verifier_run(
        strategy_id,
        verifier,
        series,
        cost_model=primary.cost_model,  # type: ignore[arg-type]
    )
    store.record_verification(
        strategy_id, outcome, primary_backtest_id=primary_id, verifier_backtest_id=verifier_id
    )

    record = store.verification_for(strategy_id)
    assert record is not None
    assert record["passed"] is outcome.passed
    assert record["primary_backtest_id"] == primary_id
    assert record["verifier_backtest_id"] == verifier_id


# ------------------------------------------------------- L/M/N. selection gates


def test_a_complete_candidate_can_pass(db: Database) -> None:
    outcome = evaluate_gates(stats(), out_of_sample=stats(), median_stop_distance=D("0.05"))
    assert outcome.passed
    assert {g.verdict for g in outcome.gates} == {GateVerdict.PASS}


def test_a_failing_gate_blocks(db: Database) -> None:
    outcome = evaluate_gates(
        stats(trade_count=5), out_of_sample=stats(), median_stop_distance=D("0.05")
    )
    assert not outcome.passed
    assert any(g.verdict is GateVerdict.FAIL for g in outcome.failures)


def test_a_missing_out_of_sample_window_is_undefined_not_passed() -> None:
    """The defect: SEL-06 used to be omitted, so silence read as consent."""
    outcome = evaluate_gates(stats(), median_stop_distance=D("0.05"))

    assert not outcome.passed
    sel06 = next(g for g in outcome.gates if g.gate.startswith("SEL-06"))
    assert sel06.verdict is GateVerdict.UNDEFINED_POLICY
    assert sel06.blocks
    assert "SEL-06 out_of_sample" in [g.gate for g in outcome.undefined]


def test_an_unmeasurable_stop_distance_is_undefined_not_passed() -> None:
    outcome = evaluate_gates(stats(), out_of_sample=stats())

    assert not outcome.passed
    sel07 = next(g for g in outcome.gates if g.gate.startswith("SEL-07"))
    assert sel07.verdict is GateVerdict.UNDEFINED_POLICY


def test_undefined_policy_is_reported_machine_readably() -> None:
    payload = evaluate_gates(stats()).as_dict()
    assert payload["passed"] is False
    assert set(payload["undefined"]) == {"SEL-06 out_of_sample", "SEL-07 stop_distance_feasible"}
    verdicts = {g["gate"]: g["verdict"] for g in payload["gates"]}
    assert verdicts["SEL-06 out_of_sample"] == "UNDEFINED_POLICY"
    assert json.dumps(payload)  # persists without special handling


def test_selection_result_is_stored(db: Database) -> None:
    strategy_id = _registered(db)
    outcome = evaluate_gates(stats(), out_of_sample=stats(), median_stop_distance=D("0.05"))
    FactoryStore(db).record_selection(strategy_id, outcome)

    record = FactoryStore(db).selection_for(strategy_id)
    assert record is not None and record["passed"] is True
    assert len(record["gates"]["gates"]) == len(outcome.gates)


# --------------------------------------------------- O/P. incubation lifecycle


def test_incubation_elapsed_is_measured_from_the_recorded_start(db: Database) -> None:
    """INC-01 regression.

    Elapsed time used to be measured from the earliest paper signal, which carries the
    timestamp of the bar that produced it. Replaying a year of history therefore
    reported a year of "incubation" the instant it was loaded, and a 60-day requirement
    was satisfiable in one second.
    """
    strategy_id = _registered(db)
    tracker = IncubationTracker(db)

    assert tracker.elapsed_days(strategy_id) == 0, "no start recorded means no observation"

    tracker.begin(
        strategy_id,
        spec_hash="spec-1",
        config_hash="cfg-1",
        started_by="operator",
        at=NOW - timedelta(days=45),
    )
    assert tracker.elapsed_days(strategy_id, now=NOW) == 45


def test_the_incubation_clock_cannot_be_restarted(db: Database) -> None:
    """Re-running the tracker must not move the start and reset the requirement."""
    strategy_id = _registered(db)
    tracker = IncubationTracker(db)
    first = tracker.begin(
        strategy_id,
        spec_hash="spec-1",
        config_hash="cfg-1",
        started_by="operator",
        at=NOW - timedelta(days=45),
    )
    again = tracker.begin(
        strategy_id, spec_hash="spec-1", config_hash="cfg-1", started_by="operator", at=NOW
    )
    assert again.started_at == first.started_at
    assert tracker.elapsed_days(strategy_id, now=NOW) == 45


def test_incubation_records_what_it_was_observing(db: Database) -> None:
    """A run whose spec or config is unknown proves nothing about the strategy."""
    strategy_id = _registered(db)
    run = IncubationTracker(db).begin(
        strategy_id, spec_hash="spec-1", config_hash="cfg-1", started_by="operator"
    )
    assert run.spec_hash == "spec-1"
    assert run.config_hash == "cfg-1"
    assert run.started_by == "operator"


def test_short_incubation_blocks_promotion(db: Database) -> None:
    """P: INC-01 is a hard stop however good the numbers look."""
    check = check_divergence([], stats(), elapsed_days=10)
    assert not check.eligible
    assert any("INC-01" in reason for reason in check.reasons)


def test_too_few_incubation_trades_blocks_promotion(db: Database) -> None:
    check = check_divergence([], stats(), elapsed_days=90)
    assert not check.eligible
    assert any("INC-02" in reason for reason in check.reasons)


# ------------------------------------------- Q/S/T. the promotion boundary


def test_promotion_requires_explicit_human_confirmation(db: Database, audit: AuditLog) -> None:
    strategy_id = _registered(db)
    StrategyRegistry(db).set_status(strategy_id, StrategyStatus.INCUBATING)
    gate = PromotionGate(db, audit)
    eligible = PromotionDecision(eligible=True, reasons=[], max_observed_correlation=D(0))

    with pytest.raises(PromotionRefused, match="explicit human approval"):
        gate.promote(strategy_id, eligible, approved_by="operator", human_confirmed=False)

    assert StrategyRegistry(db).status(strategy_id) is StrategyStatus.INCUBATING


def test_promotion_requires_a_named_approver(db: Database, audit: AuditLog) -> None:
    strategy_id = _registered(db)
    StrategyRegistry(db).set_status(strategy_id, StrategyStatus.INCUBATING)
    eligible = PromotionDecision(eligible=True, reasons=[], max_observed_correlation=D(0))

    with pytest.raises(PromotionRefused, match="named human approver"):
        PromotionGate(db, audit).promote(
            strategy_id, eligible, approved_by="   ", human_confirmed=True
        )


def test_a_candidate_cannot_skip_incubation(db: Database, audit: AuditLog) -> None:
    """T: nothing becomes LIVE without having been observed."""
    strategy_id = _registered(db)
    StrategyRegistry(db).set_status(strategy_id, StrategyStatus.VERIFIED)
    eligible = PromotionDecision(eligible=True, reasons=[], max_observed_correlation=D(0))

    with pytest.raises(PromotionRefused, match="not INCUBATING"):
        PromotionGate(db, audit).promote(
            strategy_id, eligible, approved_by="operator", human_confirmed=True
        )
    assert StrategyRegistry(db).status(strategy_id) is StrategyStatus.VERIFIED


def test_failed_gates_block_promotion_even_with_human_approval(
    db: Database, audit: AuditLog
) -> None:
    """A human may approve a promotion; they cannot approve away the evidence."""
    strategy_id = _registered(db)
    StrategyRegistry(db).set_status(strategy_id, StrategyStatus.INCUBATING)
    refused = PromotionDecision(
        eligible=False, reasons=["INC-02: 4 closed trades"], max_observed_correlation=D(0)
    )

    with pytest.raises(PromotionRefused, match="gates not satisfied"):
        PromotionGate(db, audit).promote(
            strategy_id, refused, approved_by="operator", human_confirmed=True
        )
    assert StrategyRegistry(db).status(strategy_id) is not StrategyStatus.LIVE


def test_a_complete_promotion_is_recorded_immutably(db: Database, audit: AuditLog) -> None:
    """PROM-04: the evidence snapshot is written with the approval, not after it."""
    strategy_id = _registered(db)
    StrategyRegistry(db).set_status(strategy_id, StrategyStatus.INCUBATING)
    eligible = PromotionDecision(eligible=True, reasons=[], max_observed_correlation=D("0.1"))

    promotion_id = PromotionGate(db, audit).promote(
        strategy_id,
        eligible,
        approved_by="operator",
        human_confirmed=True,
        evidence={"note": "observed"},
    )

    row = db.connection.execute("SELECT * FROM promotions WHERE id = ?", (promotion_id,)).fetchone()
    assert row is not None
    assert row["approved_by"] == "operator"
    assert json.loads(row["evidence"])["evidence"] == {"note": "observed"}
    assert StrategyRegistry(db).status(strategy_id) is StrategyStatus.LIVE


# --------------------------------------------- R/S. the research plane boundary


@pytest.mark.parametrize(
    "tool",
    [
        "place_order",
        "cancel_order",
        "size_position",
        "set_risk_parameter",
        "promote_strategy",
        "approve_promotion",
        "disarm_kill_switch",
        "withdraw",
        "read_api_credentials",
    ],
)
def test_research_cannot_reach_the_control_plane(tool: str) -> None:
    """R/S: enforcement is absence, not instruction."""
    with pytest.raises(ContainmentBreach):
        assert_tool_allowed(tool)


def test_the_research_module_imports_nothing_that_executes() -> None:
    """A module that cannot import the broker cannot call it by accident."""
    import ast
    import pathlib

    research = pathlib.Path("src/atlas/research")
    forbidden = {"atlas.execution.broker", "atlas.execution.brackets", "atlas.killswitch"}
    for path in research.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in forbidden:
                pytest.fail(f"{path.name} imports {node.module}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in forbidden, f"{path.name} imports {alias.name}"


def test_promotion_has_no_automated_caller() -> None:
    """S: `PromotionGate.promote` must be reachable only from a human-driven path."""
    import pathlib

    callers = [
        path
        for path in pathlib.Path("src/atlas").rglob("*.py")
        if ".promote(" in path.read_text() and path.name != "gate.py"
    ]
    assert callers == [], f"promote() is called from {[p.name for p in callers]}"


def test_no_cli_command_can_promote() -> None:
    """Live capital must not be one shell command away from an inspection."""
    import pathlib

    cli = pathlib.Path("src/atlas/cli.py").read_text()
    assert "PromotionGate" not in cli
    assert "StrategyStatus.LIVE" not in cli
