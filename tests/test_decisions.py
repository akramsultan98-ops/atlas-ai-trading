"""Per-symbol tick decisions are observable, and each reason is distinguishable.

The operator problem this closes: `entries=0` looked identical whether the system had
declined the market, never looked because nothing was deployed, or wanted to trade and
was refused by risk. Those have different remedies, and the audit trail recorded the
same thing - nothing - for all three.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tests.conftest import StubFilterProvider
from tests.test_backtest import POLICY, series_from
from tests.test_evaluator import percent_spec
from tests.test_execution import StubTransport, ack, oco_reply
from tests.test_runtime import NOW, _signal_series

from atlas.audit import AuditLog
from atlas.data.models import Kline, KlineSeries, Timeframe
from atlas.db.engine import Database
from atlas.execution.broker import BinanceSpotBroker
from atlas.killswitch import KillSwitch
from atlas.models import AuditEventType, ExchangeEnv, KillSwitchTrigger, StrategyStatus
from atlas.risk.limits import AccountState
from atlas.risk.sizing import ExchangeFilters, RejectReason
from atlas.runtime.decisions import REGIME_NOT_IMPLEMENTED, DecisionOutcome, render_decisions
from atlas.runtime.trading_service import TradingService
from atlas.strategy.evaluator import explain
from atlas.strategy.registry import StrategyRegistry

D = Decimal
TODAY = datetime(2026, 9, 6, tzinfo=UTC).date()


def _service(
    db: Database,
    audit: AuditLog,
    killswitch: KillSwitch,
    transport: StubTransport | None = None,
    filters: StubFilterProvider | None = None,
) -> TradingService:
    broker = BinanceSpotBroker(
        "k",
        "s",
        killswitch,
        audit,
        exchange_env=ExchangeEnv.TESTNET,
        transport=transport or StubTransport(ack("entry"), oco_reply()),
    )
    return TradingService(
        db, audit, killswitch, broker, policy=POLICY, filters=filters or StubFilterProvider()
    )


def _account(**kw: object) -> AccountState:
    base: dict[str, object] = {
        "equity": D("10000"),
        "peak_equity": D("10000"),
        "day_start_equity": D("10000"),
        "as_of": TODAY,
        "free_cash": D("10000"),
    }
    base.update(kw)
    return AccountState(**base)  # type: ignore[arg-type]


def _live(db: Database, threshold: str = "120") -> str:
    registry = StrategyRegistry(db)
    strategy_id = registry.register(percent_spec(threshold=threshold))
    registry.set_status(strategy_id, StrategyStatus.LIVE)
    return strategy_id


def _flat_series(last_close: datetime) -> KlineSeries:
    """A series that never crosses the threshold: a genuine no-signal market."""
    series = series_from([("100", "101", "99", "100")] * 3)
    shift = last_close - series.bars[-1].close_time
    bars = tuple(
        Kline(
            open_time=b.open_time + shift,
            close_time=b.close_time + shift,
            open=b.open,
            high=b.high,
            low=b.low,
            close=b.close,
            volume=b.volume,
            trades=b.trades,
        )
        for b in series.bars
    )
    return KlineSeries(symbol="BTCUSDT", timeframe=Timeframe.H1, bars=bars)


def _tick(
    service: TradingService,
    series_by_symbol: dict[str, KlineSeries] | None = None,
    account: AccountState | None = None,
    symbols: list[str] | None = None,
) -> object:
    return service.tick(
        account=account or _account(),
        series_by_symbol=series_by_symbol if series_by_symbol is not None else {},
        exchange_orders=[],
        live_returns={},
        backtest_stats={},
        symbols=symbols if symbols is not None else ["BTCUSDT"],
        now=NOW,
    )


def _only(result: object) -> object:
    decisions = result.decisions  # type: ignore[attr-defined]
    assert len(decisions) == 1, f"expected one decision, got {[d.outcome for d in decisions]}"
    return decisions[0]


# ------------------------------------------------- 1. a no-entry decision has a reason


def test_a_quiet_tick_says_why_it_was_quiet(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """The exact case observed on testnet: a tick reporting entries=0 and nothing else."""
    killswitch.initialise()
    result = _tick(_service(db, audit, killswitch))

    decision = _only(result)
    assert decision.outcome is DecisionOutcome.NO_STRATEGY  # type: ignore[attr-defined]
    assert decision.detail  # type: ignore[attr-defined]
    assert "LIVE" in decision.detail  # type: ignore[attr-defined]
    assert not decision.strategy_present  # type: ignore[attr-defined]


def test_every_configured_symbol_gets_a_decision(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """Entries come from live strategies, so a symbol with none was previously silent."""
    killswitch.initialise()
    result = _tick(_service(db, audit, killswitch), symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"])

    assert [d.symbol for d in result.decisions] == [  # type: ignore[attr-defined]
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
    ]


def test_the_decision_is_written_to_the_audit_trail(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """It has to survive the process; an operator diagnoses after the fact."""
    killswitch.initialise()
    _tick(_service(db, audit, killswitch))

    events = [e for e in audit.tail(50) if e.event_type is AuditEventType.TICK_DECISION]
    assert len(events) == 1
    assert events[0].payload["symbol"] == "BTCUSDT"
    assert events[0].payload["outcome"] == "NO_STRATEGY"
    assert audit.verify_chain() > 0


# ------------------------------------------------------- 2. an entry is observable


def test_an_entry_records_why_it_was_allowed(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    killswitch.initialise()
    strategy_id = _live(db)
    service = _service(db, audit, killswitch)

    result = _tick(service, {"BTCUSDT": _signal_series(NOW)})

    assert result.entries_placed == 1  # type: ignore[attr-defined]
    decision = _only(result)
    assert decision.outcome is DecisionOutcome.ENTERED  # type: ignore[attr-defined]
    assert decision.strategy_id == strategy_id  # type: ignore[attr-defined]
    assert decision.signal is True  # type: ignore[attr-defined]
    assert decision.quantity is not None and decision.quantity > 0  # type: ignore[attr-defined]
    assert decision.risk_reason is None  # type: ignore[attr-defined]
    # The rule that fired, with the numbers it fired on.
    assert decision.conditions and decision.conditions[0]["passed"]  # type: ignore[attr-defined]


# --------------------------- 3. risk rejection is distinguishable from "no signal"


def test_no_signal_and_risk_rejection_are_different_outcomes(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """Same symbol, same strategy, same data - only the risk verdict differs.

    Conflating these is the difference between "the market did nothing" and "the market
    did something and we could not act on it", which are opposite problems.
    """
    killswitch.initialise()
    _live(db)

    quiet = _tick(_service(db, audit, killswitch), {"BTCUSDT": _flat_series(NOW)})
    assert _only(quiet).outcome is DecisionOutcome.NO_SIGNAL  # type: ignore[attr-defined]
    assert _only(quiet).signal is False  # type: ignore[attr-defined]
    assert _only(quiet).risk_reason is None  # type: ignore[attr-defined]

    refused = _tick(
        _service(
            db,
            audit,
            killswitch,
            filters=StubFilterProvider(
                ExchangeFilters(
                    step_size=D("0.00000001"),
                    min_qty=D("0.00000001"),
                    min_notional=D("100000"),  # nothing this account can size reaches it
                    tick_size=D("0.01"),
                )
            ),
        ),
        {"BTCUSDT": _signal_series(NOW)},
    )
    decision = _only(refused)
    assert decision.outcome is DecisionOutcome.RISK_REJECTED  # type: ignore[attr-defined]
    assert decision.signal is True, "a signal existed; risk is what stopped it"  # type: ignore[attr-defined]
    assert decision.risk_reason == str(RejectReason.BELOW_MIN_NOTIONAL)  # type: ignore[attr-defined]


def test_a_no_signal_decision_carries_the_condition_truth_table(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """ATLAS strategies are rule-based, not scored, so the truth table is the score."""
    killswitch.initialise()
    _live(db)

    decision = _only(_tick(_service(db, audit, killswitch), {"BTCUSDT": _flat_series(NOW)}))

    rules = decision.conditions  # type: ignore[attr-defined]
    assert rules and rules[0]["passed"] is False
    condition = rules[0]["conditions"][0]
    assert condition["passed"] is False
    assert condition["left"] == "close"
    assert condition["left_value"] == "100"  # the actual number it was decided on
    assert condition["right_value"] == "120"  # the actual threshold


# -------------- 4. missing strategy / missing data differ from a normal no-signal


def test_missing_data_is_not_a_no_signal(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """A strategy is deployed and the market was simply not observable."""
    killswitch.initialise()
    _live(db)

    decision = _only(_tick(_service(db, audit, killswitch), {}))

    assert decision.outcome is DecisionOutcome.NO_MARKET_DATA  # type: ignore[attr-defined]
    assert decision.strategy_present  # type: ignore[attr-defined]
    assert decision.conditions == []  # type: ignore[attr-defined]


def test_the_three_families_are_distinguishable(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """Absence, unobservable market, and a real judgement never share an outcome."""
    assert not DecisionOutcome.NO_STRATEGY.evaluated_market
    assert not DecisionOutcome.NO_MARKET_DATA.evaluated_market
    assert DecisionOutcome.NO_SIGNAL.evaluated_market
    assert DecisionOutcome.RISK_REJECTED.evaluated_market
    assert DecisionOutcome.ENTERED.is_entry
    assert not DecisionOutcome.NO_SIGNAL.is_entry


def test_an_open_position_is_not_a_no_signal(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """RISK-08 declining a symbol must not read as the market being quiet."""
    killswitch.initialise()
    _live(db)

    decision = _only(
        _tick(
            _service(db, audit, killswitch),
            {"BTCUSDT": _signal_series(NOW)},
            account=_account(open_symbols=frozenset({"BTCUSDT"})),
        )
    )
    assert decision.outcome is DecisionOutcome.POSITION_ALREADY_OPEN  # type: ignore[attr-defined]


def test_a_live_strategy_on_an_unconfigured_symbol_is_reported(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """It would otherwise fetch no data and take no entry, silently forever."""
    killswitch.initialise()
    _live(db)

    result = _tick(_service(db, audit, killswitch), {}, symbols=["ETHUSDT"])

    outcomes = {d.symbol: d for d in result.decisions}  # type: ignore[attr-defined]
    assert outcomes["ETHUSDT"].outcome is DecisionOutcome.NO_STRATEGY
    assert outcomes["BTCUSDT"].outcome is DecisionOutcome.NO_MARKET_DATA
    assert "not in the configured universe" in outcomes["BTCUSDT"].detail


# ------------------------------------------- 5. safety gates behave exactly as before


def test_an_armed_switch_still_blocks_and_is_now_explained(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    killswitch.initialise()
    killswitch.arm(KillSwitchTrigger.MANUAL, "operator halted trading", "test")
    _live(db)

    result = _tick(_service(db, audit, killswitch), {"BTCUSDT": _signal_series(NOW)})

    assert result.halted and result.entries_placed == 0  # type: ignore[attr-defined]
    assert _only(result).outcome is DecisionOutcome.HALTED  # type: ignore[attr-defined]
    assert killswitch.is_armed(), "reporting must not disarm anything"


def test_stale_data_still_arms_the_switch(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """DATA-05 is unchanged; the decision now records that it fired."""
    killswitch.initialise()
    _live(db)

    result = _tick(
        _service(db, audit, killswitch),
        {"BTCUSDT": _signal_series(NOW - timedelta(hours=12))},
    )

    assert killswitch.is_armed()
    assert _only(result).outcome is DecisionOutcome.DATA_STALE  # type: ignore[attr-defined]


def test_reporting_never_places_an_order(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """Every no-entry outcome must leave the transport untouched."""
    killswitch.initialise()
    _live(db)
    transport = StubTransport(ack("entry"), oco_reply())

    _tick(_service(db, audit, killswitch, transport=transport), {"BTCUSDT": _flat_series(NOW)})

    assert transport.posts == []


# --------------------------------------------------------------- explain and render


def test_explain_reports_values_without_deciding_anything() -> None:
    """A read-only view of the same operands `evaluate` uses."""
    spec = percent_spec(threshold="120")
    series = _flat_series(NOW)

    rules = explain(spec, series.bars)

    assert len(rules) == 1
    assert not rules[0].passed
    assert len(rules[0].failed_conditions) == 1
    assert rules[0].conditions[0].left_value == D("100")
    assert "FAIL" in rules[0].conditions[0].render()


def test_explain_on_empty_bars_is_empty_not_an_error() -> None:
    assert explain(percent_spec(), []) == []


def test_render_states_that_there_is_no_regime_filter(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """An operator looking for the regime step learns there is none, rather than
    being left to wonder whether it silently rejected the trade."""
    killswitch.initialise()
    text = render_decisions(_tick(_service(db, audit, killswitch)).decisions)  # type: ignore[attr-defined]

    assert REGIME_NOT_IMPLEMENTED in text
    assert "NOT_IMPLEMENTED" in text


def test_render_with_no_symbols_is_not_a_crash() -> None:
    assert render_decisions([]) == "no symbols configured"


@pytest.mark.parametrize("outcome", list(DecisionOutcome))
def test_every_outcome_renders(outcome: DecisionOutcome) -> None:
    """A decision an operator cannot read is not observability."""
    from atlas.runtime.decisions import SymbolDecision

    lines = SymbolDecision(symbol="BTCUSDT", outcome=outcome, detail="x").render()
    assert lines and lines[0].startswith("BTCUSDT: ")
