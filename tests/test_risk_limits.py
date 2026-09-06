"""RISK-05, RISK-06, RISK-08, RISK-09 and their kill-switch triggers."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from atlas.models import ExchangeEnv, KillSwitchTrigger
from atlas.risk.filters import ExchangeFilterCache, ExchangeInfoError, parse_symbol_filters
from atlas.risk.limits import (
    AccountState,
    PortfolioLimits,
    can_open_symbol,
    check_portfolio_limits,
)

D = Decimal
TODAY = date(2026, 9, 6)


def account(equity: str, peak: str = "100", day_start: str = "100", **kw: object) -> AccountState:
    return AccountState(
        equity=D(equity),
        peak_equity=D(peak),
        day_start_equity=D(day_start),
        as_of=TODAY,
        **kw,  # type: ignore[arg-type]
    )


# ------------------------------------------------------------------ portfolio limits


def test_healthy_account_has_no_breach() -> None:
    assert check_portfolio_limits(account("98")) is None


def test_daily_loss_limit_arms_kill_switch() -> None:
    """RISK-05: 5% in one UTC day."""
    breach = check_portfolio_limits(account("94", peak="100", day_start="100"))
    assert breach is not None
    assert breach.trigger is KillSwitchTrigger.DAILY_LOSS_LIMIT


def test_daily_loss_just_under_limit_passes() -> None:
    assert check_portfolio_limits(account("95.5", day_start="100")) is None


def test_account_drawdown_arms_kill_switch() -> None:
    """RISK-06: 20% from peak."""
    breach = check_portfolio_limits(account("79", peak="100", day_start="80"))
    assert breach is not None
    assert breach.trigger is KillSwitchTrigger.MAX_ACCOUNT_DD


def test_drawdown_takes_precedence_over_daily_loss() -> None:
    """When both are breached, the more serious condition is the recorded reason."""
    breach = check_portfolio_limits(account("70", peak="100", day_start="100"))
    assert breach is not None
    assert breach.trigger is KillSwitchTrigger.MAX_ACCOUNT_DD


def test_drawdown_measured_from_peak_not_start() -> None:
    """An account up to 200 then back to 160 is 20% down even though it is up overall."""
    breach = check_portfolio_limits(account("160", peak="200", day_start="165"))
    assert breach is not None
    assert breach.trigger is KillSwitchTrigger.MAX_ACCOUNT_DD


def test_gains_produce_no_breach() -> None:
    assert check_portfolio_limits(account("130", peak="130", day_start="100")) is None


def test_limits_are_configurable() -> None:
    strict = PortfolioLimits(daily_loss_limit=D("0.01"), max_account_drawdown=D("0.05"))
    assert check_portfolio_limits(account("98.5"), strict) is not None


def test_zero_peak_equity_does_not_divide_by_zero() -> None:
    state = AccountState(equity=D("0"), peak_equity=D("0"), day_start_equity=D("0"), as_of=TODAY)
    assert check_portfolio_limits(state) is None


def test_one_position_per_symbol() -> None:
    """RISK-08: no pyramiding, averaging down or hedging."""
    state = account("100", open_symbols=frozenset({"BTCUSDT"}))
    assert not can_open_symbol(state, "BTCUSDT")
    assert not can_open_symbol(state, "btcusdt")
    assert can_open_symbol(state, "ETHUSDT")


# ------------------------------------------------------------------ exchange filters


SYMBOL_PAYLOAD = {
    "symbol": "BTCUSDT",
    "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
        {"filterType": "LOT_SIZE", "stepSize": "0.00001000", "minQty": "0.00001000"},
        {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
    ],
}


def test_parse_symbol_filters() -> None:
    filters = parse_symbol_filters(SYMBOL_PAYLOAD)
    assert filters.tick_size == D("0.01")
    assert filters.step_size == D("0.00001")
    assert filters.min_qty == D("0.00001")
    assert filters.min_notional == D("5")


def test_missing_filter_raises_rather_than_assuming() -> None:
    """RISK-09: never size against assumed values."""
    payload = {
        "symbol": "BTCUSDT",
        "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"}],
    }
    with pytest.raises(ExchangeInfoError, match="missing filters"):
        parse_symbol_filters(payload)


def test_legacy_min_notional_filter_accepted() -> None:
    payload = dict(SYMBOL_PAYLOAD)
    payload["filters"] = [
        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
        {"filterType": "MIN_NOTIONAL", "minNotional": "10.0"},
    ]
    assert parse_symbol_filters(payload).min_notional == D("10")


class StubTransport:
    def __init__(self) -> None:
        self.calls = 0

    def get_json(self, url: str) -> object:
        self.calls += 1
        return {"symbols": [SYMBOL_PAYLOAD]}


def test_filter_cache_fetches_once() -> None:
    transport = StubTransport()
    cache = ExchangeFilterCache(transport=transport)
    first = cache.get("BTCUSDT")
    second = cache.get("BTCUSDT")
    assert first == second
    assert transport.calls == 1


def test_filter_cache_force_refresh() -> None:
    transport = StubTransport()
    cache = ExchangeFilterCache(transport=transport)
    cache.get("BTCUSDT")
    cache.get("BTCUSDT", force=True)
    assert transport.calls == 2


def test_filter_cache_expires() -> None:
    transport = StubTransport()
    cache = ExchangeFilterCache(transport=transport, max_age_seconds=0)
    cache.get("BTCUSDT")
    cache.get("BTCUSDT")
    assert transport.calls == 2


def test_empty_exchange_info_raises() -> None:
    class Empty:
        def get_json(self, url: str) -> object:
            return {"symbols": []}

    with pytest.raises(ExchangeInfoError, match="no entry"):
        ExchangeFilterCache(transport=Empty()).get("NOPE")


def test_testnet_is_the_default() -> None:
    assert ExchangeFilterCache(transport=StubTransport()).base_url.endswith("binance.vision")


def test_live_uses_mainnet() -> None:
    cache = ExchangeFilterCache(ExchangeEnv.LIVE, transport=StubTransport())
    assert "api.binance.com" in cache.base_url
