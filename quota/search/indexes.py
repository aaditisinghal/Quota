"""The three Elastic indexes (ELASTIC_TASKS E2) and loading messages into quota-comms."""

import hashlib
import json
from pathlib import Path

COMMS, DECISIONS, FINDINGS = "quota-comms", "quota-decisions", "quota-findings"

QUOTA_COMMS = {"properties": {
    "doc_id": {"type": "keyword"}, "kind": {"type": "keyword"}, "thread_id": {"type": "keyword"},
    "account": {"type": "keyword"}, "deal_id": {"type": "keyword"}, "rep_id": {"type": "keyword"},
    "author": {"type": "keyword"}, "to": {"type": "keyword"}, "channel": {"type": "keyword"},
    "sent_at": {"type": "date"}, "lang": {"type": "keyword"},
    "subject": {"type": "text"}, "body": {"type": "text"},
    "content": {"type": "semantic_text"},          # subject + body, embedded with Jina
    "body_sha256": {"type": "keyword"},
    "source_file": {"type": "keyword"}, "source_row": {"type": "integer"}}}

QUOTA_DECISIONS = {"properties": {                # one-way mirror of Quota's decision log
    "id": {"type": "keyword"}, "kind": {"type": "keyword"},   # decision|approval|precedent|refusal|exception
    "seat": {"type": "keyword"}, "subject_id": {"type": "keyword"},
    "rep_id": {"type": "keyword"}, "deal_id": {"type": "keyword"}, "account": {"type": "keyword"},
    "approver": {"type": "keyword"}, "outcome": {"type": "keyword"},
    "reason_code": {"type": "keyword"}, "reason_codes": {"type": "keyword"}, "amount_cents": {"type": "long"},
    "quarter": {"type": "keyword"}, "known_at": {"type": "date"},
    "explanation": {"type": "text"}, "explanation_semantic": {"type": "semantic_text"},
    "chain_seq": {"type": "long"}, "chain_hash": {"type": "keyword"}}}

QUOTA_FINDINGS = {"properties": {                 # written by the sweep Workflow, read by Quota
    "@timestamp": {"type": "date"}, "query": {"type": "keyword"},
    "raw": {"type": "text"}, "status": {"type": "keyword"}}}   # new | ingested | rejected

FINDINGS_PIPELINE = "quota-findings-timestamp"


def body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def comms_doc(message: dict) -> dict:
    return {**message, "content": f"{message.get('subject') or ''}\n\n{message['body']}".strip(),
            "body_sha256": body_sha256(message["body"])}


def read_jsonl(path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _with_embeddings(client, mapping: dict, *fields) -> dict:
    from quota.search.elastic_client import embedding_id
    inference = embedding_id(client)
    props = {k: (dict(v, inference_id=inference) if k in fields and inference else v)
             for k, v in mapping["properties"].items()}
    return {"properties": props}


def setup(client, out=print) -> None:
    """Create (or recreate) the three indexes; quota-findings stamps @timestamp on write."""
    client.ingest.put_pipeline(id=FINDINGS_PIPELINE,
                               processors=[{"set": {"field": "@timestamp", "value": "{{_ingest.timestamp}}"}}])
    for index, mapping, settings in (
            (COMMS, _with_embeddings(client, QUOTA_COMMS, "content"), None),
            (DECISIONS, _with_embeddings(client, QUOTA_DECISIONS, "explanation_semantic"), None),
            (FINDINGS, QUOTA_FINDINGS, {"index.default_pipeline": FINDINGS_PIPELINE})):
        if client.indices.exists(index=index):
            client.indices.delete(index=index)
        client.indices.create(index=index, mappings=mapping, **({"settings": settings} if settings else {}))
        out(f"  index {index}: created")


def ingest(client, path, batch=50, out=print) -> int:
    """Bulk-load a messages JSONL into quota-comms (embeddings are computed during bulk, so batches stay small)."""
    from elasticsearch import helpers
    docs = [comms_doc(m) for m in read_jsonl(path)]
    ok, errors = helpers.bulk(client, ({"_index": COMMS, "_id": d["doc_id"], **d} for d in docs), chunk_size=batch,
                              raise_on_error=False)
    client.indices.refresh(index=COMMS)
    out(f"  {COMMS}: indexed {ok} of {len(docs)}" + (f" ({len(errors)} errors: {errors[0]})" if errors else ""))
    return ok
