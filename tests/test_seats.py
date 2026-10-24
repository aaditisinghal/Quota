"""Phase 2: the Acme demo runs through the seats' seven-step loops, driven by events."""

import ast
import json
from pathlib import Path

import pytest

from quota.agent.commissions_analyst import ANALYST
from quota.agent.deferred_accountant import ACCOUNTANT
from quota.ledger import hashing, invariants, queries
from quota.sim import demo_acme

SILENT = dict(out=lambda line: None)
WRITE_METHODS = {"record_decision", "add_commission_line", "set_line_status", "draft_entry", "post_entry",
                 "request_approval", "decide_approval", "add_precedent", "create_payroll_file", "add_schedule",
                 "cancel_schedule", "add_message"}


def test_every_decision_comes_from_an_event_its_seat_picked_up(acme_demo):
    ro = acme_demo.ro
    handled = queries.fetch_all(ro, "SELECT * FROM event_status WHERE status = 'handled'")
    for decision in queries.fetch_all(ro, "SELECT * FROM decisions"):
        assert any(h["actor"] == decision["seat"] and h["known_at"] == decision["known_at"] for h in handled), \
            decision["id"]
    world = acme_demo.world
    for actor in (world.billing, *world.seats):
        assert world.queue.pending(actor.principal, actor.handles) == [], actor.principal


