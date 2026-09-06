"""End-to-end pipeline test.

Drives one strategy through every stage the specification defines:

    research -> generation -> schema validation -> backtest -> independent
    verification -> selection gates -> incubation -> promotion (human) ->
    live execution -> monitoring -> automatic retirement

Zero real-money exposure: a stub model, a stub broker transport, testnet, and no
credentials anywhere. The point is that the stages compose — each one's output is the
next one's input, and the safety gates hold at every hand-off.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from tests.test_backtest import FILTERS, NO_COSTS, POLICY
from tests.test_evaluator import percent_spec
from tests.test_execution import StubTransport, ack
from tests.test_research_loop import StubClient, oscillating

from atlas.audit import AuditLog
from atlas.backtest.engine import run_backtest
from atlas.db.engine import Database
from atlas.execution.brackets import open_bracketed_position
from atlas.execution.broker import BinanceSpotBroker
from atlas.execution.reconcile import Reconciler
from atlas.incubation.divergence import IncubationThresholds, check_divergence
from atlas.incubation.tracker import IncubationTracker
from atlas.killswitch import KillSwitch
from atlas.models import ExchangeEnv, KillSwitchTrigger, PositionSide, StrategyStatus
from atlas.monitor.health import compute_health
from atlas.monitor.supervisor import Supervisor
from atlas.promotion.gate import PromotionGate, PromotionRefused, evaluate_promotion
from atlas.research.loop import ResearchLoop, Stage
from atlas.risk.sizing import size_position
from atlas.selection.gates import SelectionThresholds
from atlas.strategy.registry import StrategyRegistry

D = Decimal
BAR_TIME = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)

PERMISSIVE = SelectionThresholds(
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


@pytest.fixture
def pipeline(db: Database, audit: AuditLog, tmp_path: Any) -> dict[str, Any]:
    killswitch = KillSwitch(db, tmp_path / "ks.json", audit)
    killswitch.initialise()
    return {
        "db": db,
        "audit": audit,
        "killswitch": killswitch,
        "registry": StrategyRegistry(db),
    }


def test_full_pipeline_research_to_retirement(pipeline: dict[str, Any]) -> None:
    db, audit = pipeline["db"], pipeline["audit"]
    killswitch, registry = pipeline["killswitch"], pipeline["registry"]
    spec = percent_spec(threshold="120")
    series = oscillating()

    # --- stages 1-5: research loop produces a VERIFIED candidate ---------------
    loop = ResearchLoop(
        db,
        audit,
        StubClient(spec),
        policy=POLICY,
        filters=FILTERS,
        thresholds=PERMISSIVE,
        costs=NO_COSTS,
        starting_equity=D("100"),
    )
    outcome = loop.run_once(series)
    assert outcome.accepted, outcome.reason
    assert outcome.stage is Stage.ACCEPTED
    strategy_id = outcome.strategy_id
    assert strategy_id is not None
    assert registry.status(strategy_id) is StrategyStatus.VERIFIED

    # A verified strategy is not tradeable. Promotion must refuse it.
    with pytest.raises(PromotionRefused, match="not INCUBATING"):
        PromotionGate(db, audit).promote(
            strategy_id,
            evaluate_promotion(
                check_divergence(
                    [],
                    run_backtest(
                        spec,
                        series,
                        policy=POLICY,
                        filters=FILTERS,
                        starting_equity=D("100"),
                        costs=NO_COSTS,
                    ).stats,
                    90,
                ),
                [],
                {},
            ),
            approved_by="akram",
            human_confirmed=True,
        )

    # --- stage 6: incubation, zero capital ------------------------------------
    registry.set_status(strategy_id, StrategyStatus.INCUBATING)
    tracker = IncubationTracker(db)
    recorded = tracker.record_signals(strategy_id, spec, series)
    assert recorded > 0

    signals = tracker.signals_for(strategy_id)
    backtest = run_backtest(
        spec,
        series,
        policy=POLICY,
        filters=FILTERS,
        starting_equity=D("100"),
        costs=NO_COSTS,
    )
    divergence = check_divergence(
        signals,
        backtest.stats,
        elapsed_days=90,
        thresholds=IncubationThresholds(
            min_days=1,
            min_trades=1,
            min_profit_factor_retention=D("0"),
            max_drawdown_multiple=D("99"),
        ),
    )
    assert divergence.eligible, divergence.reasons

    # --- stage 7: promotion requires a human ----------------------------------
    decision = evaluate_promotion(divergence, signals, {})
    gate = PromotionGate(db, audit)

    with pytest.raises(PromotionRefused, match="explicit human approval"):
        gate.promote(strategy_id, decision, approved_by="automation")

    gate.promote(strategy_id, decision, approved_by="akram", human_confirmed=True)
    assert registry.status(strategy_id) is StrategyStatus.LIVE
    assert gate.risk_multiplier_for(strategy_id, completed_trades=0) == D("0.5")

    # --- stage 8: live execution, sized and bracketed --------------------------
    sizing = size_position(
        equity=D("100"),
        free_cash=D("100"),
        entry_price=D("100"),
        stop_price=D("95"),
        side=PositionSide.LONG,
        policy=POLICY,
        filters=FILTERS,
        risk_multiplier=gate.risk_multiplier_for(strategy_id, 0),
    )
    assert sizing.accepted

    transport = StubTransport(ack("entry"), ack("stop", status="NEW"))
    broker = BinanceSpotBroker(
        "test-key",
        "test-secret",
        killswitch,
        audit,
        exchange_env=ExchangeEnv.TESTNET,
        transport=transport,
    )
    bracket = open_bracketed_position(
        broker,
        audit,
        strategy_id=strategy_id,
        signal_bar_time=BAR_TIME,
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        quantity=sizing.quantity,
        stop_price=D("95"),
    )
    assert bracket.entry.status == "FILLED"
    assert len(transport.posts) == 2, "entry and its protective stop"

    # --- stage 9-10: monitoring retires it automatically -----------------------
    supervisor = Supervisor(db, audit)
    assert supervisor.can_open_new_entries(strategy_id)

    collapse = [D("-0.08")] * 40
    health = compute_health(
        collapse, backtest_mean_return=D("0.03"), backtest_return_sigma=D("0.05")
    )
    result = supervisor.supervise(strategy_id, health, backtest_win_rate=D("0.55"))

    assert result.retired, result.reason
    assert registry.status(strategy_id) is StrategyStatus.RETIRED
    assert not supervisor.can_open_new_entries(strategy_id)

    # Retirement is one-way, even for a human calling directly.
    with pytest.raises(ValueError, match="RETIRED"):
        registry.set_status(strategy_id, StrategyStatus.LIVE)

    # --- the audit trail covers the whole life of the strategy -----------------
    assert audit.verify_chain() > 0
    kinds = {e.event_type.value for e in audit.tail(200)}
    for expected in (
        "AI_ACTION",
        "PROMOTION_APPROVED",
        "ORDER_INTENT",
        "ORDER_RESULT",
        "STRATEGY_RETIRED",
    ):
        assert expected in kinds, f"missing {expected} from the audit trail"


def test_kill_switch_halts_the_pipeline_mid_flight(pipeline: dict[str, Any]) -> None:
    """An armed kill switch stops new exposure everywhere, at any stage."""
    audit, killswitch = pipeline["audit"], pipeline["killswitch"]

    transport = StubTransport(ack("entry"), ack("stop", status="NEW"))
    broker = BinanceSpotBroker(
        "k", "s", killswitch, audit, exchange_env=ExchangeEnv.TESTNET, transport=transport
    )
    killswitch.arm(KillSwitchTrigger.MAX_ACCOUNT_DD, "20% drawdown")

    from atlas.errors import KillSwitchArmedError

    with pytest.raises(KillSwitchArmedError):
        open_bracketed_position(
            broker,
            audit,
            strategy_id="s1",
            signal_bar_time=BAR_TIME,
            symbol="BTCUSDT",
            side=PositionSide.LONG,
            quantity=D("0.001"),
            stop_price=D("95"),
        )
    assert transport.posts == [], "nothing reached the exchange"


def test_unrecorded_exposure_stops_everything(pipeline: dict[str, Any]) -> None:
    """Reconciliation finding an ATLAS order we have no record of arms the switch."""
    db, audit, killswitch = pipeline["db"], pipeline["audit"], pipeline["killswitch"]
    assert not killswitch.is_armed()

    report = Reconciler(db, audit, killswitch).reconcile(
        [{"clientOrderId": "atlasUNKNOWN", "status": "NEW"}]
    )
    assert not report.clean
    assert killswitch.is_armed()
    assert killswitch.read_state().trigger is KillSwitchTrigger.RECONCILIATION_FAILURE


def test_no_credentials_anywhere_in_the_repository() -> None:
    """The real-money gate: nothing that looks like a live key is committed."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    key_pattern = re.compile(r"[A-Za-z0-9]{64}")
    for path in [*(root / "src").rglob("*.py"), root / ".env.example"]:
        text = path.read_text()
        for match in key_pattern.findall(text):
            # 64-char hex is our own audit-hash placeholder, not a Binance key.
            assert all(c in "0123456789abcdef" for c in match.lower()), (
                f"possible credential in {path.name}: {match[:8]}..."
            )
