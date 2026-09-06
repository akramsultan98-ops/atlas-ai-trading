"""The control-plane trading tick.

One tick is the whole live path: reconcile, validate data, check account limits,
evaluate live strategies, size, execute, then supervise. No LLM appears anywhere in
this module — the advisory plane proposes strategies, and by the time one reaches here
it is a validated spec that a deterministic engine executes.

Ordering is deliberate:

  1. reconcile first — act on the exchange's truth, not yesterday's local state
  2. portfolio limits before entries — a breached account stops before it adds risk
  3. supervise last — retirement must see this tick's fills
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from atlas.audit import AuditLog
from atlas.data.models import KlineSeries
from atlas.data.validate import check_staleness, validate_series
from atlas.db.engine import Database
from atlas.errors import AtlasError, ConfigurationError, KillSwitchArmedError
from atlas.execution.brackets import open_bracketed_position
from atlas.execution.broker import BinanceSpotBroker, OrderRejection
from atlas.execution.ledger import Ledger
from atlas.execution.reconcile import Reconciler
from atlas.killswitch import KillSwitch
from atlas.models import AuditEventType, KillSwitchTrigger, StrategyStatus, utcnow
from atlas.monitor.health import compute_health
from atlas.monitor.supervisor import Supervisor
from atlas.promotion.gate import PromotionGate
from atlas.risk.filters import SymbolFilterProvider
from atlas.risk.limits import AccountState, PortfolioLimits, can_open_symbol, check_portfolio_limits
from atlas.risk.sizing import RejectReason, SizingPolicy, size_position
from atlas.strategy.evaluator import evaluate
from atlas.strategy.registry import StrategyRegistry

TRADING_ACTOR = "trading"


@dataclass
class TickResult:
    at: datetime
    halted: bool = False
    halt_reason: str = ""
    entries_placed: int = 0
    entries_rejected: int = 0
    entries_suspended: bool = False
    retired: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class TradingService:
    """Executes one tick of the live trading loop."""

    def __init__(
        self,
        db: Database,
        audit: AuditLog,
        killswitch: KillSwitch,
        broker: BinanceSpotBroker,
        *,
        policy: SizingPolicy,
        filters: SymbolFilterProvider,
        limits: PortfolioLimits | None = None,
    ) -> None:
        if not filters.is_live:
            # RISK-09 is not advisory. Assumed filters make an order that the exchange
            # will reject, or one sized against the wrong step - both silently.
            raise ConfigurationError(
                "live trading requires a live exchange filter provider (RISK-09); "
                f"{type(filters).__name__} supplies fixed values"
            )
        self._db = db
        self._audit = audit
        self._killswitch = killswitch
        self._broker = broker
        self._policy = policy
        self._filters = filters
        self._limits = limits or PortfolioLimits()
        self._ledger = Ledger(db, audit)
        self._registry = StrategyRegistry(db)
        self._supervisor = Supervisor(db, audit)
        self._reconciler = Reconciler(db, audit, killswitch)
        self._promotion = PromotionGate(db, audit)

    def tick(
        self,
        *,
        account: AccountState,
        series_by_symbol: dict[str, KlineSeries],
        exchange_orders: list[dict[str, object]],
        live_returns: dict[str, list[Decimal]],
        backtest_stats: dict[str, tuple[Decimal, Decimal, Decimal]],
        now: datetime | None = None,
    ) -> TickResult:
        """Run one full trading tick.

        `backtest_stats` maps strategy id to (mean_return, return_sigma, win_rate) from
        its backtest — the baseline the monitor compares live behaviour against.
        """
        reference = now or utcnow()
        result = TickResult(at=reference)

        # 1. Exchange state is truth.
        self._reconciler.reconcile(exchange_orders)

        # 2. Account-level limits. A breach halts before any new risk is added.
        #
        # Skipped when the account could not be fully valued: an unpriced open position
        # understates equity by its whole notional, and comparing that against a loss
        # limit manufactures a breach out of a market-data gap. Entries stop instead,
        # which withholds new risk without arming the switch a human must then clear.
        if account.valuation_complete:
            breach = check_portfolio_limits(account, self._limits)
            if breach is not None:
                self._killswitch.arm(breach.trigger, breach.reason, TRADING_ACTOR)
                result.halted = True
                result.halt_reason = breach.reason
        else:
            result.entries_suspended = True
            result.notes.append(
                "account valuation incomplete for "
                f"{sorted(account.unpriced_symbols)}; limits not evaluated, entries suspended"
            )
            self._audit.append(
                AuditEventType.SYSTEM,
                {
                    "event": "valuation_incomplete",
                    "unpriced_symbols": sorted(account.unpriced_symbols),
                },
                TRADING_ACTOR,
            )

        # An armed switch outranks a suspension: one needs a human, the other clears
        # itself on the next tick that has prices. Reporting the milder of the two
        # would tell an operator to wait for something that is not coming.
        if self._killswitch.is_armed():
            result.halted = True
            if not result.halt_reason:
                result.halt_reason = self._killswitch.read_state().reason
            # Still supervise: retirement reduces exposure and must not be blocked.
            result.retired = self._supervise_all(live_returns, backtest_stats)
            self._log(result)
            return result

        if result.entries_suspended:
            # Nothing below this point may add risk against an equity figure that is
            # missing a position. Supervision still runs: retirement reduces exposure.
            result.retired = self._supervise_all(live_returns, backtest_stats)
            self._log(result)
            return result

        # 3. Entries, per live strategy.
        #
        # Cash and deployment are carried forward inside the loop. The account snapshot
        # is from the top of the tick, so a second entry sized against it would be
        # sized as though the first had not happened - which is how two positions that
        # each fit the deployment cap breach it together.
        open_symbols = set(account.open_symbols)
        free_cash = account.free_cash
        deployed = account.deployed
        for strategy_id in self._registry.list_by_status(StrategyStatus.LIVE):
            spec = self._registry.load_spec(strategy_id)
            if spec is None:
                result.notes.append(f"{strategy_id}: spec missing")
                continue

            series = series_by_symbol.get(spec.symbol)
            if series is None or not series.bars:
                result.notes.append(f"{spec.symbol}: no data this tick")
                continue

            if not validate_series(series).valid:
                result.notes.append(f"{spec.symbol}: series failed validation")
                continue

            last_close = series.bars[-1].close_time
            stale, age = check_staleness(last_close, series.timeframe, now=reference)
            if stale:
                self._killswitch.arm(
                    KillSwitchTrigger.DATA_STALENESS,
                    f"{spec.symbol} last bar closed {age:.0f}s ago",
                    TRADING_ACTOR,
                )
                result.halted = True
                result.halt_reason = f"{spec.symbol} data stale"
                break

            if not can_open_symbol(account, spec.symbol):
                continue
            if spec.symbol in open_symbols:
                continue

            signals = evaluate(spec, series.bars)
            # Only a signal on the most recent closed bar is actionable; older ones
            # belong to bars the loop has already passed.
            last_index = len(series.bars) - 1
            actionable = [s for s in signals if s.bar_index == last_index]
            if not actionable:
                continue

            signal = actionable[0]

            try:
                symbol_filters = self._filters.get(spec.symbol)
            except (AtlasError, OSError) as exc:
                # RISK-09: decline rather than fall back. Not knowing the exchange's
                # minimum is not the same as there being none.
                result.entries_rejected += 1
                result.notes.append(f"{spec.symbol}: exchange filters unavailable ({exc})")
                self._audit.append(
                    AuditEventType.RISK_REJECTION,
                    {
                        "strategy_id": strategy_id,
                        "symbol": spec.symbol,
                        "reason": str(RejectReason.FILTERS_UNAVAILABLE),
                        "error": str(exc),
                    },
                    TRADING_ACTOR,
                )
                continue

            sizing = size_position(
                equity=account.equity,
                free_cash=free_cash,
                entry_price=signal.reference_price,
                stop_price=signal.stop_price,
                side=signal.side,
                policy=self._policy,
                filters=symbol_filters,
                open_positions=len(open_symbols),
                deployed=deployed,
                risk_multiplier=self._promotion.risk_multiplier_for(
                    strategy_id, len(live_returns.get(strategy_id, []))
                ),
            )
            if not sizing.accepted:
                result.entries_rejected += 1
                self._audit.append(
                    AuditEventType.RISK_REJECTION,
                    {
                        "strategy_id": strategy_id,
                        "symbol": spec.symbol,
                        "reason": str(sizing.reason),
                    },
                    TRADING_ACTOR,
                )
                continue

            try:
                open_bracketed_position(
                    self._broker,
                    self._audit,
                    self._ledger,
                    strategy_id=strategy_id,
                    signal_bar_time=series.bars[last_index].open_time,
                    symbol=spec.symbol,
                    side=signal.side,
                    quantity=sizing.quantity,
                    stop_price=signal.stop_price,
                    reference_price=signal.reference_price,
                    target_price=signal.target_price,
                )
                result.entries_placed += 1
                open_symbols.add(spec.symbol)
                free_cash -= sizing.notional
                deployed += sizing.notional
            except KillSwitchArmedError:
                result.halted = True
                result.halt_reason = "kill switch armed mid-tick"
                break
            except OrderRejection as exc:
                result.entries_rejected += 1
                result.notes.append(f"{spec.symbol}: {exc}")

        # 4. Supervise last, so retirement sees this tick's activity.
        result.retired = self._supervise_all(live_returns, backtest_stats)
        self._log(result)
        return result

    def _supervise_all(
        self,
        live_returns: dict[str, list[Decimal]],
        backtest_stats: dict[str, tuple[Decimal, Decimal, Decimal]],
    ) -> list[str]:
        retired: list[str] = []
        for strategy_id in self._registry.list_by_status(StrategyStatus.LIVE):
            baseline = backtest_stats.get(strategy_id)
            if baseline is None:
                continue
            mean, sigma, win_rate = baseline
            health = compute_health(
                live_returns.get(strategy_id, []),
                backtest_mean_return=mean,
                backtest_return_sigma=sigma,
            )
            outcome = self._supervisor.supervise(strategy_id, health, win_rate)
            if outcome.retired:
                retired.append(strategy_id)
        return retired

    def _log(self, result: TickResult) -> None:
        self._audit.append(
            AuditEventType.SYSTEM,
            {
                "event": "trading_tick",
                "halted": result.halted,
                "halt_reason": result.halt_reason,
                "entries_placed": result.entries_placed,
                "entries_rejected": result.entries_rejected,
                "retired": result.retired,
                "notes": result.notes,
            },
            TRADING_ACTOR,
        )
