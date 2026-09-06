"""Content-hashed local kline cache (DATA-04).

A backtest must be reproducible byte-for-byte. The cache stores the series and its
content hash together, so a later run can prove it used the same data rather than
assuming it did.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from atlas.data.models import Kline, KlineSeries, Timeframe, datetime_to_ms, ms_to_datetime
from atlas.errors import PersistenceError


class KlineStore:
    """Filesystem cache. One gzipped JSON file per (symbol, timeframe)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, symbol: str, timeframe: Timeframe) -> Path:
        return self.root / f"{symbol.upper()}_{timeframe}.json.gz"

    def save(self, series: KlineSeries) -> str:
        """Persist a series. Returns its content hash."""
        content_hash = series.content_hash()
        payload: dict[str, Any] = {
            "symbol": series.symbol,
            "timeframe": str(series.timeframe),
            "content_hash": content_hash,
            "bar_count": len(series.bars),
            "bars": [
                [
                    datetime_to_ms(bar.open_time),
                    str(bar.open),
                    str(bar.high),
                    str(bar.low),
                    str(bar.close),
                    str(bar.volume),
                    datetime_to_ms(bar.close_time),
                    bar.trades,
                ]
                for bar in series.bars
            ],
        }
        path = self.path_for(series.symbol, series.timeframe)
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
        tmp.replace(path)
        return content_hash

    def load(self, symbol: str, timeframe: Timeframe) -> KlineSeries | None:
        """Load a cached series, verifying its content hash.

        A hash mismatch raises rather than returning the data: silently using a
        corrupted or hand-edited cache would undermine every downstream result.
        """
        path = self.path_for(symbol, timeframe)
        if not path.exists():
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise PersistenceError(f"kline cache unreadable at {path}: {exc}") from exc

        series = KlineSeries(
            symbol=str(payload["symbol"]),
            timeframe=Timeframe(payload["timeframe"]),
            bars=tuple(
                Kline(
                    open_time=ms_to_datetime(int(row[0])),
                    open=row[1],
                    high=row[2],
                    low=row[3],
                    close=row[4],
                    volume=row[5],
                    close_time=ms_to_datetime(int(row[6])),
                    trades=int(row[7]),
                )
                for row in payload["bars"]
            ),
        )
        stored_hash = str(payload.get("content_hash", ""))
        actual_hash = series.content_hash()
        if stored_hash != actual_hash:
            raise PersistenceError(
                f"kline cache integrity failure at {path}: stored hash {stored_hash[:12]} "
                f"does not match content {actual_hash[:12]}"
            )
        return series

    def coverage(self, symbol: str, timeframe: Timeframe) -> tuple[datetime, datetime] | None:
        series = self.load(symbol, timeframe)
        if series is None or not series.bars:
            return None
        start, end = series.start, series.end
        if start is None or end is None:
            return None
        return start, end
