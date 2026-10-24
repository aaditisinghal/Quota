"""The simulated company: one ledger, one clock, the event queue, the billing system, the seats and the people.

`run_until` advances the clock from one due item to the next (a scheduled event, a person's action, a timer)
and lets everyone settle at each instant: released events go to their queues, people act, then the billing
system and each seat drain their queues, repeated until nothing moves.
"""

from pathlib import Path
from uuid import uuid4

from quota import clock as clk
from quota import config
from quota.agent.personas import PeopleAPI
from quota.engine import entries
from quota.ledger import db, queries
from quota.ledger.write_path import WritePath
from quota.sim.clock_runner import ClockRunner
from quota.timeline import entry_text, format_line


class BillingSystem:
    """system:billing, the simulated ERP: mirrors invoices, payments and refunds and recognizes revenue."""

    principal = "system:billing"
    handles = frozenset({"invoice.issued", "payment.received", "refund.issued", "job.due"})
    KINDS = {"invoice.issued": ("invoice", entries.invoice_entry), "payment.received": ("payment", entries.payment_entry),
             "refund.issued": ("refund", entries.refund_entry)}

    def __init__(self, world):
        self.world = world

    def _post(self, entry, source_ref) -> str:
        wp = self.world.wp
        je_id = wp.draft_entry(self.principal, entry["effective_date"], entry["memo"], entry["lines"],
                               source_ref=source_ref)
        wp.post_entry(self.principal, je_id)
        return je_id

    def run_once(self):
        ev = self.world.queue.next_for(self.principal, self.handles)
        if ev is None:
            return None
        status = "handled"
        if ev.type in self.KINDS:
            self._mirror(ev)
        elif ev.payload["job"] == "revenue_recognition":
            self._recognize(ev.payload["period"])
        else:
            status = "ignored"
        self.world.wp.mark_event(self.principal, ev.id, status)
        return []

    def _mirror(self, ev):
        p = ev.payload
        if queries.as_of(self.world.ro, "billing", p["stripe_id"], None) is not None:
            return                                   # the same webhook twice counts once
        kind, builder = self.KINDS[ev.type]
        self.world.wp.append_fact("billing", {"id": p["stripe_id"], "kind": kind, "deal_id": p["deal_id"],
                                              "amount_cents": p["amount_cents"], "at": self.world.clock.now(),
                                              "external_ref": p["stripe_id"]}, self.principal)
        entry = builder(p["deal_id"], p["amount_cents"], p["date"])
        je_id = self._post(entry, p["stripe_id"])
        self.world.emit(self.principal, "ENTRY", f"{je_id} posted {kind} {p['deal_id']} ({p['stripe_id']}): "
                                                 f"{entry_text(entry['lines'])}")

    def _recognize(self, period):
        """ACV / 12 per invoiced deal for each month its contract is live (through its churn date)."""
        ro, now = self.world.ro, self.world.clock.now()
        invoiced = sorted({b["deal_id"] for b in queries.fetch_all(
            ro, "SELECT deal_id FROM billing WHERE kind = 'invoice' AND known_at <= ?", (now,))})
        churned = {}
        for event in queries.fetch_all(ro, "SELECT payload_json FROM events WHERE type = 'customer.churned' "
                                           "AND release_at <= ?", (now,)):
            payload = queries.loads(event["payload_json"])
            churned[payload["deal_id"]] = payload["effective_date"]
        posted = {e["source_ref"] for e in queries.fetch_all(ro, "SELECT source_ref FROM journal_entries")}
        for deal_id in invoiced:
            found = queries.fetch_one(ro, "SELECT id FROM contracts WHERE deal_id = ? AND known_at <= ? "
                                          "ORDER BY version DESC LIMIT 1", (deal_id, now))
            contract = queries.as_of(ro, "contracts", found["id"], now) if found else None
            source_ref = f"revenue:{deal_id}:{period}"
            if (contract is None or source_ref in posted or clk.period_of(contract["contract_date"]) > period
                    or churned.get(deal_id, "9999") < f"{period}-01"):
                continue
            entry = entries.revenue_entry(deal_id, contract["acv_cents"] // 12, clk.period_end(period))
            je_id = self._post(entry, source_ref)
            self.world.emit(self.principal, "ENTRY", f"{je_id} posted revenue {deal_id} {period}: "
                                                     f"{entry_text(entry['lines'])}")


class World:
    def __init__(self, start: str, db_path=None, out=print):
        if db_path and Path(db_path).exists():
            raise FileExistsError(f"{db_path} already exists; the ledger is append-only, pick a new path")
        self.uri = db_path or db.memory_uri(f"quota-world-{uuid4().hex}")
        self.conn = db.connect(self.uri)
        db.init_db(self.conn)
        self.ro = db.connect(self.uri, readonly=True)
        self.clock = clk.SimClock(start)
        self.wp = WritePath(self.conn, self.clock, config.principals(with_seed=True))
        self.out = out
        self.lines: list[str] = []
        self.queue = ClockRunner(self.ro, self.wp, self.clock, self.emit)
        self.api = PeopleAPI(self)
        self.billing = BillingSystem(self)
        self.seats = []          # drained in this order at every instant
        self.people = None

    def emit(self, actor, kind, text):
        line = format_line(self.clock.now(), actor, kind, text)
        self.lines.append(line)
        self.out(line)

    def say(self, text=""):
        self.lines.append(text)
        self.out(text)

    def settle(self) -> None:
        while True:
            progressed = bool(self.queue.release_due())
            if self.people is not None:
                progressed = self.people.act() or progressed
            for actor in (self.billing, *self.seats):
                while actor.run_once() is not None:
                    progressed = True
            if not progressed:
                return

    def next_time(self) -> str | None:
        times = [t for t in (self.queue.next_time(), self.people.next_time() if self.people else None) if t]
        return min(times) if times else None

    def run_until(self, until: str) -> None:
        while True:
            self.settle()
            upcoming = self.next_time()
            if upcoming is None or upcoming > until:
                break
            self.clock.advance_to(upcoming)
        self.clock.advance_to(until)
        self.settle()

    def close(self):
        self.ro.close()
        self.conn.close()
