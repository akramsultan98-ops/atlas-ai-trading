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
from atlas.risk.limits import AccountState, PortfolioLimits
from atlas.risk.sizing import ExchangeFilters, SizingPolicy
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
        if drift_ms > 5000:
            # recvWindow is 5s; beyond that every signed request will be rejected.
            result["clock_drift_warning"] = "drift exceeds recvWindow"

        if self.settings.has_exchange_credentials():
            balances = self.broker.account_balances()
            result["authenticated"] = "yes"
            result["quote_balance"] = str(balances.get(self.settings.quote_asset, Decimal(0)))
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

    def account_state(self) -> AccountState:
        """Build the account snapshot the risk engine bounds decisions with."""
        equity = Decimal(0)
        if self.settings.has_exchange_credentials():
            balances = self.broker.account_balances()
            equity = balances.get(self.settings.quote_asset, Decimal(0))

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
            open_symbols=self.ledger.open_symbols(),
        )

    def record_equity(self, state: AccountState) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO equity_snapshots(at, equity, free_cash, deployed, "
                "peak_equity, source) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    utcnow().isoformat(),
                    str(state.equity),
                    str(state.equity),
                    "0",
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

        state = self.account_state()
        self.record_equity(state)

        series_by_symbol = self.fetch_market_data()
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
    filters: ExchangeFilters | None = None,
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
        filters=filters or ExchangeFilters(),
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
        trading=trading,
        registry=StrategyRegistry(db),
        heartbeat=HeartbeatStore(db),
        notifier=notifier,
    )


__all__ = ["AtlasError", "AtlasService", "build_service", "date"]
