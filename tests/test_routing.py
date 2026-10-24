import json

from conftest import ANALYST, contract_row
from quota.engine import ranking, routing
from quota.engine.calculator import Booking, calc_commission
from quota.engine.disputes import evaluate_dispute, render_reply
from quota.ledger import queries


def test_routing_tables():
    assert routing.owner("plan_exception") == "marcus.lee"
    assert routing.owner("journal_entry") == routing.owner("payroll_file") == "priya.nair"
    assert routing.limit("marcus.lee", "plan_exception") == 500_000
    assert routing.limit("priya.nair", "plan_exception") is None
    assert routing.next_approver("plan_exception", 600_000, after="marcus.lee") == "priya.nair"
    assert routing.next_approver("plan_exception", 600_000, after="priya.nair") is None


def test_over_limit_routes_to_controller(ledger, wp, ro):
    first = wp.request_approval(ANALYST, "plan_exception", "acme", 600_000, approver=routing.owner("plan_exception"))
    out = wp.decide_approval("marcus.lee", first, "approved", "Honor Mar 31 for Acme.")
    assert out["result"] == "over_limit_routed"
    routed = queries.as_of(ro, "approvals", out["next_approval_id"], None)
    assert (routed["approver"], routed["parent_id"], routed["requested_by"]) == ("priya.nair", first, ANALYST)
    assert not queries.chain_satisfied(ro, first)

    assert wp.decide_approval("priya.nair", routed["id"], "approved", "ok")["result"] == "final"
    assert queries.chain_satisfied(ro, first)
    assert queries.approvals_form_satisfied_chain(ro, [first, routed["id"]])
    assert not queries.approvals_form_satisfied_chain(ro, [routed["id"]])


def test_within_limit_is_final_and_rejection_is_not_satisfied(ledger, wp, ro):
    small = wp.request_approval(ANALYST, "plan_exception", "acme", 400_000, approver="marcus.lee")
    assert wp.decide_approval("marcus.lee", small, "approved", "ok") == {"result": "final", "next_approval_id": None}
    assert queries.chain_satisfied(ro, small)
    rejected = wp.request_approval(ANALYST, "plan_exception", "birch", 400_000, approver="marcus.lee")
    assert wp.decide_approval("marcus.lee", rejected, "rejected", "no")["result"] == "rejected"
    assert not queries.chain_satisfied(ro, rejected)


# ---------- dispute policy ----------

CLAIM = {"kind": "dispute", "line": "CL-0001", "field": "booking_date", "asserted": "2026-03-31",
         "evidence_refs": [], "deal_name": "Acme Robotics"}


def _setup(acme, plan, precedents=()):
    resolved = {"booking_date": ranking.resolve_booking(acme["deal"], acme["contract"], acme["esign"],
                                                        list(precedents), "2026-04-29T16:00:00Z")}
    at_resolved = calc_commission(plan, Booking("acme", "jordan.kim", "2026-04-02T17:14:00Z", 12_000_000, 10000), 0)
    at_claim = calc_commission(plan, Booking("acme", "jordan.kim", "2026-03-31T23:59:59Z", 12_000_000, 10000),
                               30_000_000)
    return resolved, at_claim, at_resolved


def _decision(decision_id, kind):
    return {"id": decision_id, "kind": kind,
            "inputs": [{"table": "contracts", "id": "acme", "version": 1, "sha256": "x"}],
            "resolutions": {"booking_date": {"value": "2026-04-02T17:14:00Z"}}}


def test_dispute_held_then_no_new_evidence(acme, plan):
    resolved, at_claim, at_resolved = _setup(acme, plan)

    first = evaluate_dispute(CLAIM, _decision("D-0001", "commission_calc"), resolved, at_claim, at_resolved)
    assert (first.outcome, first.no_new_evidence) == ("held", False)
    assert first.reply_fields["diff_cents"] == 600_000
    assert first.reply_fields["escalate_to"] == "marcus.lee"
    reply = render_reply(first.reply_fields)
    assert "Difference: $12,000.00 vs $18,000.00 = $6,000.00" in reply
    assert 'Next step: reply "escalate" to send this to Marcus Lee (VP Sales Ops).' in reply
    assert reply.splitlines()[0].startswith("Claim: ")

    second = evaluate_dispute(CLAIM, _decision("D-0002", "dispute"), resolved, at_claim, at_resolved)
    assert (second.outcome, second.no_new_evidence) == ("held", True)
    assert "nothing new was provided since D-0002" in render_reply(second.reply_fields)


def test_dispute_conceded_when_fresh_resolution_matches_claim(acme, plan):
    resolved, at_claim, at_resolved = _setup(acme, plan, precedents=[acme["p0001"]])
    out = evaluate_dispute(CLAIM, _decision("D-0001", "commission_calc"), resolved, at_claim, at_resolved)
    assert out.outcome == "conceded"


def test_dispute_conceded_when_new_evidence_changes_value(acme, plan):
    amended = contract_row("acme", 12_000_000, 36, "2026-04-01", "2026-04-01T10:00:00Z", version=2)
    resolved = {"booking_date": ranking.resolve_booking(acme["deal"], amended, [], [], "2026-04-29T16:00:00Z")}
    _, at_claim, at_resolved = _setup(acme, plan)
    stale = evaluate_dispute(CLAIM, _decision("D-0001", "commission_calc"), resolved, at_claim, at_resolved)
    assert stale.outcome == "held"   # value moved, but the claim cited nothing new
    cited = {**CLAIM, "evidence_refs": ["contracts:acme:v2"]}
    assert evaluate_dispute(cited, _decision("D-0001", "commission_calc"), resolved, at_claim,
                            at_resolved).outcome == "conceded"


def test_escalate_offers_vp_sales_ops(acme, plan):
    resolved, at_claim, at_resolved = _setup(acme, plan)
    out = evaluate_dispute({**CLAIM, "kind": "escalate"}, _decision("D-0003", "dispute"), resolved, at_claim,
                           at_resolved)
    assert out.outcome == "escalation_offered"
    assert out.reply_fields["escalate_to"] == "marcus.lee"
    assert json.dumps(out.reply_fields)   # reply fields stay plain data
