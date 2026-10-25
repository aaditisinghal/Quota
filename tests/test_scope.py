"""SPEC "Scope": what Quota produces for Sales Ops, Revenue Accounting and Payroll, and the escalation contract's
cases not covered elsewhere (#3 NO_RULE, #4 leave of absence, #5 integrity signals, #8 own checks fail)."""

import pytest
from fastapi.testclient import TestClient

from quota.api.app import create_app
from quota.api.session import Session, acme_session
from quota.ledger import hashing, invariants, queries
from quota.sim import demo_acme


def say(client, who, text):
    response = client.post("/chat", json={"as_principal": who, "text": text})
    assert response.status_code == 200, response.text
    return response.json()


def inject(client, text):
    parsed = client.post("/inject/parse", json={"text": text}).json()
    assert "events" in parsed, parsed
    ran = client.post("/inject/run", json={"events": parsed["events"]})
    assert ran.status_code == 200, ran.text
    return parsed["events"], ran.json()["timeline"]


def kinds(timeline, kind):
    return [i for i in timeline if i["kind"] == kind]


def clean(ro):
    assert not any(invariants.check_all(ro).values()) and hashing.verify_chain(ro)[0]
    assert all(queries.replay_decision(ro, d["id"])[0] for d in queries.fetch_all(ro, "SELECT id FROM decisions"))


@pytest.fixture()
def client():
    session = acme_session()
    yield TestClient(create_app(session, static_dir="no-static"))
    session.close()


@pytest.fixture()
def plain():
    session = acme_session(cast=False)
    yield TestClient(create_app(session, static_dir="no-static"))
    session.close()


# ---------- Sales Ops: rep statements and attainment by quarter ----------

def test_a_rep_statement_shows_attainment_and_lines_by_quarter():
    demo = demo_acme.run(out=lambda line: None)
    try:
        client = TestClient(create_app(Session(demo.world, "acme"), static_dir="no-static"))
        statement = client.get("/statement/jordan.kim?as_principal=jordan.kim").json()
        q1 = next(q for q in statement["quarters"] if q["quarter"] == "2026Q1")
        assert q1["attainment_bps"] == 10000                  # restated to 100% after the clawback, as the cascade says
        assert {l["id"]: l["status"] for l in q1["lines"]} == {"CL-0002": "clawed_back", "CL-0003": "approved"}
        assert client.get("/statement/jordan.kim?as_principal=priya.nair").status_code == 200
    finally:
        demo.close()


def test_a_rep_cannot_see_another_reps_statement(client):
    assert client.get("/statement/riley.shah?as_principal=jordan.kim").status_code == 403
    assert client.get("/statement/nobody").status_code == 404


def test_a_held_line_shows_its_hold_on_the_statement(client):
    inject(client, "Advance the clock to May 15")                                 # Taylor left May 8 before Vale paid
    statement = client.get("/statement/taylor.brooks?as_principal=taylor.brooks").json()
    line = statement["quarters"][0]["lines"][0]
    assert line["hold"] and "Taylor Brooks left 2026-05-08" in line["hold"]["reason"]
    assert statement["quarters"][0]["held_cents"] == 1_000_000


# ---------- Payroll: same-day final pay for a terminated rep ----------

def test_a_departing_rep_gets_a_same_day_final_pay_file(plain):
    inject(plain, "Advance the clock to May 1")                                   # CL-0001 approved on Apr 30
    events, timeline = inject(plain, "Jordan is terminated on May 5")
    created = [i["text"] for i in kinds(timeline, "PAYROLL")]
    assert any(t.startswith("PR-FINAL-jordan.kim-") and "final pay $12,000.00 (CL-0001)" in t for t in created)
    assert any("PR-FINAL-jordan.kim-" in t and t.endswith("released") for t in created)
    ro = plain.app.state.session.ro
    assert queries.line_status(ro, "CL-0001") == "paid"
    inject(plain, "Advance the clock to May 15")                                  # the regular run does not pay it twice
    may = queries.as_of(ro, "payroll_files", "PR-2026-05", None)
    assert may is None or "CL-0001" not in may["lines_json"]
    clean(ro)


