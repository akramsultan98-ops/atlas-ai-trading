"""Live exchange filters (RISK-09).

`LOT_SIZE`, `NOTIONAL` and `PRICE_FILTER` are read from Binance `exchangeInfo` at
startup and refreshed daily. Never hardcoded: real values differ per symbol and change
without notice, and a stale minNotional silently changes which strategies are tradeable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, ClassVar, Protocol

from atlas.data.klines import MAINNET_BASE, TESTNET_BASE, KlineTransport, UrllibTransport
from atlas.errors import AtlasError
from atlas.models import ExchangeEnv, utcnow
from atlas.risk.sizing import ExchangeFilters


class ExchangeInfoError(AtlasError):
    """Exchange filters could not be obtained or parsed."""


class SymbolFilterProvider(Protocol):
    """Supplies the filters one symbol must be sized against.

    Filters are per symbol, not per account: `LOT_SIZE` and `NOTIONAL` differ between
    BTCUSDT and a low-priced altcoin by orders of magnitude. A provider that answered
    the same values for every symbol would size correctly for one of them by accident.

    `is_live` states whether the values come from the exchange. The live trading path
    asserts it (RISK-09); backtests do not, because a backtest deliberately holds its
    economics fixed.
    """

    is_live: ClassVar[bool]

    def get(self, symbol: str, *, force: bool = False) -> ExchangeFilters: ...


@dataclass(frozen=True)
class StaticFilterProvider:
    """A fixed filter set, for backtests, research and tests.

    Explicitly not live. Passing one of these into the live trading path is refused at
    construction — sizing a real order against assumed filters is how an order gets
    rejected for `LOT_SIZE` at best, and mis-sized at worst.
    """

    filters: ExchangeFilters = field(default_factory=ExchangeFilters)
    is_live: ClassVar[bool] = False

    def get(self, symbol: str, *, force: bool = False) -> ExchangeFilters:
        return self.filters


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
    """Fetches and caches per-symbol filters, refreshing on an age bound.

    The live provider (RISK-09). A fetch failure raises rather than degrading to a
    default: an unknown filter is not the same as a permissive one, and the caller must
    decline the trade rather than guess.
    """

    is_live: ClassVar[bool] = True

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
