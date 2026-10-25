"""ELASTIC_TASKS E3: the evidence search interface, offline (LocalEvidence) and the search eval."""

import json

import pytest

from quota.search import evidence, intents, search_eval
from quota.sim import messages


@pytest.fixture(scope="module")
def corpus():
    world = messages.world_for("fernhill", 7)
    try:
        return messages.generate_messages(world, 7)
    finally:
        world.close()


def test_local_search_filters_by_account_and_time_and_says_how_it_found_things(corpus):
    docs, key = corpus
    local = evidence.LocalEvidence(docs)
    p01 = next(r for r in key if r["key"] == "P01")
    hits = local.search("terminate after 60 days with a full refund", "semantic", k=5)
    assert hits[0]["doc_id"] == p01["doc_ids"][0] and hits[0]["mode"] == "local_bm25" and hits[0]["rank"] == 1
    assert set(hits[0]) >= {"doc_id", "account", "deal_id", "kind", "sent_at", "subject", "body", "body_sha256",
                            "score", "rank", "mode", "source_file", "source_row"}
    scoped = local.search("refund", "hybrid", k=50, account=p01["account"])
    assert scoped and {h["account"] for h in scoped} == {p01["account"]}
    sent = hits[0]["sent_at"]
    assert not [h for h in local.search("refund", k=50, before="2026-01-01T00:00:00Z")]
    assert all(h["sent_at"] >= sent for h in local.search("refund", k=50, after=sent))


def test_intents_merge_by_best_rank():
    merged = intents.merge({"a": [{"doc_id": "x", "rank": 3, "score": 1.0}, {"doc_id": "y", "rank": 1, "score": 2.0}],
                            "b": [{"doc_id": "x", "rank": 1, "score": 0.5}]})
    assert [(h["doc_id"], h["rank"], h["intent"]) for h in merged] == [("y", 1, "a"), ("x", 1, "b")]


def test_the_offline_search_eval_reports_measured_local_bm25_only(corpus, tmp_path):
    docs, key = corpus
    result = search_eval.run(evidence.LocalEvidence(docs), key, len(docs), out_dir=tmp_path)
    assert list(result["modes"]) == ["local_bm25"] and result["planted"] == 12 and result["decoys"] == 12
    row = result["modes"]["local_bm25"]
    assert row["recall"] == len(row["found"]) and row["mode"] == "local_bm25"
    assert {"P01", "P11"} <= set(row["found"])                 # the keyword-difficulty promises are found by BM25
    assert json.loads((tmp_path / "search_eval.json").read_text())["modes"]["local_bm25"] == row
    assert search_eval.table(result)[2].startswith("local BM25 (offline)")
