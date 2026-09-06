"""Operator CLI. See docs/RUNBOOK.md.

Deliberately minimal and human-only. Nothing here is reachable from the advisory plane.
"""

from __future__ import annotations

import argparse
import sys

from atlas.audit import AuditLog
from atlas.config import load_settings
from atlas.db.engine import Database
from atlas.errors import AtlasError
from atlas.killswitch import KillSwitch
from atlas.models import KillSwitchTrigger


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

    rn = sub.add_parser("run", help="start the ATLAS trading service")
    rn.add_argument("--ticks", type=int, default=0, help="0 = run until stopped")
    rn.add_argument("--once", action="store_true", help="run a single tick and exit")

    au = sub.add_parser("audit", help="audit trail")
    au_sub = au.add_subparsers(dest="action", required=True)
    au_sub.add_parser("verify")
    tail = au_sub.add_parser("tail")
    tail.add_argument("-n", type=int, default=50)

    args = parser.parse_args(argv)

    if args.group in ("preflight", "run"):
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


if __name__ == "__main__":
    raise SystemExit(main())
