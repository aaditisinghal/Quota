"""NEXT_TASKS.md R1-R5, as regressions."""

import pytest
from fastapi.testclient import TestClient

from quota.api.app import create_app
from quota.api.session import acme_session
from quota.ledger import hashing, invariants, queries
from quota.sim import demo_acme


def inject(client, text):
    parsed = client.post("/inject/parse", json={"text": text}).json()
    assert "events" in parsed, parsed
    ran = client.post("/inject/run", json={"events": parsed["events"]})
    assert ran.status_code == 200, ran.text
    return parsed["events"], ran.json()["timeline"]


def clean(ro):
    assert not any(invariants.check_all(ro).values()) and hashing.verify_chain(ro)[0]
    assert all(queries.replay_decision(ro, d["id"])[0] for d in queries.fetch_all(ro, "SELECT id FROM decisions"))


@pytest.fixture()
def plain():
    session = acme_session(cast=False)
    yield TestClient(create_app(session, static_dir="no-static"))
    session.close()


def clawback_entry(ro):
    entry = next(e for e in queries.journal(ro) if queries.memo_kind(e["memo"]) == "clawback")
    return {(l["account"], l["debit_cents"], l["credit_cents"]) for l in entry["lines"]}


# ---------- R2: commissions are advances until earned ----------

def test_r2_the_acme_demo_still_recovers_from_future_advances():
    demo = demo_acme.run(out=lambda line: None)
    try:
        cb = queries.fetch_one(demo.ro, "SELECT output_json FROM decisions WHERE kind = 'clawback'")
        assert queries.loads(cb["output_json"])["args"]["chargeback_code"] is None
        assert ("1290", 1_800_000, 0) in clawback_entry(demo.ro)
    finally:
        demo.close()


def test_r2_a_plan_without_future_advances_books_no_receivable(plain):
    session = plain.app.state.session
    plan = queries.fetch_one(session.ro, "SELECT * FROM plans WHERE rep_id = 'jordan.kim' ORDER BY version DESC")
    config_ = {k: v for k, v in queries.loads(plan["config_json"]).items() if k != "chargeback_method"}
    row = {k: v for k, v in plan.items() if k not in ("known_at",)}
    session.world.wp.append_fact("plans", {**row, "version": 4, "config_json": {**config_, "version": 4}}, "seed")
    inject(plain, "Acme churns in June and wants its money back")
    ro = session.ro
    assert all(account != "1290" for account, _, _ in clawback_entry(ro))
    cb = queries.fetch_one(ro, "SELECT * FROM decisions WHERE kind = 'clawback'")
    assert "CHARGEBACK_NOT_PERMITTED" in cb["explanation"]
    assert queries.fetch_one(ro, "SELECT 1 FROM messages WHERE to_principal = 'priya.nair' "
                                 "AND text LIKE 'CHARGEBACK_NOT_PERMITTED%'")
    assert queries.fetch_all(ro, "SELECT * FROM commission_lines WHERE amount_cents < 0") == []
    clean(ro)


def test_r2_a_rep_who_has_left_owes_nothing_back(plain):
    inject(plain, "Jordan is terminated on May 20")
    inject(plain, "Acme churns in June and wants its money back")
    ro = plain.app.state.session.ro
    assert all(account != "1290" for account, _, _ in clawback_entry(ro))
    cb = queries.fetch_one(ro, "SELECT * FROM decisions WHERE kind = 'clawback'")
    assert "CHARGEBACK_REP_TERMINATED" in cb["explanation"]
    assert queries.trial_balance(ro).get("1290", 0) == 0
    clean(ro)


def test_r2_account_1290_is_commission_advances_recoverable():
    from quota import config
    assert config.ACCOUNTS["1290"][0] == "Commission advances recoverable"
