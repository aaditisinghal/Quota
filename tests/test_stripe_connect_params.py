"""Stripe Connect requests that newer sandboxes accept (found filling a fresh sandbox)."""

from types import SimpleNamespace as NS

from quota.integrations import stripe_connect


class FakeAccounts:
    def __init__(self, refuse_recipient):
        self.refuse_recipient, self.calls = refuse_recipient, []

    def create(self, params):
        self.calls.append(params["tos_acceptance"]["service_agreement"])
        if self.refuse_recipient and params["tos_acceptance"]["service_agreement"] == "recipient":
            raise RuntimeError("The recipient ToS agreement is not supported for platforms in US creating accounts "
                               "in US.")
        return NS(id="acct_fake")


PERSON = {"workEmail": "ana@example.com", "key": "ana", "department": "Partners", "firstName": "Ana",
          "lastName": "Ruiz", "displayName": "Ana Ruiz"}


def test_a_us_platform_falls_back_to_the_full_service_agreement(monkeypatch):
    monkeypatch.setattr(stripe_connect, "_individual", lambda p: {})
    monkeypatch.setattr(stripe_connect, "_business_profile", lambda p: {})
    old = FakeAccounts(refuse_recipient=False)
    stripe_connect._create_account(NS(accounts=old), PERSON)
    assert old.calls == ["recipient"]                          # a sandbox that allows it keeps the recipient one
    new = FakeAccounts(refuse_recipient=True)
    assert stripe_connect._create_account(NS(accounts=new), PERSON).id == "acct_fake"
    assert new.calls == ["recipient", "full"]


def test_the_orbit_payment_uses_automatic_payment_methods():
    sent = {}

    def create(params):
        sent.update(params)
        return NS(id="pi_fake", status="succeeded")
    v1 = NS(payment_intents=NS(list=lambda params: NS(auto_paging_iter=lambda: iter([])), create=create))
    intent, created = stripe_connect._payment(v1, NS(id="cus_fake"), {"quota_deal": "orbit"})
    assert created and "payment_method_types" not in sent
    assert sent["automatic_payment_methods"] == {"enabled": True, "allow_redirects": "never"}
