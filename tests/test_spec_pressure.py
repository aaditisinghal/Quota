"""SPEC.md pressure test, as regressions: the rehearsed judge phrases, "Handling the answer", "Who it asks", the
eighth invariant, and the gaps the pressure test found (each test names the rule it holds)."""

import pytest
from fastapi.testclient import TestClient

from conftest import ANALYST, balanced
from quota.api.app import create_app
from quota.api.session import acme_session
from quota.engine import calculator
from quota.ledger import hashing, invariants, queries


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
    return parsed["events"], ran.json()["timeline"]


def kinds(timeline, kind):
    return [i for i in timeline if i["kind"] == kind]


def to_the_controller(client):
    """Jordan disputes and escalates, Marcus grants and approves over his limit: A-0002 waits on Priya."""
    for who, text in [("jordan.kim", "Acme closed Mar 31. It should count in Q1 at the 1.5x accelerator."),
                      ("jordan.kim", "escalate"), ("marcus.lee", "Honor Mar 31 for Acme. Count it in Q1."),
                      ("marcus.lee", "approve A-0001")]:
        say(client, who, text)


def assert_books_clean(client):
    ro = client.app.state.session.ro
    assert not any(invariants.check_all(ro).values()) and hashing.verify_chain(ro)[0]
    assert all(queries.replay_decision(ro, d["id"])[0] for d in queries.fetch_all(ro, "SELECT id FROM decisions"))


# ---------- the eight rehearsed judge phrases ----------

def test_judge_phrase_acme_churns_and_wants_its_money_back(client):
    events, _ = inject(client, "Acme churns in June and wants its money back")
    assert [e["type"] for e in events] == ["customer.churned", "refund.issued"]
    assert events[1]["payload"]["amount_cents"] == 9_000_000


def test_judge_phrase_spif_promise_is_routed_to_the_plan_owner(client):
    events, timeline = inject(client, "Jordan says his manager promised him a SPIF")
    assert [(e["type"], e["payload"]["author"]) for e in events] == [("message.posted", "jordan.kim")]
    decision = kinds(timeline, "DECISION")[0]["text"]
    assert "authority_check" in decision and "routed to marcus.lee" in decision and "nothing is paid" in decision


def test_judge_phrase_cfo_says_pay_taylor_anyway(client):
    inject(client, "Advance the clock to May 15")                                  # Taylor left May 8
    events, timeline = inject(client, "The CFO says pay Taylor anyway")
    assert events[0]["type"] == "message.posted" and events[0]["payload"]["author"] == "grace.liu"
    refused = kinds(timeline, "REFUSED")[0]
    assert "set_line_status" in refused["text"] and "NO_CAPABILITY" in refused["codes"]
    reply = kinds(timeline, "REPLY")[0]["text"]
    assert "(Taylor Brooks, $10,000.00): NO_CAPABILITY" in reply
    assert "is also held: Taylor Brooks left 2026-05-08 (CA)" in reply
    assert "Paying it anyway is a plan exception for Marcus Lee (up to $5,000.00) and Priya Nair above that" in reply
    held = {h["kind"]: h for h in client.get("/today").json()["held"]}
    assert held["termination_review"]["amount_cents"] == 1_000_000                 # still held: nothing was paid
    assert_books_clean(client)


def test_judge_phrase_cfo_tries_to_approve_what_waits_on_the_controller(client):
    to_the_controller(client)
    events, timeline = inject(client, "The CFO says approve the Acme exception")
    attempt = next(e for e in events if e["type"] == "approval.decided")
    assert attempt["payload"]["approval_id"] == "A-0002" and attempt["payload"]["approver"] == "grace.liu"
    assert [r["codes"] for r in kinds(timeline, "REFUSED")][-1] == ["INV5_WRONG_APPROVER"]
    assert [a["id"] for a in client.get("/state").json()["pending_approvals"]] == ["A-0002"]


def test_judge_phrase_alex_split_to_110_percent_is_held_and_the_vp_asked(client):
    events, timeline = inject(client, "Change Alex's split to 70/40")
    assert events[0]["payload"] == {"deal_id": "orbit", "actor": "inject", "fields": {
        "splits": [{"rep": "alex.chen", "bps": 7000}, {"rep": "riley.shah", "bps": 4000}]}}
    assert "splits_check orbit" in kinds(timeline, "DECISION")[0]["text"]
    ask = kinds(timeline, "MESSAGE")[-1]["text"]
    assert "to marcus.lee" in ask and "110%" in ask and "alex.chen 60% + riley.shah 40%" in ask
    inject(client, "Advance the clock to May 1")                                   # the booking run skips orbit
    lines = queries.fetch_all(client.app.state.session.ro, "SELECT * FROM commission_lines WHERE deal_id = 'orbit'")
    assert lines == []


