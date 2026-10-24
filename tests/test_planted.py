"""Phase 3: what the seats do with each planted error type (one instance of each, one world)."""

import json

import pytest

from quota.agent.commissions_analyst import ANALYST
from quota.ledger import hashing, invariants, queries
from quota.sim.generator import Fernhill

ORDER = ["E10", "E10", "E05", "E09", "E14", "E01", "E02", "E03", "E04", "E06", "E07", "E08", "E11", "E12", "E13",
         "E15", "E16"]


@pytest.fixture(scope="module")
def planted():
    fernhill = Fernhill(7, reps=16)
    for error in ORDER:
        getattr(fernhill, f"plant_{error}")()
    world = fernhill.build()
    world.run_until(world.end_ts)
    cases = {}
    for case in fernhill.answer_key:
        cases.setdefault(case["type"], case)
    yield world, cases
    world.close()


def _decisions(ro, kind, subject=None):
    sql, params = "SELECT * FROM decisions WHERE kind = ?", [kind]
    if subject:
        sql += " AND subject_id = ?"
        params.append(subject)
    return queries.fetch_all(ro, sql, tuple(params))


def _lines(ro, deal_id):
    return [{**l, "status": queries.line_status(ro, l["id"])}
            for l in queries.fetch_all(ro, "SELECT * FROM commission_lines WHERE deal_id = ? ORDER BY id", (deal_id,))]


def _asked(ro, who, needle):
    return any(needle in m["text"] for m in queries.fetch_all(
        ro, "SELECT text FROM messages WHERE author = ? AND to_principal = ?", (ANALYST, who)))


def test_the_world_stays_clean(planted):
    world, _ = planted
    ro = world.ro
    assert all(not v for v in invariants.check_all(ro).values())
    assert hashing.verify_chain(ro) == (True, None)
    assert all(queries.replay_decision(ro, d["id"])[0] for d in queries.fetch_all(ro, "SELECT id FROM decisions"))


def test_E01_E06_pay_on_contract_acv(planted):
    world, cases = planted
    for error in ("E01", "E06"):
        case = cases[error]
        calc = _decisions(world.ro, "commission_calc", f"{case['deal_id']}/{case['rep_id']}")[0]
        base = json.loads(calc["resolutions_json"])["base"]
        contract = queries.as_of(world.ro, "contracts", case["deal_id"], None)
        assert (base["basis"], base["value"]) == ("contract", contract["acv_cents"])
        assert base["disagreements"][0]["basis"] == "crm" and base["disagreements"][0]["value"] > contract["acv_cents"]


def test_E02_bad_splits_hold_both_lines_and_ask_the_vp(planted):
    world, cases = planted
    case = cases["E02"]
    assert [d["outcome"] for d in _decisions(world.ro, "splits_check", case["deal_id"])] == ["held"]
    assert _lines(world.ro, case["deal_id"]) == []
    assert _asked(world.ro, "marcus.lee", case["deal_id"])


def test_E03_dispute_is_held(planted):
    world, cases = planted
    case = cases["E03"]
    line = _lines(world.ro, case["deal_id"])[0]
    assert line["booking_quarter"] == "2026Q2"
    assert [d["outcome"] for d in _decisions(world.ro, "dispute", line["id"])] == ["held"]


def test_E04_backdating_is_flagged_and_the_booking_stands(planted):
    world, cases = planted
    case = cases["E04"]
    flag = _decisions(world.ro, "backdating_check", case["deal_id"])
    assert [d["outcome"] for d in flag] == ["escalated"]
    assert json.loads(flag[0]["output_json"])["result"]["close_date"] == "2026-03-30"
    assert [l["booking_quarter"] for l in _lines(world.ro, case["deal_id"])] == ["2026Q2"]
    assert _asked(world.ro, "marcus.lee", case["deal_id"]) and _asked(world.ro, "priya.nair", case["deal_id"])


def test_E05_a_promise_without_authority_goes_to_the_owner(planted):
    world, cases = planted
    case = cases["E05"]
    flags = [d for d in _decisions(world.ro, "authority_check")
             if queries.as_of(world.ro, "messages", d["subject_id"], None)["author"] == case["rep_id"]]
    assert [d["outcome"] for d in flags] == ["escalated"]
    assert _asked(world.ro, "marcus.lee", "SPIF")


def test_E07_late_customer_holds_the_payout(planted):
    world, cases = planted
    case = cases["E07"]
    assert [d["outcome"] for d in _decisions(world.ro, "collection_hold", case["deal_id"])] == ["held"]
    assert [l["status"] for l in _lines(world.ro, case["deal_id"])] == ["approved"]     # earned, not paid
    assert _asked(world.ro, case["rep_id"], "collection hold")


