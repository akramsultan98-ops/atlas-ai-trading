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

    au = sub.add_parser("audit", help="audit trail")
    au_sub = au.add_subparsers(dest="action", required=True)
    au_sub.add_parser("verify")
    tail = au_sub.add_parser("tail")
    tail.add_argument("-n", type=int, default=50)

    args = parser.parse_args(argv)

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


if __name__ == "__main__":
    raise SystemExit(main())
