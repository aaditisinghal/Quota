import json

import pytest

from conftest import ACCOUNTANT, ANALYST, balanced
from quota.clock import SimClock
from quota.ledger import hashing, queries
from quota.ledger.write_path import Refused, WritePath


def test_unbalanced_entry_refused(ledger, wp, ro):
    decision_id = ledger.decision(ACCOUNTANT)
    lines = [{"account": "1350", "debit_cents": 10_000, "credit_cents": 0},
             {"account": "2150", "debit_cents": 0, "credit_cents": 9_000}]
    with pytest.raises(Refused) as exc:
        wp.draft_entry(ACCOUNTANT, "2026-04-30", "commission_accrual: test", lines, decision_id=decision_id)
    assert exc.value.reason_codes == ["INV1_UNBALANCED"]
    refusal = queries.as_of(ro, "refusals", exc.value.refusal_id, None)
    assert json.loads(refusal["reason_codes_json"]) == ["INV1_UNBALANCED"]
    link = ro.execute("SELECT * FROM chain WHERE kind = 'refusals' AND ref_id = ?", (refusal["id"],)).fetchone()
    assert link is not None
    assert ro.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0


def test_one_sided_line_refused(ledger, wp):
    decision_id = ledger.decision(ACCOUNTANT)
    lines = [{"account": "1350", "debit_cents": 100, "credit_cents": 100},
             {"account": "2150", "debit_cents": 0, "credit_cents": 0}]
    with pytest.raises(Refused) as exc:
        wp.draft_entry(ACCOUNTANT, "2026-04-30", "commission_accrual: test", lines, decision_id=decision_id)
    assert exc.value.reason_codes == ["INV1_UNBALANCED"]


def test_locked_period_refused(ledger, wp):
    decision_id = ledger.decision(ACCOUNTANT)
    wp.lock_period("priya.nair", "2026-03")
    with pytest.raises(Refused) as exc:
        wp.draft_entry(ACCOUNTANT, "2026-03-31", "commission_accrual: test", balanced(), decision_id=decision_id)
    assert exc.value.reason_codes == ["INV2_PERIOD_LOCKED"]


def test_entry_without_decision_refused(ledger, wp):
    with pytest.raises(Refused) as exc:
        wp.draft_entry(ACCOUNTANT, "2026-04-30", "commission_accrual: test", balanced(), decision_id=None)
    assert exc.value.reason_codes == ["INV4_NO_DECISION"]


def test_decision_without_inputs_refused(ledger, wp):
    with pytest.raises(Refused) as exc:
        wp.record_decision(ANALYST, "commission_calc", "acme", [], {}, [], {}, "drafted", "no inputs")
    assert exc.value.reason_codes == ["INV4_NO_DECISION"]


def test_self_approval_refused(ledger, wp):
    approval_id = wp.request_approval("seed", "journal_entry", "JE-9999", 100, approver="seed")
    with pytest.raises(Refused) as exc:
        wp.decide_approval("seed", approval_id, "approved", "approving my own request")
    assert exc.value.reason_codes == ["INV5_SELF_APPROVAL"]


def test_wrong_approver_refused(ledger, wp):
    approval_id = wp.request_approval(ANALYST, "plan_exception", "acme", 100_000, approver="marcus.lee")
    with pytest.raises(Refused) as exc:
        wp.decide_approval("priya.nair", approval_id, "approved", "not my approval")
    assert exc.value.reason_codes == ["INV5_WRONG_APPROVER"]


def test_every_violated_code_is_collected(ledger, wp):
    approval_id = wp.request_approval(ANALYST, "plan_exception", "acme", 600_000, approver="priya.nair")
    with pytest.raises(Refused) as exc:
        wp.decide_approval(ANALYST, approval_id, "approved", "seat approving")
    assert exc.value.reason_codes == ["NO_CAPABILITY", "INV5_SELF_APPROVAL", "INV5_WRONG_APPROVER"]


def test_seat_entry_needs_approval(ledger, wp, ro):
    decision_id = ledger.decision(ACCOUNTANT)
    je_id = wp.draft_entry(ACCOUNTANT, "2026-04-30", "commission_accrual: test", balanced(),
                           decision_id=decision_id)
    with pytest.raises(Refused) as exc:
        wp.post_entry(ACCOUNTANT, je_id)
    assert exc.value.reason_codes == ["INV5_NOT_APPROVED"]

    approval_id = wp.request_approval(ACCOUNTANT, "journal_entry", je_id, 1_800_000, approver="priya.nair")
    assert wp.decide_approval("priya.nair", approval_id, "approved", "ok")["result"] == "final"
    wp.post_entry(ACCOUNTANT, je_id)
    assert queries.je_status(ro, je_id) == "posted"


