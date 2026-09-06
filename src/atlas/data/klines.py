"""Binance kline client (DATA-01, DATA-02).

Transport is injected so tests never touch the network, and so the default can be
replaced without touching fetch logic. The default uses `urllib` from the standard
library: the path that talks to the exchange carries no third-party dependency.

The client drops the in-progress candle (DATA-02). Binance returns the forming bar as
the last element of a live query; evaluating it would mean acting on a price that can
still change.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from atlas.data.models import (
    Kline,
    KlineSeries,
    Timeframe,
    datetime_to_ms,
    ms_to_datetime,
)
from atlas.errors import AtlasError
from atlas.models import ExchangeEnv, utcnow

MAINNET_BASE = "https://api.binance.com"
TESTNET_BASE = "https://testnet.binance.vision"
MAX_LIMIT = 1000


class MarketDataError(AtlasError):
    """The exchange could not be reached, or returned something unusable."""


class KlineTransport(Protocol):
    """Minimal HTTP contract. Implementations must apply a timeout."""

    def get_json(self, url: str) -> Any: ...


class UrllibTransport:
    """Standard-library transport with bounded retries and exponential backoff.

    Retries only transient conditions: timeouts, 5xx, and 429. A 4xx other than 429 is
    a request defect and retrying it just burns rate limit.
    """

    def __init__(self, timeout: float = 15.0, retries: int = 4, backoff_base: float = 2.0) -> None:
        self.timeout = timeout
        self.retries = retries
        self.backoff_base = backoff_base

    def _fetch(self, url: str) -> Any:
        """Perform one HTTP GET. Overridden in tests; carries no retry policy."""
        request = urllib.request.Request(url, headers={"User-Agent": "atlas/0.1"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def get_json(self, url: str) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                return self._fetch(url)
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504):
                    raise MarketDataError(f"HTTP {exc.code} from {url}") from exc
                last_error = exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                last_error = exc
            if attempt < self.retries - 1 and self.backoff_base > 0:
                time.sleep(self.backoff_base**attempt)
        raise MarketDataError(f"failed to fetch {url} after {self.retries} attempts: {last_error}")


def _parse_kline(row: list[Any]) -> Kline:
    """Parse one Binance kline row.

    Layout: [openTime, open, high, low, close, volume, closeTime, quoteVolume,
    trades, takerBuyBase, takerBuyQuote, ignore].
    """
    return Kline(
        open_time=ms_to_datetime(int(row[0])),
        open=Decimal(str(row[1])),
        high=Decimal(str(row[2])),
        low=Decimal(str(row[3])),
        close=Decimal(str(row[4])),
        volume=Decimal(str(row[5])),
        close_time=ms_to_datetime(int(row[6])),
        trades=int(row[8]),
    )


class BinanceKlineClient:
    """Fetches closed klines from Binance REST."""

    def __init__(
        self,
        exchange_env: ExchangeEnv = ExchangeEnv.TESTNET,
        transport: KlineTransport | None = None,
    ) -> None:
        self.base_url = MAINNET_BASE if exchange_env is ExchangeEnv.LIVE else TESTNET_BASE
        self.exchange_env = exchange_env
        self._transport = transport or UrllibTransport()

    def _url(self, symbol: str, timeframe: Timeframe, start_ms: int | None, limit: int) -> str:
        params: dict[str, str | int] = {
            "symbol": symbol.upper(),
            "interval": str(timeframe),
            "limit": min(limit, MAX_LIMIT),
        }
        if start_ms is not None:
            params["startTime"] = start_ms
        return f"{self.base_url}/api/v3/klines?{urllib.parse.urlencode(params)}"

    def fetch(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        max_bars: int = 20_000,
        now: datetime | None = None,
    ) -> KlineSeries:
        """Fetch closed klines, paginating forward until `end` or `max_bars`.

        The in-progress bar is excluded (DATA-02): a bar is kept only once its
        `close_time` has passed.
        """
        reference = now or utcnow()
        cursor_ms = datetime_to_ms(start) if start else None
        end_ms = datetime_to_ms(end) if end else None
        collected: list[Kline] = []
        seen: set[int] = set()

        while len(collected) < max_bars:
            rows = self._transport.get_json(self._url(symbol, timeframe, cursor_ms, MAX_LIMIT))
            if not isinstance(rows, list):
                raise MarketDataError(f"unexpected kline payload for {symbol}: {type(rows)}")
            if not rows:
                break

            progressed = False
            for row in rows:
                bar = _parse_kline(row)
                open_ms = datetime_to_ms(bar.open_time)
                if open_ms in seen:
                    continue
                if end_ms is not None and datetime_to_ms(bar.close_time) > end_ms:
                    continue
                # DATA-02: exclude the forming bar.
                if bar.close_time > reference:
                    continue
                seen.add(open_ms)
                collected.append(bar)
                progressed = True

            last_open_ms = int(rows[-1][0])
            if not progressed and cursor_ms is not None and last_open_ms <= cursor_ms:
                break
            if len(rows) < MAX_LIMIT:
                break
            cursor_ms = last_open_ms + timeframe.milliseconds

        collected.sort(key=lambda bar: bar.open_time)
        return KlineSeries(
            symbol=symbol.upper(),
            timeframe=timeframe,
            bars=tuple(collected[:max_bars]),
        )
