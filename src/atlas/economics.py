"""Economic provenance (Phase R).

A backtest is only as trustworthy as the numbers it assumes, and the dangerous failure
is forgetting which numbers were assumed. This module makes provenance explicit and
machine-readable so a report can never quietly present an assumption as an exchange
fact.

Three tiers, and they are not interchangeable:

    ASSUMPTION  a value ATLAS chose. Plausible, unverified, and possibly wrong.
    OBSERVED    a value read from the exchange at a recorded time.
    FIXTURE     a value invented for a test. Never valid outside one.

Every economic input carries its tier. `EconomicProfile.is_exchange_verified` is false
until every input that *can* be observed has been, and nothing may claim validated
economics while it returns false.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class Provenance(StrEnum):
    ASSUMPTION = "ASSUMPTION"
    OBSERVED = "OBSERVED"
    FIXTURE = "FIXTURE"


@dataclass(frozen=True)
class EconomicValue:
    """One economic input and where it came from."""

    name: str
    value: Decimal
    provenance: Provenance
    source: str
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.provenance is Provenance.OBSERVED and self.observed_at is None:
            raise ValueError(f"{self.name}: an OBSERVED value must record when it was observed")

    @property
    def is_trustworthy(self) -> bool:
        return self.provenance is Provenance.OBSERVED

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": str(self.value),
            "provenance": str(self.provenance),
            "source": self.source,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
        }


@dataclass(frozen=True)
class EconomicProfile:
    """The full set of economic inputs behind a sizing or backtest decision."""

    values: tuple[EconomicValue, ...] = field(default_factory=tuple)

    def get(self, name: str) -> EconomicValue | None:
        return next((v for v in self.values if v.name == name), None)

    @property
    def assumptions(self) -> tuple[EconomicValue, ...]:
        return tuple(v for v in self.values if v.provenance is Provenance.ASSUMPTION)

    @property
    def observations(self) -> tuple[EconomicValue, ...]:
        return tuple(v for v in self.values if v.provenance is Provenance.OBSERVED)

    @property
    def fixtures(self) -> tuple[EconomicValue, ...]:
        return tuple(v for v in self.values if v.provenance is Provenance.FIXTURE)

    @property
    def is_exchange_verified(self) -> bool:
        """True only when every observable input has actually been observed.

        A single assumption or fixture makes this false. Nothing may describe its
        economics as validated while it is.
        """
        return bool(self.values) and all(v.is_trustworthy for v in self.values)

    def warnings(self) -> list[str]:
        out = [
            f"{v.name}={v.value} is an ASSUMPTION ({v.source}), not an exchange fact"
            for v in self.assumptions
        ]
        out += [
            f"{v.name}={v.value} is a TEST FIXTURE ({v.source}) and is not valid outside a test"
            for v in self.fixtures
        ]
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "exchange_verified": self.is_exchange_verified,
            "values": [v.as_dict() for v in self.values],
            "warnings": self.warnings(),
        }


# The profile ATLAS currently operates under. Every entry is an assumption because
# nothing in ATLAS has ever contacted a Binance endpoint. `observe_filters` replaces
# the observable ones once exchangeInfo has actually been read.
DEFAULT_ASSUMED_PROFILE = EconomicProfile(
    values=(
        EconomicValue(
            "fee_rate",
            Decimal("0.001"),
            Provenance.ASSUMPTION,
            "BT-04; Binance spot taker tier not confirmed against the live account",
        ),
        EconomicValue(
            "slippage_rate",
            Decimal("0.0005"),
            Provenance.ASSUMPTION,
            "BT-04; no measured fill data exists",
        ),
        EconomicValue(
            "min_notional",
            Decimal("5"),
            Provenance.ASSUMPTION,
            "RISK-09 placeholder; the real value must come from exchangeInfo",
        ),
        EconomicValue(
            "step_size",
            Decimal("0.00001"),
            Provenance.ASSUMPTION,
            "RISK-09 placeholder; the real value must come from exchangeInfo",
        ),
        EconomicValue(
            "tick_size",
            Decimal("0.01"),
            Provenance.ASSUMPTION,
            "RISK-09 placeholder; the real value must come from exchangeInfo",
        ),
    )
)


def observe_filters(
    *,
    min_notional: Decimal,
    step_size: Decimal,
    tick_size: Decimal,
    observed_at: datetime,
    symbol: str,
    profile: EconomicProfile | None = None,
) -> EconomicProfile:
    """Promote the exchange filters from ASSUMPTION to OBSERVED.

    Called once `exchangeInfo` has genuinely been read. Fees stay assumptions until
    the account's own fee tier is confirmed, which `exchangeInfo` does not carry.
    """
    base = profile or DEFAULT_ASSUMED_PROFILE
    source = f"exchangeInfo({symbol})"
    replacements = {
        "min_notional": min_notional,
        "step_size": step_size,
        "tick_size": tick_size,
    }

    updated = tuple(
        EconomicValue(v.name, replacements[v.name], Provenance.OBSERVED, source, observed_at)
        if v.name in replacements
        else v
        for v in base.values
    )
    return EconomicProfile(values=updated)
