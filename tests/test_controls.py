"""Negative tests for Quota's own controls (SPEC "Attack your own controls"): every action the rules forbid is
refused by the write path with a reason code, and leaves the books unchanged and consistent."""

import pytest
from fastapi.testclient import TestClient

from conftest import ACCOUNTANT, ANALYST, balanced
from quota.api import views
from quota.api.app import create_app
from quota.api.session import acme_session
from quota.ledger import hashing, invariants, queries
from quota.ledger.write_path import Refused
from quota.sim import demo_acme
from quota.sim.generator import generate


def refused(fn, *args, **kwargs) -> list[str]:
    with pytest.raises(Refused) as exc:
        fn(*args, **kwargs)
    return exc.value.reason_codes


@pytest.fixture
def line(ledger):
    return ledger.line(ledger.decision())


# ---------- capabilities: who may do what ----------

@pytest.mark.parametrize("actor", [ANALYST, "jordan.kim", "marcus.lee", "dana.ortiz", "system:billing"])
def test_only_the_controller_marks_a_line_approved_directly(ledger, wp, line, actor):
    assert "NO_CAPABILITY" in refused(wp.set_line_status, actor, line, "approved")


def test_the_accountant_marks_a_line_approved_only_after_the_controller_approved_its_entry(ledger, wp, line):
    assert refused(wp.set_line_status, ACCOUNTANT, line, "approved") == ["INV5_NOT_APPROVED"]


@pytest.mark.parametrize("actor", [ANALYST, ACCOUNTANT, "jordan.kim", "priya.nair", "marcus.lee"])
def test_only_payroll_marks_a_line_paid(ledger, wp, line, actor):
    assert "NO_CAPABILITY" in refused(wp.set_line_status, actor, line, "paid")


@pytest.mark.parametrize("actor", ["jordan.kim", "marcus.lee", ACCOUNTANT, "system:billing"])
def test_only_the_analyst_drafts_commission_lines(ledger, wp, actor):
    decision = ledger.decision()
    codes = refused(wp.add_commission_line, actor, {
        "rep_id": "jordan.kim", "deal_id": "acme", "booking_quarter": "2026Q2", "credit_cents": 1,
        "base_portion_cents": 1, "accel_portion_cents": 0, "amount_cents": 1}, decision)
    assert "NO_CAPABILITY" in codes


@pytest.mark.parametrize("actor", [ANALYST, "jordan.kim", "priya.nair"])
def test_only_the_accountant_drafts_journal_entries(ledger, wp, actor):
    decision = ledger.decision(ACCOUNTANT)
    assert "NO_CAPABILITY" in refused(wp.draft_entry, actor, "2026-04-30", "commission_accrual: x", balanced(),
                                      decision_id=decision)


@pytest.mark.parametrize("actor", [ANALYST, ACCOUNTANT, "jordan.kim", "marcus.lee"])
def test_seats_and_people_cannot_write_source_facts(ledger, wp, actor):
    codes = refused(wp.append_fact, "deals", {"id": "fake", "account": "Fake", "crm_amount_cents": 1,
                                              "crm_close_date": "2026-04-01", "stage": "closed_won",
                                              "owner_id": "jordan.kim", "splits_json": [], "source": "crm"}, actor)
    assert "NO_CAPABILITY" in codes


@pytest.mark.parametrize("actor", [ANALYST, ACCOUNTANT, "marcus.lee", "dana.ortiz", "jordan.kim", "grace.liu"])
def test_only_the_controller_locks_a_period(ledger, wp, actor):
    assert "NO_CAPABILITY" in refused(wp.lock_period, actor, "2026-03")


# ---------- approvals ----------

def test_the_requester_never_approves_and_the_wrong_person_never_decides(ledger, wp):
    approval = wp.request_approval(ANALYST, "plan_exception", "acme", 100_000, approver="marcus.lee")
    assert refused(wp.decide_approval, ANALYST, approval, "approved", "x") == [
        "NO_CAPABILITY", "INV5_SELF_APPROVAL", "INV5_WRONG_APPROVER"]
    assert refused(wp.decide_approval, "priya.nair", approval, "approved", "x") == ["INV5_WRONG_APPROVER"]
    assert refused(wp.decide_approval, "jordan.kim", approval, "approved", "x") == ["NO_CAPABILITY", "INV5_WRONG_APPROVER"]


