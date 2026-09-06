"""Account valuation and live exchange filters (RISK-04, RISK-05, RISK-06, RISK-09).

Every test here fails against the code as it stood before this module was written. The
three defects were only reachable from the assembled runtime, so unit tests of the
components each passed while the system they formed would have halted itself on its
first fill and sized every order against filters nobody read from the exchange.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import StubFilterProvider
from tests.test_backtest import POLICY
from tests.test_execution import StubTransport, ack
from tests.test_runtime import NOW, _live_strategy, _signal_series

from atlas.audit import AuditLog
from atlas.config import RiskSettings, Settings
from atlas.db.engine import Database
from atlas.errors import AtlasError, ConfigurationError
from atlas.execution.broker import AssetBalance, BinanceSpotBroker
from atlas.killswitch import KillSwitch
from atlas.models import ExchangeEnv, KillSwitchTrigger, PositionSide
from atlas.risk.filters import ExchangeFilterCache, StaticFilterProvider
from atlas.risk.limits import AccountState
from atlas.risk.sizing import ExchangeFilters, RejectReason
from atlas.runtime.service import build_service
from atlas.runtime.trading_service import TradingService

D = Decimal
TODAY = date(2026, 9, 6)

RISK_ENV = {
    "ATLAS_RISK_PCT": "0.01",
    "ATLAS_MAX_CONCURRENT": "3",
    "ATLAS_MAX_POSITION_PCT": "0.3333",
    "ATLAS_MAX_DEPLOYED_PCT": "0.75",
    "ATLAS_DAILY_LOSS_LIMIT": "0.05",
    "ATLAS_MAX_ACCOUNT_DD": "0.20",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for k, v in RISK_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("ATLAS_BINANCE_API_KEY", "test-key")
    monkeypatch.setenv("ATLAS_BINANCE_API_SECRET", "test-secret")
    monkeypatch.chdir(tmp_path)


def _service(env: None) -> Any:
    return build_service(Settings(), RiskSettings())  # type: ignore[call-arg]


def _balances(svc: Any, free: str, locked: str = "0") -> None:
    """Replace the signed balance call. No network, no credentials in play."""

    def snapshot() -> dict[str, AssetBalance]:
        return {"USDT": AssetBalance(asset="USDT", free=D(free), locked=D(locked))}

    svc.broker.account_snapshot = snapshot  # type: ignore[method-assign]


# ------------------------------------------------------- equity is not just cash


def test_open_position_does_not_read_as_a_loss(env: None) -> None:
    """RISK-05/RISK-06 regression.

    An entry converts quote into base. Valuing only the quote balance would report the
    account as having lost the whole position notional the instant the fill landed --
    at $100 with a 33% cap, a fabricated 33% drawdown on the first trade, which arms
    the kill switch for a position that has not moved a cent.
    """
    svc = _service(env)
    try:
        _balances(svc, free="70")  # $30 of the $100 is now in BTC
        svc.ledger.open_position(
            _live_strategy(svc.db),
            "BTCUSDT",
            PositionSide.LONG,
            D("0.001"),
            D("30000"),
            D("29000"),
            D("32000"),
        )

        state = svc.account_state({"BTCUSDT": D("30000")})

        assert state.equity == D("100.000")  # 70 cash + 0.001 * 30000
        assert state.free_cash == D("70")
        assert state.deployed == D("30.000")
        assert state.valuation_complete
        assert state.drawdown_from_peak == 0
    finally:
        svc.close()


def test_equity_moves_with_the_position_not_with_the_fill(env: None) -> None:
    """A real loss must still be visible: the mark falls, so equity falls."""
    svc = _service(env)
    try:
        _balances(svc, free="70")
        svc.ledger.open_position(
            _live_strategy(svc.db),
            "BTCUSDT",
            PositionSide.LONG,
            D("0.001"),
            D("30000"),
            D("29000"),
            D("32000"),
        )
        state = svc.account_state({"BTCUSDT": D("20000")})
        assert state.equity == D("90.000")  # 70 cash + 0.001 * 20000
    finally:
        svc.close()


def test_locked_quote_is_still_equity(env: None) -> None:
    """Cash behind a resting order has not left the account."""
    svc = _service(env)
    try:
        _balances(svc, free="60", locked="40")
        state = svc.account_state({})
        assert state.equity == D("100")
        assert state.free_cash == D("60")  # but only the free part can fund a new entry
    finally:
        svc.close()


def test_a_position_held_behind_a_protective_order_is_still_valued(env: None) -> None:
    """Base asset locked by a resting stop is reported `locked`, never `free`.

    Positions are therefore valued from the ledger, not from base-asset balances: a
    reader of `free` alone would value a fully protected account at roughly zero.
    """
    svc = _service(env)
    try:
        _balances(svc, free="70")  # exchange reports no *free* BTC at all
        svc.ledger.open_position(
            _live_strategy(svc.db),
            "BTCUSDT",
            PositionSide.LONG,
            D("0.001"),
            D("30000"),
            D("29000"),
            D("32000"),
        )
        assert svc.account_state({"BTCUSDT": D("30000")}).deployed == D("30.000")
    finally:
        svc.close()


# ------------------------------------------------------------ incomplete valuation


def test_unpriced_position_marks_the_valuation_incomplete(env: None) -> None:
    """A missing price is not a zero price."""
    svc = _service(env)
    try:
        _balances(svc, free="70")
        svc.ledger.open_position(
            _live_strategy(svc.db),
            "BTCUSDT",
            PositionSide.LONG,
            D("0.001"),
            D("30000"),
            D("29000"),
            D("32000"),
        )
        state = svc.account_state({})  # market data failed for BTCUSDT this tick
        assert not state.valuation_complete
        assert state.unpriced_symbols == frozenset({"BTCUSDT"})
    finally:
        svc.close()


def test_incomplete_valuation_is_never_persisted(env: None) -> None:
    """`peak_equity` and `day_start_equity` are read back from this table.

    One understated row biases every drawdown comparison made afterwards, including on
    days long after the data gap closed.
    """
    svc = _service(env)
    try:
        state = AccountState(
            equity=D("70"),
            peak_equity=D("100"),
            day_start_equity=D("100"),
            as_of=TODAY,
            free_cash=D("70"),
            valuation_complete=False,
            unpriced_symbols=frozenset({"BTCUSDT"}),
        )
        with pytest.raises(AtlasError, match="incomplete account valuation"):
            svc.record_equity(state)
        rows = svc.db.connection.execute("SELECT COUNT(*) AS n FROM equity_snapshots").fetchone()
        assert rows["n"] == 0
    finally:
        svc.close()


def test_incomplete_valuation_suspends_entries_without_arming(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """The safe response to not knowing equity is to stop adding risk -- not to halt.

    Arming would demand a human to clear a switch that a transient market-data gap
    tripped. Staleness has its own trigger (DATA-05) and is checked separately.
    """
    killswitch.initialise()
    _live_strategy(db)
    transport = StubTransport(ack("entry"), ack("stop", status="NEW"))
    broker = BinanceSpotBroker(
        "k", "s", killswitch, audit, exchange_env=ExchangeEnv.TESTNET, transport=transport
    )
    service = TradingService(
        db, audit, killswitch, broker, policy=POLICY, filters=StubFilterProvider()
    )

    account = AccountState(
        equity=D("40"),  # understated: it is missing the open position
        peak_equity=D("100"),
        day_start_equity=D("100"),
        as_of=TODAY,
        free_cash=D("40"),
        valuation_complete=False,
        unpriced_symbols=frozenset({"BTCUSDT"}),
    )
    result = service.tick(
        account=account,
        series_by_symbol={"BTCUSDT": _signal_series(NOW)},
        exchange_orders=[],
        live_returns={},
        backtest_stats={},
        now=NOW,
    )

    # A 60% apparent drawdown would have armed MAX_ACCOUNT_DD had limits been evaluated.
    assert result.entries_suspended
    assert result.entries_placed == 0
    assert not result.halted
    assert not killswitch.is_armed()
    assert any(e.payload.get("event") == "valuation_incomplete" for e in audit.tail(20))


# ------------------------------------------------------------------- RISK-04 deployed


def test_deployed_capital_reaches_the_sizer(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """RISK-04 regression: the tick used to pass `deployed=0` unconditionally.

    With the cap never binding, a portfolio could deploy every cent of equity while the
    75% limit reported itself as satisfied.
    """
    killswitch.initialise()
    _live_strategy(db)
    transport = StubTransport(ack("entry"), ack("stop", status="NEW"))
    broker = BinanceSpotBroker(
        "k", "s", killswitch, audit, exchange_env=ExchangeEnv.TESTNET, transport=transport
    )
    service = TradingService(
        db, audit, killswitch, broker, policy=POLICY, filters=StubFilterProvider()
    )

    account = AccountState(
        equity=D("100"),
        peak_equity=D("100"),
        day_start_equity=D("100"),
        as_of=TODAY,
        free_cash=D("25"),
        deployed=D("75"),  # already at MAX_DEPLOYED_PCT
    )
    result = service.tick(
        account=account,
        series_by_symbol={"BTCUSDT": _signal_series(NOW)},
        exchange_orders=[],
        live_returns={},
        backtest_stats={},
        now=NOW,
    )

    assert result.entries_placed == 0
    assert result.entries_rejected == 1
    reasons = [e.payload.get("reason") for e in audit.tail(20)]
    assert str(RejectReason.MAX_DEPLOYED) in reasons


# --------------------------------------------------------------- RISK-09 live filters


def test_trading_service_refuses_fixed_filters(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """RISK-09 says NEVER hardcoded. This makes it structural rather than advisory."""
    broker = BinanceSpotBroker(
        "k", "s", killswitch, audit, exchange_env=ExchangeEnv.TESTNET, transport=StubTransport()
    )
    with pytest.raises(ConfigurationError, match="live exchange filter provider"):
        TradingService(db, audit, killswitch, broker, policy=POLICY, filters=StaticFilterProvider())


def test_unavailable_filters_decline_the_entry(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """Not knowing the exchange's minimum is not the same as there being none."""
    killswitch.initialise()
    _live_strategy(db)
    transport = StubTransport(ack("entry"), ack("stop", status="NEW"))
    broker = BinanceSpotBroker(
        "k", "s", killswitch, audit, exchange_env=ExchangeEnv.TESTNET, transport=transport
    )
    service = TradingService(
        db,
        audit,
        killswitch,
        broker,
        policy=POLICY,
        filters=StubFilterProvider(fail_for="BTCUSDT"),
    )

    result = service.tick(
        account=AccountState(
            equity=D("100"),
            peak_equity=D("100"),
            day_start_equity=D("100"),
            as_of=TODAY,
            free_cash=D("100"),
        ),
        series_by_symbol={"BTCUSDT": _signal_series(NOW)},
        exchange_orders=[],
        live_returns={},
        backtest_stats={},
        now=NOW,
    )

    assert result.entries_placed == 0
    assert result.entries_rejected == 1
    assert transport.posts == []  # nothing was transmitted
    reasons = [e.payload.get("reason") for e in audit.tail(20)]
    assert str(RejectReason.FILTERS_UNAVAILABLE) in reasons


