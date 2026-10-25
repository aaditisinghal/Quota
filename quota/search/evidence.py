"""Evidence search over sales messages (ELASTIC_TASKS E3): one interface, two backends.

`ElasticEvidence` searches quota-comms: keyword (BM25 multi_match), semantic (Jina embeddings through
semantic_text) or hybrid (RRF of both, reranked by a cross-encoder when the project has one, as Elastic's
starter does). `LocalEvidence` is the offline fallback: SQLite FTS5 BM25 over the same JSONL, whatever mode is
asked for (every hit says `local_bm25`). `get_backend()` picks Elastic when it is configured and answers.
"""

import re
import sqlite3
from typing import Literal, Protocol

from quota.search.indexes import COMMS, body_sha256, read_jsonl

Mode = Literal["keyword", "semantic", "hybrid"]
FIELDS = ["doc_id", "account", "deal_id", "kind", "sent_at", "subject", "body", "body_sha256", "source_file",
          "source_row", "thread_id", "author", "to", "rep_id", "lang"]
STOP = {"a", "an", "and", "or", "the", "to", "of", "for", "is", "it", "in", "on", "be", "can", "will", "with",
        "than", "not", "without", "against", "at", "by"}


class EvidenceSearch(Protocol):
    def search(self, query: str, mode: Mode, k: int = 10, account: str | None = None, before: str | None = None,
               after: str | None = None) -> list[dict]: ...


def _hit(source: dict, score: float, rank: int, mode: str) -> dict:
    return {**{f: source.get(f) for f in FIELDS}, "body_sha256": source.get("body_sha256") or body_sha256(source["body"]),
            "score": round(float(score), 4), "rank": rank, "mode": mode}


class LocalEvidence:
    """SQLite FTS5 BM25 over a messages JSONL (subject weighted 2x, as the Elastic keyword query is)."""

    name = "local"

    def __init__(self, messages: list[dict] | str):
        rows = read_jsonl(messages) if isinstance(messages, str) else list(messages)
        self.docs = {m["doc_id"]: {**m, "body_sha256": body_sha256(m["body"])} for m in rows}
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.execute("CREATE VIRTUAL TABLE comms USING fts5(doc_id UNINDEXED, subject, body, account UNINDEXED, "
                          "sent_at UNINDEXED, tokenize='unicode61 remove_diacritics 2')")
        self.conn.executemany("INSERT INTO comms VALUES (?, ?, ?, ?, ?)",
                              [(m["doc_id"], m.get("subject") or "", m["body"], m["account"], m["sent_at"])
                               for m in self.docs.values()])

    def add(self, message: dict) -> None:
        self.docs[message["doc_id"]] = {**message, "body_sha256": body_sha256(message["body"])}
        self.conn.execute("INSERT INTO comms VALUES (?, ?, ?, ?, ?)", (message["doc_id"], message.get("subject") or "",
                                                                       message["body"], message["account"],
                                                                       message["sent_at"]))

    def get(self, doc_id) -> dict | None:
        return self.docs.get(doc_id)

    def account_messages(self, account, after=None, before=None) -> list[dict]:
        """Every message on one account in the window, oldest first. Keyword ranking cannot find a promise that
        shares no words with any intent (a paraphrase, another language, "Consider it done."), so the offline
        gate reads the account's whole mailbox (about a dozen messages a deal) instead of a top-k."""
        docs = sorted((m for m in self.docs.values() if m["account"] == account
                       and (after is None or m["sent_at"] >= after) and (before is None or m["sent_at"] <= before)),
                      key=lambda m: (m["sent_at"], m["doc_id"]))
        return [_hit(m, 0.0, rank, "local_scan") for rank, m in enumerate(docs, 1)]

    @staticmethod
    def match(query: str) -> str:
        words = [w for w in re.findall(r"\w+", query.lower()) if w not in STOP]
        return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))

    def search(self, query, mode="hybrid", k=10, account=None, before=None, after=None) -> list[dict]:
        sql, params = "SELECT doc_id, bm25(comms, 0, 2.0, 1.0, 0, 0) AS s FROM comms WHERE comms MATCH ?", [self.match(query)]
        for clause, value in (("account = ?", account), ("sent_at <= ?", before), ("sent_at >= ?", after)):
            if value is not None:
                sql, params = sql + f" AND {clause}", params + [value]
        rows = self.conn.execute(sql + " ORDER BY s, doc_id LIMIT ?", (*params, k)).fetchall()
        return [_hit(self.docs[doc_id], -score, rank, "local_bm25") for rank, (doc_id, score) in enumerate(rows, 1)]


class ElasticEvidence:
    name = "elastic"

    def __init__(self, client=None):
        from quota.search import elastic_client
        self.client = client or elastic_client.es()
        self.reranker = elastic_client.find_reranker(self.client)

    def get(self, doc_id) -> dict | None:
        try:
            return self.client.get(index=COMMS, id=doc_id)["_source"]
        except Exception:
            return None

    @staticmethod
    def filters(account, before, after) -> list[dict]:
        out = [{"term": {"account": account}}] if account else []
        if before or after:
            out.append({"range": {"sent_at": {k: v for k, v in (("lte", before), ("gte", after)) if v}}})
        return out

    def _standard(self, query, filters):
        return {"standard": {"query": {"bool": {"must": [query], "filter": filters}}}}

    def search(self, query, mode="hybrid", k=10, account=None, before=None, after=None) -> list[dict]:
        filters = self.filters(account, before, after)
        keyword = {"multi_match": {"query": query, "fields": ["subject^2", "body"]}}
        semantic = {"semantic": {"field": "content", "query": query}}
        source = [f for f in FIELDS if f != "body_sha256"] + ["body_sha256"]
        if mode == "keyword":
            retriever = self._standard(keyword, filters)
        elif mode == "semantic":
            retriever = self._standard(semantic, filters)
        else:
            rrf = {"rrf": {"retrievers": [self._standard(keyword, filters), self._standard(semantic, filters)],
                           "rank_window_size": 50}}
            retriever = rrf if not self.reranker else {"text_similarity_reranker": {
                "retriever": rrf, "field": "body", "inference_id": self.reranker, "inference_text": query,
                "rank_window_size": 20}}
        try:
            response = self.client.search(index=COMMS, retriever=retriever, size=k, source=source)
        except Exception:
            if mode != "hybrid" or not self.reranker:
                raise
            self.reranker = None                      # reranking failed: plain RRF from now on, as the starter does
            return self.search(query, mode, k, account, before, after)
        label = mode if mode != "hybrid" else ("hybrid_rerank" if self.reranker else "hybrid_rrf")
        return [_hit(h["_source"], h["_score"] or 0, rank, label)
                for rank, h in enumerate(response["hits"]["hits"], 1)]


def get_backend(messages_path="inbox/messages.jsonl", out=print) -> EvidenceSearch:
    """Elastic when it is configured and answers a search, else the local index over the JSONL."""
    from quota.search import elastic_client
    if elastic_client.configured():
        try:
            backend = ElasticEvidence()
            backend.client.search(index=COMMS, size=0)
            out("evidence search: Elastic (quota-comms)")
            return backend
        except Exception as exc:
            out(f"evidence search: Elastic unavailable ({type(exc).__name__}); using local BM25")
    else:
        out("evidence search: local BM25 (Elastic not configured)")
    return LocalEvidence(messages_path)