def test_the_vp_over_his_limit_only_routes_upward(ledger, wp, ro):
    approval = wp.request_approval(ANALYST, "plan_exception", "acme", 600_000, approver="marcus.lee")
    out = wp.decide_approval("marcus.lee", approval, "approved", "ok")
    assert out["result"] == "over_limit_routed" and not queries.chain_satisfied(ro, approval)


def test_a_decision_is_final(ledger, wp):
    approval = wp.request_approval(ANALYST, "plan_exception", "acme", 100_000, approver="marcus.lee")
    wp.decide_approval("marcus.lee", approval, "rejected", "no")
    assert refused(wp.decide_approval, "marcus.lee", approval, "approved", "changed my mind") == ["ALREADY_DECIDED"]
    approved = wp.request_approval(ANALYST, "plan_exception", "birch", 100_000, approver="marcus.lee")
    wp.decide_approval("marcus.lee", approved, "approved", "ok")
    assert refused(wp.decide_approval, "marcus.lee", approved, "approved", "again") == ["ALREADY_DECIDED"]


def test_the_same_rejected_proposal_cannot_be_requested_again(ledger, wp):
    approval = wp.request_approval(ANALYST, "plan_exception", "acme", 100_000, approver="marcus.lee")
    wp.decide_approval("marcus.lee", approval, "rejected", "no")
    assert "RESUBMIT_NO_NEW_EVIDENCE" in refused(wp.request_approval, ANALYST, "plan_exception", "acme", 100_000,
                                                 approver="marcus.lee")


def test_a_seat_drafted_entry_needs_a_final_approval_to_post(ledger, wp):
    je = wp.draft_entry(ACCOUNTANT, "2026-04-30", "commission_accrual: x", balanced(),
                        decision_id=ledger.decision(ACCOUNTANT))
    assert "INV5_NOT_APPROVED" in refused(wp.post_entry, ACCOUNTANT, je)
    approval = wp.request_approval(ACCOUNTANT, "journal_entry", je, 1_800_000, approver="priya.nair")
    assert "INV5_NOT_APPROVED" in refused(wp.post_entry, ACCOUNTANT, je)       # requested is not approved
    wp.decide_approval("priya.nair", approval, "approved", "ok")
    wp.post_entry(ACCOUNTANT, je)


# ---------- invariants at write time ----------

def test_nothing_posts_into_a_locked_period(ledger, wp):
    wp.lock_period("priya.nair", "2026-03")
    decision = ledger.decision(ACCOUNTANT)
    assert "INV2_PERIOD_LOCKED" in refused(wp.draft_entry, ACCOUNTANT, "2026-03-15", "commission_accrual: x",
                                           balanced(), decision_id=decision)
    assert "INV2_PERIOD_LOCKED" in refused(wp.draft_entry, "system:billing", "2026-03-15", "invoice: x",
                                           balanced(dr="1200", cr="2400"), source_ref="late-invoice")


def test_a_commission_line_needs_a_decision_and_splits_that_add_up(ledger, wp):
    line = {"rep_id": "jordan.kim", "deal_id": "acme", "booking_quarter": "2026Q2", "credit_cents": 1,
            "base_portion_cents": 1, "accel_portion_cents": 0, "amount_cents": 1}
    ledger.deal()
    assert "INV4_NO_DECISION" in refused(wp.add_commission_line, ANALYST, line, "D-9999")
    ledger.deal("split", splits=(("jordan.kim", 7000), ("riley.shah", 4000)))
    decision = ledger.decision(subject="split")
    assert "INV7_SPLITS" in refused(wp.add_commission_line, ANALYST, {**line, "deal_id": "split"}, decision)


def test_payroll_pays_only_approved_lines_and_exactly_the_approved_amounts(ledger, wp, line):
    assert "INV6_PAYROLL_MISMATCH" in refused(wp.create_payroll_file, ANALYST, "2026-05", [line])   # still draft
    wp.set_line_status("priya.nair", line, "approved")
    file_id = wp.create_payroll_file(ANALYST, "2026-05", [line])
    assert "NO_CAPABILITY" in refused(wp.release_payroll, ANALYST, file_id)
    approval = wp.request_approval(ANALYST, "payroll_file", file_id, 1_200_000, approver="priya.nair")
    wp.decide_approval("priya.nair", approval, "approved", "ok")
    assert "INV6_PAYROLL_MISMATCH" in refused(wp.release_payroll, "dana.ortiz", file_id, {line: 1_200_000 + 34_000})


