"""`python -m quota elastic <action>`: one function per action (ELASTIC_TASKS E2, E3, E6)."""

ACTIONS = {}


def action(name):
    def register(fn):
        ACTIONS[name] = fn
        return fn
    return register


def run(args) -> int:
    fn = ACTIONS.get(args.action)
    if fn is None:
        print(f"elastic {args.action}: not built yet")
        return 2
    try:
        return fn(args) or 0
    except Exception as exc:
        if type(exc).__name__ in ("AuthorizationException", "AuthenticationException"):
            print(f"elastic {args.action}: refused by the project ({str(exc)[:200]}). The API key in .env needs "
                  f"privileges on quota-* indexes, ingest pipelines, inference and Agent Builder.")
            return 3
        raise


def _client():
    from quota.search.elastic_client import es
    return es()


@action("setup")
def setup(args):
    from quota.search import indexes
    indexes.setup(_client())


@action("ingest")
def ingest(args):
    from quota.search import indexes
    indexes.ingest(_client(), args.path or "inbox/messages.jsonl")


@action("verify")
def verify(args):
    """Mirror the served Acme story's ledger as it stands at the end of the demo, then compare."""
    from quota.search import mirror
    from quota.sim import demo_acme
    demo = demo_acme.run(out=lambda line: None)
    try:
        client = _client()
        mirror.mirror_decisions(demo.ro, client)
        result = mirror.verify(demo.ro, client)
    finally:
        demo.close()
    print(result["line"])
    return 0 if result["ok"] else 1


@action("eval-search")
def eval_search(args):
    import json
    from pathlib import Path

    from quota.search import evidence, search_eval
    from quota.sim import messages
    path = Path(args.path or "inbox/messages.jsonl")
    if not path.exists():
        corpus, key = messages.corpus_for("fernhill", args.seed)
        messages.write(corpus, key, path)
    key = json.loads((path.parent / "messages_key.json").read_text(encoding="utf-8"))
    backend = evidence.get_backend(str(path))
    corpus_size = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    result = search_eval.run(backend, key, corpus_size)
    for line in search_eval.table(result):
        print(line)
    print("Written to out/search_eval.json")
