"""ADR-006 (no financial defaults) and AI-01 (research plane holds no credentials)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from atlas.config import RiskSettings, Settings, load_settings
from atlas.errors import ConfigurationError
from atlas.models import Environment, ExchangeEnv


def test_risk_settings_load_from_env(risk_env: dict[str, str]) -> None:
    risk = RiskSettings()  # type: ignore[call-arg]
    assert risk.risk_pct == Decimal("0.01")
    assert risk.max_concurrent == 3


@pytest.mark.parametrize(
    "missing",
    [
        "ATLAS_RISK_PCT",
        "ATLAS_MAX_CONCURRENT",
        "ATLAS_MAX_POSITION_PCT",
        "ATLAS_MAX_DEPLOYED_PCT",
        "ATLAS_DAILY_LOSS_LIMIT",
        "ATLAS_MAX_ACCOUNT_DD",
    ],
)
def test_missing_risk_parameter_is_error(
    risk_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """ADR-006: absence must fail loudly, never fall back to a plausible number."""
    monkeypatch.delenv(missing)
    with pytest.raises(ValidationError):
        RiskSettings()  # type: ignore[call-arg]


def test_load_settings_raises_configuration_error_when_incomplete(clean_env: None) -> None:
    with pytest.raises(ConfigurationError):
        load_settings()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ATLAS_RISK_PCT", "0"),
        ("ATLAS_RISK_PCT", "1"),
        ("ATLAS_RISK_PCT", "-0.01"),
        ("ATLAS_MAX_POSITION_PCT", "1.5"),
        ("ATLAS_DAILY_LOSS_LIMIT", "0"),
        ("ATLAS_MAX_ACCOUNT_DD", "2"),
    ],
)
def test_fractions_must_be_between_zero_and_one(
    risk_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    monkeypatch.setenv(field, value)
    with pytest.raises(ValidationError):
        RiskSettings()  # type: ignore[call-arg]


def test_max_concurrent_must_be_positive(
    risk_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATLAS_MAX_CONCURRENT", "0")
    with pytest.raises(ValidationError):
        RiskSettings()  # type: ignore[call-arg]


def test_position_cap_may_not_exceed_deployed_cap(
    risk_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single position must be openable at its own cap."""
    monkeypatch.setenv("ATLAS_MAX_POSITION_PCT", "0.9")
    monkeypatch.setenv("ATLAS_MAX_DEPLOYED_PCT", "0.5")
    with pytest.raises(ValidationError):
        RiskSettings()  # type: ignore[call-arg]


def test_position_cap_times_concurrency_may_exceed_deployed_cap(
    risk_env: dict[str, str],
) -> None:
    """Not a contradiction: the caps bind at different levels.

    0.3333 x 3 = 0.9999 against a 0.75 deployed cap. Three positions cannot all open at
    full size; the aggregate cap is deliberately the tighter constraint.
    """
    risk = RiskSettings()  # type: ignore[call-arg]
    assert risk.max_position_pct * risk.max_concurrent > risk.max_deployed_pct


def test_min_feasible_stop_distance_is_structural(risk_env: dict[str, str]) -> None:
    """Specification section 6: d_min = risk_pct / max_position_pct = 3%.

    Equity cancels out of the derivation, so this bound does not move as the account
    grows. Tighter stops are permanently under-risked under this policy.
    """
    risk = RiskSettings()  # type: ignore[call-arg]
    assert risk.min_feasible_stop_distance == pytest.approx(Decimal("0.03"), rel=Decimal("0.001"))


def test_research_plane_has_no_credentials(risk_env: dict[str, str]) -> None:
    """AI-01: the values are absent, not merely forbidden by instruction."""
    settings = Settings(
        binance_api_key="key-material",  # type: ignore[arg-type]
        binance_api_secret="secret-material",  # type: ignore[arg-type]
    )
    assert settings.has_exchange_credentials()

    research = settings.for_research_plane()
    assert not research.has_exchange_credentials()
    assert research.binance_api_key is None
    assert research.binance_api_secret is None


def test_research_plane_copy_does_not_mutate_original(risk_env: dict[str, str]) -> None:
    settings = Settings(
        binance_api_key="key-material",  # type: ignore[arg-type]
        binance_api_secret="secret-material",  # type: ignore[arg-type]
    )
    settings.for_research_plane()
    assert settings.has_exchange_credentials()


def test_credentials_are_not_exposed_by_repr(risk_env: dict[str, str]) -> None:
    settings = Settings(binance_api_key="super-secret-key")  # type: ignore[arg-type]
    assert "super-secret-key" not in repr(settings)


def test_live_exchange_requires_production_env(risk_env: dict[str, str]) -> None:
    """Refuse to arm a live connection from development."""
    with pytest.raises(ValidationError):
        Settings(exchange_env=ExchangeEnv.LIVE, env=Environment.DEVELOPMENT)


def test_default_exchange_env_is_testnet(risk_env: dict[str, str]) -> None:
    assert Settings().exchange_env is ExchangeEnv.TESTNET
    assert not Settings().is_live
