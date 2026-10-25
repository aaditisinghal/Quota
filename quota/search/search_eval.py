"""`python -m quota elastic eval-search --seed 7` (ELASTIC_TASKS E3): how many of the 12 planted promises each
retrieval mode finds corpus-wide (the union over intents, top 20 each), and how many decoys it pulls in.
The numbers are measured, never edited."""

import json
from pathlib import Path

from quota.search.intents import INTENTS, merge

LABELS = {"keyword": "keyword (BM25)", "semantic": "semantic (Jina)", "hybrid": "hybrid+rerank",
          "local_bm25": "local BM25 (offline)"}


def found(backend, mode, k=20) -> list[dict]:
    return merge({intent: backend.search(text, mode, k=k) for intent, text in INTENTS.items()})


def score(hits, key) -> dict:
    ids = {h["doc_id"] for h in hits}
    planted = [r for r in key if r["expected"] == "commitment"]
    decoys = [r for r in key if r["expected"] == "not"]
    hit = {r["key"] for r in planted if ids & set(r["doc_ids"])}
    by_key = {r["key"]: r for r in key}
    return {"recall": len(hit), "planted": len(planted), "found": sorted(hit),
            "missed": sorted(r["key"] for r in planted if r["key"] not in hit),
            "decoys_in_top20": sum(1 for r in decoys if ids & set(r["doc_ids"])),
            "multilingual": "P08" in hit, "thread": "P09" in hit,
            "by_difficulty": {d: [k for k in sorted(hit) if by_key[k]["difficulty"] == d]
                              for d in sorted({r["difficulty"] for r in planted})}}


def run(backend, key, corpus_size, out_dir="out") -> dict:
    modes = ["keyword", "semantic", "hybrid"] if backend.name == "elastic" else ["local_bm25"]
    rows = {}
    for mode in modes:
        hits = found(backend, "hybrid" if mode == "local_bm25" else mode)
        rows[mode] = {"label": LABELS[mode], "mode": hits[0]["mode"] if hits else mode, **score(hits, key)}
    from quota.search import precedents
    if backend.name == "elastic":
        decisions = precedents.ElasticDecisions.library(backend.client)
    else:
        decisions = precedents.LocalDecisions(precedents.library_docs())
    recall = precedents.recall_eval(decisions)
    result = {"backend": backend.name, "messages": corpus_size, "precedent_recall": recall,
              "planted": sum(r["expected"] == "commitment" for r in key),
              "decoys": sum(r["expected"] == "not" for r in key), "k": 20, "intents": list(INTENTS), "modes": rows}
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / "search_eval.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def table(result) -> list[str]:
    tick = lambda ok: "✓" if ok else "✗"
    lines = [f"Off-contract promise sweep: {result['planted']} planted in {result['messages']} messages, "
             f"{result['decoys']} decoys",
             f"{'mode':<22}{'recall@20':<11}{'decoys_in_top20':<17}{'multilingual':<14}thread"]
    for row in result["modes"].values():
        lines.append(f"{row['label']:<22}{str(row['recall']) + '/' + str(row['planted']):<11}{row['decoys_in_top20']:<17}"
                     f"{tick(row['multilingual']):<14}{tick(row['thread'])}")
    r = result.get("precedent_recall")
    if r:
        lines.append(f"Precedent recall ({r['backend']}): hit@3 {r['hit_at_3']}/{r['requests']} reworded exception "
                     f"requests")
    return lines
