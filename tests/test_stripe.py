"""--stripe, offline: a fake client that enforces what the sandbox probes showed (3 customers per test clock,
clocks only move forward, idempotency keys replay the same object)."""

import itertools
import json
from types import SimpleNamespace as NS

import pytest

from quota.ledger import queries
from quota.sim import demo_acme
from quota.sim.generator import Fernhill
from quota.sim.stripe_billing import StripeSandbox, StripeSandboxError

from test_demo_acme import EXPECTED_JES


class FakeStripe:
    def __init__(self):
        self.ids = itertools.count(1)
        self.clocks, self.customers, self.invoices, self.calls, self.by_key = {}, {}, {}, [], {}
        v1 = NS()
        v1.test_helpers = NS(test_clocks=NS(create=self.clock_create, advance=self.clock_advance,
                                            retrieve=lambda cid: NS(status="ready")))
        v1.customers = NS(create=self.customer_create)
        v1.payment_methods = NS(list=lambda params: NS(data=[NS(id=f"pm_{params['customer']}")]))
        v1.invoices = NS(create=self.invoice_create, finalize_invoice=self.record("finalize"), pay=self.invoice_pay)
        v1.invoice_items = NS(create=self.record("invoice_item"))
        v1.invoice_payments = NS(list=lambda params: NS(data=[NS(payment=NS(
            payment_intent=self.invoices[params["invoice"]]["intent"]))]))
        v1.refunds = NS(create=self.refund_create)
        self.v1 = v1

    def _once(self, options, make):
        key = (options or {}).get("idempotency_key")
        if key and key in self.by_key:
            return self.by_key[key]
        made = make()
        if key:
            self.by_key[key] = made
        return made

    def record(self, name):
        def call(*args, params=None, options=None):
            self.calls.append((name, args, params, options))
            return NS(id=f"{name}_{next(self.ids)}")
        return call

    def clock_create(self, params, options=None):
        def make():
            cid = f"clock_{next(self.ids)}"
            self.clocks[cid] = {"time": params["frozen_time"], "customers": 0}
            return NS(id=cid)
        return self._once(options, make)

    def clock_advance(self, cid, params):
        assert params["frozen_time"] > self.clocks[cid]["time"], "test clocks only move forward"
        self.clocks[cid]["time"] = params["frozen_time"]
        self.calls.append(("advance", cid, params["frozen_time"]))

    def customer_create(self, params, options=None):
        clock = self.clocks[params["test_clock"]]
        assert clock["customers"] < 3, "a test clock holds at most 3 customers"
        clock["customers"] += 1
        cust = NS(id=f"cus_{next(self.ids)}")
        self.customers[cust.id] = params
        return cust

    def invoice_create(self, params, options=None):
        def make():
            inv = NS(id=f"in_1Fake{next(self.ids):05d}")
            self.invoices[inv.id] = {"customer": params["customer"], "paid": False, "intent": None}
            return inv
        return self._once(options, make)

    def invoice_pay(self, invoice_id, params, options=None):
        invoice = self.invoices[invoice_id]
        assert not invoice["paid"], "an invoice is paid once"
        invoice.update(paid=True, intent=f"pi_Fake{next(self.ids):05d}")
        self.calls.append(("pay", invoice_id, params["payment_method"]))

    def refund_create(self, params, options=None):
        self.calls.append(("refund", params["payment_intent"], params["amount"]))
        return NS(id=f"re_Fake{next(self.ids):05d}")


def sandbox(fake, tag="test-run"):
    return StripeSandbox("sk_test_fake", tag, client=fake)


def test_only_sandbox_keys_are_accepted():
    with pytest.raises(StripeSandboxError):
        StripeSandbox("sk_live_123", "run", client=FakeStripe())
    StripeSandbox("rk_test_123", "run", client=FakeStripe())


def test_a_missing_key_is_a_clear_error(monkeypatch):
    import quota.env
    monkeypatch.setattr(quota.env, "load_env", lambda *a, **k: None)
    monkeypatch.delenv("STRIPE_API_KEY", raising=False)
    with pytest.raises(StripeSandboxError, match="STRIPE_API_KEY"):
        StripeSandbox.from_env("run")


def test_acme_demo_with_stripe_keeps_the_same_books():
    fake = FakeStripe()
    demo = demo_acme.run(out=lambda line: None, stripe=sandbox(fake))
    try:
        journal = {e["id"]: [(l["account"], l["debit_cents"], l["credit_cents"]) for l in e["lines"]]
                   for e in queries.journal(demo.ro)}
        assert journal == EXPECTED_JES
        refs = {b["kind"]: b["external_ref"] for b in queries.fetch_all(demo.ro, "SELECT * FROM billing")}
        assert refs["invoice"].startswith("in_1Fake") and refs["payment"].startswith("pi_Fake")
        assert refs["refund"].startswith("re_Fake")
        assert ("refund", refs["payment"], 9_000_000) in fake.calls
        times = [c[2] for c in fake.calls if c[0] == "advance"]
        assert times == sorted(times) and len(times) == 2           # to the payment date, then the refund date
        assert any("Stripe in_1Fake" in line for line in demo.lines)
        assert demo.ok
    finally:
        demo.close()


def test_generated_world_respects_stripe_limits_and_counts_a_duplicate_payment_once():
    fernhill = Fernhill(5, reps=4)
    paid = next(d for d in fernhill.deals if d.paid_at)
    fernhill.events.append((paid.paid_at.replace(":00Z", ":30Z"), "payment.received",
                            {"stripe_id": f"py_{paid.id}", "deal_id": paid.id, "amount_cents": paid.acv_cents,
                             "date": paid.paid_at[:10]}))
    fake = FakeStripe()
    box = sandbox(fake, "gen-run")
    world = fernhill.build(stripe=box)
    world.run_until(world.end_ts)
    try:
        invoices = [d for d in fernhill.deals if d.invoice_at <= world.end_ts]
        assert box.created["invoices"] == len(invoices) == len(fake.invoices)
        assert box.created["payments"] == sum(1 for d in fernhill.deals if d.paid_at)   # the duplicate never paid twice
        assert box.created["clocks"] == len(fake.clocks) >= len(invoices) / 3
        assert all(c["customers"] <= 3 for c in fake.clocks.values())
        mirrored = queries.fetch_all(world.ro, "SELECT * FROM billing WHERE kind = 'payment'")
        assert len(mirrored) == box.created["payments"] and all(m["external_ref"].startswith("pi_") for m in mirrored)
        keys = [k for k in fake.by_key]
        assert keys and all(k.startswith("gen-run-") for k in keys)
    finally:
        world.close()
