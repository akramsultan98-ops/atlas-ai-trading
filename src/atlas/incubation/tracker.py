"""Zero-capital incubation tracking (INC-01..03).

David holds strategies unfunded for "a couple of months" to see whether they behave on
live data as they did in backtest [12:27-12:36]. Nothing here can commit capital: the
tracker records what the strategy *would* have done and scores it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from atlas.data.models import Kline, KlineSeries
from atlas.db.engine import Database
from atlas.models import PositionSide, utcnow
from atlas.strategy.evaluator import evaluate
from atlas.strategy.spec import StrategySpec

ZERO = Decimal(0)


@dataclass(frozen=True)
class IncubationSignal:
    id: str
    strategy_id: str
    signal_at: datetime
    side: PositionSide
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    exit_at: datetime | None = None
    exit_price: Decimal | None = None
    outcome: str | None = None
    return_pct: Decimal | None = None

    @property
    def is_closed(self) -> bool:
        return self.outcome in ("WIN", "LOSS")


class IncubationTracker:
    """Records paper signals and resolves them against subsequent price action."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def record_signals(self, strategy_id: str, spec: StrategySpec, series: KlineSeries) -> int:
        """Evaluate the strategy over `series` and persist resolved paper trades.

        Resolution uses the same pessimistic rule as the backtest engine (BT-06): when a
        bar touches both stop and target, the stop wins. Incubation must not be more
        flattering than the backtest it is being compared against.
        """
        signals = evaluate(spec, series.bars)
        recorded = 0

        for signal in signals:
            entry_index = signal.bar_index + 1
            if entry_index >= len(series.bars):
                continue
            entry_bar = series.bars[entry_index]
            exit_at, exit_price, outcome = _resolve(
                series.bars,
                entry_index,
                signal.side,
                signal.stop_price,
                signal.target_price,
                spec.max_bars_in_trade,
            )
            direction = Decimal(1) if signal.side is PositionSide.LONG else Decimal(-1)
            entry_price = entry_bar.open
            return_pct = (
                ((exit_price - entry_price) / entry_price) * direction
                if exit_price is not None and entry_price > 0
                else None
            )

            with self._db.transaction() as conn:
                conn.execute(
                    "INSERT INTO incubation_signals(id, strategy_id, signal_at, side, "
                    "entry_price, stop_price, target_price, exit_at, exit_price, outcome, "
                    "return_pct) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        strategy_id,
                        entry_bar.open_time.isoformat(),
                        "BUY" if signal.side is PositionSide.LONG else "SELL",
                        str(entry_price),
                        str(signal.stop_price),
                        str(signal.target_price),
                        exit_at.isoformat() if exit_at else None,
                        str(exit_price) if exit_price is not None else None,
                        outcome,
                        str(return_pct) if return_pct is not None else None,
                    ),
                )
            recorded += 1
        return recorded

    def signals_for(self, strategy_id: str) -> list[IncubationSignal]:
        rows = self._db.connection.execute(
            "SELECT * FROM incubation_signals WHERE strategy_id = ? ORDER BY signal_at",
            (strategy_id,),
        ).fetchall()
        return [
            IncubationSignal(
                id=str(r["id"]),
                strategy_id=str(r["strategy_id"]),
                signal_at=datetime.fromisoformat(str(r["signal_at"])),
                side=PositionSide.LONG if r["side"] == "BUY" else PositionSide.SHORT,
                entry_price=Decimal(str(r["entry_price"])),
                stop_price=Decimal(str(r["stop_price"])),
                target_price=Decimal(str(r["target_price"])),
                exit_at=datetime.fromisoformat(str(r["exit_at"])) if r["exit_at"] else None,
                exit_price=Decimal(str(r["exit_price"])) if r["exit_price"] else None,
                outcome=str(r["outcome"]) if r["outcome"] else None,
                return_pct=Decimal(str(r["return_pct"])) if r["return_pct"] else None,
            )
            for r in rows
        ]

    def elapsed_days(self, strategy_id: str, *, now: datetime | None = None) -> int:
        signals = self.signals_for(strategy_id)
        if not signals:
            return 0
        reference = now or utcnow()
        return max(0, (reference - signals[0].signal_at).days)


def _resolve(
    bars: tuple[Kline, ...],
    entry_index: int,
    side: PositionSide,
    stop: Decimal,
    target: Decimal,
    max_bars: int,
) -> tuple[datetime | None, Decimal | None, str]:
    for i in range(entry_index + 1, len(bars)):
        bar = bars[i]
        if side is PositionSide.LONG:
            stopped, targeted = bar.low <= stop, bar.high >= target
        else:
            stopped, targeted = bar.high >= stop, bar.low <= target

        if stopped:  # BT-06: pessimistic when both touch
            return bar.close_time, stop, "LOSS"
        if targeted:
            return bar.close_time, target, "WIN"
        if i - entry_index >= max_bars:
            direction = 1 if side is PositionSide.LONG else -1
            won = (bar.close - bars[entry_index].open) * direction > 0
            return bar.close_time, bar.close, "WIN" if won else "LOSS"
    return None, None, "OPEN"


def _elapsed(start: datetime, end: datetime) -> timedelta:
    return end - start
