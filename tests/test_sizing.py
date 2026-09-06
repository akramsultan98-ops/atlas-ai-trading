"""Specification section 6. Every value is an ATLAS decision, not David's."""

from __future__ import annotations

from decimal import Decimal

import pytest

from atlas.models import PositionSide
from atlas.risk.sizing import (
    ExchangeFilters,
    RejectReason,
    SizingPolicy,
    feasible_stop_band,
    size_position,
)

D = Decimal
POLICY = SizingPolicy(
    risk_pct=D("0.01"),
    max_position_pct=D("0.3333"),
    max_deployed_pct=D("0.75"),
    max_concurrent=3,
)
FILTERS = ExchangeFilters(
    step_size=D("0.00001"), min_qty=D("0.00001"), min_notional=D("5"), tick_size=D("0.01")
)


def size(entry: str, stop: str, equity: str = "100", **kw: object):
    return size_position(
        equity=D(equity),
        free_cash=D(kw.pop("free_cash", equity)),  # type: ignore[arg-type]
        entry_price=D(entry),
        stop_price=D(stop),
        side=kw.pop("side", PositionSide.LONG),  # type: ignore[arg-type]
        policy=POLICY,
        filters=FILTERS,
        **kw,  # type: ignore[arg-type]
    )


def test_five_percent_stop_risks_exactly_one_percent() -> None:
    """$100 equity, 1% risk, 5% stop -> $20 notional risking $1.00."""
    result = size("100", "95")
    assert result.accepted
    assert result.notional == pytest.approx(D("20"), rel=D("0.001"))
    assert result.risk_amount == pytest.approx(D("1"), rel=D("0.001"))
    assert not result.capped


def test_tight_stop_is_capped_and_under_risked() -> None:
    """A 1% stop implies $100 notional, above the 33.3% cap. Capping is safe."""
    result = size("100", "99")
    assert result.accepted
    assert result.capped
    assert result.notional == pytest.approx(D("33.33"), rel=D("0.01"))
    assert result.risk_amount < D("1")


def test_wide_stop_below_min_notional_is_rejected_not_rounded_up() -> None:
    """The core safety rule: never increase size to reach minNotional."""
    result = size("100", "75")  # 25% stop -> $4 notional, under the $5 minimum
    assert result.rejected
    assert result.reason is RejectReason.BELOW_MIN_NOTIONAL
    assert result.quantity == 0


def test_twenty_percent_stop_sits_exactly_on_min_notional() -> None:
    result = size("100", "80")
    assert result.accepted
    assert result.notional == pytest.approx(D("5"), rel=D("0.01"))


def test_stop_on_wrong_side_rejected_long() -> None:
    assert size("100", "105").reason is RejectReason.INVALID_LEVELS


def test_stop_on_wrong_side_rejected_short() -> None:
    result = size("100", "95", side=PositionSide.SHORT)
    assert result.reason is RejectReason.INVALID_LEVELS


def test_short_sizing_mirrors_long() -> None:
    result = size("100", "105", side=PositionSide.SHORT)
    assert result.accepted
    assert result.risk_amount == pytest.approx(D("1"), rel=D("0.001"))


def test_max_concurrent_enforced() -> None:
    assert size("100", "95", open_positions=3).reason is RejectReason.MAX_CONCURRENT
    assert size("100", "95", open_positions=2).accepted


def test_deployed_cap_enforced() -> None:
    """RISK-04: 75% deployed leaves no headroom for a fourth position."""
    result = size("100", "95", deployed=D("75"))
    assert result.reason is RejectReason.MAX_DEPLOYED


def test_deployed_headroom_truncates_size() -> None:
    result = size("100", "95", deployed=D("65"))
    assert result.accepted
    assert result.notional <= D("10")


def test_zero_free_cash_rejected() -> None:
    assert size("100", "95", free_cash="0").reason is RejectReason.NO_FREE_CASH


def test_quantity_floors_to_step_size() -> None:
    filters = ExchangeFilters(
        step_size=D("0.001"), min_qty=D("0.001"), min_notional=D("5"), tick_size=D("0.01")
    )
    result = size_position(
        equity=D("100"),
        free_cash=D("100"),
        entry_price=D("3.7"),
        stop_price=D("3.515"),
        side=PositionSide.LONG,
        policy=POLICY,
        filters=filters,
    )
    assert result.accepted
    assert result.quantity % D("0.001") == 0


