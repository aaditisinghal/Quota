"""Fernhill: a generated company for the eval (IMPLEMENTATION.md section 13).

`generate(seed)` returns a World whose facts arrive as events while the clock runs. Reps, their signed plans
and the open periods are seeded up front; every deal is created in the CRM, closed, countersigned, invoiced
and (usually) paid on its own dates; the close calendar runs the seats' jobs each month; Priya locks each
month on the 10th and Dana releases payroll on the 15th. The same seed always builds the same world.
"""

import json
import math
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from quota import clock as clk
from quota import config
from quota.agent.commissions_analyst import CommissionsAnalyst
from quota.agent.deferred_accountant import DeferredAccountant
from quota.agent.extract import ScriptedExtractor
from quota.agent.personas import PolicyPeople
from quota.engine.plans import load_plan_raw
from quota.sim import seed_acme
from quota.sim.world import World

SEED_START = "2025-12-01T00:00:00Z"
SEED = "seed"
STAFF = ["priya.nair", "marcus.lee", "dana.ortiz", "morgan.diaz"]
NAMED_REPS = [("jordan.kim", "Jordan Kim"), ("alex.chen", "Alex Chen"), ("taylor.brooks", "Taylor Brooks"),
              ("riley.shah", "Riley Shah")]
FIRST = ["Avery", "Blake", "Casey", "Devon", "Emerson", "Finley", "Gray", "Harper", "Indira", "Jules", "Kendall",
         "Logan", "Noel", "Oakley", "Parker", "Quinn", "Reese", "Sage", "Tatum", "Uma", "Vance", "Wren", "Yara",
         "Zane"]
LAST = ["Abbott", "Bishop", "Castillo", "Duarte", "Ellison", "Foster", "Garza", "Holt", "Ibarra", "Jensen",
        "Kowalski", "Lindqvist", "Mendez", "Nakamura", "Okafor", "Petrov", "Quintero", "Rasmussen", "Sato",
        "Tanaka", "Underwood", "Vasquez", "Whitfield", "Young"]
ACCOUNT_A = ["Amber", "Basalt", "Cobalt", "Driftwood", "Ember", "Fjord", "Granite", "Harbor", "Iris", "Juniper",
             "Kestrel", "Lumen", "Meridian", "Northwind", "Onyx", "Pinecrest", "Quarry", "Redwood", "Summit",
             "Tidal", "Umber", "Vantage", "Willow", "Zephyr"]
ACCOUNT_B = ["Analytics", "Bakery", "Clinics", "Dental", "Energy", "Foods", "Freight", "Health", "Insurance",
             "Labs", "Logistics", "Media", "Motors", "Outdoors", "Pharma", "Robotics", "Retail", "Studios",
             "Systems", "Textiles", "Travel", "Utilities", "Ventures", "Works"]
TERMS, TERM_WEIGHTS = [12, 24, 36], [0.5, 0.3, 0.2]
SPLIT_SHARE = 0.15
SPLITS = [5000, 6000, 7000]
APPROVES = {"priya.nair": {"journal_entry", "payroll_file", "plan_exception"}, "marcus.lee": {"plan_exception"}}


@dataclass
class Rep:
    id: str
    name: str
    state: str


@dataclass
class Deal:
    id: str
    account: str
    owner: str
    splits: list[dict]
    acv_cents: int
    term: int
    booked_at: str
    created_at: str
    crm_close_date: str
    crm_amount_cents: int
    invoice_at: str
    paid_at: str | None
    tags: set = field(default_factory=set)      # planted-error notes (step 11)


def _ts(day: date, minutes_after_nine: int = 0) -> str:
    return f"{day.isoformat()}T{9 + minutes_after_nine // 60:02d}:{minutes_after_nine % 60:02d}:00Z"


def _business_days(first: date, last: date) -> list[date]:
    days, day = [], first
    while day <= last:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def _months(start: str, end: str) -> list[str]:
    months, period = [], start[:7]
    while period <= end[:7]:
        months.append(period)
        period = clk.add_periods(period, 1)
    return months


