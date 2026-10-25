"""ELASTIC_TASKS E4: the commitment classifier, its verbatim guard and the contract check (offline: recorded
model answers in tests/fixtures/llm_cache, and the rule reader where none is recorded)."""

from pathlib import Path

import pytest

from quota.agent import commitments
from quota.agent.llm import LLM, LLMUnavailable
from quota.ledger import queries
from quota.search.indexes import body_sha256
from quota.sim import messages

CACHE = Path(__file__).parent / "fixtures" / "llm_cache"


def offline(*_args):
    raise LLMUnavailable("tests never call the model")


@pytest.fixture(scope="module")
def planted():
    world = messages.world_for("fernhill", 7)
    try:
        docs, key = messages.generate_messages(world, 7)
        contracts = {c["deal_id"]: c for c in queries.latest_rows(world.ro, "contracts", queries.END_OF_TIME)}
    finally:
        world.close()
    by_id = {m["doc_id"]: {**m, "body_sha256": body_sha256(m["body"]), "mode": "hybrid", "rank": 1, "score": 1.0}
             for m in docs}
    return {r["key"]: (by_id[r["doc_ids"][-1]], contracts[r["deal_id"]]) for r in key}


@pytest.fixture()
def model():
    return LLM(model="claude-opus-5-5", cache_dir=CACHE, transport=offline)


def test_a_paraphrased_exit_is_a_finding(planted, model):
    hit, contract = planted["P02"]
    finding = commitments.classify(hit, contract, model)
    assert finding["type"] == "early_exit" and finding["read_by"] == "model"
    assert finding["quote"] in hit["body"] and finding["body_sha256"] == hit["body_sha256"]
    assert finding["retrieval"] == {"mode": "hybrid", "rank": 1, "score": 1.0}


def test_a_proposal_is_not_a_promise(planted, model):
    hit, contract = planted["D03"]
    assert commitments.classify(hit, contract, model) is None


def test_a_promise_the_contract_already_makes_is_covered(planted):
    hit, contract = planted["D01"]
    clauses = queries.loads(contract["clauses_json"])
    assert commitments.assess(hit, clauses)["status"] == "covered"          # the rule reader calls it a promise
    assert commitments.covered("early_exit", "terminate after 60 days", clauses)
    assert not commitments.covered("early_exit", "terminate after 120 days", clauses)
    assert not commitments.covered("early_exit", "if it isn't working by Q3, we'll let you out", clauses)
    hit, contract = planted["D04"]
    assert commitments.covered("price_lock", "price stays", queries.loads(contract["clauses_json"]))


def test_a_quote_that_is_not_in_the_message_is_discarded(planted):
    hit, contract = planted["P05"]

    class Inventive:
        def complete_json(self, system, prompt, schema):
            return {"is_commitment": True, "type": "price_lock", "quote": "We guarantee a flat price forever.",
                    "promised_by": hit["author"], "to_customer": True}
    assert commitments.assess(hit, queries.loads(contract["clauses_json"]), Inventive())["status"] == "discarded"


def test_recorded_model_answers_and_the_rule_reader_agree_on_every_planted_item(planted, model):
    for key, (hit, contract) in planted.items():
        expected = key.startswith("P")
        for reader in (model, None):
            assert (commitments.classify(hit, contract, reader) is not None) == expected, (key, reader)
