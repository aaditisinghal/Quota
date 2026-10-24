"""Fresh in-memory DB + clock fixtures, plus Acme fact rows for the pure engine tests."""

import itertools
import json
from pathlib import Path

import pytest

from quota import config
from quota.clock import SimClock
from quota.engine.plans import load_plan
from quota.ledger import db, queries
from quota.ledger.write_path import WritePath

ANALYST = "seat:commissions_analyst"
ACCOUNTANT = "seat:deferred_accountant"
PLAN_PATH = Path(__file__).resolve().parents[1] / "plans" / "jordan.kim" / "AE-2026-H1-v3.yaml"

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


@pytest.fixture(scope="session")
def acme_demo():
    """The full section 9 timeline, run once per session on its own in-memory DB."""
    from quota.sim import demo_acme
    demo = demo_acme.run(out=lambda line: None)
    yield demo
    demo.close()


# ---------- engine fixtures (rows shaped exactly like DB rows) ----------

@pytest.fixture
def plan():
    return load_plan(PLAN_PATH)


def deal_row(deal_id, account, crm_cents, close, version=1, known_at=None):
    return {"id": deal_id, "version": version, "account": account, "crm_amount_cents": crm_cents,
            "crm_close_date": close, "stage": "closed_won", "owner_id": "jordan.kim",
            "splits_json": json.dumps([{"bps": 10000, "rep": "jordan.kim"}]),
            "known_at": known_at or f"{close}T18:00:00Z", "source": "crm"}


def contract_row(deal_id, acv_cents, term, contract_date, countersigned_at, version=1):
    return {"id": deal_id, "version": version, "deal_id": deal_id, "acv_cents": acv_cents, "term_months": term,
            "contract_date": contract_date, "countersigned_at": countersigned_at, "clauses_json": "{}",
            "doc_ref": f"contracts/{deal_id}-msa.pdf", "known_at": countersigned_at, "source": "esign"}


@pytest.fixture
def acme():
    return {
        "deal": deal_row("acme", "Acme Robotics", 15_000_000, "2026-03-31", known_at="2026-03-31T23:30:00Z"),
        "contract": contract_row("acme", 12_000_000, 36, "2026-04-01", "2026-04-02T17:14:00Z"),
        "esign": [{"id": "ENV-ACME-001-countersigned", "envelope_id": "ENV-ACME-001", "deal_id": "acme",
                   "event": "countersigned", "at": "2026-04-02T17:14:00Z", "known_at": "2026-04-02T17:14:00Z"}],
        "p0001": {"id": "P-0001", "statement": "Count Acme in 2026Q1",
                  "scope_json": json.dumps({"deal_id": "acme", "field": "booking_date"}),
                  "override_json": json.dumps({"value": "2026-03-31", "quarter": "2026Q1"}),
                  "approvals_json": json.dumps(["A-0001", "A-0002"]), "effective_from": "2026-04-29T16:00:00Z",
                  "expires_at": "2026-06-30", "known_at": "2026-04-29T16:00:00Z"},
    }


@pytest.fixture
def acme_rows(acme, plan):
    """Jordan's world as a decision's input rows: plan, Q1 history ($300k) and Acme."""
    history = [("birch", "Birch Health", 10_000_000, 12, "2026-01-20"),
               ("cedar", "Cedar Logistics", 12_000_000, 24, "2026-02-14"),
               ("delta", "Delta Foods", 8_000_000, 12, "2026-03-10")]
    return {
        "plans": [{"id": plan.plan_id, "version": plan.version, "rep_id": plan.rep,
                   "effective_from": plan.effective_from, "effective_to": plan.effective_to,
                   "config_json": json.dumps(plan.raw), "signed_doc_ref": None, "signed_at": plan.signed_at,
                   "known_at": "2025-12-18T17:00:00Z"}],
        "deals": [deal_row(d, a, c, close) for d, a, c, _, close in history] + [acme["deal"]],
        "contracts": [contract_row(d, c, t, close, f"{close}T18:00:00Z") for d, _, c, t, close in history]
                     + [acme["contract"]],
        "esign_events": acme["esign"],
        "precedents": [],
    }