class Fernhill:
    """The generated facts and calendar, before they are loaded into a World."""

    def __init__(self, seed: int, reps: int = 20, start: str = "2026-01-01", end: str = "2026-06-30"):
        self.seed, self.start, self.end = seed, start, end
        self.rng = random.Random(seed)
        self.reps = self._make_reps(reps)
        self.deals: list[Deal] = self._make_deals()
        self.events: list[tuple[str, str, dict]] = []
        self.actions: list[dict] = []
        self.extractions: dict[str, dict] = {}
        self.answer_key: list[dict] = []

    # ---------- people and deals ----------

    def _make_reps(self, count) -> list[Rep]:
        names = list(NAMED_REPS)
        pool = [(f"{f.lower()}.{l.lower()}", f"{f} {l}") for f in FIRST for l in LAST]
        self.rng.shuffle(pool)
        names += [p for p in pool if p[0] not in dict(names)][:max(0, count - len(names))]
        names = names[:count]
        california = {"taylor.brooks"} | set(self.rng.sample([n for n, _ in names if n != "taylor.brooks"],
                                                             max(0, round(0.3 * count) - 1)))
        return [Rep(rep_id, name, "CA" if rep_id in california else self.rng.choice(["NY", "TX", "WA", "IL", "MA"]))
                for rep_id, name in names]

    def _quarters(self) -> list[tuple[date, date]]:
        first, last = date.fromisoformat(self.start), date.fromisoformat(self.end)
        quarters, q_start = [], date(first.year, 3 * ((first.month - 1) // 3) + 1, 1)
        while q_start <= last:
            q_next = date(q_start.year + (q_start.month + 2) // 12, (q_start.month + 2) % 12 + 1, 1)
            quarters.append((max(q_start, first), min(q_next - timedelta(days=1), last)))
            q_start = q_next
        return quarters

    def _account(self, used: set) -> tuple[str, str]:
        while True:
            name = f"{self.rng.choice(ACCOUNT_A)} {self.rng.choice(ACCOUNT_B)}"
            slug = name.lower().replace(" ", "-")
            if slug not in used:
                used.add(slug)
                return slug, name

    def _make_deals(self) -> list[Deal]:
        deals, used = [], set()
        for rep in self.reps:
            for q_start, q_end in self._quarters():
                days = _business_days(q_start, q_end)
                for _ in range(self.rng.randint(4, 8)):
                    deals.append(self._deal(rep, self.rng.choice(days), used))
        return sorted(deals, key=lambda d: (d.booked_at, d.id))

    def _deal(self, rep: Rep, day: date, used: set) -> Deal:
        rng = self.rng
        deal_id, account = self._account(used)
        booked_at = _ts(day, rng.randrange(0, 510, 10))
        acv = round(math.exp(rng.uniform(math.log(15_000), math.log(250_000))) / 500) * 500 * 100
        splits = [{"rep": rep.id, "bps": 10000}]
        if rng.random() < SPLIT_SHARE:
            partner = rng.choice([r.id for r in self.reps if r.id != rep.id])
            share = rng.choice(SPLITS)
            splits = [{"rep": rep.id, "bps": share}, {"rep": partner, "bps": 10000 - share}]
        created = max(date.fromisoformat(self.start), day - timedelta(days=rng.randint(14, 45)))
        invoice_at = clk.add_minutes(booked_at, 120)
        paid_day = date.fromisoformat(invoice_at[:10]) + timedelta(days=rng.randint(10, 40))
        paid_at = _ts(paid_day, 60) if paid_day.isoformat() <= self.end else None
        return Deal(deal_id, account, rep.id, splits, acv, rng.choices(TERMS, TERM_WEIGHTS)[0], booked_at,
                    _ts(created) if created < day else clk.add_minutes(booked_at, -240), day.isoformat(), acv,
                    invoice_at, paid_at)

    # ---------- events ----------

    def deal_events(self, deal: Deal) -> list[tuple[str, str, dict]]:
        fields = {"account": deal.account, "crm_amount_cents": deal.crm_amount_cents, "crm_close_date": None,
                  "stage": "open", "owner_id": deal.owner, "splits": deal.splits}
        out = [(deal.created_at, "deal.created", {"deal_id": deal.id, "fields": fields, "actor": deal.owner}),
               (clk.add_minutes(deal.booked_at, -30), "deal.updated",
                {"deal_id": deal.id, "fields": {"stage": "closed_won", "crm_close_date": deal.crm_close_date},
                 "actor": deal.owner}),
               (deal.booked_at, "contract.countersigned",
                {"deal_id": deal.id, "timestamp": deal.booked_at, "acv_cents": deal.acv_cents,
                 "term_months": deal.term, "clauses": {"billing": "annual_upfront"}}),
               (deal.invoice_at, "invoice.issued", {"stripe_id": f"in_{deal.id}", "deal_id": deal.id,
                                                    "amount_cents": deal.acv_cents, "date": deal.invoice_at[:10]})]
        if deal.paid_at:
            out.append((deal.paid_at, "payment.received", {"stripe_id": f"py_{deal.id}", "deal_id": deal.id,
                                                           "amount_cents": deal.acv_cents, "date": deal.paid_at[:10]}))
        return out

    def calendar(self) -> list[tuple[str, str, dict]]:
        """The close calendar: month-end jobs, mid-month payroll."""
        out = []
        for period in _months(self.start, self.end):
            last = clk.period_end(period)
            job = lambda at, name: (f"{last}T{at}:00Z", "job.due", {"job": name, "period": period})
            out += [job("20:00", "commission_run"), job("20:30", "commission_accrual"),
                    job("23:00", "revenue_recognition"), job("23:00", "amortization"), job("23:20", "churn_review")]
            if period > self.start[:7]:
                out.append((f"{period}-14T17:00:00Z", "job.due", {"job": "payroll_file", "period": period}))
        return out

    def people_actions(self) -> list[dict]:
        out = []
        for period in _months(self.start, self.end):
            lock_at = f"{clk.add_periods(period, 1)}-10T20:00:00Z"
            if lock_at[:10] <= self.end:
                out.append({"at": lock_at, "who": "priya.nair", "do": "lock_period", "period": period})
            if period > self.start[:7]:
                out.append({"at": f"{period}-15T17:00:00Z", "who": "dana.ortiz", "do": "release_payroll",
                            "file": f"PR-{period}"})
        return out

    def all_events(self) -> list[tuple[str, str, dict]]:
        events = [e for deal in self.deals for e in self.deal_events(deal)] + self.calendar() + self.events
        return sorted(events, key=lambda e: (e[0], json.dumps(e[2], sort_keys=True)))

    # ---------- loading ----------

    def principals(self) -> dict:
        return {rep.id: config.rep_principal(rep.name) for rep in self.reps}

    def seed_facts(self, world: World) -> None:
        wp, clock = world.wp, world.clock
        clock.advance_to(SEED_START)
        for person in STAFF:
            wp.append_fact("people", {"id": person, "name": config.display_name(person), "role": config.title(person),
                                      "state": None, "manager_id": None, "start_date": None, "end_date": None,
                                      "source": "hr"}, SEED)
        for rep in self.reps:
            wp.append_fact("people", {"id": rep.id, "name": rep.name, "role": "AE", "state": rep.state,
                                      "manager_id": "morgan.diaz", "start_date": "2024-03-01", "end_date": None,
                                      "source": "hr"}, SEED)
        clock.advance_to("2025-12-18T17:00:00Z")
        template = load_plan_raw(seed_acme.PLAN_PATH)
        for rep in self.reps:
            plan_id = f"AE-2026-H1-{rep.id}"
            raw = {**template, "plan_id": plan_id, "rep": rep.id, "signed_doc": f"plans/{rep.id}/AE-2026-H1-v3.pdf"}
            wp.append_fact("plans", {"id": plan_id, "version": raw["version"], "rep_id": rep.id,
                                     "effective_from": raw["effective"]["from"], "effective_to": raw["effective"]["to"],
                                     "config_json": raw, "signed_doc_ref": raw["signed_doc"],
                                     "signed_at": raw["signed_at"]}, SEED)
        clock.advance_to(f"{self.start}T00:00:00Z")
        for month in range(1, 13):
            wp.append_fact("periods", {"period": f"{self.start[:4]}-{month:02d}", "status": "open", "actor": SEED},
                           SEED)
        wp.append_fact("precedents", {
            "id": "P-0000", "statement": "Monthly amortization of Controller-approved commission schedules may "
                                         "post without per-entry approval",
            "scope_json": {"kind": "amortization"}, "override_json": {"per_entry_approval": False},
            "approvals_json": ["priya.nair"], "effective_from": f"{self.start[:4]}-01-01",
            "expires_at": f"{self.start[:4]}-12-31"}, SEED)

    def build(self, db_path=None, out=None) -> World:
        config.register_principals(self.principals())
        world = World(SEED_START, db_path, out or (lambda line: None),
                      principals={**config.principals(with_seed=True), **self.principals()})
        analyst = CommissionsAnalyst(world, ScriptedExtractor(self.extractions))
        world.seats = [DeferredAccountant(world), analyst]
        world.people = PolicyPeople(world, sorted(self.actions + self.people_actions(), key=lambda a: a["at"]),
                                    APPROVES)
        world.queue.add_timer_source(analyst.timers)
        self.seed_facts(world)
        for at, event_type, payload in self.all_events():
            world.queue.schedule(at, event_type, payload)
        world.end_ts = f"{self.end}T23:59:59Z"
        world.answer_key = self.answer_key
        world.fernhill = self
        return world


def generate(seed: int, reps: int = 20, start: str = "2026-01-01", end: str = "2026-06-30", out_dir=None,
             db_path=None, out=None) -> World:
    """A seeded Fernhill World with its facts and events loaded, ready for `world.run_until(world.end_ts)`.
    The answer key (empty until errors are planted) is written to out_dir/answer_key.json when out_dir is given."""
    fernhill = Fernhill(seed, reps, start, end)
    world = fernhill.build(db_path, out)
    if out_dir is not None:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / "answer_key.json").write_text(json.dumps(fernhill.answer_key, indent=2), encoding="utf-8")
    return world
