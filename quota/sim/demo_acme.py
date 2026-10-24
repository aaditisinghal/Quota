"""The Acme demo (IMPLEMENTATION.md section 9), driven by events.

The script (acme_script.yaml) releases world events and its people act; the billing system and the two
seats do everything else by picking events off their queues and running the seven-step loop. This module
only builds the world, runs the clock and prints the step 24 report from the ledger.
"""

import json
from pathlib import Path

import yaml

from quota import clock as clk
from quota import config
from quota.agent.commissions_analyst import CommissionsAnalyst
from quota.agent.deferred_accountant import DeferredAccountant
from quota.agent.extract import ModelExtractor, ScriptedExtractor
from quota.agent.personas import LivePeople, ScriptedPeople
from quota.engine.plans import normalize
from quota.ledger import hashing, invariants, queries
from quota.money import fmt_pct, fmt_usd
from quota.sim import seed_acme
from quota.sim.world import World

SCRIPT_PATH = Path(__file__).with_name("acme_script.yaml")
REPORT_AT = "2026-06-30T23:45:00Z"
DEAL, REP = "acme", "jordan.kim"


def load_script(path=SCRIPT_PATH) -> dict:
    return normalize(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


class AcmeDemo:
    def __init__(self, db_path=None, out=print, mode="scripted", llm=None, script_path=SCRIPT_PATH):
        script = load_script(script_path)
        self.world = World(seed_acme.SEED_START, db_path, out)
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

    def run(self):
        seed_acme.seed(self.world.wp, self.world.clock, self.world.emit)
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

    def _account_total(self, account, subledger_ref, exclude=()):
        return sum(l["debit_cents"] - l["credit_cents"] for e in queries.journal(self.ro)
                   if e["status"] == "posted" and e["id"] not in exclude
                   for l in e["lines"] if l["account"] == account and l["subledger_ref"] == subledger_ref)

    def cascade(self) -> list[str]:
        journal = [e for e in queries.journal(self.ro) if e["status"] == "posted"]
        refund = next((e for e in journal if queries.memo_kind(e["memo"]) == "refund"
                       and e["lines"][0]["subledger_ref"] == DEAL), None)
        clawback_line = next((l for l in queries.fetch_all(self.ro, "SELECT * FROM commission_lines ORDER BY id")
                              if l["deal_id"] == DEAL and l["amount_cents"] < 0), None)
        clawback = clawback_line and next((e for e in journal if queries.memo_kind(e["memo"]) == "clawback"
                                           and e["decision_id"] == clawback_line["decision_id"]), None)
        if refund is None or clawback is None:   # possible in --live, where people may decide otherwise
            return ["Acme churn: what changed", "  (no posted refund and clawback for acme; nothing to cascade)"]
        clawback_lines = {l["account"]: l for l in clawback["lines"]}
        churn = json.loads(queries.fetch_one(self.ro, "SELECT payload_json FROM events WHERE type = 'customer.churned'")
                           ["payload_json"])
        contract = queries.as_of(self.ro, "contracts", DEAL, None)
        monthly = contract["acv_cents"] // 12
        recognized = len([e for e in journal if queries.memo_kind(e["memo"]) == "revenue"
                          and e["lines"][0]["subledger_ref"] == DEAL])
        remaining = monthly * (contract["term_months"] - recognized)
        clawed = queries.as_of(self.ro, "commission_lines", clawback_line["corrects_id"], None)
        booked = json.loads(queries.as_of(self.ro, "decisions", clawed["decision_id"], None)["output_json"])["result"]
        plan = queries.fetch_one(self.ro, "SELECT config_json FROM plans WHERE rep_id = ? ORDER BY version DESC", (REP,))
        quota_cents = json.loads(plan["config_json"])["quota"]["amount"] * 100
        q1_after = (booked["prior_credit_cents"] + booked["credit_cents"] + clawback_line["credit_cents"]) \
            * 10000 // quota_cents
        deferred_before = -self._account_total("2400", DEAL, exclude=(refund["id"],))
        deferred_after = -self._account_total("2400", DEAL)
        cash_effect = sum(l["debit_cents"] - l["credit_cents"] for l in refund["lines"] if l["account"] == "1000")
        items = [
            ("Deferred revenue", f"{fmt_usd(deferred_before)} -> {fmt_usd(deferred_after)} (refund {refund['id']})"),
            ("Cash", f"{fmt_usd(cash_effect)} ({refund['id']})"),
            ("Acme customer balance", f"{fmt_usd(self._account_total('1200', DEAL))}, contract terminated "
                                      f"{churn['effective_date']}"),
            ("Revenue forecast", f"{fmt_usd(-monthly)}/month from {clk.add_periods(churn['effective_date'][:7], 1)} "
                                 f"({fmt_usd(-remaining)} remaining contract)"),
            ("Jordan commission", f"{fmt_usd(clawback_line['amount_cents'])} clawback ({clawback_line['id']}, "
                                  f"{clawback['id']})"),
            ("Deferred commission", f"{fmt_usd(clawback_lines['1350']['credit_cents'])} written off ({clawback['id']})"),
            ("Commission expense", f"{fmt_usd(-clawback_lines['6110']['credit_cents'])} amortization reversed "
                                   f"({clawback['id']})"),
            ("Jordan Q1 attainment", f"{fmt_pct(booked['attainment_after_bps'])} -> {fmt_pct(q1_after)} (report only; "
                                     "2026-Q1 periods locked)"),
        ]
        return ["Acme churn: what changed"] + [f"  {n}. {label:<24}{text}" for n, (label, text) in
                                               enumerate(items, start=1)]

    @property
    def ok(self) -> bool:
        return (not any(self.results["invariants"].values()) and self.results["chain"][0]
                and all(self.results["replays"].values()))


def run(db_path=None, out=print, mode="scripted", llm=None) -> AcmeDemo:
    return AcmeDemo(db_path, out, mode, llm).run()
