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


# ---------- R3: recompute the whole quarter when one deal changes ----------

def demo_at(ts):
    demo = demo_acme.AcmeDemo(out=lambda line: None)
    demo_acme.seed_acme.seed(demo.world.wp, demo.world.clock, demo.world.emit)
    demo.world.run_until(ts)
    return demo


def entry_lines(ro, decision_id):
    entry = next(e for e in queries.journal(ro) if e["decision_id"] == decision_id)
    return entry, [(l["account"], l["debit_cents"], l["credit_cents"]) for l in entry["lines"]]


def test_birch_churns_recomputes_acme():
    demo = demo_at("2026-05-31T23:59:59Z")
    try:
        world, ro = demo.world, demo.ro
        world.queue.schedule("2026-05-31T23:59:59Z", "customer.churned",
                             {"deal_id": "birch", "effective_date": "2026-05-31", "reason": "churn", "refund_cents": 0})
        world.queue.schedule("2026-05-31T23:59:59Z", "job.due", {"job": "churn_review", "period": "2026-05"})
        world.run_until("2026-06-01T00:30:00Z")

        birch = queries.fetch_one(ro, "SELECT * FROM decisions WHERE kind = 'clawback' AND subject_id = 'CL-OB-birch'")
        entry, lines = entry_lines(ro, birch["id"])
        assert lines == [("1290", 1_000_000, 0), ("6115", 0, 1_000_000)] and entry["status"] == "pending_approval"

        true_ups = queries.fetch_all(ro, "SELECT * FROM decisions WHERE kind = 'true_up'")
        assert [t["subject_id"] for t in true_ups] == ["CL-0002"]            # cedar and delta are unchanged
        result = queries.loads(true_ups[0]["output_json"])["result"]
        assert result["delta"] == {"line_id": "CL-0002", "old_cents": 1_800_000, "new_cents": 1_300_000,
                                   "delta_cents": -500_000}
        assert (result["m_used"], result["m_total"]) == (2, 36)
        entry, lines = entry_lines(ro, true_ups[0]["id"])
        assert lines == [("1290", 500_000, 0), ("1350", 0, 472_222), ("6110", 0, 27_778)] and entry["status"] == "pending_approval"
        assert "attainment 140.00% -> 106.66%" in true_ups[0]["explanation"]

        pending = {a["subject_id"]: a["approver"] for a in queries.pending_approvals(ro, world.clock.now())}
        assert {pending[e["id"]] for e in queries.journal(ro) if e["status"] == "pending_approval"} == {"priya.nair"}
        correcting = queries.fetch_all(ro, "SELECT * FROM commission_lines WHERE decision_id = ?", (true_ups[0]["id"],))
        assert [(l["corrects_id"], l["amount_cents"]) for l in correcting] == [("CL-0002", -500_000)]

        client = TestClient(create_app(__import__("quota.api.session", fromlist=["Session"]).Session(world, "acme"),
                                       static_dir="no-static"))
        q1 = next(q for q in client.get("/statement/jordan.kim").json()["quarters"] if q["quarter"] == "2026Q1")
        assert q1["attainment_bps"] == 10666
        clean(ro)
    finally:
        demo.close()


def test_after_the_controller_approves_acme_amortizes_and_claws_back_on_the_net():
    demo = demo_at("2026-05-31T23:59:59Z")
    try:
        world, ro = demo.world, demo.ro
        world.queue.schedule("2026-05-31T23:59:59Z", "customer.churned",
                             {"deal_id": "birch", "effective_date": "2026-05-31", "reason": "churn", "refund_cents": 0})
        world.queue.schedule("2026-05-31T23:59:59Z", "job.due", {"job": "churn_review", "period": "2026-05"})
        world.run_until("2026-06-01T00:30:00Z")
        for approval in queries.pending_approvals(ro, world.clock.now()):
            world.api.approve("priya.nair", approval["id"], "Recompute per signed plan.")
        world.settle()
        assert queries.trial_balance(ro)["1290"] == 1_500_000
        assert queries.trial_balance(ro)["1350"] == 1_227_778               # $13,000 basis, 2 of 36 months taken
        clean(ro)

        world.run_until("2026-06-30T23:35:00Z")                              # June amortization, Acme churn review
        june = next(e for e in queries.journal(ro) if e["memo"] == "amortization: CL-0002 2026-06")
        assert [l["debit_cents"] for l in june["lines"] if l["account"] == "6110"] == [1_227_778 // 34]
        acme = queries.fetch_one(ro, "SELECT * FROM decisions WHERE kind = 'clawback' AND subject_id = 'CL-0002'")
        _, lines = entry_lines(ro, acme["id"])
        left = 1_227_778 - 1_227_778 // 34
        assert lines == [("1290", 1_300_000, 0), ("1350", 0, left), ("6110", 0, 1_300_000 - left)]
        recovery = queries.fetch_one(ro, "SELECT * FROM commission_lines WHERE decision_id = ?", (acme["id"],))
        assert recovery["amount_cents"] == -1_300_000                       # the net: $5,000 was already recovered
        clean(ro)
    finally:
        demo.close()


def test_recompute_quarter_returns_only_changed_lines(plan):
    from quota.engine.calculator import Booking
    from quota.engine.recompute import LineDelta, recompute_quarter

    def b(line_id, deal, ts, base, paid):
        return {"line_id": line_id, "amount_cents": paid, "booking": Booking(deal, "jordan.kim", ts, base, 10000)}
    q1 = [b("CL-OB-cedar", "cedar", "2026-02-14T18:00:00Z", 12_000_000, 1_200_000),
          b("CL-OB-delta", "delta", "2026-03-10T18:00:00Z", 8_000_000, 800_000),
          b("CL-0002", "acme", "2026-03-31T23:59:59Z", 12_000_000, 1_800_000)]
    assert recompute_quarter("jordan.kim", "2026Q1", q1, plan) == [LineDelta("CL-0002", 1_800_000, 1_300_000, -500_000)]
    # A re-booking that adds credit earlier in the quarter lifts the deals after it: Acme was paid at base.
    raised = [b("CL-OB-birch", "birch", "2026-01-20T18:00:00Z", 10_000_000, 1_000_000), *q1[:2],
              {**q1[2], "amount_cents": 1_300_000}]
    assert recompute_quarter("jordan.kim", "2026Q1", raised, plan) == [LineDelta("CL-0002", 1_300_000, 1_800_000, 500_000)]
