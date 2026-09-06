"""Phase 11: alerting, health checks and the control panel."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from tests.test_strategy_spec import make_spec

from atlas.data.models import Timeframe
from atlas.db.engine import Database
from atlas.models import ExchangeEnv, KillSwitchTrigger, StrategyStatus
from atlas.notify import telegram
from atlas.notify.telegram import Alert, Severity, TelegramNotifier
from atlas.ops.dashboard import Dashboard
from atlas.ops.health import check_system_health
from atlas.strategy.registry import StrategyRegistry

D = Decimal
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


class StubNotifyTransport:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail = fail

    def post(self, url: str, payload: dict[str, Any]) -> Any:
        if self.fail:
            raise RuntimeError("telegram unreachable")
        self.sent.append(payload)
        return {"ok": True}


# ------------------------------------------------------------------- alerting


def test_alert_names_its_environment() -> None:
    """Reading a testnet alert as live is a 3am mistake worth designing out."""
    rendered = Alert(
        Severity.CRITICAL, "Kill switch armed", "daily loss limit", ExchangeEnv.TESTNET
    ).render()
    assert "testnet" in rendered
    assert "Kill switch armed" in rendered


def test_live_alerts_are_marked_live() -> None:
    rendered = Alert(Severity.INFO, "t", "b", ExchangeEnv.LIVE).render()
    assert "live" in rendered


def test_notifier_sends() -> None:
    transport = StubNotifyTransport()
    notifier = TelegramNotifier("token", "chat", transport)
    assert notifier.send(Alert(Severity.INFO, "t", "b", ExchangeEnv.TESTNET))
    assert transport.sent[0]["chat_id"] == "chat"


def test_failed_delivery_never_raises() -> None:
    """A failed alert must not stop trading or retirement."""
    notifier = TelegramNotifier("token", "chat", StubNotifyTransport(fail=True))
    assert notifier.send(Alert(Severity.CRITICAL, "t", "b", ExchangeEnv.TESTNET)) is False


def test_notifier_has_no_inbound_path() -> None:
    """OPS: an inbound channel would be an unauthenticated path into the control plane."""
    methods = {name for name, _ in inspect.getmembers(TelegramNotifier, inspect.isfunction)}
    for forbidden in (
        "receive",
        "poll",
        "handle_update",
        "on_message",
        "get_updates",
        "listen",
        "webhook",
    ):
        assert forbidden not in methods

    source = inspect.getsource(telegram)
    for forbidden in ("getUpdates", "setWebhook", "handle_command"):
        assert forbidden not in source


# ---------------------------------------------------------------- health checks


def test_healthy_system_reports_no_issues() -> None:
    report = check_system_health(
        last_bar_close=NOW - timedelta(minutes=5),
        timeframe=Timeframe.H1,
        last_heartbeat=NOW - timedelta(minutes=1),
        api_error_rate=D("0.01"),
        stuck_order_count=0,
        now=NOW,
    )
    assert report.healthy
    assert report.kill_switch_triggers == []


def test_stale_data_triggers_the_kill_switch() -> None:
    """DATA-05."""
    report = check_system_health(
        last_bar_close=NOW - timedelta(hours=5),
        timeframe=Timeframe.H1,
        last_heartbeat=NOW,
        api_error_rate=D("0"),
        stuck_order_count=0,
        now=NOW,
    )
    assert not report.healthy
    assert KillSwitchTrigger.DATA_STALENESS in report.kill_switch_triggers


def test_no_data_at_all_triggers_the_kill_switch() -> None:
    report = check_system_health(
        last_bar_close=None,
        timeframe=Timeframe.H1,
        last_heartbeat=NOW,
        api_error_rate=D("0"),
        stuck_order_count=0,
        now=NOW,
    )
    assert KillSwitchTrigger.DATA_STALENESS in report.kill_switch_triggers


def test_api_error_rate_triggers_the_kill_switch() -> None:
    """KILL-02."""
    report = check_system_health(
        last_bar_close=NOW,
        timeframe=Timeframe.H1,
        last_heartbeat=NOW,
        api_error_rate=D("0.60"),
        stuck_order_count=0,
        now=NOW,
    )
    assert KillSwitchTrigger.API_ERROR_RATE in report.kill_switch_triggers


def test_missing_heartbeat_is_reported_without_arming() -> None:
    report = check_system_health(
        last_bar_close=NOW,
        timeframe=Timeframe.H1,
        last_heartbeat=None,
        api_error_rate=D("0"),
        stuck_order_count=0,
        now=NOW,
    )
    assert not report.healthy
    assert report.kill_switch_triggers == []


def test_stuck_orders_are_reported() -> None:
    report = check_system_health(
        last_bar_close=NOW,
        timeframe=Timeframe.H1,
        last_heartbeat=NOW,
        api_error_rate=D("0"),
        stuck_order_count=3,
        now=NOW,
    )
    assert any(i.check == "orders" for i in report.issues)


def test_all_issues_reported_together() -> None:
    report = check_system_health(
        last_bar_close=None,
        timeframe=Timeframe.H1,
        last_heartbeat=None,
        api_error_rate=D("0.9"),
        stuck_order_count=2,
        now=NOW,
    )
    assert len(report.issues) == 4


# ------------------------------------------------------------------ dashboard


def _register(db: Database, period: int, status: StrategyStatus) -> str:
    from atlas.strategy.spec import IndicatorSpec

    registry = StrategyRegistry(db)
    strategy_id = registry.register(
        make_spec(
            indicators=(
                IndicatorSpec(id="fast", name="ema", period=period),
                IndicatorSpec(id="slow", name="ema", period=26),
                IndicatorSpec(id="atr14", name="atr", period=14),
            )
        )
    )
    if status is not StrategyStatus.CANDIDATE:
        registry.set_status(strategy_id, status, reason="test")
    return strategy_id


def test_dashboard_lists_strategies(db: Database) -> None:
    _register(db, 12, StrategyStatus.CANDIDATE)
    _register(db, 13, StrategyStatus.LIVE)
    rows = Dashboard(db).strategies()
    assert len(rows) == 2
    assert all(r.symbol == "BTCUSDT" for r in rows)


def test_dashboard_filters_by_status(db: Database) -> None:
    _register(db, 12, StrategyStatus.CANDIDATE)
    live_id = _register(db, 13, StrategyStatus.LIVE)
    rows = Dashboard(db).strategies(StrategyStatus.LIVE)
    assert [r.strategy_id for r in rows] == [live_id]


def test_dashboard_surfaces_retirement_reason(db: Database) -> None:
    _register(db, 14, StrategyStatus.RETIRED)
    row = Dashboard(db).strategies(StrategyStatus.RETIRED)[0]
    assert row.retired_at is not None
    assert row.retire_reason == "test"


def test_funnel_counts_every_status(db: Database) -> None:
    _register(db, 12, StrategyStatus.CANDIDATE)
    _register(db, 13, StrategyStatus.LIVE)
    _register(db, 14, StrategyStatus.RETIRED)
    funnel = Dashboard(db).funnel()
    assert funnel["CANDIDATE"] == 1
    assert funnel["LIVE"] == 1
    assert funnel["RETIRED"] == 1
    assert funnel["INCUBATING"] == 0


def test_summary_reports_survival_rate(db: Database) -> None:
    """The source's own base rate: 99% of candidates never make it [22:36-22:53]."""
    for i in range(9):
        _register(db, 30 + i, StrategyStatus.REJECTED)
    _register(db, 12, StrategyStatus.LIVE)
    summary = Dashboard(db).summary()
    assert summary["total_candidates"] == 10
    assert summary["live"] == 1
    assert summary["survival_rate"] == 0.1


def test_summary_on_empty_database(db: Database) -> None:
    summary = Dashboard(db).summary()
    assert summary["total_candidates"] == 0
    assert summary["survival_rate"] == 0.0
