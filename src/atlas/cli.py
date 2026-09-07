"""Operator CLI. See docs/RUNBOOK.md.

Deliberately minimal and human-only. Nothing here is reachable from the advisory plane.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from typing import Any

from atlas.audit import AuditLog
from atlas.config import load_settings
from atlas.db.engine import Database
from atlas.errors import AtlasError
from atlas.killswitch import KillSwitch
from atlas.models import AuditEventType, KillSwitchTrigger
from atlas.runtime.decisions import (
    REGIME_NOT_IMPLEMENTED,
    DecisionOutcome,
    SymbolDecision,
    render_decisions,
)


def _build(settings_data_dir_required: bool = True) -> tuple[Database, KillSwitch, AuditLog]:
    settings, _risk = load_settings()
    settings.ensure_data_dir()
    db = Database(settings.db_path)
    audit = AuditLog(db)
    return db, KillSwitch(db, settings.killswitch_path, audit), audit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas", description="ATLAS V2 operator CLI")
    sub = parser.add_subparsers(dest="group", required=True)

    ks = sub.add_parser("killswitch", help="account-level halt")
    ks_sub = ks.add_subparsers(dest="action", required=True)
    ks_sub.add_parser("status")
    ks_sub.add_parser("init", help="deliberate first release to trade")
    arm = ks_sub.add_parser("arm")
    arm.add_argument("--reason", required=True)
    dis = ks_sub.add_parser("disarm")
    dis.add_argument("--reason", required=True)
    dis.add_argument("--confirm", action="store_true", help="explicit human confirmation")
    dis.add_argument("--actor", default="operator")

    pf = sub.add_parser("preflight", help="verify exchange reachability and environment")
    pf.add_argument("--json", action="store_true")

    hc = sub.add_parser("health", help="report service health (for orchestrators)")
    hc.add_argument("--quiet", action="store_true", help="exit code only, no output")
    hc.add_argument(
        "--max-age",
        type=int,
        default=0,
        help="heartbeat staleness tolerance in seconds; 0 = 3x the tick interval",
    )

    rn = sub.add_parser("run", help="start the ATLAS trading service")
    rn.add_argument("--ticks", type=int, default=0, help="0 = run until stopped")
    rn.add_argument("--once", action="store_true", help="run a single tick and exit")
    rn.add_argument(
        "--explain",
        action="store_true",
        help="print the decision for every configured symbol after the tick",
    )
    rn.add_argument("--json", action="store_true", help="with --explain, emit JSON")

    dc = sub.add_parser("decisions", help="why the last tick did what it did, per symbol")
    dc.add_argument("-n", type=int, default=1, help="how many ticks back to show")
    dc.add_argument("--json", action="store_true")

    rs = sub.add_parser("research", help="generate and evaluate candidate strategies")
    rs_sub = rs.add_subparsers(dest="action", required=True)
    rr = rs_sub.add_parser("run", help="one full factory pass on real market data")
    rr.add_argument("--passes", type=int, default=1, help="candidates to attempt")
    rr.add_argument("--bars", type=int, default=0, help="history depth; 0 = ATLAS_HISTORY_BARS")
    rr.add_argument("--symbol", default="", help="default: the first configured symbol")
    rs_sub.add_parser("provider", help="report the configured research provider")

    fa = sub.add_parser("factory", help="strategy factory: candidates and their evidence")
    fa_sub = fa.add_subparsers(dest="action", required=True)
    cand = fa_sub.add_parser("candidates", help="list strategies and their lifecycle status")
    cand.add_argument("--status", default="", help="filter by status, e.g. CANDIDATE")
    for name, helptext in (
        ("show", "spec, hash and status"),
        ("backtests", "stored runs with provenance"),
        ("verification", "independent verification result"),
        ("gates", "per-gate verdicts, including UNDEFINED_POLICY"),
        ("incubation", "observation start, elapsed days and paper metrics"),
        ("eligibility", "promotion eligibility (read-only; never promotes)"),
    ):
        sp = fa_sub.add_parser(name, help=helptext)
        sp.add_argument("strategy_id")
    inc = fa_sub.add_parser("incubate", help="begin forward observation (VERIFIED -> INCUBATING)")
    inc.add_argument("strategy_id")
    inc.add_argument("--confirm", action="store_true", help="explicit operator confirmation")
    inc.add_argument("--actor", default="operator")

    au = sub.add_parser("audit", help="audit trail")
    au_sub = au.add_subparsers(dest="action", required=True)
    au_sub.add_parser("verify")
    tail = au_sub.add_parser("tail")
    tail.add_argument("-n", type=int, default=50)

    args = parser.parse_args(argv)

    if args.group in ("preflight", "run", "health"):
        return _runtime_command(args)
    if args.group == "research":
        return _research_command(args)

    try:
        db, killswitch, audit = _build()
    except AtlasError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.group == "killswitch":
            if args.action == "status":
                state = killswitch.read_state()
                print(f"state:   {state.state}")
                print(f"trigger: {state.trigger or '-'}")
                print(f"reason:  {state.reason}")
                print(f"actor:   {state.actor}")
                print(f"at:      {state.at.isoformat()}")
            elif args.action == "init":
                print(f"state: {killswitch.initialise().state}")
            elif args.action == "arm":
                rec = killswitch.arm(KillSwitchTrigger.MANUAL, args.reason, "operator")
                print(f"ARMED: {rec.reason}")
            elif args.action == "disarm":
                rec = killswitch.disarm(args.reason, args.actor, human_confirmed=args.confirm)
                print(f"DISARMED: {rec.reason}")
        elif args.group == "factory":
            return _factory(db, audit, args)
        elif args.group == "decisions":
            return _decisions(audit, args)
        elif args.group == "audit":
            if args.action == "verify":
                n = audit.verify_chain()
                print(f"audit chain OK: {n} events verified")
            elif args.action == "tail":
                for event in audit.tail(args.n):
                    print(
                        f"{event.seq:>6}  {event.at.isoformat()}  {event.event_type}  {event.actor}"
                    )
    except AtlasError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()
    return 0


def _health(service: Any, args: argparse.Namespace) -> int:
    """Exit 0 healthy, 1 unhealthy. Used as the container HEALTHCHECK.

    A missing heartbeat is unhealthy, not unknown: the service either wrote one or it
    is not running the loop, and an orchestrator needs a decision either way.
    """
    from datetime import timedelta

    beat = service.heartbeat.read()
    tolerance = timedelta(seconds=args.max_age or service.settings.tick_interval_seconds * 3)

    if beat is None:
        if not args.quiet:
            print("UNHEALTHY: no heartbeat recorded (service has not completed a tick)")
        return 1

    stale = beat.is_stale(tolerance)
    killswitch = service.killswitch.read_state()

    if not args.quiet:
        print(f"heartbeat_age_s : {int(beat.age().total_seconds())}")
        print(f"ticks           : {beat.tick_count}")
        print(f"exchange_env    : {beat.exchange_env}")
        print(f"exchange_ok     : {beat.exchange_reachable}")
        print(f"kill_switch     : {killswitch.state}")
        print(f"last_error      : {beat.last_error or '-'}")
        print(f"status          : {'STALE' if stale else 'OK'}")

    # An armed kill switch is not unhealthy. It is the system working: the process is
    # alive and deliberately not trading. Restarting it would achieve nothing.
    return 1 if stale else 0


def _runtime_command(args: argparse.Namespace) -> int:
    """preflight and run. Kept apart from the operator commands because these
    construct the exchange-facing service, and that construction should be visible."""
    import logging

    from atlas.runtime.scheduler import IntervalScheduler
    from atlas.runtime.service import build_service

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    )
    log = logging.getLogger("atlas")

    try:
        service = build_service()
    except AtlasError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.group == "health":
            return _health(service, args)

        if args.group == "preflight":
            info = service.preflight()
            if getattr(args, "json", False):
                import json

                print(json.dumps(info, indent=2, sort_keys=True))
            else:
                for key in sorted(info):
                    print(f"{key:>24}: {info[key]}")
            return 0

        # run
        log.info("ATLAS starting | %s", service.settings.describe())
        if service.settings.is_live:
            log.warning("EXCHANGE ENVIRONMENT IS LIVE - real orders will be placed")
        else:
            log.info("exchange environment is TESTNET - no real money at risk")

        report = service.start()
        log.info(
            "recovery complete | entries=%s issues=%s",
            "enabled" if report.may_resume_entries else "HALTED",
            report.issues or "none",
        )

        if args.once:
            result = service.tick()
            log.info(
                "tick complete | halted=%s entries=%d rejected=%d retired=%s",
                result.halted,
                result.entries_placed,
                result.entries_rejected,
                result.retired or "none",
            )
            if getattr(args, "explain", False):
                if getattr(args, "json", False):
                    print(json.dumps([d.as_dict() for d in result.decisions], indent=2))
                else:
                    print(render_decisions(result.decisions))
            return 0

        max_ticks = args.ticks if args.ticks > 0 else 10**9
        scheduler = IntervalScheduler(service.settings.tick_interval_seconds)

        def one_tick() -> None:
            result = service.tick()
            log.info(
                "tick | halted=%s entries=%d rejected=%d retired=%s",
                result.halted,
                result.entries_placed,
                result.entries_rejected,
                result.retired or "none",
            )

        try:
            tick_report = scheduler.run(one_tick, max_ticks=max_ticks)
        except KeyboardInterrupt:
            log.info("interrupted; shutting down cleanly")
            return 0
        log.info("stopped after %d tick(s), %d failure(s)", tick_report.ticks, tick_report.failures)
        return 1 if tick_report.failures == tick_report.ticks and tick_report.ticks else 0
    except AtlasError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        service.close()


def _research_command(args: argparse.Namespace) -> int:
    """Run the factory pipeline on real candles.

    The research plane is built without exchange credentials (AI-01). It needs none:
    candles and symbol filters are public, unsigned endpoints. A process that cannot
    authenticate to the exchange cannot place an order however it is prompted.
    """
    from atlas.data.klines import BinanceKlineClient, UrllibTransport
    from atlas.data.models import Timeframe
    from atlas.research.loop import ResearchLoop
    from atlas.research.provider import build_spec_client
    from atlas.risk.filters import ExchangeFilterCache
    from atlas.risk.sizing import SizingPolicy

    try:
        settings, risk = load_settings()
    except AtlasError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    settings = settings.for_research_plane()

    if args.action == "provider":
        from atlas.research.provider import API_KEY_VAR

        print(f"provider    {settings.research_provider}")
        print(f"model       {settings.research_model or '(provider default)'}")
        print(f"credential  {'present' if settings.has_research_credentials() else 'ABSENT'}")
        print(f"binance_key {'PRESENT - BUG' if settings.binance_api_key else 'absent (correct)'}")
        if not settings.has_research_credentials():
            print(f"set {API_KEY_VAR} to generate candidates")
            return 1
        return 0

    try:
        client = build_spec_client(settings)
    except AtlasError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    symbol = (args.symbol or settings.symbol_list[0]).upper()
    bars = args.bars or settings.history_bars

    klines = BinanceKlineClient(settings.exchange_env, transport=UrllibTransport())
    try:
        series = klines.fetch(symbol, Timeframe(settings.timeframe), max_bars=bars)
    except AtlasError as exc:
        print(f"error: could not fetch candles for {symbol}: {exc}", file=sys.stderr)
        return 1

    # RISK-09: size against the exchange's real filters, never the assumed defaults.
    # SEL-07 feasibility is decided against the real minimum notional.
    try:
        filters = ExchangeFilterCache(settings.exchange_env, transport=UrllibTransport()).get(
            symbol
        )
    except AtlasError as exc:
        print(f"error: could not read exchange filters for {symbol}: {exc}", file=sys.stderr)
        return 1

    settings.ensure_data_dir()
    db = Database(settings.db_path)
    try:
        loop = ResearchLoop(
            db,
            AuditLog(db),
            client,
            policy=SizingPolicy(
                risk_pct=risk.risk_pct,
                max_position_pct=risk.max_position_pct,
                max_deployed_pct=risk.max_deployed_pct,
                max_concurrent=risk.max_concurrent,
            ),
            filters=filters,
        )
        print(f"{len(series.bars)} bars of {symbol} {settings.timeframe}")
        print(f"minNotional {filters.min_notional}  stepSize {filters.step_size}")

        accepted = 0
        for outcome in loop.run_batch(series, args.passes):
            mark = "ACCEPTED" if outcome.accepted else f"rejected at {outcome.stage}"
            print(f"  {mark}: {outcome.reason}")
            if outcome.strategy_id:
                print(f"    strategy {outcome.strategy_id}  spec {outcome.spec_hash}")
            if outcome.engine_defect_suspected:
                print("    ENGINE DEFECT SUSPECTED: the two engines disagree")
            accepted += 1 if outcome.accepted else 0

        print(f"{accepted}/{args.passes} accepted; evidence stored for every candidate")
        print("inspect with: atlas factory candidates")
        return 0
    finally:
        db.close()


def _factory(db: Database, audit: AuditLog, args: argparse.Namespace) -> int:
    """Inspect the strategy factory. Read-only except `incubate`.

    There is deliberately no `promote` command. Promotion is what puts real capital
    behind a strategy, and the only path to it requires an explicit human approver in
    code (PROM-01). Adding a CLI verb for it would put live trading one shell command
    away from an operator who meant to inspect something.
    """
    from atlas.backtest.stats import BacktestStats
    from atlas.factory.store import FactoryStore
    from atlas.incubation.divergence import check_divergence, compute_metrics
    from atlas.incubation.tracker import IncubationTracker
    from atlas.promotion.gate import evaluate_promotion
    from atlas.strategy.registry import StrategyRegistry

    registry = StrategyRegistry(db)
    store = FactoryStore(db)
    tracker = IncubationTracker(db)

    if args.action == "candidates":
        rows = db.connection.execute(
            "SELECT id, symbol, timeframe, status, created_at FROM strategies "
            "WHERE (? = '' OR status = ?) ORDER BY created_at",
            (args.status.upper(), args.status.upper()),
        ).fetchall()
        if not rows:
            print("no strategies recorded" + (f" with status {args.status}" if args.status else ""))
            return 0
        for row in rows:
            print(
                f"{row['id']}  {row['status']:<11} {row['symbol']:<10} "
                f"{row['timeframe']:<5} {row['created_at']}"
            )
        return 0

    strategy_id = args.strategy_id
    status = registry.status(strategy_id)
    if status is None:
        print(f"error: no strategy {strategy_id}", file=sys.stderr)
        return 2

    if args.action == "show":
        spec = registry.load_spec(strategy_id)
        print(f"id        {strategy_id}")
        print(f"status    {status}")
        if spec is None:
            print("spec      UNREADABLE")
            return 1
        print(f"name      {spec.name}")
        print(f"symbol    {spec.symbol} {spec.timeframe}")
        print(f"spec_hash {spec.content_hash()}")
        print(f"stop      {spec.stop.kind} {spec.stop.value}")
        print(f"target    {spec.target.kind} {spec.target.value}")
        for index, rule in enumerate(spec.entries):
            print(f"entry {index}   {rule.side}, {len(rule.conditions)} condition(s)")
        return 0

    if args.action == "backtests":
        runs = store.backtests_for(strategy_id)
        if not runs:
            print("no backtests recorded for this strategy")
            return 1
        for run in runs:
            print(f"{run.engine:<13} {run.engine_version:<14} {run.created_at}")
            print(f"    window      {run.window_start} .. {run.window_end}")
            print(f"    data_hash   {run.data_hash}")
            print(f"    config_hash {run.config_hash or '(not recorded)'}")
            print(f"    costs       {run.cost_model}")
            print(f"    stats       {run.stats}")
        return 0

    if args.action == "verification":
        record = store.verification_for(strategy_id)
        if record is None:
            print("no verification recorded for this strategy")
            return 1
        print(f"passed  {record['passed']}")
        print(f"detail  {record['detail']}")
        return 0

    if args.action == "gates":
        record = store.selection_for(strategy_id)
        if record is None:
            print("no selection result recorded for this strategy")
            return 1
        print(f"passed {record['passed']}")
        for gate in record["gates"]["gates"]:
            print(f"  {gate['verdict']:<17} {gate['gate']:<28} {gate['detail']}")
        undefined = record["gates"].get("undefined") or []
        if undefined:
            print(f"blocked by undefined policy: {undefined}")
        return 0

    if args.action == "incubation":
        observation = tracker.run_for(strategy_id)
        if observation is None:
            print("incubation has not been started for this strategy")
            print("observation elapsed: 0 days (INC-01 measures from the recorded start)")
            return 1
        signals = tracker.signals_for(strategy_id)
        metrics = compute_metrics(signals)
        print(f"started_at  {observation.started_at.isoformat()} by {observation.started_by}")
        print(f"spec_hash   {observation.spec_hash}")
        print(f"config_hash {observation.config_hash}")
        print(f"elapsed     {tracker.elapsed_days(strategy_id)} days")
        print(f"signals     {len(signals)} recorded, {metrics.trade_count} closed")
        print(f"win_rate    {metrics.win_rate}")
        print(f"profit_fact {metrics.profit_factor}")
        print(f"drawdown    {metrics.max_drawdown}")
        return 0

    if args.action == "eligibility":
        stored = store.primary_backtest(strategy_id)
        if stored is None:
            print("UNDEFINED_POLICY: no stored backtest to compare incubation against")
            return 1
        signals = tracker.signals_for(strategy_id)
        divergence = check_divergence(
            signals, BacktestStats.from_dict(stored.stats), tracker.elapsed_days(strategy_id)
        )
        decision = evaluate_promotion(divergence, signals, {})
        print(f"status              {status}")
        print(f"gates_satisfied     {decision.eligible}")
        print(f"max_correlation     {decision.max_observed_correlation}")
        for reason in decision.reasons:
            print(f"  blocked: {reason}")
        print("promotion still requires explicit human approval (PROM-01); this command")
        print("reports eligibility only and cannot promote anything.")
        return 0

    if args.action == "incubate":
        return _begin_incubation(db, audit, registry, tracker, store, strategy_id, status, args)

    return 0


def _begin_incubation(
    db: Database,
    audit: AuditLog,
    registry: Any,
    tracker: Any,
    store: Any,
    strategy_id: str,
    status: Any,
    args: argparse.Namespace,
) -> int:
    """VERIFIED -> INCUBATING. Zero capital (INC-03); starts the observation clock."""
    from atlas.models import StrategyStatus

    if not args.confirm:
        print(
            "refusing: pass --confirm. Beginning incubation starts the INC-01 clock, "
            "and the start time is written once and never moved.",
            file=sys.stderr,
        )
        return 2
    if status is not StrategyStatus.VERIFIED:
        print(
            f"refusing: strategy is {status}, not VERIFIED. Only a strategy that has "
            "passed verification and every selection gate may be incubated.",
            file=sys.stderr,
        )
        return 2

    spec = registry.load_spec(strategy_id)
    stored = store.primary_backtest(strategy_id)
    run = tracker.begin(
        strategy_id,
        spec_hash=spec.content_hash() if spec else "",
        config_hash=stored.config_hash if stored else "",
        started_by=args.actor,
    )
    registry.set_status(strategy_id, StrategyStatus.INCUBATING)
    audit.append(
        AuditEventType.STRATEGY_STATUS_CHANGED,
        {
            "event": "incubation_started",
            "strategy_id": strategy_id,
            "spec_hash": run.spec_hash,
            "config_hash": run.config_hash,
            "started_by": run.started_by,
        },
        args.actor,
    )
    print(f"INCUBATING from {run.started_at.isoformat()}")
    print("zero capital is committed (INC-03). Promotion remains a separate human step.")
    return 0


def _decisions(audit: AuditLog, args: argparse.Namespace) -> int:
    """Replay the recorded per-symbol decisions from the audit trail.

    Read from the audit log rather than recomputed, so what is shown is what the
    running process actually decided - including on a tick this command was not there
    for. The audit chain is append-only and hash-linked, so it cannot be edited to
    agree with a later opinion.
    """
    events = [e for e in audit.tail(2000) if e.event_type is AuditEventType.TICK_DECISION]
    if not events:
        print(
            "no tick decisions recorded yet. Run `atlas run --once` first; if that has "
            "already run, this build predates decision recording."
        )
        return 1

    # Events arrive oldest-first; group by symbol and keep the last n per symbol.
    per_symbol: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        per_symbol.setdefault(str(event.payload.get("symbol", "?")), []).append(event.payload)

    wanted = max(1, args.n)
    latest = [payload for rows in per_symbol.values() for payload in rows[-wanted:]]

    if args.json:
        print(json.dumps(latest, indent=2))
        return 0

    print(render_decisions([_decision_from_payload(p) for p in latest]))
    return 0


def _decision_from_payload(payload: dict[str, Any]) -> SymbolDecision:
    """Rebuild a decision from its audit record for display only."""

    def num(key: str) -> Decimal | None:
        raw = payload.get(key)
        return None if raw is None else Decimal(str(raw))

    return SymbolDecision(
        symbol=str(payload.get("symbol", "?")),
        outcome=DecisionOutcome(str(payload.get("outcome", "NO_STRATEGY"))),
        detail=str(payload.get("detail", "")),
        strategy_id=payload.get("strategy_id"),
        bars=payload.get("bars"),
        data_valid=payload.get("data_valid"),
        last_bar_close=payload.get("last_bar_close"),
        bar_age_seconds=payload.get("bar_age_seconds"),
        conditions=list(payload.get("conditions") or []),
        regime=str(payload.get("regime", REGIME_NOT_IMPLEMENTED)),
        signal=bool(payload.get("signal", False)),
        reference_price=num("reference_price"),
        stop_price=num("stop_price"),
        target_price=num("target_price"),
        risk_reason=payload.get("risk_reason"),
        quantity=num("quantity"),
        notional=num("notional"),
    )


if __name__ == "__main__":
    raise SystemExit(main())
