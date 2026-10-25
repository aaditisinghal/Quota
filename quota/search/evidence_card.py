"""The evidence card for a held commission (ELASTIC_TASKS E8, backend side): the message behind an
OFF_CONTRACT_COMMITMENT hold, the quote to highlight, and how each retrieval mode ranks it for the promise's
intent. With Elastic all three modes run (keyword, semantic, hybrid); offline, only local BM25 can be shown, next
to how the gate itself found it."""

import json

from quota.intake.formats import to_pacific
from quota.ledger import queries
from quota.search.intents import INTENTS


def _rank(hits, doc_id):
    return next((h["rank"] for h in hits if h["doc_id"] == doc_id), None)


def card(world, decision_id) -> dict | None:
    decision = queries.as_of(world.ro, "decisions", decision_id, None)
    if decision is None or decision["kind"] != "commitment_check":
        return None
    output = json.loads(decision["output_json"])
    findings = output["args"]["findings"]
    open_ = set(output["result"]["open"]) or {f["doc_id"] for f in findings}
    finding = next(f for f in findings if f["doc_id"] in open_)
    evidence = getattr(world, "evidence", None)
    message = evidence.get(finding["doc_id"]) if evidence is not None else None
    ranks = {}
    if evidence is not None and message is not None:
        query = INTENTS.get(finding["type"], finding["quote"])
        modes = ("keyword", "semantic", "hybrid") if getattr(evidence, "name", "") == "elastic" else ("keyword",)
        for mode in modes:
            hits = evidence.search(query, mode, k=20, account=message["account"])
            label = "local_bm25" if getattr(evidence, "name", "") == "local" else (hits[0]["mode"] if hits else mode)
            ranks[label] = _rank(hits, finding["doc_id"])
    return {"decision": decision_id, "deal_id": decision["subject_id"], "outcome": decision["outcome"],
            "reason_code": output["result"].get("reason_code"), "type": finding["type"], "quote": finding["quote"],
            "found_by": finding["retrieval"], "read_by": finding.get("read_by"), "ranks": ranks,
            "message": message and {
                "doc_id": message["doc_id"], "kind": message["kind"], "author": message["author"],
                "to": message.get("to") or [], "subject": message.get("subject"), "body": message["body"],
                "sent_at": message["sent_at"], "sent_at_pacific": to_pacific(message["sent_at"]),
                "source": f"{message.get('source_file') or 'messages.jsonl'} · row {message.get('source_row') or '—'}"},
            "others": [f["doc_id"] for f in findings if f["doc_id"] != finding["doc_id"]]}
