import json

import pytest

from quota.__main__ import main
from quota.ledger import hashing, invariants, queries

REVENUE = [("2400", 1_000_000, 0), ("4000", 0, 1_000_000)]
AMORTIZATION = [("6110", 50_000, 0), ("1350", 0, 50_000)]
EXPECTED_JES = {
    "JE-0001": [("1200", 12_000_000, 0), ("2400", 0, 12_000_000)],
    "JE-0002": [("1000", 12_000_000, 0), ("1200", 0, 12_000_000)],
    "JE-0003": REVENUE,
    "JE-0004": [("1350", 1_800_000, 0), ("2150", 0, 1_800_000)],
    "JE-0005": AMORTIZATION,
    "JE-0006": [("2150", 1_800_000, 0), ("1000", 0, 1_800_000)],
    "JE-0007": REVENUE,
    "JE-0008": AMORTIZATION,
    "JE-0009": REVENUE,
    "JE-0010": AMORTIZATION,
    "JE-0011": [("2400", 9_000_000, 0), ("1000", 0, 9_000_000)],
    "JE-0012": [("1290", 1_800_000, 0), ("1350", 0, 1_650_000), ("6110", 0, 150_000)],
}
EXPECTED_TB = {"1000": 1_200_000, "1200": 0, "1290": 1_800_000, "1350": 0, "2150": 0, "2400": 0,
               "4000": -3_000_000, "6110": 0}


def _get(demo, table, row_id):
    return queries.as_of(demo.ro, table, row_id, None)


def test_demo_trial_balance(acme_demo):
    tb = queries.trial_balance(acme_demo.ro)
    assert tb == EXPECTED_TB
    assert sum(b for b in tb.values() if b > 0) == -sum(b for b in tb.values() if b < 0) == 3_000_000
    printed = "\n".join(acme_demo.lines)
    assert "Dr $30,000.00 = Cr $30,000.00" in printed


def test_demo_ids_and_amounts(acme_demo):
    ro = acme_demo.ro
    journal = {e["id"]: e for e in queries.journal(ro)}
    assert list(journal) == list(EXPECTED_JES)
    for je_id, expected in EXPECTED_JES.items():
        assert journal[je_id]["status"] == "posted", je_id
        assert [(l["account"], l["debit_cents"], l["credit_cents"]) for l in journal[je_id]["lines"]] == expected

    refusal = _get(acme_demo, "refusals", "R-0001")
    assert (refusal["actor"], refusal["action"], refusal["subject_id"]) == (
        "seat:commissions_analyst", "decide_approval", "A-0002")
    assert json.loads(refusal["reason_codes_json"]) == ["NO_CAPABILITY", "INV5_SELF_APPROVAL", "INV5_WRONG_APPROVER"]

    lines = {cl: _get(acme_demo, "commission_lines", cl) for cl in ("CL-0001", "CL-0002", "CL-0003")}
    assert [(l["booking_quarter"], l["amount_cents"], l["corrects_id"]) for l in lines.values()] == [
        ("2026Q2", 1_200_000, None), ("2026Q1", 1_800_000, "CL-0001"), ("2026Q1", -1_800_000, "CL-0002")]
    assert [queries.line_status(ro, cl) for cl in lines] == ["superseded", "clawed_back", "approved"]

    decisions = {d["id"]: d for d in queries.fetch_all(ro, "SELECT * FROM decisions ORDER BY id")}
    assert [(d["kind"], d["outcome"]) for d in decisions.values()] == [
        ("commission_calc", "drafted"), ("dispute", "held"), ("dispute", "held"), ("escalation", "escalated"),
        ("commission_calc", "drafted"), ("commission_accrual", "drafted"), ("clawback", "escalated")]
    d3 = json.loads(decisions["D-0003"]["output_json"])["result"]
    assert d3["no_new_evidence"] is True
    d1_res = json.loads(decisions["D-0001"]["resolutions_json"])
    assert {d["basis"]: d["effect_cents"] for d in d1_res["booking_date"]["disagreements"]}["crm_close_date"] == 600_000
    assert d1_res["base"]["disagreements"][0]["effect_cents"] == 300_000
    d5 = json.loads(decisions["D-0005"]["output_json"])["result"]
    assert (d5["quarter"], d5["prior_credit_cents"], d5["tier"], d5["amount_cents"]) == (
        "2026Q1", 30_000_000, "accelerator", 1_800_000)
    assert json.loads(decisions["D-0005"]["precedent_ids_json"]) == ["P-0001"]

    approvals = {a["id"]: a for a in queries.fetch_all(ro, "SELECT * FROM approvals ORDER BY id")}
    assert [(a["kind"], a["approver"], a["amount_cents"], a["parent_id"]) for a in approvals.values()] == [
        ("plan_exception", "marcus.lee", 600_000, None), ("plan_exception", "priya.nair", 600_000, "A-0001"),
        ("journal_entry", "priya.nair", 1_800_000, None), ("payroll_file", "priya.nair", 1_800_000, None),
        ("journal_entry", "priya.nair", 1_800_000, None)]
    assert queries.approval_decision(ro, "A-0001")["result"] == "over_limit_routed"
    assert queries.approval_decision(ro, "A-0002")["result"] == "final"

    p1 = _get(acme_demo, "precedents", "P-0001")
    assert json.loads(p1["scope_json"]) == {"deal_id": "acme", "field": "booking_date"}
    assert json.loads(p1["override_json"])["value"] == "2026-03-31"
    assert (json.loads(p1["approvals_json"]), p1["expires_at"]) == (["A-0001", "A-0002"], "2026-06-30")

    schedule = _get(acme_demo, "amortization_schedules", "AS-0001")
    assert (schedule["months"], schedule["start_period"], schedule["status"]) == (36, "2026-04", "active")
    assert {c for _, c in json.loads(schedule["schedule_json"])} == {50_000}
    assert _get(acme_demo, "amortization_schedules", "AS-0001-c")["status"] == "cancelled"
    assert _get(acme_demo, "payroll_files", "PR-2026-05")["total_cents"] == 1_800_000
    churn = queries.fetch_all(acme_demo.ro, "SELECT * FROM events WHERE type = 'customer.churned'")
    assert [(e["release_at"], json.loads(e["payload_json"])["refund_cents"]) for e in churn] == [
        ("2026-06-28T17:00:00Z", 9_000_000)]


