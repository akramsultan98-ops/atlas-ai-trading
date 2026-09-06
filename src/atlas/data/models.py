"""Kline domain types.

Prices and volumes are `Decimal` (ADR-003). Binance returns them as strings, which
round-trip into Decimal losslessly — parsing them through float would discard that.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from atlas.models import Money, StrictModel


class Timeframe(StrEnum):
    """Supported bar intervals. `H1` is the specification default (DATA-06)."""

    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def duration(self) -> timedelta:
        return {
            Timeframe.M1: timedelta(minutes=1),
            Timeframe.M5: timedelta(minutes=5),
            Timeframe.M15: timedelta(minutes=15),
            Timeframe.M30: timedelta(minutes=30),
            Timeframe.H1: timedelta(hours=1),
            Timeframe.H4: timedelta(hours=4),
            Timeframe.D1: timedelta(days=1),
        }[self]

    @property
    def milliseconds(self) -> int:
        return int(self.duration.total_seconds() * 1000)


class Kline(StrictModel):
    """One closed candle.

    Only closed candles exist as `Kline` values. An in-progress bar is dropped at the
    client boundary (DATA-02) rather than carried around with a flag, so no downstream
    code can evaluate one by forgetting to check.
    """

    open_time: datetime
    close_time: datetime
    open: Money = Field(gt=0)
    high: Money = Field(gt=0)
    low: Money = Field(gt=0)
    close: Money = Field(gt=0)
    volume: Money = Field(ge=0)
    trades: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_ohlc(self) -> Self:
        if self.low > self.high:
            raise ValueError(f"low {self.low} exceeds high {self.high}")
        for name, value in (("open", self.open), ("close", self.close)):
            if not (self.low <= value <= self.high):
                raise ValueError(f"{name} {value} outside the bar range [{self.low}, {self.high}]")
        if self.close_time <= self.open_time:
            raise ValueError("close_time must be after open_time")
        return self

    @property
    def range(self) -> Decimal:
        return self.high - self.low

    @property
    def is_up(self) -> bool:
        return self.close > self.open


class KlineSeries(StrictModel):
    """An ordered, validated run of closed candles for one symbol and timeframe."""

    symbol: str = Field(min_length=1)
    timeframe: Timeframe
    bars: tuple[Kline, ...]

    def __len__(self) -> int:
        return len(self.bars)

    @property
    def start(self) -> datetime | None:
        return self.bars[0].open_time if self.bars else None

    @property
    def end(self) -> datetime | None:
        return self.bars[-1].close_time if self.bars else None

    def content_hash(self) -> str:
        """Stable hash of the series content (DATA-04).

        Persisted alongside every backtest so a result can be tied to the exact data
        that produced it, and a rerun on different data is detectable rather than
        silently comparable.
        """
        digest = hashlib.sha256()
        digest.update(f"{self.symbol}|{self.timeframe}|".encode())
        for bar in self.bars:
            digest.update(
                f"{int(bar.open_time.timestamp() * 1000)}|{bar.open}|{bar.high}|"
                f"{bar.low}|{bar.close}|{bar.volume}|{bar.trades};".encode()
            )
        return digest.hexdigest()

    def slice_by_time(self, start: datetime | None, end: datetime | None) -> KlineSeries:
        selected = tuple(
            bar
            for bar in self.bars
            if (start is None or bar.open_time >= start) and (end is None or bar.close_time <= end)
        )
        return KlineSeries(symbol=self.symbol, timeframe=self.timeframe, bars=selected)

    def split_chronological(self, in_sample_fraction: float) -> tuple[KlineSeries, KlineSeries]:
        """Chronological split for out-of-sample evaluation (VER-04).

        Chronological, never random: a shuffled split leaks future information into the
        in-sample half and makes the out-of-sample result meaningless.
        """
        if not 0 < in_sample_fraction < 1:
            raise ValueError("in_sample_fraction must be in (0, 1)")
        cut = int(len(self.bars) * in_sample_fraction)
        return (
            KlineSeries(symbol=self.symbol, timeframe=self.timeframe, bars=self.bars[:cut]),
            KlineSeries(symbol=self.symbol, timeframe=self.timeframe, bars=self.bars[cut:]),
        )


def ms_to_datetime(milliseconds: int) -> datetime:
    return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)


def datetime_to_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)
