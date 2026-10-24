import json

from quota.engine import calculator
from quota.ledger import db, queries
from quota.sim import demo_acme


def test_replay_matches(acme_demo):
    for decision_id in ("D-0001", "D-0005", "D-0007"):
        matches, stored, recomputed = queries.replay_decision(acme_demo.ro, decision_id)
        assert matches, decision_id
        assert stored == recomputed


def test_every_decision_replays(acme_demo):
    ids = [r["id"] for r in queries.fetch_all(acme_demo.ro, "SELECT id FROM decisions ORDER BY id")]
    assert ids == [f"D-000{n}" for n in range(1, 8)]
    assert all(queries.replay_decision(acme_demo.ro, d)[0] for d in ids)


def test_replay_as_of_uses_old_inputs(acme_demo):
    ro = acme_demo.ro
    assert queries.as_of(ro, "precedents", "P-0001", None) is not None
    matches, stored, recomputed = queries.replay_decision(ro, "D-0001")
    assert matches
    assert (recomputed["result"]["quarter"], recomputed["result"]["amount_cents"]) == ("2026Q2", 1_200_000)
    # The same calculation over today's facts sees P-0001; replay must not.
    today = calculator.commission_decision(queries.commission_rows(ro, "jordan.kim", "2026-05-01T00:00:00Z"),
                                           "acme", "jordan.kim", "2026-05-01T00:00:00Z")
    assert today["result"].amount_cents == 1_800_000


def test_replay_detects_altered_input():
    demo = demo_acme.run(out=lambda line: None)
    try:
        raw = db.connect(demo.uri)   # bypasses WritePath on purpose
        raw.execute("UPDATE contracts SET acv_cents = 15000000 WHERE id = 'acme' AND version = 1")
        raw.commit()
        raw.close()
        matches, stored, recomputed = queries.replay_decision(demo.ro, "D-0001")
        assert not matches
        assert "contracts:acme" in recomputed["error"]
        assert json.loads(json.dumps(stored))["result"]["amount_cents"] == 1_200_000
    finally:
        demo.close()
