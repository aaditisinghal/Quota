"""CLI entry: python -m quota ..."""

import argparse
import json
import sys
from contextlib import closing
from pathlib import Path

from quota import config
from quota.ledger import db, hashing, invariants, queries


def _open(path):
    return closing(db.connect(path, readonly=True))


def _sandbox(args, tag):
    if not getattr(args, "stripe", False):
        return None
    from uuid import uuid4
    from quota.sim.stripe_billing import StripeSandbox
    return StripeSandbox.from_env(f"{tag}-{uuid4().hex[:8]}")


def cmd_demo(args) -> int:
    from quota.sim import demo_acme
    from quota.sim.stripe_billing import StripeSandboxError
    try:
        stripe = _sandbox(args, "quota-acme")
    except StripeSandboxError as exc:
        print(f"--stripe: {exc}")
        return 2
    if args.live:
        from quota.agent.llm import LLM, LLMUnavailable
        try:
            demo = demo_acme.run(db_path=args.db, mode="live", llm=LLM(), stripe=stripe)
        except LLMUnavailable as exc:
            print(f"\n--live stopped: {exc}. Set ANTHROPIC_API_KEY (or log in with `ant auth login`) and rerun; "
                  f"answers already received are cached.")
            return 2
    else:
        demo = demo_acme.AcmeDemo(args.db, stripe=stripe).run(checkpoint_dir=".checkpoints")
        print("\nCheckpoints written to .checkpoints/ (cp_dispute, cp_routed, cp_april_close, cp_may_close, cp_end)")
    if stripe:
        print(f"\nStripe sandbox run {stripe.run_tag}: {stripe.created}")
    if args.db:
        print(f"\nLedger written to {args.db}")
    _mirror(demo.ro)
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


def cmd_eval(args) -> int:
    from quota import eval as evaluation
    from quota.sim.stripe_billing import StripeSandboxError
    out_dir = Path(args.out) / f"seed-{args.seed}"
    try:
        card = evaluation.run_eval(args.seed, reps=args.reps, out_dir=out_dir, stripe=args.stripe, after=_mirror)
    except StripeSandboxError as exc:
        print(f"--stripe: {exc}")
        return 2
    for line in evaluation.format_scorecard(card):
        print(line)
    print(f"\nAnswer key and scorecard written to {out_dir}")
    return 0


def _model(live):
    if not live:
        return None
    from quota.agent.llm import LLM
    return LLM()


def cmd_export(args) -> int:
    from quota.intake.export import export
    key = export(args.seed, args.out, reps=args.reps)
    print(f"Wrote seed {args.seed}'s exports to {args.out}/ with {len(key['planted'])} planted items "
          f"({args.out}/_answer_key.json)")
    return 0


def cmd_intake(args) -> int:
    from quota.intake.intake import Intake
    _, report = Intake(args.inbox, _model(args.live)).run(args.db)
    for f in report["files"]:
        state = "loaded" if f.get("loaded") else "held for review"
        print(f"  {f['file']:<34} {f['kind'] or '?':<14} {f['rows']:>4} rows  mapping {f.get('mapping_source')}, {state}")
    print("Loaded: " + ", ".join(f"{n} {t}" for t, n in report["loaded"].items()))
    for line in report["log"]:
        print(f"  {line}")
    print(f"Review items: {len(report['review_items'])}")
    for item in report["review_items"]:
        print(f"  {item['id']} {item['kind']} -> {item['routed_to']}: {item['question']}")
    return 0


def cmd_backtest(args) -> int:
    from quota.intake import backtest
    result = backtest.run(args.inbox, args.quarter, args.out, _model(args.live))
    for finding in result["findings"]:
        print(f"  HELD -> {finding['routed_to']}: {finding['text']}")
    print(result["headline"])
    print(f"Written to {args.out}/backtest_{args.quarter}.json")
    return 0


def _mirror(conn) -> None:
    """E2: the end of a demo or eval run pushes the decision log to the Elastic mirror when it is configured."""
    from quota.search.mirror import Mirror
    pushed = Mirror()(conn)
    if pushed is not None:
        print(f"Elastic mirror: {pushed} documents pushed to quota-decisions")


def cmd_gen_messages(args) -> int:
    from quota.sim import messages
    corpus, key = messages.corpus_for(args.world, args.seed)
    messages.write(corpus, key, args.out)
    print(f"Wrote {len(corpus)} messages to {args.out} and {len(key)} answer-key rows to messages_key.json")
    return 0


def cmd_elastic(args) -> int:
    from quota.search import elastic_client
    if not elastic_client.configured() and args.action not in ("eval-search", "sweep"):   # these run offline too
        print("Elastic is not configured: set ELASTICSEARCH_URL and ELASTIC_API_KEY in .env")
        return 2
    if args.action == "ping":
        p = elastic_client.ping()
        print(f"cluster {p['cluster']} (v{p['version']}); embeddings {p['embeddings'] or 'default semantic_text model'}; "
              f"reranker {p['reranker'] or 'none'}; Agent Builder GET /api/agent_builder/agents -> {p['agent_builder']}")
        return 0 if p["agent_builder"] == 200 else 1
    from quota.search import cli
    return cli.run(args)