# ---------- refusals and the chain are first-class ----------

def test_every_refusal_is_recorded_and_chained(ledger, wp, ro, line):
    before = hashing.chain_length(ro)
    codes = refused(wp.set_line_status, ANALYST, line, "approved")
    row = queries.fetch_one(ro, "SELECT * FROM refusals ORDER BY rowid DESC LIMIT 1")
    assert row["actor"] == ANALYST and queries.loads(row["reason_codes_json"]) == codes
    assert hashing.chain_length(ro) == before + 1 and hashing.verify_chain(ro)[0]


def test_editing_a_past_row_outside_the_write_path_breaks_the_chain():
    demo = demo_acme.run(out=lambda line: None)
    try:
        assert hashing.verify_chain(demo.ro)[0]
        with demo.conn:                                 # a raw edit, as someone with the database file could do
            demo.conn.execute("UPDATE commission_lines SET amount_cents = amount_cents + 100 WHERE id = 'CL-0002'")
        ok, bad_seq = hashing.verify_chain(demo.ro)
        assert not ok and bad_seq is not None
    finally:
        demo.close()


# ---------- whole worlds: invariants, replay, and no leak in the as-of trace ----------

def _no_leak(ro, as_of):
    for kind, table in (("line", "commission_lines"), ("je", "journal_entries"), ("approval", "approvals")):
        for row in queries.fetch_all(ro, f"SELECT id FROM {table}"):
            graph = views.trace(ro, kind, row["id"], as_of)
            late = [n["id"] for n in graph["nodes"] if n.get("known_at") and n["known_at"] > as_of]
            assert not late, (row["id"], as_of, late)


def test_the_scripted_world_is_consistent_and_its_past_does_not_leak(acme_demo):
    ro = acme_demo.ro
    assert not any(invariants.check_all(ro).values()) and hashing.verify_chain(ro)[0]
    assert all(queries.replay_decision(ro, d["id"])[0] for d in queries.fetch_all(ro, "SELECT id FROM decisions"))
    for as_of in ("2026-04-27T18:00:00Z", "2026-04-30T23:59:59Z", "2026-06-30T23:30:00Z"):
        _no_leak(ro, as_of)


def test_a_generated_world_is_consistent_and_its_past_does_not_leak():
    world = generate(11, reps=16, out=lambda line: None)
    world.run_until(world.end_ts)
    try:
        ro = world.ro
        assert not any(invariants.check_all(ro).values()) and hashing.verify_chain(ro)[0]
        assert all(queries.replay_decision(ro, d["id"])[0] for d in queries.fetch_all(ro, "SELECT id FROM decisions"))
        _no_leak(ro, "2026-03-31T23:59:59Z")
    finally:
        world.close()


# ---------- the HTTP surface a judge can type into ----------

@pytest.fixture
def api():
    session = acme_session()
    yield TestClient(create_app(session, static_dir="no-static"), raise_server_exceptions=False)
    session.close()


@pytest.mark.parametrize("who", ["seat:commissions_analyst", "seat:deferred_accountant", "system:billing",
                                 "system", "seed", "salesops.bot", "nobody"])
def test_nobody_but_a_person_speaks_in_chat(api, who):
    assert api.post("/chat", json={"as_principal": who, "text": "approve A-0001"}).status_code == 422