def test_the_symbols_own_min_notional_binds(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """Proves the per-symbol filters actually reach the sizer.

    Against the previous global default of minNotional $5 this entry was accepted; the
    real value for this symbol rejects it.
    """
    killswitch.initialise()
    _live_strategy(db)
    transport = StubTransport(ack("entry"), ack("stop", status="NEW"))
    broker = BinanceSpotBroker(
        "k", "s", killswitch, audit, exchange_env=ExchangeEnv.TESTNET, transport=transport
    )
    provider = StubFilterProvider(
        ExchangeFilters(
            step_size=D("0.00000001"),
            min_qty=D("0.00000001"),
            min_notional=D("500"),  # far above anything a $100 account can size
            tick_size=D("0.01"),
        )
    )
    service = TradingService(db, audit, killswitch, broker, policy=POLICY, filters=provider)

    result = service.tick(
        account=AccountState(
            equity=D("100"),
            peak_equity=D("100"),
            day_start_equity=D("100"),
            as_of=TODAY,
            free_cash=D("100"),
        ),
        series_by_symbol={"BTCUSDT": _signal_series(NOW)},
        exchange_orders=[],
        live_returns={},
        backtest_stats={},
        now=NOW,
    )

    assert provider.calls == ["BTCUSDT"]
    assert result.entries_rejected == 1
    reasons = [e.payload.get("reason") for e in audit.tail(20)]
    assert str(RejectReason.BELOW_MIN_NOTIONAL) in reasons


def test_the_runtime_wires_a_live_filter_provider(env: None) -> None:
    """The original defect: `build_service` passed the assumed defaults straight in."""
    svc = _service(env)
    try:
        assert isinstance(svc.filters, ExchangeFilterCache)
        assert svc.filters.is_live
        assert svc.trading._filters is svc.filters
    finally:
        svc.close()


# ------------------------------------------------------------------------ preflight


def _preflightable(svc: Any, *, drift_ms: int = 0, filters: Any = None) -> Any:
    """Give the service scripted exchange answers. No network, no real credentials."""
    from atlas.models import utcnow

    svc.broker.server_time = lambda: int(utcnow().timestamp() * 1000) + drift_ms  # type: ignore[method-assign]
    svc.broker.open_orders = lambda symbol=None: []  # type: ignore[method-assign]
    _balances(svc, free="100")
    svc.filters = filters or StubFilterProvider()
    return svc


def test_preflight_reports_the_real_symbol_filters(env: None) -> None:
    """RISK-09 is proved readable at startup, not at the moment an order is sized."""
    svc = _preflightable(_service(env))
    try:
        report = svc.preflight()
        assert report["exchange_reachable"] == "yes"
        assert report["authenticated"] == "yes"
        assert "minNotional=1" in report["filters_BTCUSDT"]
        assert "stepSize=1E-8" in report["filters_BTCUSDT"]
        assert svc.filters.calls == ["BTCUSDT"]
    finally:
        svc.close()


def test_preflight_names_clock_drift_for_what_it_is(env: None) -> None:
    """Past recvWindow every signed request fails as -1021 and reads as a bad key."""
    svc = _preflightable(_service(env), drift_ms=9_000)
    try:
        report = svc.preflight()
        assert "clock_drift_warning" in report
        assert "recvWindow" in report["clock_drift_warning"]
        assert "clock" in report["clock_drift_warning"]
    finally:
        svc.close()


def test_preflight_tolerates_drift_inside_the_window(env: None) -> None:
    svc = _preflightable(_service(env), drift_ms=500)
    try:
        report = svc.preflight()
        assert "clock_drift_warning" not in report
        assert report["recv_window_ms"] == "5000"
    finally:
        svc.close()


def test_preflight_reports_unreadable_filters_without_failing(env: None) -> None:
    """Preflight is a diagnostic. It reports every fault it finds, not just the first."""
    svc = _preflightable(_service(env), filters=StubFilterProvider(fail_for="BTCUSDT"))
    try:
        report = svc.preflight()
        assert report["filters_BTCUSDT"].startswith("UNAVAILABLE")
        assert report["authenticated"] == "yes"  # the run continued
    finally:
        svc.close()


def test_preflight_computes_the_feasible_stop_band_from_the_real_minimum(env: None) -> None:
    """Specification section 6 uses $5 illustratively; RISK-09 supplies the real value.

    The upper bound is (equity * RISK_PCT) / minNotional, so the exchange's own minimum
    decides which stop distances a $100 account can actually trade.
    """
    svc = _preflightable(
        _service(env),
        filters=StubFilterProvider(
            ExchangeFilters(
                step_size=D("0.00000001"),
                min_qty=D("0.00000001"),
                min_notional=D("10"),
                tick_size=D("0.01"),
            )
        ),
    )
    try:
        band = svc.preflight()["feasible_stop_band_BTCUSDT"]
        # lower = 0.01/0.3333 = 0.0300; upper = (100 * 0.01)/10 = 0.1000
        assert band.startswith("0.0300..0.1000")
        assert "EMPTY" not in band
    finally:
        svc.close()


def test_preflight_flags_a_balance_too_small_to_trade(env: None) -> None:
    """When the upper bound falls below the structural floor, nothing is tradeable."""
    svc = _preflightable(
        _service(env),
        filters=StubFilterProvider(
            ExchangeFilters(
                step_size=D("0.00000001"),
                min_qty=D("0.00000001"),
                min_notional=D("100"),
                tick_size=D("0.01"),
            )
        ),
    )
    try:
        _balances(svc, free="20")  # upper = (20 * 0.01)/100 = 0.002 < 0.03 floor
        assert "EMPTY" in svc.preflight()["feasible_stop_band_BTCUSDT"]
    finally:
        svc.close()


# ------------------------------------------------------------- the assembled tick


def test_a_tick_that_cannot_price_a_position_records_no_snapshot(env: None) -> None:
    """End to end: market data fails, a position is open, nothing is persisted.

    The tick still completes and still beats the heartbeat -- an unattended process
    that dies on a data gap is not unattended -- but it writes no equity row and takes
    no entry.
    """
    from atlas.data.klines import MarketDataError

    svc = _service(env)
    try:
        svc.killswitch.initialise()
        svc.ledger.open_position(
            _live_strategy(svc.db),
            "BTCUSDT",
            PositionSide.LONG,
            D("0.001"),
            D("30000"),
            D("29000"),
            D("32000"),
        )
        _balances(svc, free="70")
        svc.broker.open_orders = lambda symbol=None: []  # type: ignore[method-assign]
        svc.ingestor.ingest_all = lambda symbols: None  # type: ignore[method-assign]

        def fail(*a: object, **k: object) -> None:
            raise MarketDataError("egress blocked")

        svc.klines.fetch = fail  # type: ignore[method-assign]

        result = svc.tick()

        assert result.entries_suspended
        assert result.entries_placed == 0
        rows = svc.db.connection.execute("SELECT COUNT(*) AS n FROM equity_snapshots").fetchone()
        assert rows["n"] == 0
        assert svc.heartbeat.read() is not None
        assert not svc.killswitch.is_armed()
    finally:
        svc.close()


def test_an_armed_switch_outranks_a_suspension(
    db: Database, audit: AuditLog, killswitch: KillSwitch
) -> None:
    """Both conditions at once must report the one that needs a human."""
    killswitch.initialise()
    killswitch.arm(KillSwitchTrigger.MANUAL, "operator halted trading", "test")
    _live_strategy(db)
    broker = BinanceSpotBroker(
        "k", "s", killswitch, audit, exchange_env=ExchangeEnv.TESTNET, transport=StubTransport()
    )
    service = TradingService(
        db, audit, killswitch, broker, policy=POLICY, filters=StubFilterProvider()
    )

    result = service.tick(
        account=AccountState(
            equity=D("40"),
            peak_equity=D("100"),
            day_start_equity=D("100"),
            as_of=TODAY,
            free_cash=D("40"),
            valuation_complete=False,
            unpriced_symbols=frozenset({"BTCUSDT"}),
        ),
        series_by_symbol={"BTCUSDT": _signal_series(NOW)},
        exchange_orders=[],
        live_returns={},
        backtest_stats={},
        now=NOW,
    )

    assert result.halted
    assert "operator halted trading" in result.halt_reason
    assert result.entries_placed == 0
