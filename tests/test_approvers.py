"""Phase 2 approver behavior: digest, payroll urgency, silence escalates (never approves), rejection holds."""

import pytest

from quota import clock as clk
from quota.agent.commissions_analyst import ANALYST
from quota.ledger import queries
from quota.ledger.write_path import Refused
from quota.sim import demo_acme

SILENT = dict(out=lambda line: None)


def _messages_to(ro, who):
    return queries.fetch_all(ro, "SELECT * FROM messages WHERE to_principal = ? AND author = ? ORDER BY id",
                             (who, ANALYST))


def test_business_days_skip_weekends():
    assert clk.add_business_days("2026-04-28T15:00:00Z", 2) == "2026-04-30T15:00:00Z"   # Tue -> Thu
    assert clk.add_business_days("2026-04-30T23:05:00Z", 2) == "2026-05-04T23:05:00Z"   # Thu -> Mon
    assert clk.add_business_days("2026-05-01T09:00:00Z", 1) == "2026-05-04T09:00:00Z"   # Fri -> Mon


def test_daily_digest_and_payroll_urgency_in_the_demo(acme_demo):
    to_priya = _messages_to(acme_demo.ro, "priya.nair")
    digests = [m for m in to_priya if m["text"].startswith("Daily digest")]
    assert [(m["sent_at"], "A-0002" in m["text"]) for m in digests] == [("2026-04-29T09:00:00Z", True)]
    urgent = [m for m in to_priya if "blocks payroll" in m["text"]]
    assert [(m["sent_at"], "A-0004" in m["text"]) for m in urgent] == [("2026-05-14T17:00:00Z", True)]


def _demo(**script_changes):
    demo = demo_acme.AcmeDemo(**SILENT)
    for key, value in script_changes.items():
        if value is None:
            demo.world.people.responses.pop(key)
        else:
            demo.world.people.responses[key] = value
    demo_acme.seed_acme.seed(demo.world.wp, demo.world.clock, demo.world.emit)
    return demo


def test_silence_escalates_to_the_backup_and_never_approves():
    demo = _demo(**{"A-0001": None})                        # Marcus never answers
    demo.world.run_until("2026-04-30T16:00:00Z")
    ro = demo.ro
    assert queries.approval_decision(ro, "A-0001") is None   # silence is not approval
    backup = queries.as_of(ro, "approvals", "A-0002", None)
    assert (backup["approver"], backup["parent_id"], backup["subject_id"], backup["known_at"]) == (
        "priya.nair", None, "M-0007", "2026-04-30T15:00:00Z")
    to_marcus = [m["text"] for m in _messages_to(ro, "marcus.lee")]
    assert sum(t.startswith("Daily digest") for t in to_marcus) == 2          # 04-29 and 04-30 at 09:00
    assert any("now also with Priya Nair as A-0002" in t for t in to_marcus)
    # Priya's scripted answer to A-0002 grants the exception through the backup approval alone.
    precedent = queries.as_of(ro, "precedents", "P-0001", None)
    assert queries.loads(precedent["approvals_json"]) == ["A-0002"]
    demo.close()


def test_rejection_holds_the_line_and_the_same_proposal_cannot_come_back():
    demo = _demo(**{"A-0001": {"at": "2026-04-28T15:00:00Z", "who": "marcus.lee", "do": "reject",
                               "reason": "The plan books on countersignature."}})
    demo.world.run_until("2026-04-29T17:00:00Z")
    ro = demo.ro
    assert queries.approval_decision(ro, "A-0001")["decision"] == "rejected"
    assert queries.as_of(ro, "approvals", "A-0002", None) is None
    assert queries.as_of(ro, "precedents", "P-0001", None) is None
    assert queries.line_status(ro, "CL-0001") == "draft"
    assert any("declined the exception (A-0001)" in m["text"] and "CL-0001 stays at $12,000.00" in m["text"]
               for m in _messages_to(ro, "jordan.kim"))
    with pytest.raises(Refused) as exc:
        demo.world.wp.request_approval(ANALYST, "plan_exception", "M-0007", 600_000, approver="marcus.lee")
    assert exc.value.reason_codes == ["RESUBMIT_NO_NEW_EVIDENCE"]
    # A new grant message is a new input and may be proposed.
    demo.world.wp.request_approval(ANALYST, "plan_exception", "M-0099", 600_000, approver="marcus.lee")
    demo.close()


def test_never_escalates_the_same_line_twice():
    demo = _demo()
    demo.world.run_until("2026-04-27T19:00:00Z")
    demo.world.api.message("jordan.kim", ANALYST, "escalate")
    demo.world.settle()
    escalations = queries.fetch_all(demo.ro, "SELECT * FROM decisions WHERE kind = 'escalation'")
    assert len(escalations) == 1
    assert "already with Marcus Lee" in _messages_to(demo.ro, "jordan.kim")[-1]["text"]
    demo.close()
