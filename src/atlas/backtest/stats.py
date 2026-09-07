"""Backtest statistics (BT-07)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from atlas.backtest.trade import Trade

ZERO = Decimal(0)


@dataclass(frozen=True)
class BacktestStats:
    trade_count: int
    wins: int
    losses: int
    win_rate: Decimal
    net_return: Decimal
    profit_factor: Decimal
    expectancy: Decimal
    max_drawdown: Decimal
    avg_bars_held: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    total_fees: Decimal
    final_equity: Decimal
    buy_and_hold_return: Decimal
    beats_buy_and_hold: bool
    capped_trade_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "trade_count": self.trade_count,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": str(self.win_rate),
            "net_return": str(self.net_return),
            "profit_factor": str(self.profit_factor),
            "expectancy": str(self.expectancy),
            "max_drawdown": str(self.max_drawdown),
            "avg_bars_held": str(self.avg_bars_held),
            "gross_profit": str(self.gross_profit),
            "gross_loss": str(self.gross_loss),
            "total_fees": str(self.total_fees),
            "final_equity": str(self.final_equity),
            "buy_and_hold_return": str(self.buy_and_hold_return),
            "beats_buy_and_hold": self.beats_buy_and_hold,
            "capped_trade_count": self.capped_trade_count,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> BacktestStats:
        """Rebuild stored stats. The inverse of `as_dict`, for evidence read back.

        INC-04 and INC-05 compare incubation against the backtest, so the stored run
        has to be reconstructable - otherwise the comparison silently uses whatever
        happens to be in memory, which after a restart is nothing.
        """
        ints = {"trade_count", "wins", "losses", "capped_trade_count"}
        values: dict[str, Any] = {}
        for name in cls.__dataclass_fields__:
            raw = payload[name]
            if name in ints:
                values[name] = int(raw)
            elif name == "beats_buy_and_hold":
                values[name] = bool(raw)
            else:
                values[name] = Decimal(str(raw))
        return cls(**values)


def max_drawdown(equity_curve: list[Decimal]) -> Decimal:
    """Largest peak-to-trough decline as a fraction of the peak."""
    if not equity_curve:
        return ZERO
    peak = equity_curve[0]
    worst = ZERO
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            drawdown = (peak - value) / peak
            worst = max(worst, drawdown)
    return worst


def compute_stats(
    trades: list[Trade],
    equity_curve: list[Decimal],
    starting_equity: Decimal,
    buy_and_hold_return: Decimal,
) -> BacktestStats:
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    gross_profit = sum((t.net_pnl for t in wins), ZERO)
    gross_loss = abs(sum((t.net_pnl for t in losses), ZERO))
    final_equity = equity_curve[-1] if equity_curve else starting_equity
    net_return = (final_equity - starting_equity) / starting_equity if starting_equity > 0 else ZERO

    # An infinite profit factor is not informative. A strategy with no losses over a
    # small sample is under-tested, not perfect; SEL-01's trade-count floor is what
    # actually guards against that, so report a large finite number here.
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = Decimal(999)
    else:
        profit_factor = ZERO

    count = len(trades)
    expectancy = (sum((t.net_pnl for t in trades), ZERO) / count) if count else ZERO
    avg_bars = Decimal(sum(t.bars_held for t in trades)) / count if count else ZERO

    return BacktestStats(
        trade_count=count,
        wins=len(wins),
        losses=len(losses),
        win_rate=(Decimal(len(wins)) / count) if count else ZERO,
        net_return=net_return,
        profit_factor=profit_factor,
        expectancy=expectancy,
        max_drawdown=max_drawdown(equity_curve),
        avg_bars_held=avg_bars,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        total_fees=sum((t.fees for t in trades), ZERO),
        final_equity=final_equity,
        buy_and_hold_return=buy_and_hold_return,
        beats_buy_and_hold=net_return > buy_and_hold_return,
        capped_trade_count=sum(1 for t in trades if t.was_capped),
    )