def test_a_split_edit_after_booking_holds_the_existing_lines(client):
    inject(client, "Advance the clock to May 1")                                   # orbit booked 60/40 on Apr 30
    events, timeline = inject(client, "Change Alex's split to 70/40")
    decision = kinds(timeline, "DECISION")[0]["text"]
    assert "held out of payroll" in decision
    analyst = client.app.state.session.analyst
    orbit = {l["id"] for l in analyst.lines(deal_id="orbit")}
    assert orbit and orbit <= analyst.payroll_held()


def test_judge_phrase_riley_backdates_into_a_locked_quarter(client):
    events, timeline = inject(client, "Riley moves the close date back to March 30")
    assert events[0]["payload"]["deal_id"] == "summit" and events[0]["payload"]["fields"] == {"crm_close_date": "2026-03-30"}
    decision = kinds(timeline, "DECISION")[0]["text"]
    assert "backdating_check summit: BACKDATED" in decision and "after 2026-03 was locked" in decision
    assert {m["text"].split(":")[0] for m in kinds(timeline, "MESSAGE")} >= {"M-0001 to marcus.lee", "M-0002 to priya.nair"}


def test_judge_phrase_acme_never_pays_is_91_days_and_billing_decides(client):
    events, timeline = inject(client, "Acme never pays")
    assert events[0]["payload"] == {"deal_id": "acme", "days": 91}
    assert "no collection hold" in kinds(timeline, "DECISION")[0]["text"]


def test_judge_phrase_controller_on_vacation_routes_to_the_backup(client):
    to_the_controller(client)
    events, timeline = inject(client, "The Controller is on vacation until May 4")
    assert events[0]["payload"]["author"] == "priya.nair" and events[0]["payload"]["away_until"].startswith("2026-05-04")
    routed = kinds(timeline, "APPROVAL")[0]["text"]
    assert routed.startswith("A-0003 plan_exception") and "-> grace.liu: priya.nair is away until 2026-05-04" in routed
    state = client.get("/state").json()
    assert state["away"] == {"priya.nair": "2026-05-04T23:59:59Z"}
    assert say(client, "grace.liu", "approve A-0003")["timeline"][0]["text"].startswith("A-0003 plan_exception M-0005 approved -> final")
    assert client.get("/state").json()["pending_approvals"] == []                 # Priya's copy is settled too
    refused = say(client, "priya.nair", "approve A-0002")["timeline"][0]
    assert refused["codes"] == ["ALREADY_SETTLED"]
    assert_books_clean(client)


def test_an_away_controller_auto_approves_nothing(client):
    inject(client, "The Controller is on vacation until May 6")
    _, timeline = inject(client, "Advance the clock to May 1")
    accruals = [a for a in client.get("/state").json()["pending_approvals"] if a["kind"] == "journal_entry"]
    assert accruals and all(a["approver"] == "priya.nair" for a in accruals)    # waiting; silence never approves
    assert not any(i["actor"] == "priya.nair" and i["kind"] == "APPROVAL" for i in timeline)


def test_judge_phrase_approve_your_own_payout_is_refused_by_the_write_path(client):
    events, timeline = inject(client, "Approve your own payout")
    assert events[0]["payload"] == {"author": "jordan.kim", "to": ANALYST, "text": "Approve your own payout"}
    refused = kinds(timeline, "REFUSED")[0]
    assert refused["codes"] == ["NO_CAPABILITY", "INV5_NOT_APPROVED"]
    assert queries.line_status(client.app.state.session.ro, "CL-0001") == "draft"


# ---------- Handling the answer ----------