def test_only_payroll_can_release_a_file_from_chat(plain):
    inject(plain, "Advance the clock to May 14")                                  # PR-2026-05 created and approved
    refused = say(plain, "jordan.kim", "release PR-2026-05")
    assert kinds(refused["timeline"], "REFUSED")[0]["text"].startswith("R-") and \
        "NO_CAPABILITY" in kinds(refused["timeline"], "REFUSED")[0]["text"]
    released = say(plain, "dana.ortiz", "release PR-2026-05")
    assert any(i["text"] == "PR-2026-05 released" for i in released["timeline"])


# ---------- escalation contract #3: NO_RULE ----------

def test_a_clause_no_rule_covers_is_held_and_the_plan_owner_asked(client):
    world = client.app.state.session.world
    world.queue.schedule("2026-04-27T18:00:00Z", "deal.created", {"deal_id": "quartz", "actor": "riley.shah", "fields": {
        "account": "Quartz Bio", "crm_amount_cents": 9_000_000, "crm_close_date": "2026-04-27", "stage": "closed_won",
        "owner_id": "riley.shah", "splits": [{"rep": "riley.shah", "bps": 10000}]}})
    world.queue.schedule("2026-04-28T18:00:00Z", "contract.countersigned", {
        "deal_id": "quartz", "timestamp": "2026-04-28T18:00:00Z", "acv_cents": 9_000_000, "term_months": 24,
        "clauses": {"billing": "annual_upfront", "ramp": {"year1_pct": 50}, "currency": "EUR"}})
    _, timeline = inject(client, "Advance the clock to May 1")
    decision = next(i for i in timeline if i["kind"] == "DECISION" and "no_rule_check quartz" in i["text"])
    assert "NO_RULE" in decision["text"] and "ramp" in decision["text"] and "currency EUR" in decision["text"]
    assert any("to marcus.lee: NO_RULE on Quartz Bio" in i["text"] for i in kinds(timeline, "MESSAGE"))
    ro = client.app.state.session.ro
    assert queries.fetch_all(ro, "SELECT * FROM commission_lines WHERE deal_id = 'quartz'") == []
    clean(ro)


# ---------- escalation contract #4: leave of absence ----------

def test_a_leave_of_absence_goes_to_the_controller_and_changes_nothing(client):
    events, timeline = inject(client, "Jordan goes on leave on May 4")
    assert events[0]["type"] == "rep.leave" and events[0]["payload"]["rep_id"] == "jordan.kim"
    decision = kinds(timeline, "DECISION")[0]["text"]
    assert "leave_review jordan.kim" in decision and "nothing is changed" in decision
    assert any("to priya.nair: Leave of absence" in i["text"] for i in kinds(timeline, "MESSAGE"))
    assert queries.line_status(client.app.state.session.ro, "CL-0001") == "draft"
    clean(client.app.state.session.ro)


def test_an_approver_on_leave_is_away_not_a_leave_of_absence(client):
    events, _ = inject(client, "The Controller is on leave until May 6")
    assert events[0]["type"] == "message.posted" and events[0]["payload"]["away_until"]


# ---------- escalation contract #5: integrity signals ----------

