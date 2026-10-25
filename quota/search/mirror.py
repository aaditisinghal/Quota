"""A one-way, hash-verified mirror of Quota's decision log in quota-decisions (ELASTIC_TASKS E2).

Quota's SQLite ledger stays the system of record. Each decision, approval, precedent, exception and refusal
becomes one document whose `_id` is its Quota id (so pushing again is idempotent) and which carries the
`chain_seq` and `chain_hash` of the ledger row it mirrors. `verify` compares the count, the highest chain_seq
and that row's hash on both sides.
"""

import json

from quota import config
from quota.engine import calculator
from quota.ledger import queries


def _loads(value):
    return json.loads(value) if isinstance(value, str) else (value or {})


class _Context:
    def __init__(self, conn):
        self.conn = conn
        self.lines = {l["id"]: l for l in queries.fetch_all(conn, "SELECT * FROM commission_lines")}
        self.deals = {d["id"]: d for d in queries.latest_rows(conn, "deals", queries.END_OF_TIME)}
        self.messages = {m["id"]: m for m in queries.fetch_all(conn, "SELECT id, author FROM messages")}
        self.reps = {p["id"] for p in queries.latest_rows(conn, "people", queries.END_OF_TIME) if p["role"] == "AE"}
        self.chain = {}
        for row in queries.fetch_all(conn, "SELECT seq, kind, ref_id, hash FROM chain WHERE kind IN "
                                           "('decisions', 'approvals', 'approval_decisions', 'precedents', 'refusals') "
                                           "ORDER BY seq"):
            table = "approvals" if row["kind"] == "approval_decisions" else row["kind"]
            self.chain[(table, row["ref_id"])] = (row["seq"], row["hash"])     # the newest row wins

    def about(self, subject: str | None) -> dict:
        """rep, deal and account a subject id points at, when it points at one."""
        deal = rep = None
        subject = subject or ""
        if subject in self.lines:
            deal, rep = self.lines[subject]["deal_id"], self.lines[subject]["rep_id"]
        elif "/" in subject:
            deal, rep = subject.split("/")[0].split("+")[0], subject.split("/")[1]
        elif subject in self.deals:
            deal = subject
        elif subject in self.messages and self.messages[subject]["author"] in self.reps:
            rep = self.messages[subject]["author"]        # a rep's own message (a dispute), not an approver's grant
        if deal and not rep and deal in self.deals:
            rep = self.deals[deal]["owner_id"]
        return {"deal_id": deal, "rep_id": rep, "account": self.deals[deal]["account"] if deal in self.deals else None}

    def quarter(self, subject, known_at) -> str:
        line = self.lines.get(subject or "")
        return line["booking_quarter"] if line else calculator.quarter_of(known_at)

    def stamp(self, table, row_id) -> dict:
        seq, digest = self.chain.get((table, row_id), (None, None))
        return {"chain_seq": seq, "chain_hash": digest}


def _amount(output: dict, line: dict | None):
    for part in (output.get("result"), output.get("args")):
        if isinstance(part, dict) and isinstance(part.get("amount_cents"), int):
            return part["amount_cents"]
    return line["amount_cents"] if line else None


