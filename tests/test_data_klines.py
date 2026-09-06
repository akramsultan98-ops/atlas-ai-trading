"""DATA-01/DATA-02 kline client. No network: transport is a stub."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from atlas.data.klines import (
    MAINNET_BASE,
    TESTNET_BASE,
    BinanceKlineClient,
    MarketDataError,
    UrllibTransport,
)
from atlas.data.models import Timeframe
from atlas.models import ExchangeEnv

BASE = datetime(2026, 1, 1, tzinfo=UTC)
HOUR_MS = 3_600_000


def row(index: int, close: str = "100") -> list[Any]:
    """One Binance kline row in wire format (prices as strings)."""
    open_ms = int(BASE.timestamp() * 1000) + index * HOUR_MS
    return [
        open_ms,
        "100",
        "110",
        "90",
        close,
        "5.5",
        open_ms + HOUR_MS - 1,
        "550",
        42,
        "2.5",
        "250",
        "0",
    ]


class StubTransport:
    """Returns queued pages and records the URLs requested."""

    def __init__(self, pages: list[list[Any]]) -> None:
        self.pages = pages
        self.urls: list[str] = []

    def get_json(self, url: str) -> Any:
        self.urls.append(url)
        return self.pages.pop(0) if self.pages else []


def test_parses_rows_into_klines() -> None:
    client = BinanceKlineClient(transport=StubTransport([[row(0), row(1), row(2)]]))
    series = client.fetch("btcusdt", Timeframe.H1, now=BASE + timedelta(hours=10))

    assert len(series) == 3
    assert series.symbol == "BTCUSDT"
    from decimal import Decimal

    assert series.bars[0].open == Decimal("100")
    assert series.bars[0].volume == Decimal("5.5")
    assert series.bars[0].trades == 42


def test_in_progress_bar_is_dropped() -> None:
    """DATA-02: the forming bar must never reach the evaluator.

    Bars 0-2 have closed by the reference time; bar 3 is still forming.
    """
    client = BinanceKlineClient(transport=StubTransport([[row(i) for i in range(4)]]))
    series = client.fetch("BTCUSDT", Timeframe.H1, now=BASE + timedelta(hours=3, minutes=30))

    assert len(series) == 3
    assert series.bars[-1].close_time <= BASE + timedelta(hours=3, minutes=30)


def test_all_bars_kept_when_all_closed() -> None:
    client = BinanceKlineClient(transport=StubTransport([[row(i) for i in range(4)]]))
    series = client.fetch("BTCUSDT", Timeframe.H1, now=BASE + timedelta(days=1))
    assert len(series) == 4


def test_end_bound_respected() -> None:
    client = BinanceKlineClient(transport=StubTransport([[row(i) for i in range(10)]]))
    series = client.fetch(
        "BTCUSDT", Timeframe.H1, end=BASE + timedelta(hours=5), now=BASE + timedelta(days=1)
    )
    assert len(series) == 5


def test_duplicates_across_pages_are_ignored() -> None:
    """Pagination overlap must not produce duplicate bars."""
    page1 = [row(i) for i in range(1000)]
    page2 = [row(i) for i in range(999, 1500)]
    client = BinanceKlineClient(transport=StubTransport([page1, page2, []]))
    series = client.fetch("BTCUSDT", Timeframe.H1, now=BASE + timedelta(days=200))

    opens = [b.open_time for b in series.bars]
    assert len(opens) == len(set(opens))
    assert len(series) == 1500


def test_stops_on_short_page() -> None:
    transport = StubTransport([[row(0), row(1)]])
    client = BinanceKlineClient(transport=transport)
    client.fetch("BTCUSDT", Timeframe.H1, now=BASE + timedelta(days=1))
    assert len(transport.urls) == 1


def test_empty_response_yields_empty_series() -> None:
    client = BinanceKlineClient(transport=StubTransport([[]]))
    series = client.fetch("BTCUSDT", Timeframe.H1, now=BASE + timedelta(days=1))
    assert len(series) == 0


def test_max_bars_respected() -> None:
    client = BinanceKlineClient(transport=StubTransport([[row(i) for i in range(1000)], []]))
    series = client.fetch("BTCUSDT", Timeframe.H1, max_bars=50, now=BASE + timedelta(days=200))
    assert len(series) == 50


def test_non_list_payload_raises() -> None:
    class BadTransport:
        def get_json(self, url: str) -> Any:
            return {"code": -1121, "msg": "Invalid symbol."}

    client = BinanceKlineClient(transport=BadTransport())
    with pytest.raises(MarketDataError, match="unexpected kline payload"):
        client.fetch("NOPE", Timeframe.H1, now=BASE)


def test_bars_are_sorted_chronologically() -> None:
    shuffled = [row(3), row(0), row(2), row(1)]
    client = BinanceKlineClient(transport=StubTransport([shuffled]))
    series = client.fetch("BTCUSDT", Timeframe.H1, now=BASE + timedelta(days=1))
    times = [b.open_time for b in series.bars]
    assert times == sorted(times)


def test_testnet_is_the_default_environment() -> None:
    """No live endpoint without an explicit choice."""
    assert BinanceKlineClient(transport=StubTransport([])).base_url == TESTNET_BASE


def test_live_environment_uses_mainnet() -> None:
    client = BinanceKlineClient(ExchangeEnv.LIVE, transport=StubTransport([]))
    assert client.base_url == MAINNET_BASE


def test_url_includes_symbol_interval_and_limit() -> None:
    transport = StubTransport([[row(0)]])
    BinanceKlineClient(transport=transport).fetch(
        "btcusdt", Timeframe.H1, now=BASE + timedelta(days=1)
    )
    url = transport.urls[0]
    assert "symbol=BTCUSDT" in url
    assert "interval=1h" in url
    assert "limit=1000" in url


def test_start_time_is_forwarded() -> None:
    transport = StubTransport([[row(0)]])
    BinanceKlineClient(transport=transport).fetch(
        "BTCUSDT", Timeframe.H1, start=BASE, now=BASE + timedelta(days=1)
    )
    assert f"startTime={int(BASE.timestamp() * 1000)}" in transport.urls[0]


def test_transport_retries_are_bounded() -> None:
    """A permanent failure must terminate, not retry forever."""
    calls = {"n": 0}

    class AlwaysTimesOut(UrllibTransport):
        def _fetch(self, url: str) -> Any:
            calls["n"] += 1
            raise TimeoutError("simulated")

    transport = AlwaysTimesOut(timeout=0.01, retries=2, backoff_base=0)
    with pytest.raises(MarketDataError, match="after 2 attempts"):
        transport.get_json("https://example.invalid/klines")
    assert calls["n"] == 2
