"""DATA-04: content-hashed reproducible cache."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from tests.test_data_models import make_series

from atlas.data.models import Timeframe
from atlas.data.store import KlineStore
from atlas.errors import PersistenceError


def test_round_trip_preserves_content(tmp_path: Path) -> None:
    store = KlineStore(tmp_path)
    original = make_series(120)
    saved_hash = store.save(original)

    loaded = store.load("BTCUSDT", Timeframe.H1)
    assert loaded is not None
    assert loaded.content_hash() == saved_hash
    assert loaded.content_hash() == original.content_hash()
    assert len(loaded) == 120


def test_round_trip_preserves_decimal_precision(tmp_path: Path) -> None:
    """Prices must survive the cache exactly; a float round trip would not."""
    from decimal import Decimal

    from tests.test_data_models import make_bar

    from atlas.data.models import KlineSeries

    precise = make_bar(
        0,
        open=Decimal("0.000000012345678"),
        low=Decimal("0.00000001"),
        high=Decimal("0.0000001"),
        close=Decimal("0.000000098765432"),
    )
    series = KlineSeries(symbol="SHIBUSDT", timeframe=Timeframe.H1, bars=(precise,))
    store = KlineStore(tmp_path)
    store.save(series)

    loaded = store.load("SHIBUSDT", Timeframe.H1)
    assert loaded is not None
    assert loaded.bars[0].open == Decimal("0.000000012345678")
    assert loaded.bars[0].close == Decimal("0.000000098765432")


def test_missing_cache_returns_none(tmp_path: Path) -> None:
    assert KlineStore(tmp_path).load("BTCUSDT", Timeframe.H1) is None


def test_tampered_cache_raises(tmp_path: Path) -> None:
    """A hand-edited cache must not silently poison downstream backtests."""
    store = KlineStore(tmp_path)
    store.save(make_series(10))

    path = store.path_for("BTCUSDT", Timeframe.H1)
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    # Stay inside the bar range so the per-bar OHLC validator cannot catch it first:
    # this must be caught by the content hash and nothing else.
    payload["bars"][3][4] = "105"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)

    with pytest.raises(PersistenceError, match="integrity failure"):
        store.load("BTCUSDT", Timeframe.H1)


def test_corrupt_cache_raises(tmp_path: Path) -> None:
    store = KlineStore(tmp_path)
    store.save(make_series(5))
    store.path_for("BTCUSDT", Timeframe.H1).write_bytes(b"not gzip")
    with pytest.raises(PersistenceError, match="unreadable"):
        store.load("BTCUSDT", Timeframe.H1)


def test_coverage_reports_window(tmp_path: Path) -> None:
    store = KlineStore(tmp_path)
    series = make_series(48)
    store.save(series)
    coverage = store.coverage("BTCUSDT", Timeframe.H1)
    assert coverage == (series.start, series.end)


def test_save_is_atomic_leaving_no_temp_file(tmp_path: Path) -> None:
    store = KlineStore(tmp_path)
    store.save(make_series(5))
    assert not list(tmp_path.glob("*.tmp"))
