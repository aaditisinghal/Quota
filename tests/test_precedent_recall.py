"""ELASTIC_TASKS E7: similar past decisions on the approval card, never applied."""

from fastapi.testclient import TestClient

from quota.api import scenarios
from quota.api.app import create_app
from quota.api.session import Session
from quota.ledger import hashing, queries
from quota.search import precedents


def test_the_offline_recall_eval_is_measured():
    result = precedents.recall_eval(precedents.LocalDecisions(precedents.library_docs()))
    assert result["requests"] == 6 and result["hit_at_3"] == sum(r["hit"] for r in result["rows"])
    assert all(len(r["top3"]) <= 3 for r in result["rows"])
    hits = {r["expected"]: r["hit"] for r in result["rows"]}
    assert hits["P-1002"] and hits["A-1004"] and hits["P-1007"]


def test_an_exception_request_gets_similar_decisions_that_are_never_applied():
    demo = scenarios.served_world()
    try:
        world = demo.world
        # A past exception for another deal: similar words, but its structured scope cannot apply to Acme.
        world.wp.append_fact("precedents", {
            "id": "P-0900", "statement": "Honor the Mar 31 CRM close for Birch and count it in Q1 (booking date)",
            "scope_json": {"deal_id": "birch", "field": "booking_date"}, "override_json": {"value": "2026-03-31"},
            "approvals_json": ["marcus.lee"], "effective_from": "2026-01-01", "expires_at": "2026-03-31"}, "seed")
        before = queries.fetch_all(demo.ro, "SELECT id FROM precedents")
        world.api.message("marcus.lee", "seat:commissions_analyst", "Honor Mar 31 for Acme. Count it in Q1.")
        world.settle()
        approval = queries.fetch_one(demo.ro, "SELECT * FROM approvals WHERE kind = 'plan_exception' ORDER BY id")
        recall = queries.fetch_one(demo.ro, "SELECT * FROM decisions WHERE kind = 'precedent_recall'")
        assert recall["subject_id"] == approval["id"] and recall["outcome"] == "posted"
        output = queries.loads(recall["output_json"])
        assert output["result"]["applied"] is False and 1 <= len(output["args"]["similar"]) <= 3
        assert approval["id"] not in output["result"]["shown"] and "P-0900" in output["result"]["shown"]
        assert approval["approver"] == "marcus.lee"                       # still waiting on a person
        assert queries.replay_decision(demo.ro, recall["id"])[0] and hashing.verify_chain(demo.ro)[0]
        session = Session(world, "acme")
        state = TestClient(create_app(session, static_dir="no-static")).get("/state").json()
        card = next(a for a in state["pending_approvals"] if a["kind"] == "plan_exception")
        assert [s["id"] for s in card["similar"]] == output["result"]["shown"]
        assert queries.fetch_all(demo.ro, "SELECT id FROM precedents") == before     # nothing granted by recall
    finally:
        demo.close()
