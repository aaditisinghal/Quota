"""CLI entry: python -m quota ..."""

import argparse
import json
import sys
from contextlib import closing

from quota import config
from quota.ledger import db, hashing, invariants, queries


def _open(path):
    return closing(db.connect(path, readonly=True))


def cmd_demo(args) -> int:
    from quota.sim import demo_acme
    demo = demo_acme.run(db_path=args.db)
    if args.db:
        print(f"\nLedger written to {args.db}")
    demo.close()
    return 0 if demo.ok else 1


def cmd_verify(args) -> int:
    with _open(args.db) as conn:
        checks = invariants.check_all(conn)
        chain_ok, bad_seq = hashing.verify_chain(conn)
        rows = hashing.chain_length(conn)
    print(invariants.summary_line(checks))
    print(f"CHAIN       {'OK' if chain_ok else f'FAIL at seq {bad_seq}'} ({rows} rows)")
    for violations in checks.values():
        for v in violations:
            print(f"  {v.code} {v.subject}: {v.detail}")
    return 0 if chain_ok and not any(checks.values()) else 1


def cmd_replay(args) -> int:
    with _open(args.db) as conn:
        matches, stored, recomputed = queries.replay_decision(conn, args.decision_id)
    print("stored:")
    print(json.dumps(stored, indent=2, sort_keys=True))
    print("recomputed:")
    print(json.dumps(recomputed, indent=2, sort_keys=True))
    print(f"{args.decision_id} {'MATCH' if matches else 'MISMATCH'}")
    return 0 if matches else 1


def cmd_tb(args) -> int:
    with _open(args.db) as conn:
        tb = queries.trial_balance(conn, args.as_of)
    print(f"TRIAL BALANCE{' as of ' + args.as_of if args.as_of else ''}")
    for line in queries.format_trial_balance(tb, config.ACCOUNTS):
        print(line)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m quota")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="run a scripted demo world")
    demo.add_argument("world", choices=["acme"])
    demo.add_argument("--db", help="write the ledger to this file (default: in-memory)")
    demo.set_defaults(fn=cmd_demo)

    verify = sub.add_parser("verify", help="chain verify + all invariants")
    verify.add_argument("--db", required=True)
    verify.set_defaults(fn=cmd_verify)

    replay = sub.add_parser("replay", help="replay a decision from its recorded inputs")
    replay.add_argument("decision_id")
    replay.add_argument("--db", required=True)
    replay.set_defaults(fn=cmd_replay)

    tb = sub.add_parser("tb", help="trial balance, optionally as of a time")
    tb.add_argument("--db", required=True)
    tb.add_argument("--as-of", dest="as_of")
    tb.set_defaults(fn=cmd_tb)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