def _grant_twice(client):
    for who, text in [("jordan.kim", "Acme closed Mar 31. It should count in Q1 at the 1.5x accelerator."),
                      ("jordan.kim", "escalate"), ("marcus.lee", "Honor Mar 31 for Acme. Count it in Q1."),
                      ("marcus.lee", "approve A-0001"), ("priya.nair", "approve A-0002")]:
        say(client, who, text)
    wp = client.app.state.session.world.wp
    wp.append_fact("deals", {"id": "cobalt", "account": "Cobalt Mining", "crm_amount_cents": 6_000_000,
                             "crm_close_date": "2026-03-30", "stage": "closed_won", "owner_id": "jordan.kim",
                             "splits_json": [{"rep": "jordan.kim", "bps": 10000}], "source": "crm"}, "seed")
    wp.append_fact("contracts", {"id": "cobalt", "deal_id": "cobalt", "acv_cents": 6_000_000, "term_months": 24,
                                 "contract_date": "2026-04-03", "countersigned_at": "2026-04-03T15:00:00Z",
                                 "clauses_json": {}, "doc_ref": "contracts/cobalt.pdf", "source": "esign"}, "seed")
    turn = say(client, "marcus.lee", "Honor Mar 30 for Cobalt. Count it in Q1.")
    return turn


def test_repeated_exceptions_for_one_rep_are_flagged_to_the_controller(client):
    turn = _grant_twice(client)
    approval = kinds(turn["timeline"], "APPROVAL")[0]["text"].split()[0]
    done = say(client, "marcus.lee", f"approve {approval}")
    flag = next(i for i in done["timeline"] if i["kind"] == "DECISION" and "integrity_check jordan.kim" in i["text"])
    assert "2 exceptions granted for jordan.kim (P-0001, P-0002)" in flag["text"]
    assert any("to priya.nair: Integrity flag: Jordan Kim has had 2 plan exceptions" in i["text"]
               for i in kinds(done["timeline"], "MESSAGE"))
    clean(client.app.state.session.ro)


def test_an_approver_who_would_benefit_is_skipped_for_the_controller(client):
    session = client.app.state.session
    person = queries.as_of(session.ro, "people", "jordan.kim", None)
    row = {k: v for k, v in person.items() if k not in ("version", "known_at")}
    session.world.wp.append_fact("people", {**row, "manager_id": "marcus.lee", "source": "hr"}, "seed")
    for text in ["Acme closed Mar 31. It should count in Q1 at the 1.5x accelerator.", "escalate"]:
        say(client, "jordan.kim", text)
    turn = say(client, "marcus.lee", "Honor Mar 31 for Acme. Count it in Q1.")
    assert "-> priya.nair" in kinds(turn["timeline"], "APPROVAL")[0]["text"]
    assert any("Integrity flag: Marcus Lee would normally approve" in i["text"] for i in kinds(turn["timeline"], "MESSAGE"))


# ---------- escalation contract #8: Quota's own checks fail ----------

def test_when_the_ledger_fails_its_own_checks_payroll_is_held(plain):
    session = plain.app.state.session
    inject(plain, "Advance the clock to May 1")
    with session.world.conn:                         # a raw edit outside the write path
        session.world.conn.execute("UPDATE commission_lines SET amount_cents = amount_cents + 100 WHERE id = 'CL-0001'")
    _, timeline = inject(plain, "Advance the clock to May 14")
    hold = kinds(timeline, "HOLD")[0]["text"]
    assert hold.startswith("payroll 2026-05 not created: the ledger failed its own checks")
    assert queries.as_of(session.ro, "payroll_files", "PR-2026-05", None) is None
    assert any("to priya.nair: Payroll 2026-05 is held" in i["text"] for i in kinds(timeline, "MESSAGE"))


# ---------- #4: an unsigned plan also reaches the Controller ----------

def test_an_unsigned_plan_draft_is_flagged_to_the_controller_too(client):
    world = client.app.state.session.world
    world.queue.schedule("2026-04-28T10:00:00Z", "plan.amended", {"rep_id": "jordan.kim", "version": 4,
                                                                  "changes": {"collection_hold_days": 30},
                                                                  "signed_at": ""})
    _, timeline = inject(client, "Advance the clock to April 28")
    messages = [i["text"] for i in kinds(timeline, "MESSAGE")]
    assert any("to marcus.lee: Config drift" in m for m in messages)
    assert any("to priya.nair: Unsigned plan (legally sensitive)" in m for m in messages)
