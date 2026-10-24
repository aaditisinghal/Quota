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
# 40 planted cases, at least 2 of every type; rep-level and territory cases go first (they reserve whole books).
PLANTS = [("E10", 2), ("E05", 2), ("E09", 2), ("E14", 2), ("E16", 2), ("E08", 3), ("E15", 2), ("E07", 3), ("E12", 3),
          ("E03", 3), ("E04", 3), ("E02", 2), ("E11", 2), ("E01", 3), ("E06", 3), ("E13", 3)]
NOTHING_BOOKED = {"E02", "E05", "E10"}           # expected amount 0: no line should exist
RECOVERED = {"E08", "E15"}                        # expected amount 0: paid, then recovered in full
NEVER_PAID = {"E02", "E05", "E07", "E10"}         # the most that may reach payroll is $0


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
    tags: set = field(default_factory=set)      # planted case ids
    created_as: dict | None = None              # CRM fields at creation when a later edit changed them (E10)
    amendment: dict | None = None               # {"acv_cents", "signed_at"} when an amendment raises ACV (E12)


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
        self.payroll_edits: dict[str, dict] = {}      # period -> a hand edit in Dana's export (E16)
        self.plan_drafts: dict[str, dict] = {}         # rep -> unsigned plan version (E14)
        self.used_deals: set[str] = set()
        self.exclusive_reps: set[str] = set()         # reps whose whole book is one case

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
        deals, self.accounts_used = [], set()
        for rep in self.reps:
            for q_start, q_end in self._quarters():
                days = _business_days(q_start, q_end)
                for _ in range(self.rng.randint(4, 8)):
                    deals.append(self._deal(rep, self.rng.choice(days), self.accounts_used))
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

    # ---------- planting (one instance per call; each returns its answer-key case) ----------

    @staticmethod
    def deal_reps(deal: Deal) -> set[str]:
        return {deal.owner} | {s["rep"] for s in deal.splits}

    def pick(self, predicate=lambda d: True, single=True) -> Deal:
        """A deal no other case uses, on a rep whose book is not reserved for a rep-level case."""
        candidates = [d for d in self.deals if d.id not in self.used_deals and predicate(d)
                      and not self.deal_reps(d) & self.exclusive_reps and (len(d.splits) == 1 or not single)]
        if not candidates:
            raise ValueError("no deal fits this plant")
        deal = self.rng.choice(candidates)
        self.used_deals.add(deal.id)
        return deal

    def take_rep(self, predicate=lambda r: True) -> Rep:
        """A rep reserved for a rep-level case: none of their deals is used by any other case."""
        busy = {r for d in self.deals if d.id in self.used_deals for r in self.deal_reps(d)} | self.exclusive_reps
        candidates = [r for r in self.reps if r.id not in busy and predicate(r)]
        if not candidates:
            raise ValueError("no rep fits this plant")
        rep = self.rng.choice(candidates)
        self.exclusive_reps.add(rep.id)
        return rep

    def rebook(self, deal: Deal, booked_at: str) -> None:
        lag = (date.fromisoformat(deal.paid_at[:10]) - date.fromisoformat(deal.invoice_at[:10])).days \
            if deal.paid_at else None
        deal.booked_at, deal.crm_close_date = booked_at, booked_at[:10]
        deal.invoice_at = clk.add_minutes(booked_at, 120)
        if lag is not None:
            paid = date.fromisoformat(deal.invoice_at[:10]) + timedelta(days=lag)
            deal.paid_at = _ts(paid, 60) if paid.isoformat() <= self.end else None
        if deal.created_at >= deal.booked_at:
            deal.created_at = clk.add_minutes(booked_at, -60 * 24 * 14)

    def case(self, error, deal: Deal | None, rep: str, outcome: str, asks: list[str], note: str, **extra) -> dict:
        number = sum(c["type"] == error for c in self.answer_key) + 1
        row = {"case_id": f"{error}-{number:04d}", "type": error, "deal_id": deal.id if deal else None, "rep_id": rep,
               "expected_outcome": outcome, "expected_amount_cents": None, "expected_asks": asks, "note": note, **extra}
        self.answer_key.append(row)
        if deal:
            deal.tags.add(row["case_id"])
        return row

    def _in(self, first: str, last: str):
        return lambda d: first <= d.booked_at[:10] <= last

    def plant_E01(self):
        """CRM amount higher than the signed contract ACV."""
        deal = self.pick()
        deal.crm_amount_cents = round(deal.acv_cents * self.rng.choice([1.15, 1.25, 1.4]) / 100_000) * 100_000
        return self.case("E01", deal, deal.owner, "corrected", [], f"CRM {deal.crm_amount_cents} vs ACV {deal.acv_cents}")

    def plant_E02(self):
        """Deal splits sum to 110%."""
        deal = self.pick()
        partner = self.rng.choice([r.id for r in self.reps if r.id != deal.owner and r.id not in self.exclusive_reps])
        deal.splits = [{"rep": deal.owner, "bps": 6000}, {"rep": partner, "bps": 5000}]
        return self.case("E02", deal, deal.owner, "held", ["marcus.lee"], f"60% {deal.owner} + 50% {partner}")

    def plant_E03(self):
        """CRM close Mar 31, countersigned Apr 2; the rep disputes for Q1."""
        deal = self.pick(self._in("2026-04-01", "2026-06-30"))
        self.rebook(deal, f"2026-04-02T{self.rng.randint(10, 16)}:00:00Z")
        deal.crm_close_date = "2026-03-31"
        text = f"{deal.account} closed Mar 31. It should count in Q1."
        self.actions.append({"at": "2026-05-05T10:00:00Z", "who": deal.owner, "do": "message",
                             "to": "seat:commissions_analyst", "text": text})
        self.extractions[text] = {"kind": "dispute", "deal_ref": deal.id, "line_ref": None, "field": "booking_date",
                                  "asserted_value": "2026-03-31", "amount_cents": None,
                                  "quote": f"{deal.account} closed Mar 31"}
        return self.case("E03", deal, deal.owner, "held", [], "countersigned Apr 2; rep disputes for Q1")

    def plant_E04(self):
        """Close date backdated into a locked quarter after the lock."""
        deal = self.pick(self._in("2026-04-13", "2026-05-29"))
        at = clk.add_minutes(deal.booked_at, 60 * 24 * self.rng.randint(3, 10))
        self.events.append((at, "deal.updated", {"deal_id": deal.id, "fields": {"crm_close_date": "2026-03-30"},
                                                 "actor": deal.owner}))
        return self.case("E04", deal, deal.owner, "escalated", ["marcus.lee", "priya.nair"],
                         f"close date moved to 2026-03-30 at {at}, after the Q1 lock")

    def plant_E05(self):
        """A SPIF promised in chat by a manager without authority."""
        rep = self.take_rep()
        amount = self.rng.choice([1_500, 2_000, 2_500])
        text = f"Morgan promised me a ${amount:,} SPIF for the Q1 push. When does it hit payroll?"
        quote = f"Morgan promised me a ${amount:,} SPIF"
        self.actions.append({"at": f"2026-03-{self.rng.randint(16, 26):02d}T11:00:00Z", "who": rep.id, "do": "message",
                             "to": "seat:commissions_analyst", "text": text})
        self.extractions[text] = {"kind": "spiff_claim", "deal_ref": None, "line_ref": None, "field": None,
                                  "asserted_value": None, "amount_cents": amount * 100, "quote": quote}
        return self.case("E05", None, rep.id, "escalated", ["marcus.lee"], f"${amount} SPIF promised by Morgan",
                         scope="rep")

    def plant_E06(self):
        """Multi-year deal commissioned on TCV in the CRM."""
        deal = self.pick(lambda d: d.term > 12)
        deal.crm_amount_cents = deal.acv_cents * deal.term // 12
        return self.case("E06", deal, deal.owner, "corrected", [], f"CRM shows TCV {deal.crm_amount_cents}")

    def plant_E07(self):
        """Customer 120 days late."""
        deal = self.pick(self._in("2026-01-05", "2026-02-27"))
        deal.paid_at = None
        at = clk.add_minutes(deal.invoice_at, 60 * 24 * 120)
        self.events.append((at, "payment.overdue", {"deal_id": deal.id, "days": 120}))
        return self.case("E07", deal, deal.owner, "held", [], "no payment; 120 days late")

    def plant_E08(self):
        """Churn inside the 6-month clawback window."""
        deal = self.pick(lambda d: d.term > 12 and "2026-01-05" <= d.booked_at[:10] <= "2026-02-27")
        deal.paid_at = _ts(date.fromisoformat(deal.invoice_at[:10]) + timedelta(days=12), 60)
        effective = min(clk.add_months(deal.booked_at[:10], self.rng.randint(3, 4)), "2026-06-25")
        recognized = len(_months(deal.booked_at[:10], effective))     # booking month through churn month
        refund = deal.acv_cents * (12 - recognized) // 12
        self.events += [(f"{clk.add_days(effective + 'T10:00:00Z', -2)}", "customer.churned",
                         {"deal_id": deal.id, "effective_date": effective, "reason": "termination_for_convenience",
                          "refund_cents": refund}),
                        (f"{effective}T18:00:00Z", "refund.issued", {"stripe_id": f"re_{deal.id}", "deal_id": deal.id,
                                                                     "amount_cents": refund, "date": effective})]
        return self.case("E08", deal, deal.owner, "escalated", ["priya.nair"], f"churned {effective}")

    def plant_E09(self):
        """Rep terminated before payout, California."""
        rep = self.take_rep(lambda r: r.state == "CA" and any(
            d.owner == r.id and len(d.splits) == 1 and "2026-04-01" <= d.booked_at[:10] <= "2026-05-25"
            for d in self.deals))
        end = "2026-06-24"
        self.deals = [d for d in self.deals if not (d.owner == rep.id and d.booked_at[:10] > "2026-06-10")]
        mine = [d for d in self.deals if d.owner == rep.id]
        for deal in mine:
            if deal.booked_at[:10] <= "2026-06-10":
                deal.paid_at = _ts(date.fromisoformat(deal.invoice_at[:10]) + timedelta(days=10), 60)
        case_deal = max((d for d in mine if d.booked_at[:10] <= "2026-05-25" and len(d.splits) == 1),
                        key=lambda d: d.booked_at)
        self.used_deals.add(case_deal.id)
        self.events.append((f"{end}T17:00:00Z", "rep.terminated", {"rep_id": rep.id, "end_date": end, "state": rep.state}))
        return self.case("E09", case_deal, rep.id, "posted", ["priya.nair"], f"{rep.id} left {end} ({rep.state})",
                         scope="rep")

    def plant_E10(self, moved=3, unchanged=2):
        """Territory move half done by the Sales Ops bot."""
        def pipeline(rep):
            return [d for d in self.deals if d.owner == rep.id and len(d.splits) == 1 and d.booked_at[:10] >= "2026-04-08"]
        try:
            old = self.take_rep(lambda r: len(pipeline(r)) >= moved + unchanged)
        except ValueError:                      # nobody has enough Q2 pipeline: give a free rep more deals
            old = self.take_rep()
            days = _business_days(date(2026, 4, 8), date.fromisoformat(min(self.end, "2026-06-25")))
            while len(pipeline(old)) < moved + unchanged:
                deal = self._deal(old, self.rng.choice(days), self.accounts_used)
                deal.splits = [{"rep": old.id, "bps": 10000}]
                self.deals.append(deal)
            self.deals.sort(key=lambda d: (d.booked_at, d.id))
        new = self.take_rep()
        deals = sorted(pipeline(old), key=lambda d: d.booked_at)[:moved + unchanged]
        at = "2026-04-01T09:00:00Z"
        for deal in deals:
            deal.created_at = min(deal.created_at, "2026-03-10T09:00:00Z")
            self.used_deals.add(deal.id)
        bot_moved, left = deals[:moved], deals[moved:]
        for deal in bot_moved:
            deal.created_as = {"owner_id": deal.owner, "splits": deal.splits}
            deal.owner, deal.splits = new.id, [{"rep": new.id, "bps": 10000}]
            self.events.append((at, "deal.updated", {"deal_id": deal.id, "actor": "salesops.bot",
                                                     "fields": {"owner_id": new.id, "splits": deal.splits}}))
        self.events.append((at, "rep.territory_changed", {"rep_id": new.id, "accounts": [d.id for d in deals],
                                                         "effective_date": "2026-04-01", "actor": "salesops.bot"}))
        return self.case("E10", left[0], old.id, "held", ["marcus.lee"],
                         f"{moved + unchanged} accounts to {new.id}; {unchanged} not moved",
                         deal_ids=[d.id for d in left], scope="move", moved_to=new.id)

    def plant_E11(self):
        """Same deal credited to two reps after a territory move."""
        deal = self.pick(self._in("2026-04-01", "2026-06-25"))
        other = self.rng.choice([r.id for r in self.reps if r.id != deal.owner and r.id not in self.exclusive_reps])
        deal.splits = [{"rep": other, "bps": 10000}, {"rep": deal.owner, "bps": 10000}]
        return self.case("E11", deal, deal.owner, "corrected", [], f"also credited 100% to {other}")

    def plant_E12(self):
        """Contract amendment raises ACV mid-term."""
        deal = self.pick(lambda d: d.term > 12 and d.booked_at[:10] <= "2026-03-20")
        increase = round(deal.acv_cents * self.rng.uniform(0.2, 0.5) / 100_000) * 100_000
        at = f"2026-05-{self.rng.randint(12, 22):02d}T15:00:00Z"
        deal.amendment = {"acv_cents": deal.acv_cents + increase, "signed_at": at}
        self.events.append((at, "contract.amended", {"deal_id": deal.id, "signed_at": at,
                                                     "terms": {"acv_cents": deal.acv_cents + increase,
                                                               "kind": "amendment"}}))
        return self.case("E12", deal, deal.owner, "posted", [], f"ACV +{increase} on {at[:10]}",
                         amendment_key=f"{deal.id}+v2")

    def plant_E13(self):
        """Duplicate payment webhook."""
        deal = self.pick(lambda d: d.paid_at is not None)
        self.events.append((clk.add_minutes(deal.paid_at, 2), "payment.received",
                            {"stripe_id": f"py_{deal.id}", "deal_id": deal.id, "amount_cents": deal.acv_cents,
                             "date": deal.paid_at[:10]}))
        return self.case("E13", deal, deal.owner, "posted", [], "payment webhook delivered twice")

    def plant_E14(self):
        """Plan YAML draft differs from the signed plan."""
        rep = self.take_rep(lambda r: any(d.owner == r.id and d.booked_at[:10] >= "2026-04-06" and len(d.splits) == 1
                                          for d in self.deals))
        mine = [d for d in self.deals if d.owner == rep.id and d.booked_at[:10] >= "2026-04-06" and len(d.splits) == 1]
        case_deal = max(mine, key=lambda d: d.booked_at)
        self.used_deals.add(case_deal.id)
        changes = {"rates": [{"tier": "base", "up_to_attainment": 1.0, "rate": 0.10},
                             {"tier": "accelerator", "above_attainment": 1.0, "rate": 0.20}]}
        self.plan_drafts[rep.id] = {"version": 4, "changes": changes}
        self.events.append(("2026-04-03T10:00:00Z", "plan.amended",
                            {"rep_id": rep.id, "version": 4, "signed_at": "", "changes": changes}))
        return self.case("E14", case_deal, rep.id, "escalated", ["marcus.lee"], "unsigned v4 raises the accelerator",
                         scope="rep")

    def plant_E15(self):
        """Rep paid on a deal later voided."""
        deal = self.pick(lambda d: d.term > 12 and "2026-01-05" <= d.booked_at[:10] <= "2026-02-20")
        deal.paid_at = _ts(date.fromisoformat(deal.invoice_at[:10]) + timedelta(days=12), 60)
        at = f"2026-04-{self.rng.randint(14, 28):02d}T11:00:00Z"
        self.events.append((at, "deal.updated", {"deal_id": deal.id, "fields": {"stage": "voided"}, "actor": "crm.ops"}))
        return self.case("E15", deal, deal.owner, "corrected", ["priya.nair"], f"voided {at[:10]} after payout")

    def plant_E16(self):
        """Payroll export differs from the approved lines by $340 (May's release, then June's)."""
        period = next(p for p in ("2026-05", "2026-06") if p not in self.payroll_edits)
        booked_in = clk.add_periods(period, -1)
        deal = self.pick(self._in(f"{booked_in}-01", f"{booked_in}-22"))
        deal.paid_at = _ts(date.fromisoformat(deal.invoice_at[:10]) + timedelta(days=10), 60)
        self.payroll_edits[period] = {"deal_id": deal.id, "cents": 34_000}
        return self.case("E16", deal, deal.owner, "blocked", ["dana.ortiz"], f"{period} export +$340 on this line")

    # ---------- the answer key ----------

    def plant_all(self) -> list[dict]:
        if len(self.reps) < 16:
            raise ValueError("planting 40 cases needs at least 16 reps")
        for error, count in PLANTS:
            for _ in range(count):
                getattr(self, f"plant_{error}")()
        self.fill_expected()
        return self.answer_key

    def truth_rows(self, rep_id: str) -> dict:
        """The facts as they truly end up (from the generator, not from Quota's ledger), shaped like DB rows."""
        rows = {"deals": [], "contracts": [], "esign_events": [], "plans": [], "precedents": []}
        for d in self.deals:
            if rep_id not in self.deal_reps(d):
                continue
            rows["deals"].append({"id": d.id, "version": 1, "account": d.account, "crm_amount_cents": d.crm_amount_cents,
                                  "crm_close_date": d.crm_close_date, "stage": "closed_won", "owner_id": d.owner,
                                  "splits_json": json.dumps(d.splits), "known_at": d.booked_at, "source": "truth"})
            contract = {"id": d.id, "version": 1, "deal_id": d.id, "acv_cents": d.acv_cents, "term_months": d.term,
                        "contract_date": d.booked_at[:10], "countersigned_at": d.booked_at,
                        "clauses_json": json.dumps({"billing": "annual_upfront"}), "doc_ref": "truth",
                        "known_at": d.booked_at, "source": "truth"}
            rows["contracts"].append(contract)
            if d.amendment:
                rows["contracts"].append({**contract, "version": 2, "acv_cents": d.amendment["acv_cents"],
                                          "clauses_json": json.dumps({"billing": "annual_upfront", "amendment": {
                                              "kind": "amendment", "signed_at": d.amendment["signed_at"]}})})
            rows["esign_events"].append({"id": f"ENV-{d.id}-countersigned", "envelope_id": f"ENV-{d.id}",
                                         "deal_id": d.id, "event": "countersigned", "at": d.booked_at,
                                         "known_at": d.booked_at})
        template = load_plan_raw(seed_acme.PLAN_PATH)
        plan = {"id": f"AE-2026-H1-{rep_id}", "version": 3, "rep_id": rep_id, "effective_from": "2026-01-01",
                "effective_to": "2026-06-30", "config_json": json.dumps({**template, "rep": rep_id}),
                "signed_doc_ref": "truth", "signed_at": template["signed_at"], "known_at": "2025-12-18T17:00:00Z"}
        rows["plans"].append(plan)
        if rep_id in self.plan_drafts:
            draft = self.plan_drafts[rep_id]
            rows["plans"].append({**plan, "version": draft["version"], "signed_at": None,
                                  "config_json": json.dumps({**template, "rep": rep_id, **draft["changes"]})})
        return rows

    def fill_expected(self) -> None:
        from quota.engine.calculator import evaluate
        end = f"{self.end}T23:59:59Z"
        for case in self.answer_key:
            if case["type"] in NOTHING_BOOKED:
                earned = 0
            else:
                rows = self.truth_rows(case["rep_id"])
                earned = evaluate(rows, case["deal_id"], case["rep_id"], end).amount_cents
                if case["type"] == "E12":
                    earned += evaluate(rows, case["amendment_key"], case["rep_id"], end).amount_cents
            case["expected_amount_cents"] = 0 if case["type"] in RECOVERED else earned
            case["max_payout_cents"] = 0 if case["type"] in NEVER_PAID else earned

    # ---------- events ----------

    def deal_events(self, deal: Deal) -> list[tuple[str, str, dict]]:
        fields = {"account": deal.account, "crm_amount_cents": deal.crm_amount_cents, "crm_close_date": None,
                  "stage": "open", "owner_id": deal.owner, "splits": deal.splits, **(deal.created_as or {})}
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
            run = (f"{last}T20:00:00Z", "job.due", {"job": "commission_run", "period": period, "since": self.start[:7]})
            out += [run, job("20:30", "commission_accrual"),
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
                            "file": f"PR-{period}", "export_adjust": self.payroll_edits.get(period)})
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
             db_path=None, out=None, plant: bool = True) -> World:
    """A seeded Fernhill World with its facts and events loaded, ready for `world.run_until(world.end_ts)`.
    With `plant`, errors E01-E16 are planted (40 cases) and the answer key is written to out_dir/answer_key.json
    when out_dir is given."""
    fernhill = Fernhill(seed, reps, start, end)
    if plant:
        fernhill.plant_all()
    world = fernhill.build(db_path, out)
    if out_dir is not None:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / "answer_key.json").write_text(json.dumps(fernhill.answer_key, indent=2), encoding="utf-8")
    return world
