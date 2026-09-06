"""Trade record produced by the backtest engine."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from atlas.models import PositionSide, StrictModel


class ExitReason(StrEnum):
    STOP = "STOP"
    TARGET = "TARGET"
    TIME = "TIME"
    END_OF_DATA = "END_OF_DATA"


class Trade(StrictModel):
    entry_index: int
    exit_index: int
    entry_time: datetime
    exit_time: datetime
    side: PositionSide
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    gross_pnl: Decimal
    fees: Decimal
    net_pnl: Decimal
    exit_reason: ExitReason
    was_capped: bool

    @property
    def bars_held(self) -> int:
        return self.exit_index - self.entry_index

    @property
    def is_win(self) -> bool:
        return self.net_pnl > 0
