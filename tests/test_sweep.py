"""ELASTIC_TASKS E6, offline: the findings poller, the agent setup's upsert flow, and the converse proxy."""

import json

from fastapi.testclient import TestClient

from quota.api import scenarios
from quota.api.app import create_app
from quota.api.session import Session
from quota.ledger import queries
from quota.search import agent_setup, findings

QUOTE = "if your team hasn't adopted it by end of Q3 we'll let you out of year two, no penalty."


def test_findings_parse_tolerates_fences_and_prose():
    assert findings.parse('```json\n[{"doc_id": "MSG-1", "type": "credit", "quote": "x"}]\n```')[0]["doc_id"] == "MSG-1"
    assert findings.parse('Here you go: [{"doc_id": "MSG-2", "quote": "y"}] Done.')[0]["doc_id"] == "MSG-2"
    assert findings.parse("[]") == [] and findings.parse("not json") == []
    assert findings.parse('[{"doc_id": "MSG-3"}]') == []                  # no quote, nothing to check


class Store:
    def __init__(self, rows):
        self.rows, self.status = rows, {}

    def new(self):
        return [r for r in self.rows if r["id"] not in self.status]

    def mark(self, finding_id, status):
        self.status[finding_id] = status


def test_a_swept_finding_becomes_an_event_and_the_seat_holds_the_line():
    demo = scenarios.served_world([scenarios.SIDE_LETTER_MESSAGE])
    try:
        world = demo.world
        world.run_until("2026-05-05T17:00:00Z")
        store = Store([
            {"id": "F-1", "raw": "```json\n" + json.dumps([{"doc_id": "MSG-SIDE-0001", "type": "early_exit",
                                                             "quote": QUOTE}]) + "\n```"},
            {"id": "F-2", "raw": json.dumps([{"doc_id": "MSG-SIDE-0001", "type": "credit",
                                              "quote": "We will refund everything."}])}])
        passed = findings.poll(store, world.evidence, world, out=lambda line: None)
        world.settle()
        assert passed == ["MSG-SIDE-0001"] and store.status == {"F-1": "ingested", "F-2": "rejected"}
        event = queries.fetch_one(demo.ro, "SELECT * FROM events WHERE type = 'evidence.found'")
        assert json.loads(event["payload_json"])["finding_id"] == "F-1"
        check = queries.fetch_one(demo.ro, "SELECT * FROM decisions WHERE kind = 'commitment_check'")
        assert (check["subject_id"], check["outcome"]) == ("summit", "held")
    finally:
        demo.close()


class Kibana:
    """Answers like Agent Builder and Workflows: nothing exists the first time, everything the second."""

    def __init__(self, exists=False):
        self.calls, self.exists = [], exists

    def __call__(self, method, path, **kw):
        self.calls.append((method, path, kw.get("json")))
        ok = method != "GET" or self.exists

        class R:
            status_code = 200 if ok else 404
            text = "{}"

            def json(self_inner):
                if path == "/api/workflows":
                    return {"results": [{"id": "wf-1", "name": "promise-sweep"}] if self.exists else []}
                return {"id": "wf-1"}
        R.ok = ok
        return R()


def test_agent_setup_creates_then_updates_and_wires_the_workflow_tool():
    kibana = Kibana()
    result = agent_setup.setup(kibana, out=lambda line: None)
    assert result == {"workflow_id": "wf-1", "agent": True, "smoke": 200}
    posted = {c[2]["id"]: c[2] for c in kibana.calls if c[0] == "POST" and c[2] and "id" in c[2]}
    assert set(posted) == {t["id"] for t in agent_setup.TOOLS} | {"quota-auditor"}
    assert posted["quota_run_promise_sweep"]["configuration"]["workflow_id"] == "wf-1"
    assert posted["quota-auditor"]["avatar_symbol"] == "QA" and "Read only" in \
        posted["quota-auditor"]["configuration"]["instructions"]
    again = Kibana(exists=True)
    agent_setup.setup(again, out=lambda line: None)
    assert {c[0] for c in again.calls if "/agent_builder/agents/" in c[1]} == {"GET", "PUT"}
    assert agent_setup.TOOLS[-1]["configuration"]["workflow_id"] is None       # the module's own copy is untouched


def test_converse_is_parsed_like_the_starter_and_the_proxy_says_when_elastic_is_off():
    data = {"conversation_id": "c-1", "response": {"message": "- D-0012 holds Summit"},
            "steps": [{"type": "reasoning"}, {"type": "tool_call", "tool_id": "quota_held_by_reason", "params": {}}]}
    assert agent_setup.parse_converse(data) == {"conversation_id": "c-1", "message": "- D-0012 holds Summit",
                                                "tools": [{"tool": "quota_held_by_reason", "params": {}}]}
    demo = scenarios.served_world()
    try:
        client = TestClient(create_app(Session(demo.world, "acme"), static_dir="no-static"))
        assert client.post("/api/auditor", json={"message": "hi"}).status_code == 503
        assert client.get("/api/elastic/verify").json()["line"] == "Elastic is not configured"
    finally:
        demo.close()
