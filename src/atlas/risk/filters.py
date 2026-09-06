"""Live exchange filters (RISK-09).

`LOT_SIZE`, `NOTIONAL` and `PRICE_FILTER` are read from Binance `exchangeInfo` at
startup and refreshed daily. Never hardcoded: real values differ per symbol and change
without notice, and a stale minNotional silently changes which strategies are tradeable.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from atlas.data.klines import MAINNET_BASE, TESTNET_BASE, KlineTransport, UrllibTransport
from atlas.errors import AtlasError
from atlas.models import ExchangeEnv, utcnow
from atlas.risk.sizing import ExchangeFilters


class ExchangeInfoError(AtlasError):
    """Exchange filters could not be obtained or parsed."""


def parse_symbol_filters(payload: dict[str, Any]) -> ExchangeFilters:
    """Extract the filters ATLAS sizes with from one `exchangeInfo` symbol entry."""
    by_type = {f.get("filterType"): f for f in payload.get("filters", [])}

    lot = by_type.get("LOT_SIZE")
    price = by_type.get("PRICE_FILTER")
    notional = by_type.get("NOTIONAL") or by_type.get("MIN_NOTIONAL")

    if lot is None or price is None or notional is None:
        missing = [
            name
            for name, value in (("LOT_SIZE", lot), ("PRICE_FILTER", price), ("NOTIONAL", notional))
            if value is None
        ]
        raise ExchangeInfoError(
            f"symbol {payload.get('symbol')} is missing filters: {missing}; "
            "refusing to size against assumed values"
        )

    return ExchangeFilters(
        step_size=Decimal(str(lot["stepSize"])),
        min_qty=Decimal(str(lot["minQty"])),
        min_notional=Decimal(str(notional.get("minNotional", notional.get("notional")))),
        tick_size=Decimal(str(price["tickSize"])),
    )


class ExchangeFilterCache:
    """Fetches and caches per-symbol filters, refreshing on an age bound."""

    def __init__(
        self,
        exchange_env: ExchangeEnv = ExchangeEnv.TESTNET,
        transport: KlineTransport | None = None,
        max_age_seconds: float = 86_400.0,
    ) -> None:
        self.base_url = MAINNET_BASE if exchange_env is ExchangeEnv.LIVE else TESTNET_BASE
        self._transport = transport or UrllibTransport()
        self._max_age = max_age_seconds
        self._cache: dict[str, tuple[ExchangeFilters, float]] = {}

    def get(self, symbol: str, *, force: bool = False) -> ExchangeFilters:
        key = symbol.upper()
        now = utcnow().timestamp()
        cached = self._cache.get(key)
        if cached is not None and not force and now - cached[1] < self._max_age:
            return cached[0]

        payload = self._transport.get_json(f"{self.base_url}/api/v3/exchangeInfo?symbol={key}")
        if not isinstance(payload, dict):
            raise ExchangeInfoError(f"unexpected exchangeInfo payload for {key}")
        symbols = payload.get("symbols") or []
        if not symbols:
            raise ExchangeInfoError(f"exchangeInfo returned no entry for {key}")

        filters = parse_symbol_filters(symbols[0])
        self._cache[key] = (filters, now)
        return filters

    def is_cached(self, symbol: str) -> bool:
        return symbol.upper() in self._cache
