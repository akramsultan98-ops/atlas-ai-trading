"""Economic provenance (Phase R): assumptions must never pass as exchange facts."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from atlas.economics import (
    DEFAULT_ASSUMED_PROFILE,
    EconomicProfile,
    EconomicValue,
    Provenance,
    observe_filters,
)

D = Decimal
NOW = datetime(2026, 9, 6, tzinfo=UTC)


def test_current_profile_is_entirely_assumed() -> None:
    """Nothing in ATLAS has ever contacted Binance, so nothing may be OBSERVED."""
    assert not DEFAULT_ASSUMED_PROFILE.is_exchange_verified
    assert DEFAULT_ASSUMED_PROFILE.observations == ()
    assert len(DEFAULT_ASSUMED_PROFILE.assumptions) == 5


def test_every_assumption_names_why_it_is_unverified() -> None:
    for value in DEFAULT_ASSUMED_PROFILE.assumptions:
        assert value.source, f"{value.name} has no stated source"


def test_warnings_name_each_assumption() -> None:
    warnings = DEFAULT_ASSUMED_PROFILE.warnings()
    assert len(warnings) == 5
    assert all("ASSUMPTION" in w for w in warnings)
    assert any("min_notional" in w for w in warnings)


def test_an_assumption_is_not_trustworthy() -> None:
    assert not DEFAULT_ASSUMED_PROFILE.values[0].is_trustworthy


def test_observed_value_requires_a_timestamp() -> None:
    """An observation with no time is indistinguishable from a guess."""
    with pytest.raises(ValueError, match="when it was observed"):
        EconomicValue("fee_rate", D("0.001"), Provenance.OBSERVED, "exchangeInfo")


def test_observe_filters_promotes_only_the_observable() -> None:
    profile = observe_filters(
        min_notional=D("10"),
        step_size=D("0.001"),
        tick_size=D("0.01"),
        observed_at=NOW,
        symbol="BTCUSDT",
    )
    assert {v.name for v in profile.observations} == {"min_notional", "step_size", "tick_size"}
    # exchangeInfo does not carry the account's fee tier, so fees stay assumed.
    assert {v.name for v in profile.assumptions} == {"fee_rate", "slippage_rate"}


def test_profile_is_not_verified_while_any_assumption_remains() -> None:
    """One unverified input is enough. Partial observation is not validation."""
    profile = observe_filters(
        min_notional=D("10"),
        step_size=D("0.001"),
        tick_size=D("0.01"),
        observed_at=NOW,
        symbol="BTCUSDT",
    )
    assert not profile.is_exchange_verified


def test_fully_observed_profile_is_verified() -> None:
    profile = EconomicProfile(
        values=tuple(
            EconomicValue(name, D("1"), Provenance.OBSERVED, "exchange", NOW)
            for name in ("fee_rate", "slippage_rate", "min_notional")
        )
    )
    assert profile.is_exchange_verified
    assert profile.warnings() == []


def test_empty_profile_is_not_verified() -> None:
    """Knowing nothing is not the same as having verified everything."""
    assert not EconomicProfile().is_exchange_verified


def test_fixtures_are_flagged_as_test_only() -> None:
    profile = EconomicProfile(
        values=(EconomicValue("min_notional", D("5"), Provenance.FIXTURE, "test_backtest.py"),)
    )
    assert not profile.is_exchange_verified
    assert "not valid outside a test" in profile.warnings()[0]


def test_observed_value_records_its_symbol_and_time() -> None:
    profile = observe_filters(
        min_notional=D("10"),
        step_size=D("0.001"),
        tick_size=D("0.01"),
        observed_at=NOW,
        symbol="ETHUSDT",
    )
    value = profile.get("min_notional")
    assert value is not None
    assert value.observed_at == NOW
    assert "ETHUSDT" in value.source


def test_serialises_for_a_report() -> None:
    payload = DEFAULT_ASSUMED_PROFILE.as_dict()
    assert payload["exchange_verified"] is False
    assert len(payload["values"]) == 5
    assert len(payload["warnings"]) == 5