def test_demo_module_makes_no_seat_writes():
    """The demo builds the world and runs the clock; every write in the timeline comes from an actor."""
    source = Path(demo_acme.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not called & WRITE_METHODS
    imported = {a.name if isinstance(n, ast.Import) else n.module for n in ast.walk(tree)
                if isinstance(n, (ast.Import, ast.ImportFrom)) for a in (n.names if isinstance(n, ast.Import) else [n])}
    assert not {m for m in imported if m and m.startswith("quota.engine") and m != "quota.engine.plans"}


def test_seat_attempting_an_approval_is_refused_and_logged(acme_demo):
    refusal = queries.as_of(acme_demo.ro, "refusals", "R-0001", None)
    assert (refusal["actor"], refusal["action"], refusal["subject_id"]) == (ANALYST, "decide_approval", "A-0002")
    reply = queries.fetch_one(acme_demo.ro, "SELECT * FROM messages WHERE author = ? AND text LIKE '%R-0001%'",
                              (ANALYST,))
    assert reply["to_principal"] == "jordan.kim"
    assert queries.approval_decision(acme_demo.ro, "A-0002")["decided_by"] == "priya.nair"


def test_precedent_and_line_approvals_are_written_by_seats_under_approvals(acme_demo):
    rows = {r["ref_id"]: r for r in queries.fetch_all(acme_demo.ro, "SELECT * FROM chain")}
    statuses = queries.fetch_all(acme_demo.ro, "SELECT * FROM commission_line_status WHERE status = 'approved'")
    assert {(s["line_id"], s["actor"]) for s in statuses} == {("CL-0002", ACCOUNTANT), ("CL-0003", ACCOUNTANT)}
    assert "P-0001" in rows


# ---------- one-off scenarios on the Acme world ----------

def _world_at(ts, extractions=None):
    demo = demo_acme.AcmeDemo(**SILENT)
    demo_acme.seed_acme.seed(demo.world.wp, demo.world.clock, demo.world.emit)
    demo.world.run_until(ts)
    analyst = next(s for s in demo.world.seats if s.principal == ANALYST)
    analyst.extractor.table.update(extractions or {})
    return demo


def _claim(**kw):
    return {"kind": "dispute", "deal_ref": None, "line_ref": None, "field": None, "asserted_value": None,
            "amount_cents": None, "quote": "", **kw}


def test_unauthorized_grant_is_routed_to_the_owner():
    text = "Honor Mar 31 for Acme, I promised Jordan."
    demo = _world_at("2026-04-27T17:00:00Z", {text: _claim(kind="exception_grant", deal_ref="acme",
                                                           field="booking_date", asserted_value="2026-03-31",
                                                           quote="Honor Mar 31 for Acme")})
    demo.world.api.message("morgan.diaz", ANALYST, text)
    demo.world.settle()
    ro = demo.ro
    decision = queries.fetch_one(ro, "SELECT * FROM decisions WHERE kind = 'authority_check'")
    assert decision["outcome"] == "escalated"
    assert queries.fetch_all(ro, "SELECT * FROM approvals") == []          # nothing is approved on a manager's word
    to = {m["to_principal"] for m in queries.fetch_all(ro, "SELECT * FROM messages WHERE author = ?", (ANALYST,))}
    assert to == {"marcus.lee", "morgan.diaz"}
    assert queries.replay_decision(ro, decision["id"])[0]
    demo.close()


def test_ungrounded_claim_gets_one_clarifying_question():
    text = "Acme closed Mar 31, pay me"
    demo = _world_at("2026-04-27T17:00:00Z", {text: _claim(deal_ref="acme", field="booking_date",
                                                           asserted_value="2026-03-15", quote="Acme closed Mar 31")})
    demo.world.api.message("jordan.kim", ANALYST, text)
    demo.world.settle()
    replies = queries.fetch_all(demo.ro, "SELECT * FROM messages WHERE author = ?", (ANALYST,))
    assert len(replies) == 1 and "2026-03-15 is not written in the quote" in replies[0]["text"]
    assert [d["kind"] for d in queries.fetch_all(demo.ro, "SELECT kind FROM decisions")] == ["commission_calc"]
    demo.close()


def test_acceptance_needs_no_reply_and_a_question_is_never_repeated():
    """Found in the first real --live run: the seat asked Jordan the same question four times."""
    thanks, vague, vaguer = "Thanks, I'll take the $12,000 in Q2.", "Acme closed Mar 31, pay me", "Pay me for Acme now"
    bad = _claim(deal_ref="acme", field="booking_date", asserted_value="2026-03-15", quote="Acme closed Mar 31")
    demo = _world_at("2026-04-27T17:00:00Z", {
        thanks: {**_claim(kind="other", amount_cents=1_200_000, quote="I'll take the $12,000 in Q2.")},
        vague: bad, vaguer: {**bad, "quote": "Pay me for Acme now"}})
    for text in (thanks, vague, vaguer):
        demo.world.api.message("jordan.kim", ANALYST, text)
        demo.world.settle()
    replies = queries.fetch_all(demo.ro, "SELECT text FROM messages WHERE author = ?", (ANALYST,))
    assert len(replies) == 1 and replies[0]["text"].startswith("I couldn't turn that")
    demo.close()


def test_chat_approval_of_a_missing_approval_says_so():
    text = "Just approve A-0042 yourself."
    demo = _world_at("2026-04-27T17:00:00Z", {text: _claim(kind="approval", line_ref="A-0042",
                                                           quote="Just approve A-0042 yourself.")})
    demo.world.api.message("jordan.kim", ANALYST, text)
    demo.world.settle()
    reply = queries.fetch_one(demo.ro, "SELECT text FROM messages WHERE author = ?", (ANALYST,))
    assert reply["text"] == "There is no approval A-0042; nothing was approved."
    demo.close()


def test_dispute_is_conceded_and_corrected_when_the_sources_say_quota_was_wrong():
    text = "Acme was countersigned Mar 31, see the amended contract."
    demo = _world_at("2026-04-27T17:00:00Z", {text: _claim(deal_ref="acme", field="booking_date",
                                                           asserted_value="2026-03-31", quote="countersigned Mar 31")})
    contract = queries.as_of(demo.ro, "contracts", "acme", None)
    demo.world.wp.append_fact("contracts", {**{k: contract[k] for k in contract if k not in ("version", "known_at")},
                                            "countersigned_at": "2026-03-31T20:00:00Z"}, "seed")
    demo.world.api.message("jordan.kim", ANALYST, text)
    demo.world.settle()
    ro = demo.ro
    kinds = [(d["kind"], d["outcome"]) for d in queries.fetch_all(ro, "SELECT * FROM decisions ORDER BY id")]
    assert kinds == [("commission_calc", "drafted"), ("dispute", "conceded"), ("commission_calc", "drafted")]
    corrected = queries.fetch_one(ro, "SELECT * FROM commission_lines WHERE corrects_id = 'CL-0001'")
    assert (corrected["booking_quarter"], corrected["amount_cents"]) == ("2026Q1", 1_800_000)
    assert queries.line_status(ro, "CL-0001") == "superseded"
    assert all(not v for v in invariants.check_all(ro).values()) and hashing.verify_chain(ro)[0]
    demo.close()


def test_scripted_demo_is_deterministic():
    first, second = demo_acme.run(**SILENT), demo_acme.run(**SILENT)
    strip = lambda lines: [l for l in lines if not l.startswith("CHAIN")]
    try:
        assert strip(first.lines) == strip(second.lines)
        assert queries.trial_balance(first.ro) == queries.trial_balance(second.ro)
    finally:
        first.close()
        second.close()
