"""Runtime orchestration: scheduler, restart recovery, and the trading tick."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from tests.conftest import StubFilterProvider
from tests.test_backtest import POLICY, series_from
from tests.test_evaluator import percent_spec
from tests.test_execution import StubTransport, ack, oco_reply

from atlas.audit import AuditLog
from atlas.data.models import KlineSeries, Timeframe
from atlas.db.engine import Database
from atlas.execution.broker import BinanceSpotBroker
from atlas.killswitch import KillSwitch
from atlas.models import ExchangeEnv, KillSwitchTrigger, StrategyStatus
from atlas.risk.limits import AccountState, PortfolioLimits
from atlas.runtime.recovery import recover
from atlas.runtime.scheduler import IntervalScheduler
from atlas.runtime.trading_service import TradingService
from atlas.strategy.registry import StrategyRegistry

D = Decimal
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.slept: list[float] = []
        self._now = NOW

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self._now += timedelta(seconds=seconds)


# ------------------------------------------------------------------- scheduler


def test_scheduler_runs_the_requested_number_of_ticks() -> None:
    calls = {"n": 0}

    def tick() -> None:
        calls["n"] += 1

    report = IntervalScheduler(60, FakeClock()).run(tick, max_ticks=5)
    assert calls["n"] == 5
    assert report.ticks == 5
    assert report.failures == 0


def test_a_failing_tick_does_not_stop_the_loop() -> None:
    """An unattended system that exits on the first transient error is not unattended."""
    calls = {"n": 0}

    def tick() -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("transient")

    report = IntervalScheduler(60, FakeClock()).run(tick, max_ticks=4)
    assert calls["n"] == 4
    assert report.failures == 1
    assert "RuntimeError: transient" in report.errors[0]


def test_sustained_failure_stops_the_loop() -> None:
    """A structural fault should stop, not fill the log forever."""

    def tick() -> None:
        raise RuntimeError("broken")

    report = IntervalScheduler(60, FakeClock()).run(
        tick, max_ticks=100, stop_after_consecutive_failures=3
    )
    assert report.ticks == 3


def test_a_success_resets_the_failure_streak() -> None:
    calls = {"n": 0}

    def tick() -> None:
        calls["n"] += 1
        if calls["n"] != 3:
            raise RuntimeError("flaky")

    report = IntervalScheduler(60, FakeClock()).run(
        tick, max_ticks=6, stop_after_consecutive_failures=3
    )
    assert report.ticks == 6


def test_scheduler_sleeps_the_interval() -> None:
    clock = FakeClock()
    IntervalScheduler(900, clock).run(lambda: None, max_ticks=3)
    assert clock.slept == [900, 900, 900]


def test_non_positive_interval_rejected() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        IntervalScheduler(0)


# -------------------------------------------------------------------- recovery


def test_clean_recovery_permits_entries(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    killswitch.initialise()
    report = recover(db, audit, killswitch, [])
    assert not report.kill_switch_armed
    assert report.reconciled
    assert report.may_resume_entries


def test_armed_switch_blocks_resumption(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    killswitch.initialise()
    killswitch.arm(KillSwitchTrigger.MANUAL, "operator halted before restart")
    report = recover(db, audit, killswitch, [])
    assert report.kill_switch_armed
    assert not report.may_resume_entries


def test_uninitialised_system_does_not_resume(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """KILL-06: a system never deliberately released to trade stays halted."""
    report = recover(db, audit, killswitch, [])
    assert report.kill_switch_armed
    assert not report.may_resume_entries


def test_unreconcilable_order_blocks_resumption(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """EXEC-05: unrecorded exposure stops the restart."""
    killswitch.initialise()
    report = recover(db, audit, killswitch, [{"clientOrderId": "atlasGHOST", "status": "NEW"}])
    assert not report.reconciled
    assert report.kill_switch_armed
    assert not report.may_resume_entries


def test_recovery_is_audited(db: Database, audit: AuditLog, killswitch: KillSwitch) -> None:
    killswitch.initialise()
    recover(db, audit, killswitch, [])
    events = [e for e in audit.tail(10) if e.payload.get("event") == "restart_recovery"]
    assert events
    assert audit.verify_chain() > 0


# ------------------------------------------------------------- trading service


def _account(equity: str = "100", peak: str = "100", day_start: str = "100") -> AccountState:
    return AccountState(
        equity=D(equity),
        peak_equity=D(peak),
        day_start_equity=D(day_start),
        as_of=date(2026, 9, 6),
        free_cash=D(equity),
    )


def _live_strategy(db: Database) -> str:
    registry = StrategyRegistry(db)
    strategy_id = registry.register(percent_spec(threshold="120"))
    registry.set_status(strategy_id, StrategyStatus.LIVE)
    return strategy_id


def _service(
    db: Database,
    audit: AuditLog,
    killswitch: KillSwitch,
    transport: StubTransport,
    filters: StubFilterProvider | None = None,
) -> TradingService:
    broker = BinanceSpotBroker(
        "k", "s", killswitch, audit, exchange_env=ExchangeEnv.TESTNET, transport=transport
    )
    return TradingService(
        db,
        audit,
        killswitch,
        broker,
        policy=POLICY,
        filters=filters or StubFilterProvider(),
        limits=PortfolioLimits(),
    )


def _signal_series(last_close: datetime) -> KlineSeries:
    """A series whose final bar crosses the entry threshold."""
    rows = [("100", "101", "99", "100"), ("100", "101", "99", "100"), ("100", "130", "99", "125")]
    series = series_from(rows)
    shift = last_close - series.bars[-1].close_time
    from atlas.data.models import Kline

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


def test_tick_places_a_bracketed_entry(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    killswitch.initialise()
    _live_strategy(db)
    transport = StubTransport(ack("entry"), oco_reply())
    service = _service(db, audit, killswitch, transport)

    result = service.tick(
        account=_account(),
        series_by_symbol={"BTCUSDT": _signal_series(NOW)},
        exchange_orders=[],
        live_returns={},
        backtest_stats={},
        now=NOW,
    )
    assert not result.halted
    assert result.entries_placed == 1
    assert len(transport.posts) == 2, "entry plus its protective stop"


def test_daily_loss_breach_halts_before_any_entry(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """RISK-05: a breached account stops before it adds risk."""
    killswitch.initialise()
    _live_strategy(db)
    transport = StubTransport(ack("entry"), oco_reply())
    service = _service(db, audit, killswitch, transport)

    result = service.tick(
        account=_account(equity="90", day_start="100"),
        series_by_symbol={"BTCUSDT": _signal_series(NOW)},
        exchange_orders=[],
        live_returns={},
        backtest_stats={},
        now=NOW,
    )
    assert result.halted
    assert result.entries_placed == 0
    assert transport.posts == [], "nothing reached the exchange"
    assert killswitch.read_state().trigger is KillSwitchTrigger.DAILY_LOSS_LIMIT


def test_drawdown_breach_halts(db: Database, audit: AuditLog, killswitch: KillSwitch) -> None:
    killswitch.initialise()
    _live_strategy(db)
    service = _service(db, audit, killswitch, StubTransport())
    result = service.tick(
        account=_account(equity="75", peak="100", day_start="76"),
        series_by_symbol={"BTCUSDT": _signal_series(NOW)},
        exchange_orders=[],
        live_returns={},
        backtest_stats={},
        now=NOW,
    )
    assert result.halted
    assert killswitch.read_state().trigger is KillSwitchTrigger.MAX_ACCOUNT_DD


def test_stale_data_halts_and_arms(db: Database, audit: AuditLog, killswitch: KillSwitch) -> None:
    """DATA-05: the last bar is five hours old on an hourly timeframe."""
    killswitch.initialise()
    _live_strategy(db)
    service = _service(db, audit, killswitch, StubTransport())
    result = service.tick(
        account=_account(),
        series_by_symbol={"BTCUSDT": _signal_series(NOW - timedelta(hours=5))},
        exchange_orders=[],
        live_returns={},
        backtest_stats={},
        now=NOW,
    )
    assert result.halted
    assert killswitch.read_state().trigger is KillSwitchTrigger.DATA_STALENESS


def test_no_duplicate_position_per_symbol(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """RISK-08."""
    killswitch.initialise()
    _live_strategy(db)
    transport = StubTransport(ack("entry"), oco_reply())
    service = _service(db, audit, killswitch, transport)

    account = AccountState(
        equity=D("100"),
        peak_equity=D("100"),
        day_start_equity=D("100"),
        as_of=date(2026, 9, 6),
        free_cash=D("100"),
        open_symbols=frozenset({"BTCUSDT"}),
    )
    result = service.tick(
        account=account,
        series_by_symbol={"BTCUSDT": _signal_series(NOW)},
        exchange_orders=[],
        live_returns={},
        backtest_stats={},
        now=NOW,
    )
    assert result.entries_placed == 0
    assert transport.posts == []


def test_retirement_still_runs_while_halted(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """Retirement reduces exposure, so an armed switch must not block it."""
    killswitch.initialise()
    strategy_id = _live_strategy(db)
    killswitch.arm(KillSwitchTrigger.MANUAL, "halted")
    service = _service(db, audit, killswitch, StubTransport())

    result = service.tick(
        account=_account(),
        series_by_symbol={"BTCUSDT": _signal_series(NOW)},
        exchange_orders=[],
        live_returns={strategy_id: [D("-0.08")] * 40},
        backtest_stats={strategy_id: (D("0.03"), D("0.05"), D("0.55"))},
        now=NOW,
    )
    assert result.halted
    assert strategy_id in result.retired
    assert StrategyRegistry(db).status(strategy_id) is StrategyStatus.RETIRED


def test_retired_strategy_takes_no_further_entries(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    killswitch.initialise()
    strategy_id = _live_strategy(db)
    transport = StubTransport(ack("entry"), oco_reply())
    service = _service(db, audit, killswitch, transport)

    collapsing: dict[str, Any] = {strategy_id: [D("-0.08")] * 40}
    baseline: dict[str, Any] = {strategy_id: (D("0.03"), D("0.05"), D("0.55"))}

    first = service.tick(
        account=_account(),
        series_by_symbol={"BTCUSDT": _signal_series(NOW)},
        exchange_orders=[],
        live_returns=collapsing,
        backtest_stats=baseline,
        now=NOW,
    )
    assert strategy_id in first.retired

    transport.posts.clear()
    second = service.tick(
        account=_account(),
        series_by_symbol={"BTCUSDT": _signal_series(NOW)},
        exchange_orders=[],
        live_returns=collapsing,
        backtest_stats=baseline,
        now=NOW,
    )
    assert second.entries_placed == 0
    assert transport.posts == []


def test_missing_data_is_a_note_not_a_crash(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    killswitch.initialise()
    _live_strategy(db)
    service = _service(db, audit, killswitch, StubTransport())
    result = service.tick(
        account=_account(),
        series_by_symbol={},
        exchange_orders=[],
        live_returns={},
        backtest_stats={},
        now=NOW,
    )
    assert not result.halted
    assert any("no data" in n for n in result.notes)


def test_every_tick_is_audited(db: Database, audit: AuditLog, killswitch: KillSwitch) -> None:
    killswitch.initialise()
    service = _service(db, audit, killswitch, StubTransport())
    service.tick(
        account=_account(),
        series_by_symbol={},
        exchange_orders=[],
        live_returns={},
        backtest_stats={},
        now=NOW,
    )
    assert any(e.payload.get("event") == "trading_tick" for e in audit.tail(10))
    assert audit.verify_chain() > 0
