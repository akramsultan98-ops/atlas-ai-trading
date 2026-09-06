"""AI-05, AI-06, AI-07 and pipeline stages 1-5, driven by a stub model."""

from __future__ import annotations

from decimal import Decimal

from tests.test_backtest import FILTERS, NO_COSTS, POLICY, series_from
from tests.test_evaluator import percent_spec

from atlas.audit import AuditLog
from atlas.db.engine import Database
from atlas.models import AuditEventType, StrategyStatus
from atlas.research.generator import validate_generated_spec
from atlas.research.loop import ResearchLoop, Stage
from atlas.selection.gates import SelectionThresholds
from atlas.strategy.registry import StrategyRegistry
from atlas.strategy.spec import StrategySpec

D = Decimal


class StubClient:
    """Returns queued specs, or raises queued exceptions."""

    model_id = "stub-model"

    def __init__(self, *items: object) -> None:
        self.items = list(items)
        self.calls: list[tuple[str, str]] = []

    def generate(self, system: str, user: str) -> StrategySpec:
        self.calls.append((system, user))
        item = self.items.pop(0) if self.items else self.items
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, StrategySpec)
        return item


def oscillating(n: int = 400):
    rows = []
    for i in range(n):
        base = 100 + (i * 17) % 45
        rows.append((str(base), str(base + 5), str(base - 5), str(base + (i % 7) - 3)))
    return series_from(rows)


def make_loop(db: Database, audit: AuditLog, client: object, **kw: object) -> ResearchLoop:
    return ResearchLoop(
        db,
        audit,
        client,  # type: ignore[arg-type]
        policy=POLICY,
        filters=FILTERS,
        costs=NO_COSTS,
        starting_equity=D("100"),
        **kw,  # type: ignore[arg-type]
    )


def test_generation_failure_is_recorded_not_raised(db: Database, audit: AuditLog) -> None:
    """AI-08: a failing model degrades throughput, never correctness."""
    loop = make_loop(db, audit, StubClient(RuntimeError("api down")))
    outcome = loop.run_once(oscillating())
    assert not outcome.accepted
    assert outcome.stage is Stage.GENERATION
    assert "api down" in outcome.reason


def test_symbol_mismatch_is_rejected(db: Database, audit: AuditLog) -> None:
    """A model that quietly substitutes a symbol must not be backtested elsewhere."""
    spec = percent_spec().model_copy(update={"symbol": "ETHUSDT"})
    loop = make_loop(db, audit, StubClient(spec))
    outcome = loop.run_once(oscillating())
    assert outcome.stage is Stage.SCHEMA
    assert "symbol mismatch" in outcome.reason


def test_weak_strategy_is_rejected_at_selection(db: Database, audit: AuditLog) -> None:
    loop = make_loop(db, audit, StubClient(percent_spec(threshold="120")))
    outcome = loop.run_once(oscillating())
    assert not outcome.accepted
    assert outcome.stage in (Stage.SELECTION, Stage.VERIFICATION)


def test_accepted_strategy_is_persisted_as_verified(db: Database, audit: AuditLog) -> None:
    """The loop's terminal state is VERIFIED — never LIVE. Promotion is human-gated."""
    lenient = SelectionThresholds(
        min_trades=1,
        max_drawdown=D("0.99"),
        min_profit_factor=D("0"),
        min_expectancy=D("-999"),
        require_beats_buy_and_hold=False,
        min_oos_profit_factor=D("0"),
        min_oos_retention=D("0"),
        stop_band_low=D("0.001"),
        stop_band_high=D("0.99"),
    )
    loop = make_loop(db, audit, StubClient(percent_spec(threshold="120")), thresholds=lenient)
    outcome = loop.run_once(oscillating())

    assert outcome.accepted, outcome.reason
    assert outcome.stage is Stage.ACCEPTED
    assert outcome.strategy_id is not None
    assert StrategyRegistry(db).status(outcome.strategy_id) is StrategyStatus.VERIFIED


