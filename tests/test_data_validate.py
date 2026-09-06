"""DATA-03 series validation and DATA-05 staleness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.test_data_models import BASE, make_bar, make_series

from atlas.data.models import KlineSeries, Timeframe
from atlas.data.validate import (
    SeriesValidationError,
    check_staleness,
    require_valid_series,
    validate_series,
)


def test_clean_series_passes() -> None:
    assert validate_series(make_series(50)).valid


def test_empty_series_rejected() -> None:
    empty = KlineSeries(symbol="BTCUSDT", timeframe=Timeframe.H1, bars=())
    report = validate_series(empty)
    assert not report.valid
    assert "empty" in report.reasons[0]


def test_gap_detected() -> None:
    """A missing bar must be reported, never interpolated."""
    bars = tuple(make_bar(i) for i in [0, 1, 2, 5, 6])
    report = validate_series(KlineSeries(symbol="BTCUSDT", timeframe=Timeframe.H1, bars=bars))
    assert not report.valid
    assert report.gap_count == 1
    assert "gap of 2 bar(s)" in report.reasons[0]


def test_gap_allowed_when_permitted_but_still_counted() -> None:
    bars = tuple(make_bar(i) for i in [0, 1, 4])
    series = KlineSeries(symbol="BTCUSDT", timeframe=Timeframe.H1, bars=bars)
    report = validate_series(series, allow_gaps=True)
    assert report.valid
    assert report.gap_count == 1


def test_duplicate_detected() -> None:
    bars = (make_bar(0), make_bar(1), make_bar(1), make_bar(2))
    report = validate_series(KlineSeries(symbol="BTCUSDT", timeframe=Timeframe.H1, bars=bars))
    assert not report.valid
    assert report.duplicate_count == 1


def test_out_of_order_detected() -> None:
    bars = (make_bar(0), make_bar(3), make_bar(1))
    report = validate_series(KlineSeries(symbol="BTCUSDT", timeframe=Timeframe.H1, bars=bars))
    assert not report.valid
    assert any("out-of-order" in r for r in report.reasons)


def test_require_valid_series_raises() -> None:
    bars = tuple(make_bar(i) for i in [0, 1, 9])
    series = KlineSeries(symbol="BTCUSDT", timeframe=Timeframe.H1, bars=bars)
    with pytest.raises(SeriesValidationError, match="gap"):
        require_valid_series(series)


def test_require_valid_series_returns_series_when_clean() -> None:
    series = make_series(10)
    assert require_valid_series(series) is series


def test_validation_never_mutates_the_series() -> None:
    """Rejection, not repair: a bad series must come out exactly as it went in."""
    bars = tuple(make_bar(i) for i in [0, 1, 7])
    series = KlineSeries(symbol="BTCUSDT", timeframe=Timeframe.H1, bars=bars)
    before = series.content_hash()
    validate_series(series)
    assert series.content_hash() == before
    assert len(series) == 3


def test_staleness_fresh() -> None:
    now = BASE + timedelta(hours=1, minutes=5)
    stale, age = check_staleness(BASE + timedelta(hours=1), Timeframe.H1, now=now)
    assert not stale
    assert age == pytest.approx(300.0)


def test_staleness_detected_beyond_two_intervals() -> None:
    """DATA-05: beyond 2x the bar interval halts new entries."""
    now = BASE + timedelta(hours=3, minutes=1)
    stale, _ = check_staleness(BASE + timedelta(hours=1), Timeframe.H1, now=now)
    assert stale


def test_staleness_boundary_is_not_stale() -> None:
    now = BASE + timedelta(hours=3)
    stale, _ = check_staleness(BASE + timedelta(hours=1), Timeframe.H1, now=now)
    assert not stale


def test_staleness_scales_with_timeframe() -> None:
    last = datetime(2026, 1, 2, tzinfo=UTC)
    now = last + timedelta(hours=36)
    assert not check_staleness(last, Timeframe.D1, now=now)[0]
    assert check_staleness(last, Timeframe.H1, now=now)[0]
