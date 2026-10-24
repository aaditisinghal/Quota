"""--stripe against the real sandbox. Opt-in: set QUOTA_STRIPE_LIVE=1 (and STRIPE_API_KEY in the environment or
.env). It creates a test clock, a customer, an invoice, a payment and a refund in the sandbox."""

import os
from uuid import uuid4

import pytest

from quota.env import load_env
from quota.ledger import queries
from quota.sim import demo_acme
from quota.sim.stripe_billing import StripeSandbox

from test_demo_acme import EXPECTED_JES

pytestmark = pytest.mark.skipif(os.environ.get("QUOTA_STRIPE_LIVE") != "1",
                                reason="live Stripe sandbox test; set QUOTA_STRIPE_LIVE=1 to run")


def test_acme_demo_bills_through_the_stripe_sandbox():
    load_env()
    box = StripeSandbox.from_env(f"quota-livetest-{uuid4().hex[:8]}")
    demo = demo_acme.run(out=lambda line: None, stripe=box)
    try:
        refs = {b["kind"]: b["external_ref"] for b in queries.fetch_all(demo.ro, "SELECT * FROM billing")}
        assert refs["invoice"].startswith("in_1") and refs["payment"].startswith("pi_") and refs["refund"].startswith("re_")
        invoice = box.v1.invoices.retrieve(refs["invoice"])
        assert (invoice.status, invoice.amount_paid) == ("paid", 12_000_000)
        refund = box.v1.refunds.retrieve(refs["refund"])
        assert (refund.status, refund.amount) == ("succeeded", 9_000_000)
        journal = {e["id"]: [(l["account"], l["debit_cents"], l["credit_cents"]) for l in e["lines"]]
                   for e in queries.journal(demo.ro)}
        assert journal == EXPECTED_JES and demo.ok
    finally:
        demo.close()
