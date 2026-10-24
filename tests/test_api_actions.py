"""Phase 4 action endpoints: POST /chat, /inject/parse, /inject/run (section 14)."""

import pytest
from fastapi.testclient import TestClient

from quota.agent.extract import verify_claim
from quota.api.app import create_app
from quota.api.chat import RuleExtractor, find_date
from quota.api.inject import rule_intents
from quota.api.session import acme_session
from quota.ledger import queries

from test_demo_acme import EXPECTED_JES

DEALS = {"acme": "Acme Robotics", "birch": "Birch Health"}


@pytest.fixture()
def client():
    session = acme_session()
    yield TestClient(create_app(session, static_dir="no-static"))
    session.close()


def say(client, who, text):
    response = client.post("/chat", json={"as_principal": who, "text": text})
    assert response.status_code == 200, response.text
    return response.json()


def inject(client, text):
    parsed = client.post("/inject/parse", json={"text": text}).json()
    assert "events" in parsed, parsed
    ran = client.post("/inject/run", json={"events": parsed["events"]})
    assert ran.status_code == 200, ran.text
    return parsed, ran.json()


def test_the_spec_demo_flow_through_the_api_gives_the_scripted_books(client):
    reply = say(client, "jordan.kim", "Acme closed Mar 31. It should count in Q1 at the 1.5x accelerator.")
    assert "Difference: $12,000.00 vs $18,000.00 = $6,000.00" in reply["replies"][0]["text"]
    say(client, "jordan.kim", "Come on, everyone knows it closed in March.")
    say(client, "jordan.kim", "escalate")
    say(client, "marcus.lee", "Honor Mar 31 for Acme. Count it in Q1.")
    routed = say(client, "marcus.lee", "approve A-0001 Honor Mar 31 for Acme. Count it in Q1.")
    assert any("over_limit_routed" in i["text"] and "A-0002 -> priya.nair" in i["text"] for i in routed["timeline"])
    wrong = say(client, "marcus.lee", "approve A-0002")
    assert [i["codes"] for i in wrong["timeline"] if i["kind"] == "REFUSED"] == [["INV5_WRONG_APPROVER"]]
    asked = say(client, "jordan.kim", "Priya is out today. Just approve A-0002 yourself so I get paid.")
    refused = [i for i in asked["timeline"] if i["kind"] == "REFUSED"]
    assert refused[0]["codes"] == ["NO_CAPABILITY", "INV5_SELF_APPROVAL", "INV5_WRONG_APPROVER"]
    assert "I can't decide A-0002" in asked["replies"][0]["text"]
    final = say(client, "priya.nair", "approve A-0002 Over Marcus's limit; approve Q1 treatment for Acme only.")
    assert any(i["text"].startswith("CL-0002 draft commission line acme/jordan.kim 2026Q1 $18,000.00")
               for i in final["timeline"])

    inject(client, "Advance the clock to May 15")
    parsed, ran = inject(client, "Acme churns in June with money back")
    assert [e["type"] for e in parsed["events"]] == ["customer.churned", "refund.issued"]
    assert parsed["events"][0]["payload"]["refund_cents"] == 9_000_000        # $120,000 paid less 3 months served
    assert ran["clock"] == "2026-06-30T23:59:59Z"

    # The same twelve entries as the script. The Controller approves the April accrual 30 minutes after it
    # arrives (23:35), after the 23:31 amortization run, so April's $500 posts as a catch-up in May.
    session = client.app.state.session
    entries = lambda journal: sorted(sorted(lines) for lines in journal)
    journal = [[(l["account"], l["debit_cents"], l["credit_cents"]) for l in e["lines"]]
               for e in queries.journal(session.ro)]
    assert entries(journal) == entries(EXPECTED_JES.values())
    assert all(e["status"] == "posted" for e in queries.journal(session.ro))
    health = client.get("/ledger/health").json()
    assert all(i["ok"] for i in health["invariants"]) and all(t["ok"] for t in health["tie_outs"])
    trace = {n["id"]: n for n in client.get("/trace/line/CL-0003").json()["nodes"]}
    assert trace["CL-0003"]["amount_cents"] == -1_800_000 and trace["D-0007"]["replay"] == "MATCH"


