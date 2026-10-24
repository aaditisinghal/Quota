import pytest

from conftest import ACCOUNTANT, ANALYST
from quota.ledger import hashing, queries
from quota.ledger.write_path import Refused
from quota.sim import events as ev
from quota.sim.clock_runner import ClockRunner

CHURN = {"deal_id": "acme", "effective_date": "2026-06-30", "reason": "termination_for_convenience",
         "refund_cents": 9_000_000}


@pytest.fixture
def runner(ledger, wp, ro, clock):
    lines = []
    r = ClockRunner(ro, wp, clock, emit=lambda actor, kind, text: lines.append((actor, kind, text)))
    r.printed = lines
    return r


def test_event_schemas_validate():
    assert ev.validate("customer.churned", CHURN) == []
    assert ev.validate("customer.churned", {"deal_id": "acme"}) == [
        "missing effective_date", "missing reason", "missing refund_cents"]
    assert ev.validate("customer.churned", {**CHURN, "refund_cents": "9000000"}) == ["refund_cents must be integer"]
    assert ev.validate("customer.churned", {**CHURN, "mood": "sad"}) == ["unexpected field mood"]
    assert ev.validate("job.due", {"job": "make_coffee"})[0].startswith("job must be one of")
    assert ev.validate("acme.exploded", {}) == ["unknown event type 'acme.exploded'"]
    with pytest.raises(ValueError):
        ev.make("invoice.issued", {"deal_id": "acme"}, "2026-04-02T18:00:00Z")


def test_scheduled_event_is_invisible_until_its_time(runner, ledger, ro):
    runner.schedule("2026-06-28T17:00:00Z", "customer.churned", CHURN)
    assert runner.release_due() == []
    assert runner.pending(ACCOUNTANT, {"customer.churned"}) == []
    assert queries.fetch_all(ro, "SELECT * FROM events") == []      # the ledger does not know the future
    assert runner.next_time() == "2026-06-28T17:00:00Z"

    ledger.at("2026-06-28T17:00:00Z")
    released = runner.release_due()
    assert [e.type for e in released] == ["customer.churned"]
    row = queries.as_of(ro, "events", released[0].id, None)
    assert (row["release_at"], row["known_at"]) == ("2026-06-28T17:00:00Z", "2026-06-28T17:00:00Z")
    assert [e.id for e in runner.pending(ACCOUNTANT, {"customer.churned"})] == [released[0].id]
    assert runner.printed[-1][1] == "EVENT" and "customer.churned acme" in runner.printed[-1][2]
    assert runner.next_time() is None


def test_future_dated_event_waits_for_the_clock(runner, ledger, wp):
    wp.append_fact("events", {"type": "customer.churned", "payload_json": CHURN,
                              "release_at": "2026-05-01T00:00:00Z", "created_by": "system"}, "system")
    assert runner.pending(ACCOUNTANT, {"customer.churned"}) == []
    assert runner.next_time() == "2026-05-01T00:00:00Z"
    ledger.at("2026-05-01T00:00:00Z")
    assert len(runner.pending(ACCOUNTANT, {"customer.churned"})) == 1


def test_each_actor_picks_up_events_once(runner, ledger, wp):
    first = runner.emit_now("job.due", {"job": "churn_review", "period": "2026-04"})
    second = runner.emit_now("job.due", {"job": "amortization", "period": "2026-04"})
    assert [e.id for e in runner.pending(ACCOUNTANT, {"job.due"})] == [first.id, second.id]
    wp.mark_event(ACCOUNTANT, first.id, "handled")
    assert runner.next_for(ACCOUNTANT, {"job.due"}).id == second.id
    assert runner.next_for(ANALYST, {"job.due"}).id == first.id     # claims are per actor
    assert runner.pending(ACCOUNTANT, {"message.posted"}) == []


def test_marking_needs_capability_and_a_released_event(runner, ledger, wp):
    early = wp.append_fact("events", {"type": "customer.churned", "payload_json": CHURN,
                                      "release_at": "2026-06-28T17:00:00Z", "created_by": "system"}, "system")
    with pytest.raises(Refused) as exc:
        wp.mark_event(ACCOUNTANT, early["id"], "handled")
    assert exc.value.reason_codes == ["EVENT_NOT_RELEASED"]
    now = runner.emit_now("job.due", {"job": "amortization", "period": "2026-04"})
    with pytest.raises(Refused) as exc:
        wp.mark_event("jordan.kim", now.id, "handled")
    assert exc.value.reason_codes == ["NO_CAPABILITY"]


def test_timers_fire_once_and_only_while_wanted(runner, ledger, conn):
    wanted = {"on": True}
    runner.add_timer_source(lambda now: [("2026-04-02T09:00:00Z", "job.due",
                                          {"job": "approver_digest", "date": "2026-04-02"})] if wanted["on"] else [])
    assert runner.next_time() == "2026-04-02T09:00:00Z"
    ledger.at("2026-04-02T09:00:00Z")
    assert [e.payload["job"] for e in runner.release_due()] == ["approver_digest"]
    assert runner.release_due() == []                                # same timer, already released
    assert runner.next_time() is None

    wanted["on"] = False
    runner.add_timer_source(lambda now: [("2026-04-03T09:00:00Z", "job.due",
                                          {"job": "approver_digest", "date": "2026-04-03"})] if wanted["on"] else [])
    assert runner.next_time() is None
    ledger.at("2026-04-03T09:00:00Z")
    assert runner.release_due() == []
    assert hashing.verify_chain(conn) == (True, None)
