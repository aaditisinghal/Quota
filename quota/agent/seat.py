"""Seat base class: the seven-step loop.

A seat picks up events from its own queue; nobody prompts it. Steps 2-6 read only through the read-only
connection (models read words, code decides dollars and outcomes), and step 7 writes only through WritePath.
Subclasses route each event to a name and implement `<step>_<route>` for the steps that route needs; a step
without a method passes through. A route may change during the loop (a message becomes a dispute once its
claim is extracted); an event with no route is marked ignored.
"""

from dataclasses import dataclass, field

from quota.ledger import queries
from quota.sim.events import Event


@dataclass
class Context:
    event: Event
    route: str | None
    rows: dict = field(default_factory=dict)    # rows read for the decision (become its inputs)
    data: dict = field(default_factory=dict)    # anything else read along the way
    facts: object = None
    res: object = None
    calc: object = None
    decision: object = None


class Seat:
    principal = ""
    handles: frozenset[str] = frozenset()

    def __init__(self, world):
        self.world = world
        self.ro = world.ro
        self.wp = world.wp
        self.clock = world.clock

    @property
    def now(self) -> str:
        return self.clock.now()

    def emit(self, kind, text):
        self.world.emit(self.principal, kind, text)

    def get(self, table, row_id):
        return queries.as_of(self.ro, table, row_id, self.now)

    # ---------- the loop ----------

    def run_once(self) -> list[str] | None:
        """Handle one event; returns the ids of decisions made, or None when the queue is empty."""
        ev = self.claim_next_event()            # 1 pick up
        if ev is None:
            return None
        ctx = self.gather(ev)                   # 2 read-only; records (table, id, version, sha256)
        facts = self.extract(ctx)               # 3 model, verified
        res = self.reconcile(facts, ctx)        # 4 ranking.py
        calc = self.calculate(res, ctx)         # 5 calculator/entries
        decision = self.decide(calc, res, ctx)  # 6 policy code: post|hold|escalate|correct
        return self.write(decision, ctx)        # 7 WritePath only

    def claim_next_event(self) -> Event | None:
        return self.world.queue.next_for(self.principal, self.handles)

    def route(self, ev: Event) -> str | None:
        raise NotImplementedError

    def gather(self, ev: Event) -> Context:
        ctx = Context(ev, self.route(ev))
        self._step("gather", ctx)
        return ctx

    def extract(self, ctx: Context):
        ctx.facts = self._step("extract", ctx)
        return ctx.facts

    def reconcile(self, facts, ctx: Context):
        ctx.res = self._step("reconcile", ctx)
        return ctx.res

    def calculate(self, res, ctx: Context):
        ctx.calc = self._step("calculate", ctx)
        return ctx.calc

    def decide(self, calc, res, ctx: Context):
        ctx.decision = self._step("decide", ctx)
        return ctx.decision

    def write(self, decision, ctx: Context) -> list[str]:
        decision_ids = self._step("write", ctx) or []
        self.wp.mark_event(self.principal, ctx.event.id, "handled" if ctx.route else "ignored")
        return decision_ids

    def _step(self, step, ctx: Context):
        handler = getattr(self, f"{step}_{ctx.route}", None) if ctx.route else None
        return handler(ctx) if handler else None

    # ---------- shared reads ----------

    def lines(self, deal_id=None, rep_id=None) -> list[dict]:
        found = queries.fetch_all(self.ro, "SELECT * FROM commission_lines WHERE known_at <= ? ORDER BY id",
                                  (self.now,))
        return [l for l in found if (deal_id is None or l["deal_id"] == deal_id)
                and (rep_id is None or l["rep_id"] == rep_id)]

    def line_status(self, line_id) -> str | None:
        return queries.line_status(self.ro, line_id, self.now)

    def current_line(self, deal_id, rep_id) -> dict | None:
        """The newest positive line for (deal, rep) that has not been superseded or clawed back."""
        live = [l for l in self.lines(deal_id, rep_id) if l["amount_cents"] > 0
                and self.line_status(l["id"]) not in ("superseded", "clawed_back")]
        return live[-1] if live else None

    def decisions(self, kind=None, subject_id=None) -> list[dict]:
        found = queries.fetch_all(self.ro, "SELECT * FROM decisions WHERE known_at <= ? ORDER BY known_at, id",
                                  (self.now,))
        return [d for d in found if (kind is None or d["kind"] == kind)
                and (subject_id is None or d["subject_id"] == subject_id)]

    def approval_root(self, approval_id) -> dict:
        approval = self.get("approvals", approval_id)
        while approval["parent_id"] is not None:
            approval = self.get("approvals", approval["parent_id"])
        return approval
