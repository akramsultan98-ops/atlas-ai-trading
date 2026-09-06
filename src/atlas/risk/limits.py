"""Portfolio-level risk limits (RISK-05, RISK-06, RISK-08) and their kill-switch
triggers (KILL-02).

Per-trade sizing bounds one position. These bound the account, which is the layer the
source video has no equivalent for: David's shutdown is per-strategy only, so a
portfolio of individually well-behaved strategies can still drain an account together.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from atlas.models import KillSwitchTrigger

ZERO = Decimal(0)


@dataclass(frozen=True)
class PortfolioLimits:
    """ATLAS decisions. The video supplies no account-level limits at all."""

    daily_loss_limit: Decimal = Decimal("0.05")
    max_account_drawdown: Decimal = Decimal("0.20")


@dataclass(frozen=True)
class AccountState:
    """A valued snapshot of the account.

    `equity` is the marked total: quote cash (free and locked) plus every open position
    valued at its last known price. Quote cash alone is not equity — the moment an entry
    fills, cash leaves and base asset arrives, and an equity figure that counted only the
    cash would read that as an instantaneous loss the size of the position. At $100 with
    a 33% position cap that is a fabricated 33% drawdown on the first fill, which trips
    RISK-06 and halts the account for a trade that has not yet moved.

    `free_cash` and `deployed` are separate inputs to sizing (RISK-04) and are not
    interchangeable with equity: cash that is already in a position cannot fund another.

    `valuation_complete` is false when an open position had no price to mark against.
    A partial valuation is not a small error — it understates equity by exactly the
    unpriced position — so it must never be compared against a loss limit or persisted
    as a snapshot.
    """

    equity: Decimal
    peak_equity: Decimal
    day_start_equity: Decimal
    as_of: date
    free_cash: Decimal
    deployed: Decimal = ZERO
    open_symbols: frozenset[str] = frozenset()
    valuation_complete: bool = True
    unpriced_symbols: frozenset[str] = frozenset()

    @property
    def drawdown_from_peak(self) -> Decimal:
        if self.peak_equity <= 0:
            return ZERO
        return max(ZERO, (self.peak_equity - self.equity) / self.peak_equity)

    @property
    def daily_loss(self) -> Decimal:
        if self.day_start_equity <= 0:
            return ZERO
        return max(ZERO, (self.day_start_equity - self.equity) / self.day_start_equity)


@dataclass(frozen=True)
class LimitBreach:
    trigger: KillSwitchTrigger
    reason: str


def check_portfolio_limits(
    state: AccountState, limits: PortfolioLimits | None = None
) -> LimitBreach | None:
    """Return the breach that should arm the kill switch, or None.

    Drawdown is checked before the daily limit: it is the more serious condition and
    should be the reason recorded when both are true.
    """
    bounds = limits or PortfolioLimits()

    if state.drawdown_from_peak >= bounds.max_account_drawdown:
        return LimitBreach(
            KillSwitchTrigger.MAX_ACCOUNT_DD,
            f"account drawdown {state.drawdown_from_peak:.2%} reached the "
            f"{bounds.max_account_drawdown:.0%} limit (peak {state.peak_equity}, "
            f"now {state.equity})",
        )

    if state.daily_loss >= bounds.daily_loss_limit:
        return LimitBreach(
            KillSwitchTrigger.DAILY_LOSS_LIMIT,
            f"daily loss {state.daily_loss:.2%} reached the "
            f"{bounds.daily_loss_limit:.0%} limit on {state.as_of.isoformat()}",
        )

    return None


def can_open_symbol(state: AccountState, symbol: str) -> bool:
    """RISK-08: one position per symbol. No pyramiding, averaging down or hedging."""
    return symbol.upper() not in state.open_symbols
