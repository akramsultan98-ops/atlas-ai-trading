"""AtlasService construction and the operating loop (Phase E), plus safety gates."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from atlas.config import RiskSettings, Settings
from atlas.errors import ConfigurationError
from atlas.models import Environment, ExchangeEnv
from atlas.runtime.service import build_service

RISK_ENV = {
    "ATLAS_RISK_PCT": "0.01",
    "ATLAS_MAX_CONCURRENT": "3",
    "ATLAS_MAX_POSITION_PCT": "0.3333",
    "ATLAS_MAX_DEPLOYED_PCT": "0.75",
    "ATLAS_DAILY_LOSS_LIMIT": "0.05",
    "ATLAS_MAX_ACCOUNT_DD": "0.20",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for k, v in RISK_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path / "var"))
    monkeypatch.chdir(tmp_path)


def service(env: None, **overrides: Any):
    settings = Settings(**overrides)
    return build_service(settings, RiskSettings())  # type: ignore[call-arg]


def test_service_constructs_without_credentials(env: None) -> None:
    """The system must be inspectable before any key exists."""
    svc = service(env)
    try:
        assert svc.broker.base_url.endswith("binance.vision")
        assert not svc.settings.has_exchange_credentials()
        assert svc.notifier is None
    finally:
        svc.close()


def test_every_component_is_wired(env: None) -> None:
    """The audit found these existed but were unreachable. They must now be connected."""
    svc = service(env)
    try:
        for name in (
            "db",
            "audit",
            "killswitch",
            "broker",
            "klines",
            "store",
            "ledger",
            "ingestor",
            "trading",
            "registry",
        ):
            assert getattr(svc, name) is not None, f"{name} not wired"
    finally:
        svc.close()


def test_broker_has_a_real_transport(env: None) -> None:
    """Phase B: the audit's blocker was that no concrete transport existed."""
    from atlas.execution.broker import UrllibBrokerTransport

    svc = service(env)
    try:
        assert isinstance(svc.broker._transport, UrllibBrokerTransport)
    finally:
        svc.close()


def test_live_requires_production_environment(env: None) -> None:
    """A live connection cannot be armed from a development configuration."""
    with pytest.raises(ValidationError, match="requires env=production"):
        Settings(exchange_env=ExchangeEnv.LIVE, env=Environment.DEVELOPMENT)


def test_build_service_refuses_live_outside_production(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings()
    object.__setattr__(settings, "__dict__", {**settings.__dict__})
    forced = settings.model_copy(update={"exchange_env": ExchangeEnv.LIVE})
    with pytest.raises(ConfigurationError, match="production"):
        build_service(forced, RiskSettings())  # type: ignore[call-arg]


def test_testnet_is_the_default(env: None) -> None:
    svc = service(env)
    try:
        assert svc.settings.exchange_env is ExchangeEnv.TESTNET
        assert not svc.settings.is_live
    finally:
        svc.close()


def test_describe_contains_no_secret_material(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase Q: the startup summary is logged, so it must never carry a credential."""
    monkeypatch.setenv("ATLAS_BINANCE_API_KEY", "SECRET-KEY-VALUE")
    monkeypatch.setenv("ATLAS_BINANCE_API_SECRET", "SECRET-SECRET-VALUE")
    settings = Settings()
    summary = settings.describe()

    rendered = str(summary)
    assert "SECRET-KEY-VALUE" not in rendered
    assert "SECRET-SECRET-VALUE" not in rendered
    assert summary["exchange_credentials"] == "present"


def test_describe_reports_absent_credentials(env: None) -> None:
    assert Settings().describe()["exchange_credentials"] == "absent"


def test_symbol_list_parsing(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_SYMBOLS", "btcusdt, ethusdt ,")
    assert Settings().symbol_list == ["BTCUSDT", "ETHUSDT"]


def test_notifications_disabled_without_both_values(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATLAS_TELEGRAM_BOT_TOKEN", "token")
    assert not Settings().notifications_enabled(), "chat id missing"


def test_market_data_failure_is_survivable(env: None) -> None:
    """Phase N: an unreachable exchange must not crash the tick."""

    class Broken:
        def fetch(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("exchange unreachable")

    svc = service(env)
    try:
        svc.klines = Broken()  # type: ignore[assignment]
        assert svc.fetch_market_data() == {}, "failure yields no data, not an exception"
    finally:
        svc.close()


def test_invalid_series_is_rejected_not_traded(env: None) -> None:
    """DATA-03: a gapped series must be dropped, never repaired."""
    from tests.test_data_models import make_bar

    from atlas.data.models import KlineSeries, Timeframe

    gapped = KlineSeries(
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        bars=tuple(make_bar(i) for i in (0, 1, 9)),
    )

    class Gapped:
        def fetch(self, *a: Any, **k: Any) -> KlineSeries:
            return gapped

    svc = service(env)
    try:
        svc.klines = Gapped()  # type: ignore[assignment]
        assert svc.fetch_market_data() == {}, "invalid data must not reach the evaluator"
    finally:
        svc.close()


def test_notify_without_a_notifier_is_a_no_op(env: None) -> None:
    """A missing Telegram config must never stop trading or retirement."""
    from atlas.notify.telegram import Severity

    svc = service(env)
    try:
        svc.notify(Severity.CRITICAL, "t", "b")  # must not raise
    finally:
        svc.close()