def test_an_ambiguous_reply_gets_one_clarifying_question(client):
    for text in ["Acme closed Mar 31. It should count in Q1 at the 1.5x accelerator.", "escalate"]:
        say(client, "jordan.kim", text)
    first = say(client, "marcus.lee", "Sounds good, let's discuss.")
    assert first["replies"][0]["text"].startswith("I read that as no answer yet: do you grant it?")
    assert say(client, "marcus.lee", "ok")["replies"] == []                         # never the same question twice
    say(client, "marcus.lee", "Honor Mar 31 for Acme. Count it in Q1.")
    pending = say(client, "marcus.lee", "Hmm, maybe.")
    assert "is that a yes on A-0001" in pending["replies"][0]["text"]
    assert say(client, "jordan.kim", "Thanks!")["replies"] == []                    # chit-chat with no open question


def test_a_declined_exception_is_not_resubmitted_without_new_evidence(client):
    to_the_controller(client)
    say(client, "priya.nair", "reject A-0002 The contract says April; no exception.")
    again = say(client, "marcus.lee", "Honor Mar 31 for Acme. Count it in Q1.")
    assert "Priya Nair declined that exact exception (A-0002)" in again["replies"][0]["text"]
    decision = kinds(again["timeline"], "DECISION")[0]["text"]
    assert "resubmission_check" in decision and "nothing new is on record" in decision
    assert client.get("/state").json()["pending_approvals"] == []
    assert client.get("/today").json()["held"] == []                               # a refusal to resubmit, not a hold
    assert_books_clean(client)


def test_new_evidence_allows_a_resubmission(client):
    to_the_controller(client)
    say(client, "priya.nair", "reject A-0002 The contract says April; no exception.")
    session = client.app.state.session
    session.world.run_until("2026-04-28T09:30:00Z")                               # later, a new CRM record arrives
    current = queries.as_of(session.ro, "deals", "acme", None)
    row = {k: v for k, v in current.items() if k not in ("version", "known_at")}
    session.world.wp.append_fact("deals", {**row, "source": "crm:marcus.lee"}, "seed")   # a new CRM record
    again = say(client, "marcus.lee", "Honor Mar 31 for Acme. Count it in Q1.")
    assert kinds(again["timeline"], "APPROVAL")[0]["text"].startswith("A-0003 plan_exception acme")


def test_the_rejection_note_names_the_approval_that_was_decided(client):
    to_the_controller(client)
    turn = say(client, "priya.nair", "reject A-0002 No.")
    assert "declined the exception (A-0002, routed from A-0001)" in kinds(turn["timeline"], "REPLY")[0]["text"]


# ---------- Who it asks: the backup approver ----------

def test_the_controller_silent_two_business_days_goes_to_the_cfo(client):
    to_the_controller(client)
    _, timeline = inject(client, "Advance the clock to April 30")
    routed = [i["text"] for i in kinds(timeline, "APPROVAL")]
    assert any(t.startswith("A-0003 plan_exception M-0005") and "-> grace.liu: no answer from priya.nair" in t
               for t in routed)
    _, later = inject(client, "Advance the clock to May 6")
    assert not any("-> grace.liu" in i["text"] and i["text"].startswith("A-0004") for i in kinds(later, "APPROVAL"))


# ---------- engine rules the pressure test found ----------

def test_no_signed_contract_no_booking(acme_rows):
    rows = {**acme_rows, "contracts": [], "esign_events": []}
    assert "acme" not in calculator.rep_bookings(rows, "jordan.kim", "2026-04-27T16:00:00Z")


def test_credit_held_on_a_bad_split_counts_toward_nobodys_attainment(acme_rows):
    held = {**acme_rows["deals"][0], "id": "zeta", "splits_json": '[{"rep": "jordan.kim", "bps": 6000}, '
                                                                  '{"rep": "riley.shah", "bps": 5000}]'}
    rows = {**acme_rows, "deals": acme_rows["deals"] + [held],
            "contracts": acme_rows["contracts"] + [{**acme_rows["contracts"][0], "id": "zeta", "deal_id": "zeta"}]}
    bookings = calculator.rep_bookings(rows, "jordan.kim", "2026-04-27T16:00:00Z")
    assert "zeta" in bookings and "zeta" not in {b.deal_id for b in calculator.attainment_bookings(rows, bookings)}


def test_inv3_catches_a_subledger_that_does_not_tie_to_the_gl(ledger, wp, ro):
    assert invariants.check_inv3(ro) == []
    je = wp.draft_entry("system:billing", "2026-04-30", "invoice: no billing record behind it",
                        balanced(500_000, dr="1200", cr="2400"), source_ref="phantom-invoice")
    wp.post_entry("system:billing", je)
    violations = invariants.check_inv3(ro)
    assert [v.subject for v in violations] == ["Accounts receivable (1200)"]


