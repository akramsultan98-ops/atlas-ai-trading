"""Position sizing (specification section 6).

Every value here is an ATLAS decision. The source video supplies no sizing policy at
all; its only reference is "percentage of portfolio" in passing [17:30].

The rejection rules carry the safety. Rounding a position *up* to satisfy minNotional
silently exceeds the risk budget, so an unfillable trade is refused instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

from atlas.models import PositionSide


class RejectReason(StrEnum):
    NONE = "NONE"
    INVALID_LEVELS = "INVALID_LEVELS"
    BELOW_MIN_NOTIONAL = "BELOW_MIN_NOTIONAL"
    BELOW_MIN_QTY = "BELOW_MIN_QTY"
    NO_FREE_CASH = "NO_FREE_CASH"
    MAX_CONCURRENT = "MAX_CONCURRENT"
    MAX_DEPLOYED = "MAX_DEPLOYED"
    ZERO_QUANTITY = "ZERO_QUANTITY"


@dataclass(frozen=True)
class ExchangeFilters:
    """Binance symbol filters (RISK-09).

    Read from `exchangeInfo` at runtime and refreshed daily. Never hardcoded: the real
    values differ per symbol and change without notice.
    """

    step_size: Decimal = Decimal("0.00001")
    min_qty: Decimal = Decimal("0.00001")
    min_notional: Decimal = Decimal("5")
    tick_size: Decimal = Decimal("0.01")

    def floor_quantity(self, quantity: Decimal) -> Decimal:
        """Round *down* to the step size. Never up: up increases risk."""
        if self.step_size <= 0:
            return quantity
        return (quantity / self.step_size).to_integral_value(rounding=ROUND_DOWN) * self.step_size

    def round_price(self, price: Decimal) -> Decimal:
        if self.tick_size <= 0:
            return price
        return (price / self.tick_size).to_integral_value(rounding=ROUND_DOWN) * self.tick_size


@dataclass(frozen=True)
class SizingPolicy:
    """Risk policy inputs (RISK-01..04)."""

    risk_pct: Decimal
    max_position_pct: Decimal
    max_deployed_pct: Decimal
    max_concurrent: int

    @property
    def min_feasible_stop_distance(self) -> Decimal:
        """d_min = risk_pct / max_position_pct. Structural: independent of equity."""
        return self.risk_pct / self.max_position_pct

    def max_feasible_stop_distance(self, equity: Decimal, min_notional: Decimal) -> Decimal:
        """d_max = (equity * risk_pct) / minNotional. Scales with equity."""
        if min_notional <= 0:
            return Decimal(1)
        return (equity * self.risk_pct) / min_notional


@dataclass(frozen=True)
class SizingResult:
    accepted: bool
    quantity: Decimal
    notional: Decimal
    risk_amount: Decimal
    reason: RejectReason
    capped: bool = False

    @property
    def rejected(self) -> bool:
        return not self.accepted


def _reject(reason: RejectReason) -> SizingResult:
    return SizingResult(
        accepted=False,
        quantity=Decimal(0),
        notional=Decimal(0),
        risk_amount=Decimal(0),
        reason=reason,
    )


def size_position(
    *,
    equity: Decimal,
    free_cash: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
    side: PositionSide,
    policy: SizingPolicy,
    filters: ExchangeFilters,
    open_positions: int = 0,
    deployed: Decimal = Decimal(0),
    risk_multiplier: Decimal = Decimal(1),
) -> SizingResult:
    """Size one position, or reject it.

    `risk_multiplier` implements PROM-03's reduced allocation for a newly promoted
    strategy's first trades.
    """
    if entry_price <= 0 or stop_price <= 0 or equity <= 0:
        return _reject(RejectReason.INVALID_LEVELS)
    if side is PositionSide.LONG and stop_price >= entry_price:
        return _reject(RejectReason.INVALID_LEVELS)
    if side is PositionSide.SHORT and stop_price <= entry_price:
        return _reject(RejectReason.INVALID_LEVELS)
    if open_positions >= policy.max_concurrent:
        return _reject(RejectReason.MAX_CONCURRENT)
    if free_cash <= 0:
        return _reject(RejectReason.NO_FREE_CASH)

    stop_distance = abs(entry_price - stop_price) / entry_price
    risk_budget = equity * policy.risk_pct * risk_multiplier

    notional_by_risk = risk_budget / stop_distance
    notional_cap = equity * policy.max_position_pct
    deployed_headroom = equity * policy.max_deployed_pct - deployed
    if deployed_headroom <= 0:
        return _reject(RejectReason.MAX_DEPLOYED)

    notional = min(notional_by_risk, notional_cap, free_cash, deployed_headroom)
    capped = notional < notional_by_risk

    quantity = filters.floor_quantity(notional / entry_price)
    if quantity <= 0:
        return _reject(RejectReason.ZERO_QUANTITY)
    if quantity < filters.min_qty:
        return _reject(RejectReason.BELOW_MIN_QTY)

    actual_notional = quantity * entry_price
    # Reject rather than round up: reaching minNotional by increasing size would
    # silently exceed the risk budget the policy just computed.
    if actual_notional < filters.min_notional:
        return _reject(RejectReason.BELOW_MIN_NOTIONAL)

    return SizingResult(
        accepted=True,
        quantity=quantity,
        notional=actual_notional,
        risk_amount=actual_notional * stop_distance,
        reason=RejectReason.NONE,
        capped=capped,
    )


def feasible_stop_band(
    equity: Decimal, policy: SizingPolicy, filters: ExchangeFilters
) -> tuple[Decimal, Decimal]:
    """The stop-distance range that sizes at full intended risk (SEL-07).

    Below the lower bound the position cap binds and the trade is under-risked; above
    the upper bound the position falls under minNotional and is refused.
    """
    return (
        policy.min_feasible_stop_distance,
        policy.max_feasible_stop_distance(equity, filters.min_notional),
    )
