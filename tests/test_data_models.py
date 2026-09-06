"""Kline domain types: OHLC consistency, hashing, chronological splitting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from atlas.data.models import Kline, KlineSeries, Timeframe

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def make_bar(
    index: int, *, close: object = "100", tf: Timeframe = Timeframe.H1, **kw: object
) -> Kline:
    open_time = BASE + tf.duration * index
    defaults: dict[str, object] = {
        "open_time": open_time,
        "close_time": open_time + tf.duration,
        "open": Decimal("100"),
        "high": Decimal("110"),
        "low": Decimal("90"),
        "close": close,
        "volume": Decimal("5"),
        "trades": 10,
    }
    defaults.update(kw)
    return Kline(**defaults)  # type: ignore[arg-type]


def make_series(n: int, tf: Timeframe = Timeframe.H1) -> KlineSeries:
    return KlineSeries(
        symbol="BTCUSDT", timeframe=tf, bars=tuple(make_bar(i, tf=tf) for i in range(n))
    )


def test_timeframe_durations() -> None:
    assert Timeframe.H1.duration == timedelta(hours=1)
    assert Timeframe.H1.milliseconds == 3_600_000
    assert Timeframe.D1.milliseconds == 86_400_000


def test_valid_bar_constructs() -> None:
    assert make_bar(0).range == Decimal("20")


def test_low_above_high_rejected() -> None:
    with pytest.raises(ValidationError, match="exceeds high"):
        make_bar(0, low=Decimal("200"), high=Decimal("100"))


def test_close_outside_range_rejected() -> None:
    with pytest.raises(ValidationError, match="outside the bar range"):
        make_bar(0, close="999")


def test_open_outside_range_rejected() -> None:
    with pytest.raises(ValidationError, match="outside the bar range"):
        make_bar(0, open=Decimal("1"))


def test_close_time_must_follow_open_time() -> None:
    with pytest.raises(ValidationError, match="close_time must be after"):
        make_bar(0, close_time=BASE - timedelta(hours=1))


def test_negative_price_rejected() -> None:
    with pytest.raises(ValidationError):
        make_bar(0, low=Decimal("-1"))


def test_float_price_rejected() -> None:
    """ADR-003 applies to market data too."""
    with pytest.raises(ValidationError, match="float is not permitted"):
        make_bar(0, close=100.5)


def test_content_hash_is_stable() -> None:
    assert make_series(10).content_hash() == make_series(10).content_hash()


def test_content_hash_changes_with_content() -> None:
    a = make_series(5)
    tweaked = KlineSeries(
        symbol=a.symbol,
        timeframe=a.timeframe,
        bars=(*a.bars[:-1], make_bar(4, close="105")),
    )
    assert a.content_hash() != tweaked.content_hash()


def test_content_hash_changes_with_symbol() -> None:
    a = make_series(3)
    b = KlineSeries(symbol="ETHUSDT", timeframe=a.timeframe, bars=a.bars)
    assert a.content_hash() != b.content_hash()


def test_series_bounds() -> None:
    series = make_series(24)
    assert series.start == BASE
    assert series.end == BASE + timedelta(hours=24)
    assert len(series) == 24


def test_chronological_split_preserves_order() -> None:
    """VER-04: the split must be chronological, never shuffled."""
    series = make_series(100)
    in_sample, out_of_sample = series.split_chronological(0.7)
    assert len(in_sample) == 70
    assert len(out_of_sample) == 30
    assert in_sample.bars[-1].open_time < out_of_sample.bars[0].open_time


def test_split_rejects_invalid_fraction() -> None:
    with pytest.raises(ValueError, match="must be in"):
        make_series(10).split_chronological(1.5)


def test_slice_by_time() -> None:
    series = make_series(48)
    window = series.slice_by_time(BASE + timedelta(hours=10), BASE + timedelta(hours=20))
    assert len(window) == 10
    assert window.bars[0].open_time == BASE + timedelta(hours=10)
