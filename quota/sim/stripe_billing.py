"""Billing in a Stripe sandbox (`--stripe`).

The simulated company's invoices, payments and refunds become real Stripe test-mode objects: one customer per
deal, an invoice for its ACV, a card payment on the payment date, a refund against that payment. Stripe test
clocks follow the simulation's calendar (a clock holds at most 3 customers and only moves forward, so customers
share "lanes" of clocks that advance as simulated time does). The billing system mirrors each object into
`billing` with the Stripe id as `external_ref`. Only sandbox / test keys are accepted.
"""

import os
import time
from dataclasses import dataclass, field

from quota.clock import parse_ts

CUSTOMERS_PER_CLOCK = 3          # Stripe's limit
TEST_CARD = "pm_card_visa"


class StripeSandboxError(RuntimeError):
    pass


@dataclass
class Lane:
    clock_id: str
    frozen_time: int
    customers: int = 0


@dataclass
class Account:
    customer_id: str
    lane: Lane
    payment_method_id: str
    invoice_id: str | None = None
    payment_intent_id: str | None = None


@dataclass
class StripeSandbox:
    api_key: str
    run_tag: str                                  # labels this run's objects and scopes its idempotency keys
    client: object = None
    lanes: list[Lane] = field(default_factory=list)
    accounts: dict[str, Account] = field(default_factory=dict)
    created: dict[str, int] = field(default_factory=lambda: {"clocks": 0, "customers": 0, "invoices": 0,
                                                             "payments": 0, "refunds": 0})

    def __post_init__(self):
        if not self.api_key.startswith(("sk_test_", "rk_test_")):
            raise StripeSandboxError("--stripe only runs against a sandbox / test key (sk_test_ or rk_test_)")
        if self.client is None:
            import stripe
            self.client = stripe.StripeClient(self.api_key, max_network_retries=3)

    @classmethod
    def from_env(cls, run_tag: str) -> "StripeSandbox":
        from quota.env import load_env
        load_env()
        key = os.environ.get("STRIPE_API_KEY")
        if not key:
            raise StripeSandboxError("--stripe needs STRIPE_API_KEY (a sandbox key) in the environment or .env")
        return cls(key, run_tag)

    # ---------- plumbing ----------

    @property
    def v1(self):
        return self.client.v1

    def _key(self, *parts) -> dict:
        return {"idempotency_key": "-".join([self.run_tag, *parts])}

    def _meta(self, deal_id) -> dict:
        return {"quota_run": self.run_tag, "quota_deal": deal_id}

    def _advance(self, lane: Lane, at: int) -> None:
        if at <= lane.frozen_time:
            return
        self.v1.test_helpers.test_clocks.advance(lane.clock_id, params={"frozen_time": at})
        for _ in range(240):
            if self.v1.test_helpers.test_clocks.retrieve(lane.clock_id).status == "ready":
                break
            time.sleep(0.5)
        else:
            raise StripeSandboxError(f"test clock {lane.clock_id} did not finish advancing")
        lane.frozen_time = at

    def _lane(self, at: int) -> Lane:
        open_lanes = [l for l in self.lanes if l.customers < CUSTOMERS_PER_CLOCK and l.frozen_time <= at]
        if open_lanes:
            return open_lanes[0]
        clock = self.v1.test_helpers.test_clocks.create(
            params={"frozen_time": at, "name": f"{self.run_tag} lane {len(self.lanes) + 1}"},
            options=self._key("clock", str(len(self.lanes) + 1)))
        lane = Lane(clock.id, at)
        self.lanes.append(lane)
        self.created["clocks"] += 1
        return lane

    # ---------- what the billing system calls ----------

    def invoice(self, deal_id: str, account_name: str, amount_cents: int, ts: str) -> str:
        """Customer (on a clock lane), year-1 invoice for the ACV, finalized at the simulated time."""
        at = int(parse_ts(ts).timestamp())
        lane = self._lane(at)
        self._advance(lane, at)
        customer = self.v1.customers.create(
            params={"name": account_name, "test_clock": lane.clock_id, "payment_method": TEST_CARD,
                    "metadata": self._meta(deal_id)}, options=self._key(deal_id, "customer"))
        lane.customers += 1
        self.created["customers"] += 1
        card = self.v1.payment_methods.list(params={"customer": customer.id, "type": "card"}).data[0].id
        invoice = self.v1.invoices.create(
            params={"customer": customer.id, "collection_method": "charge_automatically", "auto_advance": False,
                    "metadata": self._meta(deal_id)}, options=self._key(deal_id, "invoice"))
        self.v1.invoice_items.create(
            params={"customer": customer.id, "invoice": invoice.id, "amount": amount_cents, "currency": "usd",
                    "description": f"{account_name}: year-1 subscription", "metadata": self._meta(deal_id)},
            options=self._key(deal_id, "invoice-item"))
        self.v1.invoices.finalize_invoice(invoice.id, params={"auto_advance": False},
                                          options=self._key(deal_id, "finalize"))
        self.created["invoices"] += 1
        self.accounts[deal_id] = Account(customer.id, lane, card, invoice.id)
        return invoice.id

    def pay(self, deal_id: str, ts: str) -> str:
        """The customer pays the invoice by card on the simulated payment date; returns the PaymentIntent id."""
        account = self._account(deal_id)
        self._advance(account.lane, int(parse_ts(ts).timestamp()))
        self.v1.invoices.pay(account.invoice_id, params={"payment_method": account.payment_method_id},
                             options=self._key(deal_id, "pay"))
        payments = self.v1.invoice_payments.list(params={"invoice": account.invoice_id})
        intent = payments.data[0].payment.payment_intent
        account.payment_intent_id = intent if isinstance(intent, str) else intent.id
        self.created["payments"] += 1
        return account.payment_intent_id

    def refund(self, deal_id: str, amount_cents: int, ts: str, ref: str) -> str:
        account = self._account(deal_id)
        if account.payment_intent_id is None:
            raise StripeSandboxError(f"{deal_id} has no Stripe payment to refund")
        self._advance(account.lane, int(parse_ts(ts).timestamp()))
        refund = self.v1.refunds.create(
            params={"payment_intent": account.payment_intent_id, "amount": amount_cents,
                    "metadata": self._meta(deal_id)}, options=self._key(deal_id, "refund", ref))
        self.created["refunds"] += 1
        return refund.id

    def _account(self, deal_id) -> Account:
        if deal_id not in self.accounts:
            raise StripeSandboxError(f"{deal_id} has no Stripe invoice in this run")
        return self.accounts[deal_id]