def cmd_serve(args) -> int:
    import uvicorn
    from quota.api import session as sessions
    from quota.api.app import create_app
    from quota.sim.stripe_billing import StripeSandboxError
    try:
        stripe = _sandbox(args, f"quota-serve-{args.world}")
    except StripeSandboxError as exc:
        print(f"--stripe: {exc}")
        return 2
    llm = None
    if args.live:
        from quota.agent.llm import LLM
        llm = LLM()
    print(f"Building the {args.world} world ...")
    if args.world == "acme":
        session = sessions.acme_session("live" if args.live else "scripted", llm, stripe, evidence=True)
    else:
        session = sessions.fernhill_session(args.seed, args.reps, stripe)
    if args.from_inbox:
        intake = session.attach_inbox(args.from_inbox, llm)
        print(f"Inbox {args.from_inbox}: {sum(intake['report']['loaded'].values())} facts with their file and row; "
              f"{len(intake['report']['review_items'])} review items. {intake['backtest']['headline']}")
    print(f"Clock at {session.world.clock.now()}; serving on http://{args.host}:{args.port}")
    uvicorn.run(create_app(session, runs_dir=args.runs), host=args.host, port=args.port, log_level="warning")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m quota")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="run a scripted demo world")
    demo.add_argument("world", choices=["acme"])
    demo.add_argument("--db", help="write the ledger to this file (default: in-memory)")
    mode = demo.add_mutually_exclusive_group()
    mode.add_argument("--scripted", action="store_true", default=True,
                      help="people and extractions come from the demo script; no model calls (default)")
    mode.add_argument("--live", action="store_true",
                      help="people are played by the model (QUOTA_MODEL, default claude-opus-5-5)")
    demo.add_argument("--stripe", action="store_true",
                      help="invoices, payments and refunds become real objects in a Stripe sandbox (STRIPE_API_KEY)")
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

    evaluate = sub.add_parser("eval", help="run Fernhill for a seed and score Quota against the answer key")
    evaluate.add_argument("--seed", type=int, required=True)
    evaluate.add_argument("--reps", type=int, default=20)
    evaluate.add_argument("--out", default="runs", help="directory for answer_key.json and scorecard.json")
    evaluate.add_argument("--stripe", action="store_true",
                          help="run billing in a Stripe sandbox with test clocks (STRIPE_API_KEY; takes minutes)")
    evaluate.set_defaults(fn=cmd_eval)

    exporter = sub.add_parser("export", help="write a seed's world as messy exports with _answer_key.json (R6)")
    exporter.add_argument("--seed", type=int, default=7)
    exporter.add_argument("--reps", type=int, default=20)
    exporter.add_argument("--out", default="inbox")
    exporter.set_defaults(fn=cmd_export)

    intake = sub.add_parser("intake", help="classify, map, verify and load an inbox of exports (R6)")
    intake.add_argument("inbox", nargs="?", default="inbox")
    intake.add_argument("--live", action="store_true", help="propose mappings and read plans with the model")
    intake.add_argument("--db", help="write the intake ledger to this SQLite file")
    intake.set_defaults(fn=cmd_intake)

    back = sub.add_parser("backtest", help="recompute a quarter's payout spreadsheet from the inbox (R6)")
    back.add_argument("--quarter", default="2026Q1")
    back.add_argument("--inbox", default="inbox")
    back.add_argument("--out", default="out")
    back.add_argument("--live", action="store_true")
    back.set_defaults(fn=cmd_backtest)

    gen = sub.add_parser("gen-messages", help="the sales email and Slack corpus with planted promises (E1)")
    gen.add_argument("--world", choices=["fernhill", "acme"], default="fernhill")
    gen.add_argument("--seed", type=int, default=7)
    gen.add_argument("--out", default="inbox/messages.jsonl")
    gen.set_defaults(fn=cmd_gen_messages)

    elastic = sub.add_parser("elastic", help="the Elastic evidence layer (ELASTIC_TASKS)")
    elastic.add_argument("action", choices=["ping", "setup", "ingest", "verify", "eval-search", "agent-setup",
                                            "sweep"])
    elastic.add_argument("path", nargs="?", help="ingest: the messages JSONL")
    elastic.add_argument("--seed", type=int, default=7)
    elastic.set_defaults(fn=cmd_elastic)

    serve = sub.add_parser("serve", help="serve the API and screens over one running world")
    serve.add_argument("--world", choices=["acme", "fernhill"], default="acme")
    serve.add_argument("--seed", type=int, default=7, help="fernhill only")
    serve.add_argument("--reps", type=int, default=20, help="fernhill only")
    serve.add_argument("--live", action="store_true",
                       help="read chat with the model and parse injected text with it (ANTHROPIC_API_KEY)")
    serve.add_argument("--stripe", action="store_true", help="billing in a Stripe sandbox (STRIPE_API_KEY)")
    serve.add_argument("--runs", default="runs", help="where /scorecard finds or writes runs/seed-N/scorecard.json")
    serve.add_argument("--from-inbox", nargs="?", const="inbox", default=None, dest="from_inbox",
                       help="attach an inbox (exported from --seed if missing): intake, backtest, source cards")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(fn=cmd_serve)

    from quota.env import load_env
    load_env()
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
