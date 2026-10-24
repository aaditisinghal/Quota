"""Fresh in-memory DB + clock fixtures."""

import itertools

import pytest

from quota import config
from quota.clock import SimClock
from quota.ledger import db, queries
from quota.ledger.write_path import WritePath

ANALYST = "seat:commissions_analyst"
ACCOUNTANT = "seat:deferred_accountant"

_db_names = itertools.count(1)


@pytest.fixture
def db_uri():
    # One shared-cache in-memory DB per test (file:memdbN?mode=memory&cache=shared).
    return db.memory_uri(f"memdb{next(_db_names)}")


@pytest.fixture
def conn(db_uri):
    c = db.connect(db_uri)
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture
def ro(conn, db_uri):
    c = db.connect(db_uri, readonly=True)
    yield c
    c.close()


@pytest.fixture
def clock():
    return SimClock("2026-01-01T00:00:00Z")


@pytest.fixture
def wp(conn, clock):
    return WritePath(conn, clock, config.principals(with_seed=True))


class Ledger:
    """Small builders for ledger tests."""

    def __init__(self, wp, conn, clock):
        self.wp, self.conn, self.clock = wp, conn, clock

    def at(self, ts):
        self.clock.advance_to(ts)
        return self

    def periods(self):
        for month in range(1, 13):
            self.wp.append_fact("periods", {"period": f"2026-{month:02d}", "status": "open", "actor": "seed"},
                                "seed")

    def deal(self, deal_id="acme", splits=(("jordan.kim", 10000),), amount_cents=15_000_000):
        return self.wp.append_fact("deals", {
            "id": deal_id, "account": deal_id.title(), "crm_amount_cents": amount_cents,
            "crm_close_date": "2026-03-31", "stage": "closed_won", "owner_id": splits[0][0],
            "splits_json": [{"rep": rep, "bps": bps} for rep, bps in splits], "source": "crm",
        }, "seed")

    def decision(self, actor=ANALYST, subject="acme", kind="test"):
        deal = queries.as_of(self.conn, "deals", subject, None) or self.deal(subject)
        return self.wp.record_decision(actor, kind, subject, [queries.input_ref("deals", deal)], {}, [],
                                       {"fn": "test"}, "drafted", "test decision")

    def line(self, decision_id, deal_id="acme", amount_cents=1_200_000):
        return self.wp.add_commission_line(ANALYST, {
            "rep_id": "jordan.kim", "deal_id": deal_id, "booking_quarter": "2026Q2",
            "credit_cents": 12_000_000, "base_portion_cents": 12_000_000, "accel_portion_cents": 0,
            "amount_cents": amount_cents}, decision_id)


def balanced(cents=1_800_000, dr="1350", cr="2150"):
    return [{"account": dr, "debit_cents": cents, "credit_cents": 0},
            {"account": cr, "debit_cents": 0, "credit_cents": cents}]


@pytest.fixture
def ledger(wp, conn, clock):
    builder = Ledger(wp, conn, clock)
    builder.periods()
    builder.at("2026-04-01T00:00:00Z")
    return builder
