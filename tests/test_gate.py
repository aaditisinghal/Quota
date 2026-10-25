"""ELASTIC_TASKS E5: the off-contract promise gate in the seat, and the side_letter_summit scenario."""

import json

import pytest

from quota.api import scenarios
from quota.engine import checks
from quota.ledger import hashing, invariants, queries


def clean(ro):
    assert not any(invariants.check_all(ro).values()) and hashing.verify_chain(ro)[0]
    assert all(queries.replay_decision(ro, d["id"])[0] for d in queries.fetch_all(ro, "SELECT id FROM decisions"))


def pr(ro, file_id):
    row = queries.fetch_one(ro, "SELECT lines_json FROM payroll_files WHERE id = ?", (file_id,))
    return [l["line_id"] for l in json.loads(row["lines_json"])] if row else []


@pytest.fixture(scope="module")
def side_letter():
    demo, result = scenarios.run("side_letter_summit")
    yield demo, result
    demo.close()


def test_the_side_letter_holds_rileys_summit_line_out_of_may_payroll(side_letter):
    demo, result = side_letter
    res = result["result"]
    assert res["held"] == "held" and res["summit_lines"] == ["CL-0004"] and res["in_pr_2026_05"] == []
    assert res["asked"] == ["marcus.lee", "priya.nair"]
    assert res["evidence"]["doc_id"] == "MSG-SIDE-0001" and res["evidence"]["quote"] in scenarios.SIDE_LETTER
    decision = queries.as_of(demo.ro, "decisions", res["decision"], None)
    assert decision["explanation"].startswith("OFF_CONTRACT_COMMITMENT: Riley Shah wrote")
    refs = json.loads(decision["inputs_json"])
    comms = [r for r in refs if r["table"] == "quota-comms"]
    assert [(r["id"], r["version"], r["mode"]) for r in comms] == [("MSG-SIDE-0001", 1, "local_scan")]
    from quota.search.indexes import body_sha256
    assert comms[0]["sha256"] == body_sha256(scenarios.SIDE_LETTER_MESSAGE["body"])
    review = queries.fetch_one(demo.ro, "SELECT * FROM approvals WHERE kind = 'commitment_review'")
    assert (review["subject_id"], review["approver"], review["amount_cents"]) == (res["decision"], "priya.nair", 600_000)
    clean(demo.ro)                                     # replay uses the stored finding, never a search


def test_without_the_email_summit_is_paid_in_may():
    demo = scenarios.served_world()
    try:
        scenarios.schedule(demo.world, scenarios.SUMMIT_BILLING)
        demo.world.run_until("2026-05-15T18:00:00Z")
        assert "CL-0004" in pr(demo.ro, "PR-2026-05")
        assert not queries.fetch_all(demo.ro, "SELECT id FROM decisions WHERE kind = 'commitment_check'")
    finally:
        demo.close()


def test_not_binding_releases_it_into_the_next_payroll(side_letter):
    demo, result = side_letter
    world = demo.world
    review = queries.fetch_one(demo.ro, "SELECT id FROM approvals WHERE kind = 'commitment_review'")
    world.api.approve("priya.nair", review["id"], "Not binding: Riley had no authority; customer confirmed.")
    world.queue.schedule("2026-06-14T17:00:00Z", "job.due", {"job": "payroll_file", "period": "2026-06"})
    world.run_until("2026-06-14T18:00:00Z")
    assert "CL-0004" in pr(demo.ro, "PR-2026-06")
    clean(demo.ro)


def test_a_promise_the_contract_now_makes_is_released_by_the_check():
    finding = {"doc_id": "MSG-1", "type": "early_exit", "quote": "you can terminate after 60 days"}
    rows = {"contracts": [{"id": "d", "deal_id": "d", "version": 2,
                           "clauses_json": {"termination_for_convenience": {"window_days": 90}}}]}
    assert checks.check_commitments(rows, "d", [finding])["result"] == {
        "held": False, "open": [], "covered": ["MSG-1"], "reason_code": None}
    rows["contracts"][0]["clauses_json"] = {}
    assert checks.check_commitments(rows, "d", [finding])["result"]["reason_code"] == "OFF_CONTRACT_COMMITMENT"
