"""One running world behind the API.

`acme`: the Acme story (section 9) run to 2026-04-27T17:00Z, just after the April commission run booked
CL-0001, and paused there. From then on the people are whoever is using the screens: they chat as a principal
and inject events. The close calendar still runs on its own, Dana still releases May payroll, and the
Controller still approves routine journal entries and payroll files 30 minutes after they arrive (as she does
in the script); plan exceptions wait for a person. The script's churn and refund are taken off the calendar,
because injecting them is the judge's move.

`fernhill`: a generated world (Phase 3) run to its end, for browsing traces of the planted cases.

Every call that touches the world holds `lock`.
"""

import threading

from quota import config
from quota.agent.extract import ModelExtractor
from quota.agent.personas import PolicyPeople
from quota.api.chat import RuleExtractor
from quota.ledger import queries
from quota.sim import demo_acme, seed_acme
from quota.sim.generator import generate

ACME_START = "2026-04-27T17:00:00Z"
INJECTED_BY_PEOPLE = {"customer.churned", "refund.issued"}
ROUTINE_APPROVALS = {"priya.nair": {"journal_entry", "payroll_file"}}
HUMAN_KINDS = {"human"}


class Session:
    def __init__(self, world, name: str, mode: str = "scripted", llm=None, seed: int | None = None):
        self.world, self.name, self.mode, self.llm, self.seed = world, name, mode, llm, seed
        self.lock = threading.RLock()

    @property
    def ro(self):
        return self.world.ro

    @property
    def analyst(self):
        return next(s for s in self.world.seats if s.principal == "seat:commissions_analyst")

    def principals(self) -> list[dict]:
        """Who the Chat tab can speak as: the people, not the seats or systems."""
        return [{"id": p, "name": spec.get("name", p), "title": spec.get("title", ""),
                 "capabilities": sorted(spec["capabilities"]), "limit_cents": spec.get("limit")}
                for p, spec in self.world.wp.principals.items() if spec.get("kind") in HUMAN_KINDS]

    def state(self) -> dict:
        now = self.world.clock.now()
        return {"world": self.name, "mode": self.mode, "seed": self.seed, "clock": now,
                "timeline_length": len(self.world.feed), "principals": self.principals(),
                "pending_approvals": [{"id": a["id"], "kind": a["kind"], "subject": a["subject_id"],
                                       "approver": a["approver"], "amount_cents": a["amount_cents"],
                                       "since": a["known_at"]} for a in queries.pending_approvals(self.ro, now)],
                "upcoming": [{"at": e.release_at, "type": e.type, "payload": e.payload}
                             for e in self.world.queue.upcoming()][:20]}

    def messages(self, principal: str | None = None) -> list[dict]:
        rows = queries.fetch_all(self.ro, "SELECT * FROM messages ORDER BY known_at, id")
        if principal:
            rows = [m for m in rows if principal in (m["author"], m["to_principal"])]
        return [{"id": m["id"], "author": m["author"], "to": m["to_principal"], "text": m["text"],
                 "at": m["known_at"], "author_name": config.display_name(m["author"])} for m in rows]

    def close(self):
        self.world.close()


def acme_session(mode: str = "scripted", llm=None, stripe=None, db_path=None) -> Session:
    """Up to the pause the story is the scripted one in every mode; `live` then reads people's messages with
    the model and words the analyst's replies with it."""
    demo = demo_acme.AcmeDemo(db_path, out=lambda line: None, stripe=stripe)
    world = demo.world
    script = demo_acme.load_script()
    seed_acme.seed(world.wp, world.clock, world.emit)
    world.run_until(ACME_START)
    world.queue.unschedule(INJECTED_BY_PEOPLE)
    later = [a for a in script["people"]["actions"] if a["at"] > ACME_START and a["do"] != "message"]
    world.people = PolicyPeople(world, later, ROUTINE_APPROVALS)
    session = Session(world, "acme", mode, llm)
    if mode == "live":
        session.analyst.extractor, session.analyst.llm = ModelExtractor(llm), llm
    else:
        session.analyst.extractor = RuleExtractor(script["extractions"])
    return session


def fernhill_session(seed: int = 7, reps: int = 20, stripe=None) -> Session:
    world = generate(seed, reps=reps, out=lambda line: None, stripe=stripe)
    world.run_until(world.end_ts)
    session = Session(world, "fernhill", seed=seed)
    session.analyst.extractor = RuleExtractor(session.analyst.extractor.table)
    return session
