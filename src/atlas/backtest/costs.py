"""Cost model (BT-04). ATLAS decision.

0.10% taker fee per side plus 0.05% slippage per side — 0.30% round trip. The fee tier
must be confirmed against the live account before the live cutover; a lower tier makes
these results conservative, which is the safe direction to be wrong in.

At a $100 account with a 3% stop, a 0.30% round trip consumes 10% of the risk budget per
trade. Strategies with small edges will not survive costing, and that is the point.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from atlas.models import PositionSide


@dataclass(frozen=True)
class CostModel:
    fee_rate: Decimal = Decimal("0.001")
    slippage_rate: Decimal = Decimal("0.0005")

    @property
    def round_trip_rate(self) -> Decimal:
        return (self.fee_rate + self.slippage_rate) * 2

    def fee_on(self, notional: Decimal) -> Decimal:
        return notional * self.fee_rate

    def fill_price(self, quoted: Decimal, side: PositionSide, *, entering: bool) -> Decimal:
        """Apply slippage against the trader, always.

        Buying fills higher, selling fills lower, whether opening or closing.
        """
        buying = (side is PositionSide.LONG) == entering
        factor = 1 + self.slippage_rate if buying else 1 - self.slippage_rate
        return quoted * factor

    def as_dict(self) -> dict[str, str]:
        return {"fee_rate": str(self.fee_rate), "slippage_rate": str(self.slippage_rate)}


DEFAULT_COSTS = CostModel()
