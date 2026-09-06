"""The ATLAS service: construction and the operating loop (Phase E).

Everything before this existed as components with no assembly. This is the assembly:
configuration -> database -> market data -> broker -> ledger -> risk -> registry ->
monitor -> supervisor -> scheduler -> recovery -> notifications.

Two properties are deliberate.

The broker is constructed with an explicit transport. `BinanceSpotBroker` still refuses
to build an HTTP client implicitly, so gaining network capability is always a visible
line of code rather than a default that drifts into place.

Startup order is recovery first, then trading. A process that begins taking entries
before it has reconciled is trading against state it has not verified.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from atlas.audit import AuditLog
from atlas.config import RiskSettings, Settings
from atlas.data.klines import BinanceKlineClient, UrllibTransport
from atlas.data.models import KlineSeries, Timeframe
from atlas.data.store import KlineStore
from atlas.data.validate import validate_series
from atlas.db.engine import Database
from atlas.errors import AtlasError, ConfigurationError
from atlas.execution.broker import BinanceSpotBroker, UrllibBrokerTransport
from atlas.execution.ingest import FillIngestor
from atlas.execution.ledger import Ledger
from atlas.killswitch import KillSwitch
from atlas.models import AuditEventType, StrategyStatus, utcnow
from atlas.monitor.health import return_distribution
from atlas.notify.telegram import Alert, Severity, TelegramNotifier
from atlas.ops.heartbeat import HeartbeatStore
from atlas.risk.filters import ExchangeFilterCache, SymbolFilterProvider
from atlas.risk.limits import AccountState, PortfolioLimits
from atlas.risk.sizing import SizingPolicy
from atlas.runtime.recovery import RecoveryReport, recover
from atlas.runtime.trading_service import TickResult, TradingService
from atlas.strategy.registry import StrategyRegistry

log = logging.getLogger("atlas.runtime")
SERVICE_ACTOR = "runtime"


@dataclass
class AtlasService:
    """A constructed, runnable ATLAS instance."""

    settings: Settings
    risk: RiskSettings
    db: Database
    audit: AuditLog
    killswitch: KillSwitch
    broker: BinanceSpotBroker
    klines: BinanceKlineClient
    store: KlineStore
    ledger: Ledger
    ingestor: FillIngestor
    filters: SymbolFilterProvider
    trading: TradingService
    registry: StrategyRegistry
    heartbeat: HeartbeatStore
    notifier: TelegramNotifier | None
    tick_count: int = 0
    exchange_reachable: bool = False

    # ---------------------------------------------------------------- lifecycle

    def preflight(self) -> dict[str, str]:
        """Prove the exchange is reachable and the environment is what we think.

        Unsigned `/time` first: it separates "cannot reach the exchange" from "the
        credentials are wrong", which are different faults with different fixes.
        """
        if self.settings.is_live and self.settings.env.value != "production":
            raise ConfigurationError(
                "live exchange requires ATLAS_ENV=production; refusing to start"
            )

        result: dict[str, str] = dict(self.settings.describe())
        result["base_url"] = self.broker.base_url

        server_time = self.broker.server_time()
        result["exchange_reachable"] = "yes"
        result["server_time_ms"] = str(server_time)
        drift_ms = abs(int(utcnow().timestamp() * 1000) - server_time)
        result["clock_drift_ms"] = str(drift_ms)
        recv_window = self.broker.recv_window_ms
        result["recv_window_ms"] = str(recv_window)
        if drift_ms > recv_window:
            # Past recvWindow the exchange rejects every signed request as -1021. It
            # reads as a credential fault and is not one, so name it here.
            result["clock_drift_warning"] = (
                f"drift {drift_ms}ms exceeds recvWindow {recv_window}ms; "
                "signed requests will be rejected until the host clock is synchronised"
            )

        # RISK-09: prove the real filters are readable before trading, not at the
        # moment an order is being sized. A symbol whose filters cannot be read cannot
        # be traded, and it is better to learn that at startup.
        observed: dict[str, Decimal] = {}
        for symbol in self.settings.symbol_list:
            try:
                filters = self.filters.get(symbol, force=True)
            except (AtlasError, OSError) as exc:
                result[f"filters_{symbol}"] = f"UNAVAILABLE: {exc}"
                continue
            observed[symbol] = filters.min_notional
            result[f"filters_{symbol}"] = (
                f"minNotional={filters.min_notional} stepSize={filters.step_size} "
                f"minQty={filters.min_qty} tickSize={filters.tick_size}"
            )

        if self.settings.has_exchange_credentials():
            snapshot = self.broker.account_snapshot()
            balance = snapshot.get(self.settings.quote_asset)
            free = balance.free if balance else Decimal(0)
            locked = balance.locked if balance else Decimal(0)
            result["authenticated"] = "yes"
            result["quote_free"] = str(free)
            result["quote_locked"] = str(locked)
            result["open_orders"] = str(len(self.broker.open_orders()))

            # Specification section 6: which stop distances are actually tradeable at
            # this balance, computed from the exchange's real minNotional rather than
            # the $5 the document uses illustratively. Outside this band a strategy
            # cannot be sized at its intended risk.
            policy = SizingPolicy(
                risk_pct=self.risk.risk_pct,
                max_position_pct=self.risk.max_position_pct,
                max_deployed_pct=self.risk.max_deployed_pct,
                max_concurrent=self.risk.max_concurrent,
            )
            equity = free + locked
            for symbol, min_notional in observed.items():
                lower = policy.min_feasible_stop_distance
                upper = policy.max_feasible_stop_distance(equity, min_notional)
                result[f"feasible_stop_band_{symbol}"] = (
                    f"{lower:.4f}..{upper:.4f} at equity {equity}"
                    + ("" if upper > lower else "  EMPTY: balance too small to trade")
                )
        else:
            result["authenticated"] = "no credentials configured"
        return result

    def start(self) -> RecoveryReport:
        """Recover before trading. Never the other way round."""
        exchange_orders: list[dict[str, object]] = []
        if self.settings.has_exchange_credentials():
            exchange_orders = list(self.broker.open_orders())

        report = recover(self.db, self.audit, self.killswitch, exchange_orders)

        if self.settings.has_exchange_credentials():
            self.ingestor.ingest_all(self.settings.symbol_list)

        self.notify(
            Severity.INFO if report.may_resume_entries else Severity.WARNING,
            "ATLAS started",
            f"entries {'enabled' if report.may_resume_entries else 'HALTED'}; "
            f"{len(report.live_strategies)} live strategy(ies); "
            f"issues: {report.issues or 'none'}",
        )
        return report

    # -------------------------------------------------------------------- tick

    def fetch_market_data(self) -> dict[str, KlineSeries]:
        """Acquire and validate candles for every configured symbol (Phase C).

        A symbol that fails to fetch or validate is omitted rather than substituted.
        The trading tick treats an absent symbol as "no data this tick" and takes no
        entry, which is the correct response to not knowing the price.
        """
        timeframe = Timeframe(self.settings.timeframe)
        series_by_symbol: dict[str, KlineSeries] = {}

        for symbol in self.settings.symbol_list:
            try:
                series = self.klines.fetch(symbol, timeframe, max_bars=self.settings.history_bars)
            except Exception as exc:
                log.warning("market data fetch failed for %s: %s", symbol, exc)
                self.exchange_reachable = False
                self.audit.append(
                    AuditEventType.SYSTEM,
                    {"event": "market_data_failed", "symbol": symbol, "error": str(exc)},
                    SERVICE_ACTOR,
                )
                continue
            self.exchange_reachable = True

            report = validate_series(series)
            if not report.valid:
                log.warning("market data rejected for %s: %s", symbol, report.reasons)
                self.audit.append(
                    AuditEventType.SYSTEM,
                    {
                        "event": "market_data_rejected",
                        "symbol": symbol,
                        "reasons": report.reasons,
                    },
                    SERVICE_ACTOR,
                )
                continue

            self.store.save(series)
            series_by_symbol[symbol] = series
        return series_by_symbol

    def account_state(self, prices: Mapping[str, Decimal]) -> AccountState:
        """Value the account: quote cash plus open positions marked to market.

        Cash alone is not equity. An entry converts quote into base, so an equity figure
        built from the quote balance drops by the full position notional the instant a
        fill lands — a fabricated loss that RISK-05 and RISK-06 would read as real. At
        $100 with a 33% position cap the first fill alone would look like a 33%
        drawdown and halt the account.

        Positions are valued from the ledger rather than from base-asset balances, so a
        holding sitting behind a resting protective order (reported `locked`, not
        `free`) is still counted. Ledger and exchange are reconciled before this runs.

        A position with no price this tick is not valued at zero — it is recorded as
        unpriced and the snapshot is marked incomplete, so no limit is evaluated
        against a figure that is missing a position.
        """
        free = Decimal(0)
        locked = Decimal(0)
        if self.settings.has_exchange_credentials():
            balance = self.broker.account_snapshot().get(self.settings.quote_asset)
            if balance is not None:
                free, locked = balance.free, balance.locked

        deployed = Decimal(0)
        unpriced: set[str] = set()
        open_symbols: set[str] = set()
        for position in self.ledger.open_positions():
            open_symbols.add(position.symbol)
            price = prices.get(position.symbol)
            if price is None:
                unpriced.add(position.symbol)
                continue
            deployed += position.quantity * price

        equity = free + locked + deployed

        row = self.db.connection.execute(
            "SELECT equity, peak_equity FROM equity_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        peak = max(equity, Decimal(str(row["peak_equity"]))) if row else equity

        today = utcnow().date()
        day_row = self.db.connection.execute(
            "SELECT equity FROM equity_snapshots WHERE at >= ? ORDER BY id ASC LIMIT 1",
            (today.isoformat(),),
        ).fetchone()
        day_start = Decimal(str(day_row["equity"])) if day_row else equity

        return AccountState(
            equity=equity,
            peak_equity=peak,
            day_start_equity=day_start,
            as_of=today,
            free_cash=free,
            deployed=deployed,
            open_symbols=frozenset(open_symbols),
            valuation_complete=not unpriced,
            unpriced_symbols=frozenset(unpriced),
        )

    def record_equity(self, state: AccountState) -> None:
        """Persist a valued snapshot. Never call this with an incomplete valuation.

        The snapshot table is what `peak_equity` and `day_start_equity` are read back
        from, so one understated row biases every drawdown comparison made afterwards —
        including on days when the data gap is long gone.
        """
        if not state.valuation_complete:
            raise AtlasError(
                "refusing to record an incomplete account valuation; "
                f"unpriced: {sorted(state.unpriced_symbols)}"
            )
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO equity_snapshots(at, equity, free_cash, deployed, "
                "peak_equity, source) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    utcnow().isoformat(),
                    str(state.equity),
                    str(state.free_cash),
                    str(state.deployed),
                    str(state.peak_equity),
                    "runtime",
                ),
            )

    def tick(self) -> TickResult:
        """One full operating cycle. Reconcile, ingest, then decide.

        The heartbeat is written whether the cycle succeeds or fails. A process that
        hangs and one that errors every cycle look identical from outside unless the
        failure itself is recorded.
        """
        self.tick_count += 1
        try:
            return self._tick()
        except Exception as exc:
            self.heartbeat.beat(
                tick_count=self.tick_count,
                exchange_env=self.settings.exchange_env,
                exchange_reachable=self.exchange_reachable,
                last_error=f"{type(exc).__name__}: {exc}",
            )
            raise

    def _tick(self) -> TickResult:
        if self.settings.has_exchange_credentials():
            self.ingestor.ingest_all(self.settings.symbol_list)

        # Market data first: the account cannot be valued without prices to mark open
        # positions against, and an unvalued account must not reach the risk limits.
        series_by_symbol = self.fetch_market_data()
        prices = {
            symbol: series.bars[-1].close
            for symbol, series in series_by_symbol.items()
            if series.bars
        }

        state = self.account_state(prices)
        if state.valuation_complete:
            self.record_equity(state)
        else:
            log.warning(
                "account valuation incomplete; unpriced: %s", sorted(state.unpriced_symbols)
            )

        exchange_orders: list[dict[str, object]] = []
        if self.settings.has_exchange_credentials():
            exchange_orders = list(self.broker.open_orders())

        live_returns: dict[str, list[Decimal]] = {}
        backtest_stats: dict[str, tuple[Decimal, Decimal, Decimal]] = {}
        for strategy_id in self.registry.list_by_status(StrategyStatus.LIVE):
            returns = self.ledger.realised_returns(strategy_id)
            live_returns[strategy_id] = returns
            mean, sigma = return_distribution(returns)
            wins = sum(1 for r in returns if r > 0)
            win_rate = Decimal(wins) / len(returns) if returns else Decimal(0)
            backtest_stats[strategy_id] = (mean, sigma, win_rate)

        result = self.trading.tick(
            account=state,
            series_by_symbol=series_by_symbol,
            exchange_orders=exchange_orders,
            live_returns=live_returns,
            backtest_stats=backtest_stats,
        )

        self.heartbeat.beat(
            tick_count=self.tick_count,
            exchange_env=self.settings.exchange_env,
            exchange_reachable=self.exchange_reachable,
            last_error=None,
        )

        if result.halted:
            self.notify(Severity.CRITICAL, "ATLAS halted", result.halt_reason)
        for strategy_id in result.retired:
            self.notify(
                Severity.WARNING,
                "Strategy retired",
                f"{strategy_id} retired automatically. Reactivation is a human action.",
            )
        return result

    def notify(self, severity: Severity, title: str, body: str) -> None:
        """Best-effort alert. A failed notification never propagates."""
        if self.notifier is None:
            return
        self.notifier.send(Alert(severity, title, body, self.settings.exchange_env))

    def close(self) -> None:
        self.db.close()


def build_service(
    settings: Settings | None = None,
    risk: RiskSettings | None = None,
    *,
    filters: SymbolFilterProvider | None = None,
) -> AtlasService:
    """Construct a runnable ATLAS instance from configuration."""
    cfg = settings or Settings()
    risk_cfg = risk or RiskSettings()  # type: ignore[call-arg]

    if cfg.is_live and cfg.env.value != "production":
        raise ConfigurationError(
            "live exchange requires ATLAS_ENV=production; refusing to construct"
        )

    cfg.ensure_data_dir()
    db = Database(cfg.db_path)
    audit = AuditLog(db)
    killswitch = KillSwitch(db, cfg.killswitch_path, audit)

    key = cfg.binance_api_key.get_secret_value() if cfg.binance_api_key else ""
    secret = cfg.binance_api_secret.get_secret_value() if cfg.binance_api_secret else ""
    if not key or not secret:
        # Placeholders keep the object constructible for read-only inspection. Every
        # signed call still fails at the exchange, and nothing unsigned is dangerous.
        key, secret = "unconfigured", "unconfigured"

    broker = BinanceSpotBroker(
        key,
        secret,
        killswitch,
        audit,
        exchange_env=cfg.exchange_env,
        transport=UrllibBrokerTransport(),
    )
    klines = BinanceKlineClient(cfg.exchange_env, transport=UrllibTransport())
    # RISK-09: the live path reads LOT_SIZE, NOTIONAL and PRICE_FILTER from the
    # exchange. The cache refreshes daily and raises rather than falling back, so a
    # symbol whose filters cannot be read is simply not traded.
    filter_provider = filters or ExchangeFilterCache(cfg.exchange_env, transport=UrllibTransport())
    store = KlineStore(cfg.data_dir / "klines")
    ledger = Ledger(db, audit)
    ingestor = FillIngestor(db, audit, broker)

    policy = SizingPolicy(
        risk_pct=risk_cfg.risk_pct,
        max_position_pct=risk_cfg.max_position_pct,
        max_deployed_pct=risk_cfg.max_deployed_pct,
        max_concurrent=risk_cfg.max_concurrent,
    )
    trading = TradingService(
        db,
        audit,
        killswitch,
        broker,
        policy=policy,
        filters=filter_provider,
        limits=PortfolioLimits(
            daily_loss_limit=risk_cfg.daily_loss_limit,
            max_account_drawdown=risk_cfg.max_account_dd,
        ),
    )

    notifier: TelegramNotifier | None = None
    if cfg.notifications_enabled() and cfg.telegram_bot_token and cfg.telegram_chat_id:
        notifier = TelegramNotifier(cfg.telegram_bot_token.get_secret_value(), cfg.telegram_chat_id)

    return AtlasService(
        settings=cfg,
        risk=risk_cfg,
        db=db,
        audit=audit,
        killswitch=killswitch,
        broker=broker,
        klines=klines,
        store=store,
        ledger=ledger,
        ingestor=ingestor,
        filters=filter_provider,
        trading=trading,
        registry=StrategyRegistry(db),
        heartbeat=HeartbeatStore(db),
        notifier=notifier,
    )


__all__ = ["AtlasError", "AtlasService", "build_service", "date"]