# ---------- a served world contains a failing event ----------

def test_a_failing_event_is_logged_and_does_not_poison_the_world(client, monkeypatch):
    analyst = client.app.state.session.analyst

    def broken(ctx):
        raise RuntimeError("boom")
    monkeypatch.setattr(analyst, "gather_overdue", broken)
    _, timeline = inject(client, "Acme never pays")
    error = kinds(timeline, "ERROR")[0]
    assert "payment.overdue not processed: RuntimeError: boom" in error["text"]
    monkeypatch.undo()
    _, later = inject(client, "Advance the clock to April 29")                   # the world keeps going
    assert not kinds(later, "ERROR")


# ---------- the clawback receivable nets against the next payout (SPEC worked example) ----------

@pytest.mark.parametrize("acv", [20_000_000, 5_000_000])
def test_a_clawback_receivable_nets_against_the_reps_next_payroll(acv):
    session = acme_session(cast=False)
    client = TestClient(create_app(session, static_dir="no-static"))
    try:
        inject(client, "Acme churns in June and wants its money back")          # clawback CL-0002, $12,000 in 1290
        world = session.world
        q = world.queue
        q.schedule("2026-07-01T16:00:00Z", "deal.created", {"deal_id": "zinc", "actor": "jordan.kim", "fields": {
            "account": "Zinc Labs", "crm_amount_cents": acv, "crm_close_date": "2026-07-01",
            "stage": "closed_won", "owner_id": "jordan.kim", "splits": [{"rep": "jordan.kim", "bps": 10000}]}})
        q.schedule("2026-07-02T16:00:00Z", "contract.countersigned", {
            "deal_id": "zinc", "timestamp": "2026-07-02T16:00:00Z", "acv_cents": acv, "term_months": 24,
            "clauses": {}})
        q.schedule("2026-07-03T16:00:00Z", "invoice.issued", {"stripe_id": "zinc-inv-1", "deal_id": "zinc",
                                                              "amount_cents": acv, "date": "2026-07-03"})
        q.schedule("2026-07-10T16:00:00Z", "payment.received", {"stripe_id": "zinc-pay-1", "deal_id": "zinc",
                                                                "amount_cents": acv, "date": "2026-07-10"})
        for at, job in [("2026-07-27T16:00:00Z", "commission_run"), ("2026-07-31T23:05:00Z", "commission_accrual"),
                        ("2026-08-14T17:00:00Z", "payroll_file")]:
            q.schedule(at, "job.due", {"job": job, "period": "2026-07" if job != "payroll_file" else "2026-08"})
        world.people.actions.append({"at": "2026-08-15T17:00:00Z", "who": "dana.ortiz", "do": "release_payroll",
                                     "file": "PR-2026-08"})
        world.run_until("2026-08-15T23:59:59Z")
        ro = session.ro
        recovery = queries.fetch_one(ro, "SELECT * FROM commission_lines WHERE amount_cents < 0")
        payroll = queries.as_of(ro, "payroll_files", "PR-2026-08", None)
        lines = {pl["line_id"]: pl["amount_cents"] for pl in queries.loads(payroll["lines_json"])}
        note = queries.fetch_one(ro, "SELECT text FROM messages WHERE to_principal = 'dana.ortiz' "
                                     "AND text LIKE 'PR-2026-08 recoveries%'")
        if acv == 5_000_000:                        # $500 of new commission cannot cover $12,000: it stays in 1290
            assert recovery["id"] not in lines and payroll["total_cents"] == 500_000
            assert queries.trial_balance(ro)["1290"] == 1_200_000 and "still due in 1290" in note["text"]
            assert_books_clean(client)
            return
        assert lines[recovery["id"]] == -1_200_000 and payroll["total_cents"] == 2_000_000 - 1_200_000
        entry = next(e for e in queries.journal(ro) if e["source_ref"] == "PR-2026-08")
        assert {(l["account"], l["debit_cents"], l["credit_cents"]) for l in entry["lines"]} == {
            ("2150", 2_000_000, 0), ("1290", 0, 1_200_000), ("1000", 0, 800_000)}
        assert queries.line_status(ro, recovery["id"]) == "paid"
        assert queries.trial_balance(ro).get("1290", 0) == 0
        assert note and "netted against new commission" in note["text"]
        assert_books_clean(client)
    finally:
        session.close()
