"""The Acme demo (IMPLEMENTATION.md section 9), driven by events.

The script (acme_script.yaml) releases world events and its people act; the billing system and the two
seats do everything else by picking events off their queues and running the seven-step loop. This module
only builds the world, runs the clock and prints the step 24 report from the ledger.
"""

from pathlib import Path

import yaml

from quota import config
from quota.agent.commissions_analyst import CommissionsAnalyst
from quota.agent.deferred_accountant import DeferredAccountant
from quota.agent.extract import ModelExtractor, ScriptedExtractor
from quota.agent.personas import LivePeople, ScriptedPeople
from quota.engine.plans import normalize
from quota.ledger import cascade, hashing, invariants, queries
from quota.money import fmt_usd
from quota.sim import seed_acme
from quota.sim.world import World

SCRIPT_PATH = Path(__file__).with_name("acme_script.yaml")
REPORT_AT = "2026-06-30T23:45:00Z"
DEAL, REP = "acme", "jordan.kim"


def load_script(path=SCRIPT_PATH) -> dict:
    return normalize(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


class AcmeDemo:
    def __init__(self, db_path=None, out=print, mode="scripted", llm=None, script_path=SCRIPT_PATH, stripe=None,
                 edit=None):
        script = load_script(script_path)
        if edit is not None:          # a rehearsed scenario (R4) starts from a changed script
            edit(script)
        self.script = script
        self.world = World(seed_acme.SEED_START, db_path, out, stripe=stripe)
        live = mode == "live"
        extractor = ModelExtractor(llm) if live else ScriptedExtractor(script["extractions"])
        analyst = CommissionsAnalyst(self.world, extractor, llm if live else None)
        self.world.seats = [DeferredAccountant(self.world), analyst]
        self.world.people = LivePeople(self.world, script["people"], llm) if live else \
            ScriptedPeople(self.world, script["people"])
        self.world.queue.add_timer_source(analyst.timers)
        for event in script["events"]:
            self.world.queue.schedule(event["at"], event["type"], event["payload"])
        self.results: dict = {}

    # ---------- plumbing ----------

    @property
    def ro(self):
        return self.world.ro

    @property
    def conn(self):
        return self.world.conn

    @property
    def uri(self):
        return self.world.uri

    @property
    def lines(self):
        return self.world.lines

    def close(self):
        self.world.close()

    def run(self, checkpoint_dir=None):
        seed_acme.seed(self.world.wp, self.world.clock, self.world.emit)
        if checkpoint_dir is not None:    # R4: snapshot the ledger at each named checkpoint on the way
            from quota.sim import checkpoints
            for name, at in checkpoints.CHECKPOINTS.items():
                self.world.run_until(at)
                checkpoints.snapshot(self.world, name, checkpoint_dir)
        self.world.run_until(REPORT_AT)
        self.report()
        return self

    # ---------- step 24: report ----------

    def report(self):
        say = self.world.say
        say()
        say(f"REPORT as of {self.world.clock.now()}")
        say()
        say("JOURNAL")
        for entry in queries.journal(self.ro):
            say(f"  {entry['id']}  {entry['effective_date']}  {entry['status']:<8} {entry['memo']}")
            for l in entry["lines"]:
                debit = fmt_usd(l["debit_cents"]) if l["debit_cents"] else ""
                credit = fmt_usd(l["credit_cents"]) if l["credit_cents"] else ""
                say(f"           {'Dr' if debit else 'Cr'} {l['account']} {config.ACCOUNTS[l['account']][0]:<36}"
                    f"{debit:>14}{credit:>14}".rstrip())
        say()
        say("TRIAL BALANCE")
        for line in queries.format_trial_balance(queries.trial_balance(self.ro), config.ACCOUNTS):
            say(line)
        say()
        for line in self.cascade():
            say(line)
        say()
        checks = invariants.check_all(self.ro)
        chain_ok, bad_seq = hashing.verify_chain(self.ro)
        replays = {d: queries.replay_decision(self.ro, d)[0] for d in self.replay_ids()}
        self.results = {"invariants": checks, "chain": (chain_ok, bad_seq), "replays": replays}
        say(invariants.summary_line(checks))
        say(f"CHAIN       {'OK' if chain_ok else f'FAIL at seq {bad_seq}'} ({hashing.chain_length(self.ro)} rows)")
        say("REPLAY      " + "  ".join(f"{d} {'MATCH' if ok else 'MISMATCH'}" for d, ok in replays.items()))

    def replay_ids(self) -> list[str]:
        """The first commission calc, the latest one, and the clawback (D-0001, D-0005, D-0007 when scripted)."""
        decisions = queries.fetch_all(self.ro, "SELECT id, kind FROM decisions ORDER BY id")
        calcs = [d["id"] for d in decisions if d["kind"] == "commission_calc"]
        clawbacks = [d["id"] for d in decisions if d["kind"] == "clawback"]
        return list(dict.fromkeys(calcs[:1] + calcs[-1:] + clawbacks[:1]))

    def cascade(self) -> list[str]:
        items = cascade.churn_cascade(self.ro, DEAL)
        if items is None:                    # possible in --live, where people may decide otherwise
            return ["Acme churn: what changed", "  (no posted refund and clawback for acme; nothing to cascade)"]
        return ["Acme churn: what changed"] + [f"  {n}. {i['label']:<24}{i['text']}" for n, i in
                                               enumerate(items, start=1)]

    @property
    def ok(self) -> bool:
        return (not any(self.results["invariants"].values()) and self.results["chain"][0]
                and all(self.results["replays"].values()))


def run(db_path=None, out=print, mode="scripted", llm=None, stripe=None) -> AcmeDemo:
    return AcmeDemo(db_path, out, mode, llm, stripe=stripe).run()