def test_splits_must_sum(ledger, wp):
    ledger.deal("oak", splits=(("jordan.kim", 7000), ("sam.ruiz", 4000)))
    decision_id = ledger.decision(ANALYST, subject="oak")
    with pytest.raises(Refused) as exc:
        ledger.line(decision_id, deal_id="oak")
    assert exc.value.reason_codes == ["INV7_SPLITS"]


def test_payroll_total_must_match(ledger, wp):
    decision_id = ledger.decision(ANALYST)
    line_id = ledger.line(decision_id)
    with pytest.raises(Refused) as exc:
        wp.create_payroll_file(ANALYST, "2026-05", [line_id])
    assert exc.value.reason_codes == ["INV6_PAYROLL_MISMATCH"]

    wp.set_line_status("priya.nair", line_id, "approved")
    file_id = wp.create_payroll_file(ANALYST, "2026-05", [line_id])
    approval_id = wp.request_approval(ANALYST, "payroll_file", file_id, 1_200_000, approver="priya.nair")
    wp.decide_approval("priya.nair", approval_id, "approved", "ok")
    wp.set_line_status(ANALYST, line_id, "clawed_back")
    with pytest.raises(Refused) as exc:
        wp.release_payroll("dana.ortiz", file_id)
    assert exc.value.reason_codes == ["INV6_PAYROLL_MISMATCH"]


def test_payroll_release_pays_lines(ledger, wp, ro):
    line_id = ledger.line(ledger.decision(ANALYST))
    wp.set_line_status("priya.nair", line_id, "approved")
    file_id = wp.create_payroll_file(ANALYST, "2026-05", [line_id])
    with pytest.raises(Refused) as exc:
        wp.release_payroll("dana.ortiz", file_id)
    assert exc.value.reason_codes == ["INV5_NOT_APPROVED"]
    approval_id = wp.request_approval(ANALYST, "payroll_file", file_id, 1_200_000, approver="priya.nair")
    wp.decide_approval("priya.nair", approval_id, "approved", "ok")
    je_id = wp.release_payroll("dana.ortiz", file_id)
    assert queries.je_status(ro, je_id) == "posted"
    assert queries.line_status(ro, line_id) == "paid"
    assert queries.trial_balance(ro) == {"1000": -1_200_000, "2150": 1_200_000}


def test_no_capability_refused(ledger, wp):
    with pytest.raises(Refused) as exc:
        wp.lock_period("jordan.kim", "2026-04")
    assert exc.value.reason_codes == ["NO_CAPABILITY"]


def test_clock_backwards_refused(ledger, wp, conn):
    with pytest.raises(ValueError):
        ledger.clock.advance_to("2026-03-01T00:00:00Z")

    ledger.at("2026-04-10T00:00:00Z")
    wp.lock_period("priya.nair", "2026-01")
    stale = WritePath(conn, SimClock("2026-04-09T00:00:00Z"), wp.principals)
    with pytest.raises(Refused) as exc:
        stale.lock_period("priya.nair", "2026-02")
    assert exc.value.reason_codes == ["CLOCK_BACKWARDS"]
    assert not queries.period_locked(conn, "2026-02")
    assert hashing.verify_chain(conn) == (True, None)


def test_as_of_returns_old_version(ledger, wp, ro):
    v1 = ledger.deal("acme", amount_cents=15_000_000)
    ledger.at("2026-04-05T00:00:00Z")
    v2 = ledger.deal("acme", amount_cents=12_000_000)
    assert (v1["version"], v2["version"]) == (1, 2)
    assert queries.as_of(ro, "deals", "acme", "2026-04-01T00:00:00Z")["crm_amount_cents"] == 15_000_000
    assert queries.as_of(ro, "deals", "acme", "2026-04-04T23:59:59Z")["version"] == 1
    assert queries.as_of(ro, "deals", "acme", "2026-04-05T00:00:00Z")["version"] == 2
    assert queries.as_of(ro, "deals", "acme", "2025-12-31T00:00:00Z") is None


def test_readonly_connection_cannot_write(ledger, ro):
    with pytest.raises(Exception):
        ro.execute("INSERT INTO periods (period, status, actor, known_at) VALUES ('2026-05', 'locked', 'x', 'y')")
