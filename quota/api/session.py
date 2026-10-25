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
from quota.api import views
from quota.api.chat import RuleExtractor
from quota.ledger import queries
from quota.sim import demo_acme, demo_cast, seed_acme
from quota.sim.generator import generate

ACME_START = "2026-04-27T17:00:00Z"
INJECTED_BY_PEOPLE = {"customer.churned", "refund.issued"}
ROUTINE_APPROVALS = {"priya.nair": {"journal_entry", "payroll_file"}}
FINAL_PAY_RELEASES = {"dana.ortiz": "PR-FINAL-"}      # Payroll releases an approved final-pay file the same day
HUMAN_KINDS = {"human"}


class Session:
    def __init__(self, world, name: str, mode: str = "scripted", llm=None, seed: int | None = None, restart=None):
        self.world, self.name, self.mode, self.llm, self.seed = world, name, mode, llm, seed
        self.restart = restart   # builds this session's start again (POST /demo/reset)
        self.scenario: str | None = None
        self.checkpoint_dir = ".checkpoints"     # R4 snapshots (quota.sim.checkpoints)
        self.inbox, self.intake, self.provenance = None, None, {}     # R6 (attach_inbox)
        self.lock = threading.RLock()
        world.strict = False     # judges type things: a failing event is logged and contained, never fatal

    def attach_inbox(self, inbox, llm=None) -> dict:
        """R6: read an inbox (exported from this seed when it is not there yet), keep its intake report and
        backtest, and index where each fact came from for the trace's source cards."""
        from pathlib import Path

        from quota.intake import backtest
        from quota.intake.export import export
        from quota.intake.intake import Intake
        if not (Path(inbox) / "_answer_key.json").exists():
            export(self.seed if self.seed is not None else 7, inbox)
        intake = Intake(inbox, llm)
        result = backtest.run(inbox, intake=intake)
        self.inbox, self.intake = str(inbox), {"report": intake.report_data, "backtest": result}
        self.provenance = intake.provenance()
        return self.intake

    def poll_findings(self, out=print) -> list[str]:
        """E6: pull the promise sweep's new findings into the served world as evidence.found events (when Elastic
        is configured and the world has evidence); the seats then run under the lock."""
        from quota.search import elastic_client
        if getattr(self.world, "evidence", None) is None or not elastic_client.configured():
            return []
        from quota.search.findings import ElasticFindings, poll
        with self.lock:
            try:
                passed = poll(ElasticFindings(elastic_client.es()), self.world.evidence, self.world, out)
            except Exception as exc:
                out(f"findings poll skipped: {type(exc).__name__}: {str(exc)[:160]}")
                return []
            self.world.settle()
            return passed

    def swap(self, world, scenario: str | None) -> None:
        """Serve another world from now on (a scenario's, or the start again); the old one is closed."""
        old, self.world, self.scenario = self.world, world, scenario
        world.strict = False
        old.close()

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
        return {"world": self.name, "mode": self.mode, "seed": self.seed, "scenario": self.scenario, "clock": now,
                "timeline_length": len(self.world.feed), "principals": self.principals(),
                "pending_approvals": [{"id": a["id"], "kind": a["kind"], "subject": a["subject_id"],
                                       "approver": a["approver"], "amount_cents": a["amount_cents"],
                                       "since": a["known_at"]} for a in queries.pending_approvals(self.ro, now)],
                "upcoming": [{"at": e.release_at, "type": e.type, "payload": e.payload}
                             for e in self.world.queue.upcoming()][:20],
                "away": dict(getattr(self.world.people, "away", {}))}

    def messages(self, principal: str | None = None) -> list[dict]:
        rows = queries.fetch_all(self.ro, "SELECT * FROM messages ORDER BY known_at, id")
        if principal:
            rows = [m for m in rows if principal in (m["author"], m["to_principal"])]
        return [{"id": m["id"], "author": m["author"], "to": m["to_principal"], "text": m["text"],
                 "at": m["known_at"], "author_name": config.display_name(m["author"]),
                 "about": (views.message_decisions(self.ro, m) or [None])[-1]} for m in rows]

    def close(self):
        self.world.close()


def attach_evidence(world, extra=()) -> None:
    """ELASTIC_TASKS E5: the served Acme world's sales messages (its own noise-only corpus) as evidence for the
    payout gate, plus any messages a scenario adds."""
    from quota.search.evidence import LocalEvidence
    from quota.sim import messages
    corpus, _ = messages.corpus_for("acme", 7)
    world.evidence = LocalEvidence([*corpus, *extra])


def acme_session(mode: str = "scripted", llm=None, stripe=None, db_path=None, cast: bool = True,
                 evidence: bool = False) -> Session:
    """Up to the pause the story is the scripted one in every mode; `live` then reads people's messages with
    the model and words the analyst's replies with it. `cast` adds SPEC's other named reps (Alex, Riley,
    Taylor) so the rehearsed judge phrases have someone to happen to."""
    demo = demo_acme.AcmeDemo(db_path, out=lambda line: None, stripe=stripe)
    world = demo.world
    script = demo_acme.load_script()
    seed_acme.seed(world.wp, world.clock, world.emit)
    if cast:
        demo_cast.add_cast(world)
    world.run_until(ACME_START)
    world.queue.unschedule(INJECTED_BY_PEOPLE)
    later = [a for a in script["people"]["actions"] if a["at"] > ACME_START and a["do"] != "message"]
    world.people = PolicyPeople(world, later, ROUTINE_APPROVALS, releases=FINAL_PAY_RELEASES)
    if evidence:
        attach_evidence(world)
    session = Session(world, "acme", mode, llm,
                      restart=lambda: acme_session(mode, llm, stripe, cast=cast, evidence=evidence).world)
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
