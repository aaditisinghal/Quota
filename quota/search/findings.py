"""The findings poller (ELASTIC_TASKS E6). The sweep Workflow runs in Elastic and writes what its agent judged to
quota-findings; Elastic Cloud cannot call a laptop, so Quota pulls. For each new finding: parse the agent's raw
JSON (code fences tolerated), fetch the message, apply E4's verbatim guard and contract check in code, and
emit an `evidence.found` event. The workflow never holds anything; the seat (E5) does, through its own gate.
"""

import json
import re
from typing import Protocol

from quota.agent import commitments
from quota.ledger import queries


class FindingsStore(Protocol):
    def new(self) -> list[dict]: ...                       # [{"id", "raw"}]
    def mark(self, finding_id: str, status: str) -> None: ...


class ElasticFindings:
    def __init__(self, client, index="quota-findings"):
        self.client, self.index = client, index

    def new(self) -> list[dict]:
        hits = self.client.search(index=self.index, size=50, query={"term": {"status": "new"}},
                                  sort=[{"@timestamp": "asc"}])["hits"]["hits"]
        return [{"id": h["_id"], "raw": h["_source"].get("raw", "")} for h in hits]

    def mark(self, finding_id, status) -> None:
        self.client.update(index=self.index, id=finding_id, doc={"status": status}, refresh=True)


def parse(raw: str) -> list[dict]:
    """The agent was asked for a bare JSON array; tolerate a ```json fence or prose around it."""
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("["):
        start, end = text.find("["), text.rfind("]")
        text = text[start:end + 1] if start >= 0 and end > start else "[]"
    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        return []
    return [i for i in items if isinstance(i, dict) and i.get("doc_id") and i.get("quote")]


def poll(store: FindingsStore, evidence, world, out=print) -> list[str]:
    """Turn new findings into evidence.found events. Returns the doc ids that passed the code checks."""
    passed = []
    for finding in store.new():
        items, kept = parse(finding["raw"]), []
        for item in items:
            doc = evidence.get(item["doc_id"])
            if doc is None or not commitments.verbatim(item["quote"], doc["body"]):
                continue                                          # a quote not in the message is dropped
            found = queries.fetch_one(world.ro, "SELECT id FROM contracts WHERE deal_id = ? ORDER BY version DESC",
                                      (doc["deal_id"],))
            contract = queries.as_of(world.ro, "contracts", found["id"], None) if found else None
            clauses = queries.loads(contract["clauses_json"]) if contract else {}
            if commitments.covered(item.get("type", ""), item["quote"], clauses):
                continue
            world.queue.emit_now("evidence.found", {"doc_id": doc["doc_id"], "deal_id": doc["deal_id"],
                                                    "account": doc["account"], "type": item.get("type") or "",
                                                    "quote": item["quote"], "finding_id": finding["id"]})
            kept.append(doc["doc_id"])
        store.mark(finding["id"], "ingested" if kept else "rejected")
        out(f"finding {finding['id']}: {len(items)} candidate(s), {len(kept)} passed the code checks")
        passed += kept
    return passed