def test_quantity_never_rounds_up() -> None:
    filters = ExchangeFilters(
        step_size=D("1"), min_qty=D("1"), min_notional=D("5"), tick_size=D("0.01")
    )
    result = size_position(
        equity=D("100"),
        free_cash=D("100"),
        entry_price=D("7"),
        stop_price=D("6.65"),
        side=PositionSide.LONG,
        policy=POLICY,
        filters=filters,
    )
    if result.accepted:
        assert result.notional <= D("100") * POLICY.max_position_pct


def test_risk_multiplier_halves_exposure() -> None:
    """PROM-03: a newly promoted strategy trades at reduced size."""
    full = size("100", "95")
    half = size("100", "95", risk_multiplier=D("0.5"))
    assert half.accepted
    assert half.notional == pytest.approx(full.notional / 2, rel=D("0.01"))


def test_invalid_equity_rejected() -> None:
    assert size("100", "95", equity="0").reason is RejectReason.INVALID_LEVELS


def test_feasible_band_matches_specification_table() -> None:
    """Specification section 6: 3.0% .. 20.0% at $100."""
    low, high = feasible_stop_band(D("100"), POLICY, FILTERS)
    assert low == pytest.approx(D("0.03"), rel=D("0.001"))
    assert high == pytest.approx(D("0.20"), rel=D("0.001"))


def test_lower_band_is_structural_and_equity_independent() -> None:
    """It is risk_pct / max_position_pct; equity cancels out of the derivation."""
    for equity in ("100", "250", "500", "1000"):
        low, _ = feasible_stop_band(D(equity), POLICY, FILTERS)
        assert low == pytest.approx(D("0.03"), rel=D("0.001"))


def test_upper_band_scales_with_equity() -> None:
    """This is where $100 actually bites; it relaxes as the account grows."""
    _, at_100 = feasible_stop_band(D("100"), POLICY, FILTERS)
    _, at_500 = feasible_stop_band(D("500"), POLICY, FILTERS)
    assert at_100 == pytest.approx(D("0.20"), rel=D("0.001"))
    assert at_500 == pytest.approx(D("1.00"), rel=D("0.001"))


@pytest.mark.parametrize("stop_pct", ["0.03", "0.05", "0.10", "0.15", "0.20"])
def test_every_stop_inside_the_band_is_accepted(stop_pct: str) -> None:
    entry = D("100")
    result = size_position(
        equity=D("100"),
        free_cash=D("100"),
        entry_price=entry,
        stop_price=entry * (1 - D(stop_pct)),
        side=PositionSide.LONG,
        policy=POLICY,
        filters=FILTERS,
    )
    assert result.accepted, f"stop {stop_pct} should size at $100"


@pytest.mark.parametrize("stop_pct", ["0.22", "0.25", "0.30", "0.50"])
def test_every_stop_above_the_band_is_rejected(stop_pct: str) -> None:
    entry = D("100")
    result = size_position(
        equity=D("100"),
        free_cash=D("100"),
        entry_price=entry,
        stop_price=entry * (1 - D(stop_pct)),
        side=PositionSide.LONG,
        policy=POLICY,
        filters=FILTERS,
    )
    assert result.rejected
    assert result.reason is RejectReason.BELOW_MIN_NOTIONAL


def test_never_exceeds_risk_budget_over_random_inputs() -> None:
    """Property: no accepted position may risk more than the budget allows."""
    import random

    random.seed(20260906)
    budget = D("100") * POLICY.risk_pct
    for _ in range(500):
        entry = D(str(round(random.uniform(0.5, 5000), 2)))
        stop_frac = D(str(round(random.uniform(0.005, 0.5), 4)))
        result = size_position(
            equity=D("100"),
            free_cash=D("100"),
            entry_price=entry,
            stop_price=entry * (1 - stop_frac),
            side=PositionSide.LONG,
            policy=POLICY,
            filters=FILTERS,
        )
        if result.accepted:
            assert result.risk_amount <= budget * D("1.001"), (
                f"entry={entry} stop_frac={stop_frac} risked {result.risk_amount}"
            )
            assert result.notional <= D("100") * POLICY.max_position_pct * D("1.001")
