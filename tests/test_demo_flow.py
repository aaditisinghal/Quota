"""DEMO_FLOW_TASKS D1 (confidence) and the read model behind D2-D5."""

import pytest
from fastapi.testclient import TestClient

from quota.api.app import create_app
from quota.api.session import Session
from quota.engine import confidence
from quota.ledger import queries
from quota.search import auditor_world
from quota.sim import demo_acme


@pytest.fixture(scope="module")
def world():
    demo = auditor_world.build()          # the served Acme story: dispute, over-limit exception, Riley's side letter
    yield demo
    demo.close()


@pytest.fixture(scope="module")
def client(world):
    return TestClient(create_app(Session(world.world, "acme"), static_dir="no-static"))


def test_acme_crm_versus_contract_settled_by_rules_is_80_cleared_with_a_note(client):
    c = client.get("/api/lines/CL-0001/confidence").json()
    assert (c["score"], c["band"]) == (80, "note")
    low = {f["name"]: f["value"] for f in c["factors"] if f["value"] < 1}
    assert low == {"booking_date": 0.8, "amount": 0.8, "extraction": 0.9}
    assert "R-BOOKING-01" in next(f["reason"] for f in c["factors"] if f["name"] == "booking_date")


def test_the_summit_side_letter_is_40_or_less_and_held(client):
    c = client.get("/api/lines/CL-0004/confidence").json()
    assert c["score"] <= 40 and c["band"] == "held" and c["stored"]["score"] > 40      # learned after booking
    assert next(f for f in c["factors"] if f["name"] == "integrity")["reason"].startswith("off-contract promise")


def test_a_110_percent_split_is_zero_and_held():
    res = {"splits": {"value": [{"rep": "a", "bps": 6000}, {"rep": "b", "bps": 5000}], "source": "deals:x:v1"}}
    c = confidence.stored(res, [])
    assert (c["score"], c["band"]) == (0, "held")
    assert next(f for f in c["factors"] if f["name"] == "owner_splits")["reason"] == "splits add to 110%, not 100%"


def test_a_clean_deal_is_100_and_replay_reproduces_the_score():
    demo = demo_acme.run(out=lambda line: None)
    try:
        d = queries.as_of(demo.ro, "decisions", "D-OB-cedar", None)
        assert queries.loads(d["output_json"])["confidence"]["score"] == 100
        ok, stored, recomputed = queries.replay_decision(demo.ro, "D-OB-cedar")
        assert ok and stored["confidence"] == recomputed["confidence"]
    finally:
        demo.close()


def test_calibration_lists_each_source_with_agrees_or_disagrees_and_the_rule(client):
    cal = client.get("/api/lines/CL-0001/calibration").json()
    rows = {(e["source"], e["field"]): e for e in cal["evidence"]}
    assert rows[("CRM", "amount")]["label"] == "disagrees" and rows[("Contract", "ACV")]["label"] == "agrees"
    assert rows[("E-signature", "countersigned")]["label"] == "agrees"
    assert {r["field"]: r["rule"] for r in cal["rules"]}["booking_date"] == "R-BOOKING-01"
    assert cal["amount"]["total_cents"] == 1_200_000 and cal["confidence"]["score"] == 80


def test_reps_board_sorts_held_first_and_labels_the_pipeline_simulated(client):
    board = client.get("/api/reps").json()
    assert [r["status"] for r in board] == sorted((r["status"] for r in board), key=["Held", "Note", "Cleared"].index)
    riley = next(r for r in board if r["rep"] == "riley.shah")
    assert riley["status"] == "Held" and riley["lowest_confidence"] <= 40 and riley["pipeline"]["simulated"] is True
    detail = client.get("/api/reps/riley.shah").json()
    assert all(d["simulated"] and not d["earned"] for d in detail["pipeline"]["deals"])
    assert {c["id"] for c in detail["contracts"]} >= {"orbit", "summit"}


def test_contract_card_has_recipients_and_a_split_bar(client):
    orbit = client.get("/api/contracts/orbit").json()
    assert [(r["who"], r["share_bps"]) for r in orbit["split_bar"]] == [("alex.chen", 6000), ("riley.shah", 4000)]
    assert sum(r["share_bps"] for r in orbit["split_bar"]) == 10000
    assert any(r["kind"] == "manager_override" and r["who"] == "morgan.diaz" for r in orbit["recipients"])
    assert client.get("/api/contracts/nope").status_code == 404


def test_needs_you_groups_by_person_and_shows_the_chain(client):
    ny = client.get("/api/needs-you").json()
    people = {p["person"]: p for p in ny["people"]}
    priya = {i["kind"]: i for i in people["priya.nair"]["items"]}
    assert "commitment_review" in priya and "plan_exception" in priya
    assert not priya["plan_exception"]["question"].startswith("Daily digest")
    assert any("R-0001 refused" in e["text"] for e in priya["plan_exception"]["chain"])
    home = client.get("/api/home").json()
    assert home["needs_you"] == ny["count"] and home["held_cents"] > 0


def test_sources_rail_and_raw_rows(client):
    kinds = {s["kind"]: s for s in client.get("/api/sources").json()}
    assert {"contracts", "messages", "crm", "hr", "stripe"} <= set(kinds)
    rows = client.get("/api/sources/messages/rows", params={"q": "year two"}).json()
    assert rows["rows"] and rows["rows"][0]["highlight"]                    # the side letter's quote is highlighted
    assert client.get("/api/sources/nope/rows").status_code == 404


def test_the_cfo_presenter_script_restores_the_morning_and_june(tmp_path):
    from quota.api.session import acme_session
    session = acme_session(cast=False)
    session.checkpoint_dir = tmp_path
    client = TestClient(create_app(session, static_dir="no-static"))
    try:
        steps = client.get("/demo/steps", params={"script": "cfo"}).json()
        assert [s["checkpoint"] for s in steps][:6] == ["cfo_morning"] * 6 and steps[6]["checkpoint"] == "cp_end"
        morning = client.post("/demo/step", json={"n": 0, "script": "cfo"}).json()
        assert morning["clock"] == "2026-05-15T18:00:00Z" and client.get("/state").json()["scenario"] == "cfo_morning"
        ny = {i["id"]: i for p in client.get("/api/needs-you").json()["people"] for i in p["items"]}
        assert ny["A-0002"]["person"] == "priya.nair" and any(i["kind"] == "commitment_review" for i in ny.values())
        assert client.get("/api/lines/CL-0004/confidence").json()["score"] <= 40
        last = client.post("/demo/step", json={"n": 8, "script": "cfo"}).json()
        assert last["checkpoint"] == "cp_end" and client.get("/api/needs-you").json()["count"] == 0
        assert client.get("/demo/steps", params={"script": "nope"}).status_code == 404
        assert len(client.get("/demo/steps").json()) == 6                       # the default script is unchanged
    finally:
        session.close()


def test_an_amendment_is_a_child_card_with_incremental_commission_only(client):
    orbit = client.get("/api/contracts/orbit").json()
    assert orbit["acv_cents"] == 8_000_000 and [l["id"] for l in orbit["lines"]] == ["CL-0002", "CL-0003"]
    child = orbit["children"][0]
    assert (child["id"], child["version"], child["acv_cents"]) == ("orbit+v2", 2, 10_000_000)
    assert child["incremental_commission_cents"] == sum(r["amount_cents"] for r in child["recipients"]) == 200_000
    assert [(r["who"], r["line"]) for r in child["recipients"]] == [("alex.chen", "CL-0006"), ("riley.shah", "CL-0007")]
