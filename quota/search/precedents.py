"""Precedent recall for approvers (ELASTIC_TASKS E7). When Quota opens an approval for a plan exception, the three
most similar past decisions (precedents and approvals in the mirror) are shown on the approval card. They are
never applied: a policy still matches only by its structured scope."""

import re
import sqlite3

from quota.search.evidence import STOP

KINDS = ("precedent", "approval")


class LocalDecisions:
    """BM25 over mirrored decision documents (offline)."""

    name = "local"

    def __init__(self, docs):
        self.docs = {d["id"]: d for d in docs if d["kind"] in KINDS}
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.execute("CREATE VIRTUAL TABLE d USING fts5(id UNINDEXED, explanation)")
        self.conn.executemany("INSERT INTO d VALUES (?, ?)", [(i, d["explanation"] or "") for i, d in self.docs.items()])

    def similar(self, text, k=3, exclude=()) -> list[dict]:
        words = [w for w in re.findall(r"\w+", text.lower()) if w not in STOP]
        if not words:
            return []
        query = " OR ".join(f'"{w}"' for w in dict.fromkeys(words))
        rows = self.conn.execute("SELECT id, bm25(d) s FROM d WHERE d MATCH ? ORDER BY s, id LIMIT ?",
                                 (query, k + len(exclude))).fetchall()
        return [card(self.docs[i], -s) for i, s in rows if i not in exclude][:k]


class ElasticDecisions:
    name = "elastic"

    def __init__(self, client, index="quota-decisions"):
        self.client, self.index = client, index

    @classmethod
    def library(cls, client, index="quota-recall-eval"):
        """The eval's library in its own index (the live mirror does not hold it)."""
        from elasticsearch import helpers

        from quota.search.indexes import QUOTA_DECISIONS, _with_embeddings
        if client.indices.exists(index=index):
            client.indices.delete(index=index)
        client.indices.create(index=index, mappings=_with_embeddings(client, QUOTA_DECISIONS, "explanation_semantic"))
        helpers.bulk(client, ({"_index": index, "_id": d["id"], **d, "explanation_semantic": d["explanation"]}
                              for d in library_docs()))
        client.indices.refresh(index=index)
        return cls(client, index)

    def similar(self, text, k=3, exclude=()) -> list[dict]:
        hits = self.client.search(index=self.index, size=k + len(exclude), query={"bool": {
            "must": [{"semantic": {"field": "explanation_semantic", "query": text}}],
            "filter": [{"terms": {"kind": list(KINDS)}}]}})["hits"]["hits"]
        return [card(h["_source"], h["_score"] or 0) for h in hits if h["_id"] not in exclude][:k]


def card(doc, score) -> dict:
    return {"id": doc["id"], "kind": doc["kind"], "outcome": doc.get("outcome"), "approver": doc.get("approver"),
            "reason": doc.get("explanation"), "score": round(float(score), 4)}


# ---------- the recall eval: a library of past decisions and six reworded requests ----------

LIBRARY = [
    ("P-1001", "precedent", "granted", "marcus.lee",
     "Book acme on the CRM close date 2026-03-31 instead of the countersignature: the customer signed on the last "
     "day of the quarter and countersignature slipped two days. Q1 treatment for this deal only."),
    ("P-1002", "precedent", "granted", "marcus.lee",
     "Split credit on orbit 60/40 between Alex Chen and Riley Shah although the CRM shows 100% to Alex: the VP's "
     "split record governs."),
    ("P-1003", "precedent", "granted", "priya.nair",
     "Release commission on vale before collection: the customer paid by wire that billing had not yet matched."),
    ("A-1004", "approval", "rejected", "priya.nair",
     "Pay a SPIFF of $2,000 for the Q1 push without a signed SPIFF plan: rejected, promises by a manager are not "
     "a plan."),
    ("P-1005", "precedent", "granted", "marcus.lee",
     "Count the ramped second-year price in commissionable base for summit: the contract fixes both years."),
    ("A-1006", "approval", "approved", "priya.nair",
     "Waive the clawback on cedar after a churn caused by our outage: the Controller approved writing it off."),
    ("P-1007", "precedent", "granted", "marcus.lee",
     "Credit a deal to the new territory owner after the account moved mid-quarter: credit follows the owner at "
     "countersignature."),
    ("A-1008", "approval", "rejected", "marcus.lee",
     "Book delta in Q1 on a CRM close date edited after the period was locked: rejected as backdating."),
]
REQUESTS = [   # (reworded request, the past decision an approver should see)
    ("Jordan's deal closed at the very end of March but our signature came in April. Can it count in Q1 like we "
     "did before?", "P-1001"),
    ("The CRM gives Alex all the credit on the shared deal, but the split record says sixty-forty with Riley.",
     "P-1002"),
    ("The customer's wire landed but billing hasn't matched it yet; can we release the commission now?", "P-1003"),
    ("Morgan promised the team a bonus for the push. Can we pay it this cycle?", "A-1004"),
    ("The churn was our fault, the platform was down for days. Do we still claw the commission back?", "A-1006"),
    ("The account changed hands to a new rep halfway through the quarter. Who gets the credit?", "P-1007"),
]


def library_docs() -> list[dict]:
    return [{"id": i, "kind": kind, "outcome": outcome, "approver": approver, "explanation": text}
            for i, kind, outcome, approver, text in LIBRARY]


def recall_eval(backend) -> dict:
    hits = [(text, want, [s["id"] for s in backend.similar(text, k=3)]) for text, want in REQUESTS]
    found = sum(1 for _, want, got in hits if want in got)
    return {"backend": backend.name, "requests": len(REQUESTS), "hit_at_3": found,
            "rows": [{"request": t, "expected": w, "top3": g, "hit": w in g} for t, w, g in hits]}