def test_demo_output_lines(acme_demo):
    printed = "\n".join(acme_demo.lines)
    assert ("[2026-04-27T16:00:00Z] seat:commissions_analyst   DECISION  D-0001 commission_calc acme/jordan.kim: "
            "2026Q2 base tier $12,000.00 (booking=countersignature R-BOOKING-01; base=contract $120,000.00 over "
            "CRM $150,000.00 R-AMOUNT-01)") in printed
    assert ("[2026-04-28T15:01:00Z] seat:commissions_analyst   REFUSED   R-0001 decide_approval A-0002: "
            "NO_CAPABILITY, INV5_SELF_APPROVAL, INV5_WRONG_APPROVER") in printed
    assert printed.count("Difference: $12,000.00 vs $18,000.00 = $6,000.00") == 2
    assert "nothing new was provided since D-0002" in printed
    for expected in ("  1. Deferred revenue        $90,000.00 -> $0.00 (refund JE-0011)",
                     "  2. Cash                    -$90,000.00 (JE-0011)",
                     "  3. Acme customer balance   $0.00, contract terminated 2026-06-30",
                     "  4. Revenue forecast        -$10,000.00/month from 2026-07 (-$330,000.00 remaining contract)",
                     "  5. Jordan commission       -$18,000.00 clawback (CL-0003, JE-0012)",
                     "  6. Deferred commission     $16,500.00 written off (JE-0012)",
                     "  7. Commission expense      -$1,500.00 amortization reversed (JE-0012)",
                     "  8. Jordan Q1 attainment    140.00% -> 100.00% (report only; 2026-Q1 periods locked)",
                     "INVARIANTS  INV1 OK  INV2 OK  INV4 OK  INV5 OK  INV6 OK  INV7 OK",
                     "REPLAY      D-0001 MATCH  D-0005 MATCH  D-0007 MATCH"):
        assert expected in acme_demo.lines


def test_demo_invariants_and_chain(acme_demo):
    assert all(not v for v in invariants.check_all(acme_demo.ro).values())
    assert hashing.verify_chain(acme_demo.ro) == (True, None)
    assert f"CHAIN       OK ({hashing.chain_length(acme_demo.ro)} rows)" in acme_demo.lines
    assert acme_demo.ok


def test_cli_against_a_file_db(tmp_path, capsys):
    path = str(tmp_path / "acme.db")
    assert main(["demo", "acme", "--db", path]) == 0
    with pytest.raises(FileExistsError):
        main(["demo", "acme", "--db", path])
    capsys.readouterr()

    assert main(["verify", "--db", path]) == 0
    out = capsys.readouterr().out
    assert "INVARIANTS  INV1 OK" in out and "CHAIN       OK" in out

    assert main(["replay", "D-0005", "--db", path]) == 0
    assert capsys.readouterr().out.strip().endswith("D-0005 MATCH")

    assert main(["tb", "--db", path, "--as-of", "2026-04-30T23:59:59Z"]) == 0
    out = capsys.readouterr().out
    assert "1350 Deferred commissions" in out and "$17,500.00 Dr" in out
