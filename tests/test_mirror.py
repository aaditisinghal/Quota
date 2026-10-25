"""ELASTIC_TASKS E2: the decision-log mirror's documents and its verify, offline."""

import os

import pytest

from quota.search import indexes, mirror
from quota.sim import demo_acme


@pytest.fixture(scope="module")
def demo():
    run = demo_acme.run(out=lambda line: None)
    yield run
    run.close()


def test_every_decision_approval_precedent_and_refusal_is_one_document(demo):
    docs = {d["id"]: d for d in mirror.documents(demo.ro)}
    assert {d["kind"] for d in docs.values()} == {"decision", "approval", "precedent", "exception", "refusal"}
    assert docs["R-0001"]["reason_code"] == "NO_CAPABILITY" and docs["R-0001"]["seat"] == "seat:commissions_analyst"
    a2 = docs["A-0002"]
    assert (a2["kind"], a2["approver"], a2["outcome"], a2["amount_cents"]) == ("approval", "priya.nair", "approved",
                                                                               600_000)
    p1 = docs["P-0001:exception"]
    assert (p1["deal_id"], p1["rep_id"], p1["account"], p1["quarter"]) == ("acme", "jordan.kim", "Acme Robotics",
                                                                           "2026Q2")
    d5 = docs["D-0005"]
    assert (d5["rep_id"], d5["deal_id"], d5["amount_cents"], d5["quarter"]) == ("jordan.kim", "acme", 1_800_000,
                                                                                "2026Q2")
    assert all(d["chain_seq"] and len(d["chain_hash"]) == 64 for d in docs.values())
    assert set(docs["D-0001"]) == set(indexes.QUOTA_DECISIONS["properties"])      # the mapping covers each field


def test_verify_compares_count_top_seq_and_hash(demo):
    docs = mirror.documents(demo.ro)
    local = mirror.head(docs)
    assert mirror.compare(local, dict(local)).startswith(f"MIRROR OK seq={local['seq']}")
    assert mirror.compare(local, {**local, "count": local["count"] - 1}).startswith("MIRROR MISMATCH count")
    assert mirror.compare(local, {**local, "hash": "0" * 64}).startswith("MIRROR MISMATCH hash")


def test_the_mirror_is_off_in_unit_tests_and_never_touches_the_ledger(demo):
    assert os.environ.get("QUOTA_ELASTIC_OFF") == "1"
    assert mirror.Mirror()(demo.ro) is None


def test_comms_documents_carry_the_body_hash():
    doc = indexes.comms_doc({"doc_id": "MSG-1", "subject": "Re: plan", "body": "hello"})
    assert doc["content"] == "Re: plan\n\nhello"
    assert doc["body_sha256"] == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
