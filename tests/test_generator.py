"""Phase 3: Fernhill, the generated world."""

from collections import Counter

import pytest

from quota.ledger import hashing, invariants, queries
from quota.sim.generator import Fernhill, generate


def _quarter(ts):
    return f"{ts[:4]}Q{(int(ts[5:7]) - 1) // 3 + 1}"


def test_same_seed_same_world_and_different_seed_different_world():
    a, b, c = Fernhill(7), Fernhill(7), Fernhill(8)
    assert [vars(d) for d in a.deals] == [vars(d) for d in b.deals]
    assert a.all_events() == b.all_events()
    assert [d.id for d in a.deals] != [d.id for d in c.deals]


def test_world_shape_matches_section_13():
    f = Fernhill(7, reps=20)
    assert len(f.reps) == 20
    assert [r.id for r in f.reps[:4]] == ["jordan.kim", "alex.chen", "taylor.brooks", "riley.shah"]
    california = [r for r in f.reps if r.state == "CA"]
    assert len(california) == 6 and any(r.id == "taylor.brooks" for r in california)
    per_rep_quarter = Counter((d.owner, _quarter(d.booked_at)) for d in f.deals)
    assert set(per_rep_quarter.values()) <= set(range(4, 9))
    assert len(per_rep_quarter) == 40                                   # every rep sells in both quarters
    assert all(1_500_000 <= d.acv_cents <= 25_000_000 for d in f.deals)
    assert {d.term for d in f.deals} == {12, 24, 36}
    split = sum(len(d.splits) == 2 for d in f.deals) / len(f.deals)
    assert 0.05 < split < 0.30
    assert all(sum(s["bps"] for s in d.splits) == 10000 for d in f.deals)
    assert all(d.created_at < d.booked_at < d.invoice_at for d in f.deals)
    assert all(d.paid_at is None or d.paid_at > d.invoice_at for d in f.deals)
    assert len({d.id for d in f.deals}) == len(f.deals)


@pytest.fixture(scope="module")
def small_world():
    world = generate(11, reps=4, plant=False)
    world.run_until("2026-02-15T12:00:00Z")
    mid = {"now": world.clock.now(), "deals": queries.latest_rows(world.ro, "deals", world.clock.now())}
    world.run_until(world.end_ts)
    yield world, mid
    world.close()


def test_facts_arrive_only_when_their_events_do(small_world):
    world, mid = small_world
    spec = {d.id: d for d in world.fernhill.deals}
    assert mid["deals"], "some deals exist by mid-February"
    for row in mid["deals"]:
        assert spec[row["id"]].created_at <= mid["now"]
        if row["stage"] == "closed_won":
            assert spec[row["id"]].booked_at[:10] <= mid["now"][:10]
    unborn = [d for d in spec.values() if d.created_at > mid["now"]]
    assert unborn and not {d.id for d in unborn} & {r["id"] for r in mid["deals"]}


def test_small_world_runs_clean_end_to_end(small_world):
    world, _ = small_world
    ro = world.ro
    assert all(not v for v in invariants.check_all(ro).values())
    assert hashing.verify_chain(ro) == (True, None)
    tb = queries.trial_balance(ro)
    assert sum(tb.values()) == 0
    assert queries.fetch_all(ro, "SELECT * FROM refusals") == []
    # every booked deal is commissioned once per credited rep, and every line is accrued
    expected = {(d.id, s["rep"]) for d in world.fernhill.deals for s in d.splits}
    calcs = {tuple(d["subject_id"].split("/")) for d in queries.fetch_all(ro, "SELECT subject_id FROM decisions "
                                                                                "WHERE kind = 'commission_calc'")}
    assert calcs == expected
    accrued = {d["subject_id"] for d in queries.fetch_all(ro, "SELECT subject_id FROM decisions "
                                                               "WHERE kind = 'commission_accrual'")}
    assert accrued == {l["id"] for l in queries.fetch_all(ro, "SELECT id FROM commission_lines")}


def test_payroll_pays_only_collected_deals(small_world):
    world, _ = small_world
    ro = world.ro
    paid = [l for l in queries.fetch_all(ro, "SELECT * FROM commission_lines") if queries.line_status(ro, l["id"]) == "paid"]
    assert paid
    for line in paid:
        payment = queries.fetch_one(ro, "SELECT known_at FROM billing WHERE deal_id = ? AND kind = 'payment'",
                                    (line["deal_id"],))
        assert payment is not None
    never_paid = {d.id for d in world.fernhill.deals if d.paid_at is None}
    assert never_paid and not never_paid & {l["deal_id"] for l in paid}


def test_every_decision_replays(small_world):
    world, _ = small_world
    ids = [d["id"] for d in queries.fetch_all(world.ro, "SELECT id FROM decisions")]
    assert all(queries.replay_decision(world.ro, d)[0] for d in ids)
