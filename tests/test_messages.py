"""ELASTIC_TASKS E1: the message corpus and its answer key."""

import hashlib
import json

import pytest

from quota.ledger import queries
from quota.sim import messages


@pytest.fixture(scope="module")
def fernhill():
    world = messages.world_for("fernhill", 7)
    yield world
    world.close()


def test_the_same_seed_writes_byte_identical_files(fernhill, tmp_path):
    digests = []
    for n in (1, 2):
        corpus, key = messages.generate_messages(fernhill, 7)
        messages.write(corpus, key, tmp_path / f"run{n}" / "messages.jsonl")
        digests.append([hashlib.sha256((tmp_path / f"run{n}" / name).read_bytes()).hexdigest()
                        for name in ("messages.jsonl", "messages_key.json")])
    assert digests[0] == digests[1]


def test_every_key_row_resolves_to_its_messages(fernhill):
    corpus, key = messages.generate_messages(fernhill, 7)
    by_id = {m["doc_id"]: m for m in corpus}
    assert 2500 <= len(corpus) <= 3500
    assert all("2026-01-01" <= m["sent_at"][:10] <= "2026-06-30" for m in corpus)
    assert [r["key"] for r in key] == [f"P{n:02d}" for n in range(1, 13)] + [f"D{n:02d}" for n in range(1, 13)]
    assert len({r["deal_id"] for r in key}) == 24                       # one planted item per deal
    for row in key:
        docs = [by_id[d] for d in row["doc_ids"]]
        assert docs and all(d["deal_id"] == row["deal_id"] and d["account"] == row["account"] for d in docs)
    texts = {k: t for k, _, _, _, t in messages.PROMISES} | {k: t for k, _, _, t, _ in messages.DECOYS}
    for row in key:
        assert texts[row["key"]] in by_id[row["doc_ids"][-1]]["body"], row["key"]
    p09 = next(r for r in key if r["key"] == "P09")
    assert messages.P09_QUESTION in by_id[p09["doc_ids"][1]]["body"]  # the reply quotes the question
    assert by_id[next(r for r in key if r["key"] == "P08")["doc_ids"][0]]["lang"] == "es"
    assert by_id[next(r for r in key if r["key"] == "P10")["doc_ids"][0]]["kind"] == "slack"


def test_promises_sit_on_contracts_without_the_clause_and_covered_decoys_on_contracts_with_it(fernhill):
    _, key = messages.generate_messages(fernhill, 7)
    contracts = {c["deal_id"]: queries.loads(c["clauses_json"])
                 for c in queries.latest_rows(fernhill.ro, "contracts", queries.END_OF_TIME)}
    for row in key:
        clause = messages.CONTRACT_KEY.get(row["type"])
        if row["expected"] == "commitment" and clause:
            assert clause not in contracts[row["deal_id"]], row["key"]
    assert "termination_for_convenience" in contracts[next(r for r in key if r["key"] == "D01")["deal_id"]]
    assert "price_increase_cap" in contracts[next(r for r in key if r["key"] == "D04")["deal_id"]]
    assert not {r["deal_id"] for r in key} & fernhill.fernhill.used_deals    # no planted case is disturbed


def test_the_acme_story_gets_its_own_unplanted_corpus():
    corpus, key = messages.corpus_for("acme", 7)
    assert key == [] and {m["deal_id"] for m in corpus} >= {"acme", "summit", "orbit"}
    assert len(json.dumps(corpus)) > 0
