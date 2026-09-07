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

    au = sub.add_parser("audit", help="audit trail")
    au_sub = au.add_subparsers(dest="action", required=True)
    au_sub.add_parser("verify")
    tail = au_sub.add_parser("tail")
    tail.add_argument("-n", type=int, default=50)

    args = parser.parse_args(argv)

    if args.group in ("preflight", "run", "health"):
        return _runtime_command(args)

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