def test_loop_never_promotes_to_live(db: Database, audit: AuditLog) -> None:
    """AI-05: no path through the loop commits capital."""
    lenient = SelectionThresholds(
        min_trades=1,
        max_drawdown=D("0.99"),
        min_profit_factor=D("0"),
        min_expectancy=D("-999"),
        require_beats_buy_and_hold=False,
        min_oos_profit_factor=D("0"),
        min_oos_retention=D("0"),
        stop_band_low=D("0.001"),
        stop_band_high=D("0.99"),
    )
    loop = make_loop(db, audit, StubClient(percent_spec(threshold="120")), thresholds=lenient)
    loop.run_once(oscillating())
    registry = StrategyRegistry(db)
    assert registry.list_by_status(StrategyStatus.LIVE) == []
    assert registry.list_by_status(StrategyStatus.PROMOTED) == []


def test_every_pass_writes_an_audit_event(db: Database, audit: AuditLog) -> None:
    """AI-07: model id, prompt hash and output hash on every attempt."""
    loop = make_loop(db, audit, StubClient(percent_spec(threshold="120")))
    loop.run_once(oscillating())

    events = [e for e in audit.tail(10) if e.event_type is AuditEventType.AI_ACTION]
    assert events
    payload = events[-1].payload
    assert payload["model_id"] == "stub-model"
    assert len(str(payload["prompt_hash"])) == 64
    assert events[-1].actor == "research"
    assert audit.verify_chain() > 0


def test_batch_continues_past_a_failure(db: Database, audit: AuditLog) -> None:
    """One bad candidate never stops the factory."""
    client = StubClient(
        RuntimeError("transient"),
        percent_spec(threshold="120"),
        RuntimeError("transient again"),
    )
    outcomes = make_loop(db, audit, client).run_batch(oscillating(), passes=3)
    assert len(outcomes) == 3
    assert [o.stage for o in outcomes].count(Stage.GENERATION) == 2


def test_prompt_varies_between_passes(db: Database, audit: AuditLog) -> None:
    """The video's instruction not to converge on the same setup [07:54-08:12]."""
    client = StubClient(*[percent_spec(threshold="120") for _ in range(4)])
    make_loop(db, audit, client).run_batch(oscillating(), passes=4)
    hints = {user for _system, user in client.calls}
    assert len(hints) == 4


def test_prompt_is_short(db: Database, audit: AuditLog) -> None:
    """David: more information reduces the results you get [07:36]."""
    client = StubClient(percent_spec(threshold="120"))
    make_loop(db, audit, client).run_once(oscillating())
    system, user = client.calls[0]
    assert len(system) + len(user) < 2000


def test_prompt_never_contains_credentials(db: Database, audit: AuditLog) -> None:
    client = StubClient(percent_spec(threshold="120"))
    make_loop(db, audit, client).run_once(oscillating())
    combined = "".join(part for call in client.calls for part in call).lower()
    for leak in ("api_key", "secret", "binance", "password", "token"):
        assert leak not in combined


# ------------------------------------------------------------------ spec validation


def test_validate_generated_spec_accepts_matching_request() -> None:
    from atlas.data.models import Timeframe

    ok, reason = validate_generated_spec(percent_spec(), "BTCUSDT", Timeframe.H1)
    assert ok
    assert reason == "accepted"


def test_validate_generated_spec_rejects_timeframe_mismatch() -> None:
    from atlas.data.models import Timeframe

    ok, reason = validate_generated_spec(percent_spec(), "BTCUSDT", Timeframe.D1)
    assert not ok
    assert "timeframe mismatch" in reason


def test_llm_output_is_never_executed_as_code() -> None:
    """AI-06: the spec is data. Nothing in the pipeline evals or execs it."""
    import ast
    from pathlib import Path

    research = Path(__file__).resolve().parents[1] / "src" / "atlas" / "research"
    for path in research.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in ("eval", "exec", "compile", "__import__"), (
                    f"{path.name} calls {node.func.id}"
                )