def documents(conn) -> list[dict]:
    """Every mirrored document, from the ledger alone (pure over the connection)."""
    ctx, out = _Context(conn), []
    # A hold that opened an approval (an off-contract promise's commitment_review) is worth the approval's amount.
    opened = {a["subject_id"]: a["amount_cents"] for a in queries.fetch_all(conn, "SELECT * FROM approvals ORDER BY id")}
    for d in queries.fetch_all(conn, "SELECT * FROM decisions ORDER BY id"):
        output = _loads(d["output_json"])
        result = output.get("result") if isinstance(output.get("result"), dict) else {}
        code = output.get("reason_code") or result.get("reason_code") or d["kind"].upper()
        # A dispute Quota "held" means it upheld its own answer; in the mirror, "held" means commission is held.
        outcome = "upheld" if d["kind"] in ("dispute", "escalation") and d["outcome"] == "held" else d["outcome"]
        out.append({"id": d["id"], "kind": "decision", "seat": d["seat"], "subject_id": d["subject_id"],
                    **ctx.about(d["subject_id"]), "approver": None, "outcome": outcome, "reason_code": code,
                    "reason_codes": [code],
                    "amount_cents": _amount(output, ctx.lines.get(d["subject_id"])) or opened.get(d["id"]),
                    "quarter": ctx.quarter(d["subject_id"], d["known_at"]), "known_at": d["known_at"],
                    "explanation": d["explanation"], **ctx.stamp("decisions", d["id"])})
    for a in queries.fetch_all(conn, "SELECT * FROM approvals ORDER BY id"):
        decided = queries.fetch_one(conn, "SELECT * FROM approval_decisions WHERE approval_id = ? ORDER BY rowid DESC",
                                    (a["id"],))
        outcome = decided["decision"] if decided else "pending"
        limit = config.principals().get(a["approver"], {}).get("limit")
        routed = queries.fetch_one(conn, "SELECT id, approver FROM approvals WHERE parent_id = ?", (a["id"],))
        over = limit is not None and a["amount_cents"] > limit
        detail = (f"{decided['decision']}, {decided['result']}" + (f" to {routed['id']} ({routed['approver']})" if routed else "")
                  + f": {decided['reason']}") if decided else "pending"
        limit_text = f", limit ${limit / 100:,.2f}" + (" (over the limit)" if over else "") if limit is not None else ""
        out.append({"id": a["id"], "kind": "approval", "seat": a["requested_by"], "subject_id": a["subject_id"],
                    **ctx.about(a["subject_id"]), "approver": a["approver"], "outcome": outcome,
                    "reason_code": a["kind"].upper(), "reason_codes": [a["kind"].upper()],
                    "amount_cents": a["amount_cents"], "quarter": calculator.quarter_of(a["known_at"]),
                    "known_at": a["known_at"],
                    "explanation": f"{a['kind']} {a['subject_id']} ${a['amount_cents'] / 100:,.2f} for {a['approver']}"
                                   f"{limit_text}" + (f", routed from {a['parent_id']}" if a["parent_id"] else "")
                                   + f": {detail}",
                    **ctx.stamp("approvals", a["id"])})
    for p in queries.fetch_all(conn, "SELECT * FROM precedents ORDER BY id"):
        scope, approvers = _loads(p["scope_json"]), _loads(p["approvals_json"])
        about = ctx.about(scope.get("deal_id"))
        doc = {"id": p["id"], "kind": "precedent", "seat": None, "subject_id": scope.get("deal_id") or scope.get("kind"),
               **about, "approver": approvers[-1] if approvers else None, "outcome": "granted",
               "reason_code": "PRECEDENT", "reason_codes": ["PRECEDENT"], "amount_cents": None,
               "quarter": calculator.quarter_of(p["known_at"]), "known_at": p["known_at"],
               "explanation": p["statement"], **ctx.stamp("precedents", p["id"])}
        out.append(doc)
        if scope.get("deal_id"):          # a granted plan exception, for the pattern questions
            out.append({**doc, "id": f"{p['id']}:exception", "kind": "exception", "reason_code": "PLAN_EXCEPTION",
                        "reason_codes": ["PLAN_EXCEPTION"]})
    for r in queries.fetch_all(conn, "SELECT * FROM refusals ORDER BY id"):
        codes = _loads(r["reason_codes_json"]) or ["REFUSED"]
        out.append({"id": r["id"], "kind": "refusal", "seat": r["actor"], "subject_id": r["subject_id"],
                    **ctx.about(r["subject_id"]), "approver": None, "outcome": "refused", "reason_code": codes[0],
                    "reason_codes": codes, "amount_cents": None, "quarter": calculator.quarter_of(r["known_at"]),
                    "known_at": r["known_at"], "explanation": f"{r['action']} {r['subject_id']}: {r['detail']}",
                    **ctx.stamp("refusals", r["id"])})
    for doc in out:
        doc["explanation_semantic"] = doc["explanation"]
    return out


def head(docs) -> dict:
    """What verify compares: the count, the highest chain_seq, and that row's hash."""
    stamped = [d for d in docs if d.get("chain_seq") is not None]
    top = max(stamped, key=lambda d: d["chain_seq"]) if stamped else {"chain_seq": None, "chain_hash": None}
    return {"count": len(docs), "seq": top["chain_seq"], "hash": top["chain_hash"]}


# ---------- Elastic side (only when the project is configured) ----------

def push(client, docs, index="quota-decisions") -> int:
    from elasticsearch import helpers
    ok, _ = helpers.bulk(client, ({"_index": index, "_id": d["id"], **d} for d in docs), chunk_size=200,
                         raise_on_error=False)
    client.indices.refresh(index=index)
    return ok


def mirrored_head(client, index="quota-decisions") -> dict:
    count = client.count(index=index)["count"]
    top = client.search(index=index, size=1, sort=[{"chain_seq": "desc"}], source=["chain_seq", "chain_hash"],
                        query={"exists": {"field": "chain_seq"}})["hits"]["hits"]
    src = top[0]["_source"] if top else {}
    return {"count": count, "seq": src.get("chain_seq"), "hash": src.get("chain_hash")}


def mirror_decisions(conn, client, full=False) -> int:
    """Push the documents whose chain row is newer than the mirror's newest (decided approvals move up); `full`
    pushes every document again (after the mapping itself changed)."""
    docs = documents(conn)
    try:
        since = 0 if full else mirrored_head(client)["seq"] or 0
    except Exception:
        since = 0
    return push(client, [d for d in docs if (d.get("chain_seq") or 0) > since or d.get("chain_seq") is None])


def compare(local: dict, remote: dict) -> str:
    if local == remote:
        return f"MIRROR OK seq={local['seq']} hash={(local['hash'] or '')[:12]}… ({local['count']} documents)"
    first = next(k for k in ("count", "seq", "hash") if local[k] != remote[k])
    return f"MIRROR MISMATCH {first}: ledger {local[first]} vs mirror {remote[first]}"


def verify(conn, client) -> dict:
    local, remote = head(documents(conn)), mirrored_head(client)
    return {"ok": local == remote, "ledger": local, "mirror": remote, "line": compare(local, remote)}


class Mirror:
    """The serve / demo / eval hook: mirror after every action when Elastic is configured. A failure (no
    network, a key without privileges) is logged once and turns the mirror off for the process; Quota's own
    ledger is never affected."""

    def __init__(self, out=print):
        self.out, self.disabled, self.client = out, False, None

    def __call__(self, conn) -> int | None:
        from quota.search import elastic_client
        if self.disabled or not elastic_client.configured():
            return None
        try:
            self.client = self.client or elastic_client.es()
            return mirror_decisions(conn, self.client)
        except Exception as exc:
            self.disabled = True
            self.out(f"Elastic mirror off for this run: {type(exc).__name__}: {str(exc)[:160]}")
            return None
