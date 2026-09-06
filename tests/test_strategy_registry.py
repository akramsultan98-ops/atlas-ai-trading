"""STRAT-06 versioning and MON-06 one-way retirement."""

from __future__ import annotations

import pytest
from tests.test_strategy_spec import make_spec

from atlas.db.engine import Database
from atlas.models import StrategyStatus
from atlas.strategy.registry import StrategyRegistry


def test_register_returns_id_and_persists_spec(db: Database) -> None:
    registry = StrategyRegistry(db)
    spec = make_spec()
    strategy_id = registry.register(spec)

    loaded = registry.load_spec(strategy_id)
    assert loaded is not None
    assert loaded.content_hash() == spec.content_hash()
    assert registry.status(strategy_id) is StrategyStatus.CANDIDATE


def test_registering_identical_spec_is_idempotent(db: Database) -> None:
    """Identical rules are the same strategy; re-registering must not fork its evidence."""
    registry = StrategyRegistry(db)
    first = registry.register(make_spec())
    second = registry.register(make_spec())
    assert first == second


def test_different_spec_creates_different_strategy(db: Database) -> None:
    from atlas.strategy.spec import IndicatorSpec

    registry = StrategyRegistry(db)
    first = registry.register(make_spec())
    second = registry.register(
        make_spec(
            indicators=(
                IndicatorSpec(id="fast", name="ema", period=9),
                IndicatorSpec(id="slow", name="ema", period=26),
                IndicatorSpec(id="atr14", name="atr", period=14),
            )
        )
    )
    assert first != second


def test_status_transitions(db: Database) -> None:
    registry = StrategyRegistry(db)
    strategy_id = registry.register(make_spec())
    for status in (StrategyStatus.VERIFIED, StrategyStatus.INCUBATING, StrategyStatus.LIVE):
        registry.set_status(strategy_id, status)
        assert registry.status(strategy_id) is status


def test_retirement_is_one_way(db: Database) -> None:
    """MON-06: reactivation is a human action outside the trading loop."""
    registry = StrategyRegistry(db)
    strategy_id = registry.register(make_spec())
    registry.set_status(strategy_id, StrategyStatus.LIVE)
    registry.set_status(strategy_id, StrategyStatus.RETIRED, reason="equity band break")

    with pytest.raises(ValueError, match="RETIRED"):
        registry.set_status(strategy_id, StrategyStatus.LIVE)
    assert registry.status(strategy_id) is StrategyStatus.RETIRED


def test_retirement_records_reason_and_timestamp(db: Database) -> None:
    registry = StrategyRegistry(db)
    strategy_id = registry.register(make_spec())
    registry.set_status(strategy_id, StrategyStatus.RETIRED, reason="rolling PF below 1.0")

    row = db.connection.execute(
        "SELECT retired_at, retire_reason FROM strategies WHERE id = ?", (strategy_id,)
    ).fetchone()
    assert row["retired_at"] is not None
    assert row["retire_reason"] == "rolling PF below 1.0"


def test_list_by_status(db: Database) -> None:
    from atlas.strategy.spec import IndicatorSpec

    registry = StrategyRegistry(db)
    a = registry.register(make_spec())
    b = registry.register(
        make_spec(
            indicators=(
                IndicatorSpec(id="fast", name="sma", period=5),
                IndicatorSpec(id="slow", name="ema", period=26),
                IndicatorSpec(id="atr14", name="atr", period=14),
            )
        )
    )
    registry.set_status(b, StrategyStatus.LIVE)
    assert registry.list_by_status(StrategyStatus.CANDIDATE) == [a]
    assert registry.list_by_status(StrategyStatus.LIVE) == [b]


def test_load_spec_for_unknown_strategy_is_none(db: Database) -> None:
    assert StrategyRegistry(db).load_spec("does-not-exist") is None