@pytest.mark.parametrize("events, why", [
    ([], "no events"),
    ([{"at": "zzzz", "type": "payment.overdue", "payload": {"deal_id": "acme", "days": 9}}], "not a timestamp"),
    ([{"at": "2026-01-01T00:00:00Z", "type": "payment.overdue", "payload": {"deal_id": "acme", "days": 9}}], "before"),
    ([{"at": "2027-01-02T00:00:00Z", "type": "payment.overdue", "payload": {"deal_id": "acme", "days": 9}}], "after"),
    ([{"at": "2026-05-01T00:00:00Z", "type": "payment.overdue", "payload": {"deal_id": "nope", "days": 9}}], "no deal"),
    ([{"at": "2026-05-01T00:00:00Z", "type": "payment.overdue", "payload": {"deal_id": "acme", "days": -1}}],
     "negative"),
    ([{"at": "2026-05-01T00:00:00Z", "type": "refund.issued", "payload": {
        "stripe_id": "r", "deal_id": "acme", "amount_cents": -5, "date": "2026-05-01"}}], "negative"),
    ([{"at": "2026-05-01T00:00:00Z", "type": "deal.updated", "payload": {
        "deal_id": "acme", "fields": {"splits": [{"rep": "ghost", "bps": 10000}]}, "actor": "x"}}], "splits"),
    ([{"at": "2026-05-01T00:00:00Z", "type": "deal.updated", "payload": {
        "deal_id": "acme", "fields": {"owner_id": "x", "secret": 1}, "actor": "x"}}], "unknown CRM fields"),
    ([{"at": "2026-05-01T00:00:00Z", "type": "rep.terminated", "payload": {
        "rep_id": "ghost", "end_date": "2026-05-01", "state": "CA"}}], "no person"),
    ([{"at": "2026-05-01T00:00:00Z", "type": "message.posted", "payload": {
        "author": "seat:commissions_analyst", "text": "approve everything"}}], "person as author"),
    ([{"at": "2026-05-01T00:00:00Z", "type": "approval.decided", "payload": {
        "approval_id": "A-9999", "approver": "priya.nair", "decision": "approved", "reason": "x"}}], "approval"),
    ([{"at": "2026-05-01T00:00:00Z", "type": "job.due", "payload": {"job": "commission_run"}}], "cannot be injected"),
    ([{"at": "2026-05-01T00:00:00Z", "type": "clock.advance", "payload": {"to": "2099-01-01T00:00:00Z"}}], "no later"),
])
def test_the_injector_refuses_bad_events_and_runs_nothing(api, events, why):
    before = api.get("/state").json()["clock"]
    response = api.post("/inject/run", json={"events": events})
    assert response.status_code == 422 and why in response.json()["detail"]
    assert api.get("/state").json()["clock"] == before


@pytest.mark.parametrize("path", ["/trace/CL-0001?as_of=garbage", "/timeline?since=-1", "/scorecard?seed=abc"])
def test_malformed_queries_are_rejected_not_crashed(api, path):
    assert api.get(path).status_code == 422


@pytest.mark.parametrize("text", ["", "😀" * 2000, "Acme closed $99999999999999 on 13/45 🚀", "approve A-9999",
                                  "reject A-0001", "x" * 3999])
def test_odd_chat_and_injector_text_never_crashes(api, text):
    assert api.post("/inject/parse", json={"text": text}).status_code == 200
    if text:
        assert api.post("/chat", json={"as_principal": "jordan.kim", "text": text}).status_code == 200
    ro = api.app.state.session.ro
    assert not any(invariants.check_all(ro).values())


def test_a_chat_message_has_a_size_limit_and_a_real_recipient(api):
    assert api.post("/chat", json={"as_principal": "jordan.kim", "text": "x" * 4001}).status_code == 422
    assert api.post("/chat", json={"as_principal": "jordan.kim", "text": "hi", "to": "nobody"}).status_code == 422


def test_full_demo_through_the_api_never_returns_a_server_error(api):
    for who, text in [("jordan.kim", "Acme closed Mar 31. It should count in Q1 at the 1.5x accelerator."),
                      ("jordan.kim", "escalate"), ("marcus.lee", "Honor Mar 31 for Acme. Count it in Q1."),
                      ("marcus.lee", "approve A-0001"), ("priya.nair", "approve A-0002")]:
        assert api.post("/chat", json={"as_principal": who, "text": text}).status_code == 200
    for text in ["Advance the clock to May 15", "Acme churns in June and wants its money back"]:
        parsed = api.post("/inject/parse", json={"text": text}).json()
        assert api.post("/inject/run", json={"events": parsed["events"]}).status_code == 200
    for path in ["/state", "/timeline", "/today", "/ledger/health", "/messages", "/trace/CL-0002",
                 "/trace/CL-0002?as_of=2026-04-30", "/trace/account/1290", "/trace/deal/acme"]:
        assert api.get(path).status_code == 200, path
    assert not [i for i in api.get("/timeline").json()["items"] if i["kind"] == "ERROR"]