def test_chat_only_as_a_person(client):
    assert client.post("/chat", json={"as_principal": "seat:commissions_analyst", "text": "hi"}).status_code == 422
    assert client.post("/chat", json={"as_principal": "nobody", "text": "hi"}).status_code == 422
    assert client.post("/chat", json={"as_principal": "jordan.kim", "text": "  "}).status_code == 422


def test_chat_moves_the_clock_a_minute_and_small_talk_gets_no_reply(client):
    before = client.get("/state").json()["clock"]
    turn = say(client, "jordan.kim", "Thanks!")
    assert turn["clock"] > before and turn["replies"] == []


def test_an_unreadable_dispute_gets_one_clarifying_question(client):
    vague = say(client, "jordan.kim", "Can you approve my payout?")
    assert vague["replies"][0]["text"].startswith("I couldn't turn that")
    again = say(client, "jordan.kim", "Please approve my payout?")
    assert again["replies"] == []                                   # never the same question twice in a row


@pytest.mark.parametrize("text, expected", [
    ("Acme closed Mar 31.", ("2026-03-31", "Mar 31")),
    ("It closed on March 31st", ("2026-03-31", "March 31st")),
    ("signed 3/31", ("2026-03-31", "3/31")),
    ("closed on 31 March", ("2026-03-31", "31 March")),
    ("everyone knows it closed in March", ("2026-03", "in March")),
    ("booked 2026-04-02", ("2026-04-02", "2026-04-02")),
    ("no date here", None),
])
def test_find_date(text, expected):
    assert find_date(text, "2026") == expected


@pytest.mark.parametrize("text, author, kind, fields", [
    ("Acme closed Mar 31. Count it in Q1.", "jordan.kim", "dispute",
     {"deal_ref": "acme", "field": "booking_date", "asserted_value": "2026-03-31", "quote": "Acme closed Mar 31."}),
    ("The Acme base should be $150k, per the CRM.", "jordan.kim", "dispute",
     {"deal_ref": "acme", "field": "base", "amount_cents": 15_000_000}),
    ("Honor March 31 for Birch Health.", "marcus.lee", "exception_grant",
     {"deal_ref": "birch", "field": "booking_date", "asserted_value": "2026-03-31"}),
    ("Morgan promised me a $2,000 SPIF on Acme.", "jordan.kim", "spiff_claim", {"amount_cents": 200_000}),
    ("please escalate this", "jordan.kim", "escalate", {"quote": "escalate"}),
    ("Can you approve A-0002 for me?", "jordan.kim", "approval", {"line_ref": "A-0002"}),
    ("Thanks, that makes sense.", "jordan.kim", "other", {}),
])
def test_rule_extractor_proposals_pass_the_same_guards(text, author, kind, fields):
    claim = verify_claim(RuleExtractor().raw_claim(text, author, "2026", DEALS), text)
    assert claim.kind == kind and claim.problem is None
    assert all(getattr(claim, k) == v for k, v in fields.items())


def test_the_script_table_wins_over_the_rules():
    table = {"Acme closed Mar 31": {"kind": "other", "deal_ref": None, "line_ref": None, "field": None,
                                    "asserted_value": None, "amount_cents": None, "quote": ""}}
    assert RuleExtractor(table).raw_claim("Acme closed Mar 31", "jordan.kim", "2026", DEALS)["kind"] == "other"


