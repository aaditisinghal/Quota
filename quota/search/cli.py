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
    return fn(args) or 0