def test_E08_churn_in_the_window_claws_back(planted):
    world, cases = planted
    case = cases["E08"]
    lines = _lines(world.ro, case["deal_id"])
    assert sum(l["amount_cents"] for l in lines) == 0
    original = lines[0]
    assert [d["outcome"] for d in _decisions(world.ro, "clawback", original["id"])] == ["escalated"]


def test_E09_terminated_rep_is_paid_what_the_signed_plan_says(planted):
    world, cases = planted
    case = cases["E09"]
    review = _decisions(world.ro, "termination_review", case["rep_id"])
    assert [d["outcome"] for d in review] == ["posted"]
    assert [l["status"] for l in _lines(world.ro, case["deal_id"])] == ["paid"]
    assert _asked(world.ro, "priya.nair", "Policy flag")


def test_E10_half_done_territory_move_holds_the_rest(planted):
    world, cases = planted
    case = cases["E10"]
    for deal_id in case["deal_ids"]:
        assert [d["outcome"] for d in _decisions(world.ro, "territory_check", deal_id)] == ["held"]
        assert _lines(world.ro, deal_id) == []
    moved = json.loads(_decisions(world.ro, "territory_check", case["deal_id"])[0]["output_json"])["result"]["moved"]
    assert moved and all(l["rep_id"] == case["moved_to"] for d in moved for l in _lines(world.ro, d))
    assert _asked(world.ro, "marcus.lee", "territory move")


def test_E11_one_owner_keeps_the_credit(planted):
    world, cases = planted
    case = cases["E11"]
    lines = _lines(world.ro, case["deal_id"])
    assert [l["rep_id"] for l in lines] == [case["rep_id"]]
    calc = _decisions(world.ro, "commission_calc", f"{case['deal_id']}/{case['rep_id']}")[0]
    assert json.loads(calc["resolutions_json"])["splits"]["basis"] == "owner_of_record"


def test_E12_amendment_pays_the_increase_once(planted):
    world, cases = planted
    case = cases["E12"]
    increment = _decisions(world.ro, "commission_calc", f"{case['amendment_key']}/{case['rep_id']}")
    assert len(increment) == 1
    lines = _lines(world.ro, case["deal_id"])
    assert len(lines) == 2 and lines[1]["booking_quarter"] == "2026Q2" and lines[1]["amount_cents"] > 0


def test_E13_duplicate_webhook_counts_once(planted):
    world, cases = planted
    case = cases["E13"]
    payments = [e for e in queries.journal(world.ro) if e["source_ref"] == f"py_{case['deal_id']}"]
    assert len(payments) == 1


def test_E14_unsigned_draft_is_flagged_and_never_pays(planted):
    world, cases = planted
    case = cases["E14"]
    assert [d["outcome"] for d in _decisions(world.ro, "plan_drift", case["rep_id"])] == ["escalated"]
    calc = _decisions(world.ro, "commission_calc", f"{case['deal_id']}/{case['rep_id']}")[0]
    assert json.loads(calc["output_json"])["args"]["plan"]["version"] == 3


def test_E15_voided_deal_is_reversed_and_stops_earning_revenue(planted):
    world, cases = planted
    case = cases["E15"]
    lines = _lines(world.ro, case["deal_id"])
    assert sum(l["amount_cents"] for l in lines) == 0
    assert [d["outcome"] for d in _decisions(world.ro, "void_reversal", lines[0]["id"])] == ["drafted"]
    voided_at = queries.fetch_one(world.ro, "SELECT known_at FROM deals WHERE id = ? AND stage = 'voided'",
                                  (case["deal_id"],))["known_at"]
    revenue = [e for e in queries.journal(world.ro) if e["source_ref"] and e["source_ref"].startswith(
        f"revenue:{case['deal_id']}:")]
    assert revenue and all(e["known_at"] < voided_at for e in revenue)


def test_E16_edited_payroll_export_is_blocked(planted):
    world, cases = planted
    case = cases["E16"]
    line = _lines(world.ro, case["deal_id"])[0]
    refusal = queries.fetch_one(world.ro, "SELECT * FROM refusals WHERE action = 'release_payroll'")
    assert refusal["actor"] == "dana.ortiz" and f"{line['id']} (export $" in refusal["detail"]
    assert json.loads(refusal["reason_codes_json"]) == ["INV6_PAYROLL_MISMATCH"]
    assert line["status"] == "paid"            # the next (correct) release pays it