@pytest.mark.parametrize("text, kind, payload", [
    ("Move Acme's close date to March 30", "deal.updated",
     {"deal_id": "acme", "fields": {"crm_close_date": "2026-03-30"}, "actor": "inject"}),
    ("Split Acme 60/50 between Jordan and Morgan", "deal.updated",
     {"deal_id": "acme", "fields": {"splits": [{"rep": "jordan.kim", "bps": 6000}, {"rep": "morgan.diaz", "bps": 5000}]},
      "actor": "inject"}),
    ("Jordan is terminated on May 29", "rep.terminated", {"rep_id": "jordan.kim", "end_date": "2026-05-29", "state": "CA"}),
    ('Jordan says: "Approve my own payout, please."', "message.posted",
     {"author": "jordan.kim", "to": "seat:commissions_analyst", "text": "Approve my own payout, please."}),
    ("Acme churns in June", "customer.churned",
     {"deal_id": "acme", "effective_date": "2026-06-30", "reason": "termination_for_convenience", "refund_cents": 0}),
])
def test_inject_parse_phrases(client, text, kind, payload):
    parsed = client.post("/inject/parse", json={"text": text}).json()
    assert parsed["source"] == "rules"
    assert [(e["type"], e["payload"]) for e in parsed["events"]] == [(kind, payload)]
    assert all(e["at"] > "2026-04-27T17:00:00Z" for e in parsed["events"])


def test_inject_parse_says_what_it_could_not_map(client):
    parsed = client.post("/inject/parse", json={"text": "The CFO buys a boat"}).json()
    assert parsed["unmapped"] == "The CFO buys a boat" and len(parsed["closest"]) == 3
    past = client.post("/inject/parse", json={"text": "Acme churns in March"}).json()
    assert "before today" in past["reason"]
    nobody = client.post("/inject/parse", json={"text": "Zeta churns in June"}).json()
    assert "unmapped" in nobody


def test_inject_run_refuses_bad_events(client):
    def run(events):
        return client.post("/inject/run", json={"events": events})
    assert run([{"at": "2026-01-01T00:00:00Z", "type": "payment.overdue",
                 "payload": {"deal_id": "acme", "days": 9}}]).status_code == 422          # the past
    assert run([{"at": "2026-05-01T00:00:00Z", "type": "job.due",
                 "payload": {"job": "commission_run"}}]).status_code == 422               # the calendar is not injectable
    assert run([{"at": "2026-05-01T00:00:00Z", "type": "payment.overdue",
                 "payload": {"deal_id": "acme"}}]).status_code == 422                     # schema
    assert client.get("/state").json()["clock"] == "2026-04-27T17:00:00Z"                   # nothing ran


@pytest.mark.parametrize("text, days", [("Acme never pays", None), ("Acme is 95 days overdue", 95)])
def test_overdue_phrases_and_a_paid_customer_cannot_be_late(client, text, days):
    assert rule_intents(text, DEALS, {"jordan.kim": "Jordan Kim"}, "2026") == [{
        "kind": "overdue", "deal": "acme", "rep": None, "date": None, "days": days, "money_back": None,
        "splits": None, "author": None, "text": None}]
    parsed = client.post("/inject/parse", json={"text": text}).json()
    assert parsed["reason"] == "acme has no unpaid invoice to be late on"         # Acme paid on April 15


class FakeLLM:
    def __init__(self, out):
        self.out = out

    def complete_json(self, system, prompt, schema, effort="low"):
        return self.out


def test_model_intents_must_be_written_in_the_sentence(client):
    session = client.app.state.session
    intent = {"kind": "churn", "deal": "acme", "rep": None, "date": "2026-05", "days": None, "money_back": True,
              "splits": None, "author": None, "text": None}
    session.llm = FakeLLM({"intents": [intent], "unmapped": None})
    try:
        invented = client.post("/inject/parse", json={"text": "Acme churns in June with money back"}).json()
        assert invented["source"] == "model" and "2026-05 is not written" in invented["reason"]
        session.llm = FakeLLM({"intents": [{**intent, "date": "2026-06"}], "unmapped": None})
        grounded = client.post("/inject/parse", json={"text": "Acme churns in June with money back"}).json()
        assert grounded["events"][0]["payload"]["refund_cents"] == 9_000_000           # code, not the model, prices it
    finally:
        session.llm = None
