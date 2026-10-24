"""Phase 4 read endpoints over a running world (section 14)."""

import json

import pytest
from fastapi.testclient import TestClient

from quota.api.app import create_app
from quota.api.session import ACME_START, Session, acme_session
from quota.sim import demo_acme


@pytest.fixture(scope="module")
def paused():
    session = acme_session()
    yield TestClient(create_app(session, static_dir="no-static"))
    session.close()


@pytest.fixture(scope="module")
def finished():
    """The whole scripted Acme story, through the churn and clawback."""
    demo = demo_acme.run(out=lambda line: None)
    yield TestClient(create_app(Session(demo.world, "acme"), static_dir="no-static"))
    demo.close()


def test_the_acme_session_pauses_after_the_april_commission_run(paused):
    state = paused.get("/state").json()
    assert state["clock"] == ACME_START
    assert {"jordan.kim", "marcus.lee", "priya.nair", "dana.ortiz"} <= {p["id"] for p in state["principals"]}
    assert all(not p["id"].startswith(("seat:", "system")) for p in state["principals"])
    upcoming = {e["type"] for e in state["upcoming"]}
    assert "job.due" in upcoming and not upcoming & {"customer.churned", "refund.issued"}   # the judges inject those
    assert state["pending_approvals"] == []


def test_timeline_returns_printed_line_objects_after_since(paused):
    items = paused.get("/timeline").json()["items"]
    assert [i["seq"] for i in items] == list(range(1, len(items) + 1))
    last = items[-1]
    assert last["kind"] == "ENTRY" and "CL-0001" in last["refs"] and last["line"].startswith(f"[{last['clock']}]")
    later = paused.get(f"/timeline?since={len(items) - 2}").json()["items"]
    assert [i["seq"] for i in later] == [len(items) - 1, len(items)]


def test_refusals_carry_their_reason_codes(finished):
    refused = [i for i in finished.get("/timeline").json()["items"] if i["kind"] == "REFUSED"]
    assert refused and refused[0]["codes"] == ["NO_CAPABILITY", "INV5_SELF_APPROVAL", "INV5_WRONG_APPROVER"]
    graph = finished.get(f"/trace/refusal/{refused[0]['refs'][-1]}").json()
    assert graph["nodes"][0]["codes"] == refused[0]["codes"]


def test_trace_goes_from_a_number_to_its_decision_resolutions_and_highlighted_sources(paused):
    graph = paused.get("/trace/line/CL-0001").json()
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert nodes["CL-0001"]["amount_cents"] == 1_200_000
    decision = nodes["D-0001"]
    assert decision["replay"] == "MATCH" and decision["fn"] == "calc_commission"
    assert nodes["D-0001:base"]["value"] == 12_000_000 and nodes["D-0001:base"]["rule"] == "R-AMOUNT-01"
    contract = nodes["contracts:acme:v1"]
    assert {"field": "acv_cents", "role": "chosen", "resolves": "base"} in contract["highlight"]
    assert {"field": "countersigned_at", "role": "chosen", "resolves": "booking_date"} in contract["highlight"]
    crm = nodes["deals:acme:v1"]
    assert {"field": "crm_close_date", "role": "alternative", "resolves": "booking_date"} in crm["highlight"]
    assert crm["snippet"]["crm_amount_cents"] == 15_000_000
    edges = {(e["from"], e["to"], e["label"]) for e in graph["edges"]}
    assert ("D-0001:base", "contracts:acme:v1", "chosen") in edges
    assert ("D-0001:base", "deals:acme:v1", "alternative") in edges
    assert paused.get("/trace/CL-0001").json()["nodes"] == graph["nodes"]          # kind read from the id


def test_trace_as_of_shows_what_was_known_then(finished):
    assert finished.get("/trace/line/CL-0002?as_of=2026-04-28T00:00:00Z").json()["known"] is False
    may = {n["id"]: n for n in finished.get("/trace/line/CL-0001?as_of=2026-05-01T00:00:00Z").json()["nodes"]}
    end = {n["id"]: n for n in finished.get("/trace/line/CL-0001").json()["nodes"]}
    assert may["CL-0002"]["status"] == "approved" and end["CL-0002"]["status"] == "clawed_back"
    assert finished.get("/trace/line/CL-9999").status_code == 404
    assert finished.get("/trace/widget/X-1").status_code == 404


def test_trace_follows_approvals_precedents_and_payroll(finished):
    approval = finished.get("/trace/approval/A-0002").json()
    ids = {n["id"] for n in approval["nodes"]}
    assert {"A-0001", "M-0007", "P-0001"} <= ids
    a2 = next(n for n in approval["nodes"] if n["id"] == "A-0002")
    assert (a2["approver"], a2["decision"]) == ("priya.nair", "approved")
    entry = {n["id"]: n for n in finished.get("/trace/je/JE-0004").json()["nodes"]}
    assert {"D-0006", "CL-0002", "A-0003"} <= set(entry) and entry["JE-0004"]["status"] == "posted"
    assert sum(l["debit_cents"] for l in entry["JE-0004"]["lines"]) == entry["JE-0004"]["amount_cents"]
    payroll = {n["id"] for n in finished.get("/trace/payroll/PR-2026-05").json()["nodes"]}
    assert {"CL-0002", "A-0004"} <= payroll
    invoice = {n["id"]: n for n in finished.get("/trace/je/JE-0001").json()["nodes"]}
    assert invoice["billing:acme-inv-1"]["external_ref"] == "acme-inv-1"


def test_ledger_health_after_the_whole_story(finished):
    health = finished.get("/ledger/health").json()
    assert all(i["ok"] for i in health["invariants"]) and health["chain"]["ok"]
    assert {i["name"] for i in health["invariants"]} == {"INV1", "INV2", "INV4", "INV5", "INV6", "INV7", "INV8"}
    tb = health["trial_balance"]
    assert tb["debits_cents"] == tb["credits_cents"] == 3_000_000
    balances = {a["account"]: a["balance_cents"] for a in tb["accounts"]}
    assert balances["1290"] == 1_800_000
    assert all(t["ok"] for t in health["tie_outs"])
    assert {p["period"]: p["status"] for p in health["periods"]}["2026-03"] == "locked"


def test_today_lists_work_done_waiting_and_held(paused, finished):
    today = finished.get("/today").json()
    assert today["day"] == "2026-06-30"
    assert [d["id"] for d in today["done"]] == ["D-0007"]
    assert [e["id"] for e in today["posted"]][-1] == "JE-0012"
    assert today["held"] == [] and today["waiting"] == []          # the March 31 disputes were settled by P-0001
    assert paused.get("/today").json()["done"][0]["id"] == "D-0001"


def test_scorecard_serves_the_eval_output(tmp_path):
    card = {"seed": 7, "planted": 40, "correct": 40, "recall": 1.0}
    (tmp_path / "seed-7").mkdir()
    (tmp_path / "seed-7" / "scorecard.json").write_text(json.dumps(card), encoding="utf-8")
    session = acme_session()
    try:
        client = TestClient(create_app(session, runs_dir=tmp_path, static_dir="no-static"))
        got = client.get("/scorecard?seed=7").json()
        assert got["recall"] == 1.0 and got["source"].endswith("scorecard.json")
    finally:
        session.close()


def test_messages_filter_by_principal(finished):
    jordan = finished.get("/messages?principal=jordan.kim").json()
    assert jordan and all("jordan.kim" in (m["author"], m["to"]) for m in jordan)
    assert len(finished.get("/messages").json()) > len(jordan)
