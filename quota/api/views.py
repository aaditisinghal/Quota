"""Read-only views behind the API: timeline objects, the trace graph, ledger health, Today. Everything reads
through the read-only connection; nothing here writes."""

import json
import re

from quota import config
from quota.ledger import hashing, invariants, queries
from quota.timeline import format_line

ID_KINDS = {"JE": "je", "CL": "line", "D": "decision", "A": "approval", "P": "precedent", "PR": "payroll",
            "R": "refusal", "M": "message", "AS": "schedule", "EV": "event"}
ID_PATTERN = re.compile(r"\b(JE|CL|D|A|P|PR|R|M|AS|EV)-[0-9][0-9-]*\b")
# Which column of the source row a resolution's basis was read from (highlighted in the source snippet).
BASIS_COLUMNS = {"countersignature": ["countersigned_at", "at"], "contract_date": ["contract_date"],
                 "crm_close_date": ["crm_close_date"], "contract": ["acv_cents"], "amendment_increase": ["acv_cents"],
                 "amendment": ["clauses_json"], "owner_of_record": ["owner_id", "splits_json"],
                 "precedent": ["override_json"]}
HIDDEN = {"known_at"}


def basis_columns(field: str, basis: str | None) -> list[str]:
    if basis == "crm":
        return ["splits_json"] if field == "splits" else ["crm_amount_cents"]
    return BASIS_COLUMNS.get(basis, [])


def kind_of(object_id: str) -> str | None:
    prefix = object_id.split("-", 1)[0]
    return ID_KINDS.get(prefix)


def _loads(value):
    return json.loads(value) if isinstance(value, str) else value


# ---------- timeline ----------

def timeline(world, since: int = 0) -> list[dict]:
    out = []
    for item in world.feed[since:]:
        entry = {**item, "line": format_line(item["clock"], item["actor"], item["kind"], item["text"]),
                 "refs": sorted(set(m.group(0) for m in ID_PATTERN.finditer(item["text"])))}
        if item["kind"] == "REFUSED":
            codes = item["text"].split(": ", 1)[1].split(" (")[0] if ": " in item["text"] else ""
            entry["codes"] = [c.strip() for c in codes.split(",") if c.strip()]
        out.append(entry)
    return out


# ---------- trace ----------

class Graph:
    def __init__(self, ro, as_of):
        self.ro, self.as_of = ro, as_of
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []

    def known(self, row) -> bool:
        return row is not None and (self.as_of is None or row["known_at"] <= self.as_of)

    def node(self, node_id, kind, label, **fields) -> bool:
        if node_id in self.nodes:
            return False
        self.nodes[node_id] = {"id": node_id, "kind": kind, "label": label, **fields}
        return True

    def edge(self, a, b, label):
        if a in self.nodes and b in self.nodes and not any({e["from"], e["to"]} == {a, b} for e in self.edges):
            self.edges.append({"from": a, "to": b, "label": label})

    def get(self, table, row_id):
        row = queries.as_of(self.ro, table, row_id, self.as_of)
        return row if self.known(row) else None

    # ----- expanders: each adds its node and returns its id (or None if not known as of the slider) -----

    def expand(self, kind, object_id, depth):
        return getattr(self, f"_{kind}")(object_id, depth)

    def _je(self, je_id, depth):
        entry = self.get("journal_entries", je_id)
        if entry is None:
            return None
        lines = queries.lines_for(self.ro, je_id)
        total = sum(l["debit_cents"] for l in lines)
        if not self.node(je_id, "je", f"{je_id} {entry['memo']}", amount_cents=total, period=entry["period"],
                         effective_date=entry["effective_date"], status=queries.je_status(self.ro, je_id, self.as_of),
                         created_by=entry["created_by"], known_at=entry["known_at"],
                         lines=[{"account": l["account"], "name": config.ACCOUNTS[l["account"]][0],
                                 "debit_cents": l["debit_cents"], "credit_cents": l["credit_cents"],
                                 "subledger_ref": l["subledger_ref"]} for l in lines]) or depth <= 0:
            return je_id
        if entry["decision_id"]:
            self.edge(je_id, self._decision(entry["decision_id"], depth - 1), "decided by")
        if entry["source_ref"]:
            billing = self.get("billing", entry["source_ref"])
            if billing:
                self.node(f"billing:{billing['id']}", "billing", f"{billing['kind']} {billing['id']}",
                          amount_cents=billing["amount_cents"], external_ref=billing["external_ref"], at=billing["at"],
                          known_at=billing["known_at"])
                self.edge(je_id, f"billing:{billing['id']}", "source")
            if entry["source_ref"].startswith("PR-"):
                self.edge(je_id, self._payroll(entry["source_ref"], depth - 1), "pays")
        for approval in queries.fetch_all(self.ro, "SELECT id FROM approvals WHERE kind = 'journal_entry' "
                                                   "AND subject_id = ?", (je_id,)):
            self.edge(je_id, self._approval(approval["id"], depth - 1), "approval")
        return je_id

    def _line(self, line_id, depth):
        line = self.get("commission_lines", line_id)
        if line is None:
            return None
        if not self.node(line_id, "line", f"{line_id} {line['deal_id']}/{line['rep_id']} {line['booking_quarter']}",
                         amount_cents=line["amount_cents"], credit_cents=line["credit_cents"], rep=line["rep_id"],
                         deal=line["deal_id"], quarter=line["booking_quarter"], known_at=line["known_at"],
                         status=queries.line_status(self.ro, line_id, self.as_of)) or depth <= 0:
            return line_id
        self.edge(line_id, self._decision(line["decision_id"], depth - 1), "calculated in")
        if line["corrects_id"]:
            self.edge(line_id, self._line(line["corrects_id"], 0), "corrects")
        for child in queries.fetch_all(self.ro, "SELECT id FROM commission_lines WHERE corrects_id = ?", (line_id,)):
            self.edge(line_id, self._line(child["id"], 0), "corrected by")
        about = [d["id"] for d in queries.fetch_all(self.ro, "SELECT id FROM decisions WHERE subject_id = ?", (line_id,))]
        for decision_id in [line["decision_id"], *about]:
            for entry in queries.fetch_all(self.ro, "SELECT id FROM journal_entries WHERE decision_id = ?", (decision_id,)):
                self.edge(line_id, self._je(entry["id"], depth - 1), "entry")
        for payroll in queries.fetch_all(self.ro, "SELECT id, lines_json FROM payroll_files"):
            if any(pl["line_id"] == line_id for pl in json.loads(payroll["lines_json"])):
                self.edge(line_id, self._payroll(payroll["id"], 0), "in payroll")
        return line_id

    def _decision(self, decision_id, depth):
        decision = self.get("decisions", decision_id)
        if decision is None:
            return None
        output = _loads(decision["output_json"])
        result = output.get("result") or {}
        amount = result.get("amount_cents") if isinstance(result, dict) else None
        matches, _, _ = queries.replay_decision(self.ro, decision_id)
        if not self.node(decision_id, "decision", f"{decision_id} {decision['kind']} {decision['subject_id']}",
                         amount_cents=amount, seat=decision["seat"], outcome=decision["outcome"],
                         explanation=decision["explanation"], fn=output.get("fn"), known_at=decision["known_at"],
                         model=decision["model"], replay="MATCH" if matches else "MISMATCH") or depth <= 0:
            return decision_id
        for ref in _loads(decision["inputs_json"]):
            row = queries.load_input(self.ro, ref)
            key = f"{ref['table']}:{ref['id']}" + (f":v{ref['version']}" if ref.get("version") is not None else "")
            newer = None
            if ref.get("version") is not None:
                latest = queries.as_of(self.ro, ref["table"], ref["id"], self.as_of)
                if latest and latest["version"] > ref["version"]:
                    newer = f"v{latest['version']} known {latest['known_at']}"
            snippet = {k: v for k, v in (row or {}).items() if k not in HIDDEN}
            self.node(key, "input", key, table=ref["table"], sha256=ref["sha256"], snippet=snippet, highlight=[],
                      known_at=(row or {}).get("known_at"), newer_version=newer)
            self.edge(decision_id, key, "read")
            linked = {"commission_lines": "line", "approvals": "approval", "journal_entries": "je"}.get(ref["table"])
            if linked:
                self.edge(key, self.expand(linked, ref["id"], 0), "is")
        for field, res in _loads(decision["resolutions_json"]).items():
            res_id = f"{decision_id}:{field}"
            self.node(res_id, "resolution", f"{field} = {res['value']}", field=field, value=res["value"],
                      source=res["source"], rule=res["rule_id"], basis=res.get("basis"),
                      disagreements=res.get("disagreements", []))
            self.edge(decision_id, res_id, "resolved")
            for source, basis, role in [(res["source"], res.get("basis"), "chosen")] + \
                    [(d["source"], d.get("basis"), "alternative") for d in res.get("disagreements", [])]:
                if source in self.nodes:
                    node = self.nodes[source]
                    node["highlight"] += [{"field": f, "role": role, "resolves": field}
                                          for f in basis_columns(field, basis) if f in node["snippet"]]
                    self.edge(res_id, source, role)
                elif source.startswith("precedent:"):
                    self.edge(res_id, self._precedent(source.split(":", 1)[1], depth - 1), role)
        for precedent_id in _loads(decision["precedent_ids_json"]):
            self.edge(decision_id, self._precedent(precedent_id, depth - 1), "precedent")
        return decision_id

    def _approval(self, approval_id, depth):
        approval = self.get("approvals", approval_id)
        if approval is None:
            return None
        decided = queries.approval_decision(self.ro, approval_id, self.as_of)
        if not self.node(approval_id, "approval", f"{approval_id} {approval['kind']} {approval['subject_id']}",
                         approval_kind=approval["kind"], subject=approval["subject_id"],
                         amount_cents=approval["amount_cents"], approver=approval["approver"],
                         requested_by=approval["requested_by"], known_at=approval["known_at"],
                         decision=decided["decision"] if decided else "pending",
                         result=decided["result"] if decided else None, reason=decided["reason"] if decided else None,
                         decided_at=decided["known_at"] if decided else None) or depth <= 0:
            return approval_id
        if approval["parent_id"]:
            self.edge(approval_id, self._approval(approval["parent_id"], depth - 1), "routed from")
        for child in queries.fetch_all(self.ro, "SELECT id FROM approvals WHERE parent_id = ?", (approval_id,)):
            self.edge(approval_id, self._approval(child["id"], depth - 1), "routed to")
        subject = approval["subject_id"]
        kind = kind_of(subject)
        if kind in ("message", "je", "payroll"):
            self.edge(approval_id, self.expand(kind, subject, 0), "about")
        for precedent in queries.fetch_all(self.ro, "SELECT id, approvals_json FROM precedents"):
            if approval_id in json.loads(precedent["approvals_json"]):
                self.edge(approval_id, self._precedent(precedent["id"], 0), "made")
        return approval_id

    def _precedent(self, precedent_id, depth):
        precedent = self.get("precedents", precedent_id)
        if precedent is None:
            return None
        if not self.node(precedent_id, "precedent", f"{precedent_id} {precedent['statement']}",
                         scope=_loads(precedent["scope_json"]), override=_loads(precedent["override_json"]),
                         effective_from=precedent["effective_from"], expires_at=precedent["expires_at"],
                         known_at=precedent["known_at"]) or depth <= 0:
            return precedent_id
        for approval_id in _loads(precedent["approvals_json"]):
            if approval_id.startswith("A-"):
                self.edge(precedent_id, self._approval(approval_id, depth - 1), "approved by")
        return precedent_id

    def _payroll(self, file_id, depth):
        payroll = self.get("payroll_files", file_id)
        if payroll is None:
            return None
        lines = json.loads(payroll["lines_json"])
        status = queries.latest_status(self.ro, "payroll_status", "file_id", file_id, self.as_of)
        if not self.node(file_id, "payroll", f"{file_id} {len(lines)} line(s)", amount_cents=payroll["total_cents"],
                         period=payroll["period"], status=status, known_at=payroll["known_at"]) or depth <= 0:
            return file_id
        for pl in lines:
            self.edge(file_id, self._line(pl["line_id"], 0), "pays")
        for approval in queries.fetch_all(self.ro, "SELECT id FROM approvals WHERE kind = 'payroll_file' "
                                                   "AND subject_id = ?", (file_id,)):
            self.edge(file_id, self._approval(approval["id"], depth - 1), "approval")
        return file_id

    def _refusal(self, refusal_id, depth):
        refusal = self.get("refusals", refusal_id)
        if refusal is None:
            return None
        self.node(refusal_id, "refusal", f"{refusal_id} {refusal['action']} {refusal['subject_id'] or ''}",
                  actor=refusal["actor"], action=refusal["action"], subject=refusal["subject_id"],
                  codes=_loads(refusal["reason_codes_json"]), detail=refusal["detail"], known_at=refusal["known_at"])
        subject = refusal["subject_id"] or ""
        kind = kind_of(subject)
        if depth > 0 and kind in ("approval", "je", "payroll", "line"):
            self.edge(refusal_id, self.expand(kind, subject, depth - 1), "refused on")
        return refusal_id

    def _message(self, message_id, depth):
        message = self.get("messages", message_id)
        if message is None:
            return None
        self.node(message_id, "message", f"{message_id} {message['author']} -> {message['to_principal']}",
                  author=message["author"], to=message["to_principal"], text=message["text"],
                  known_at=message["known_at"])
        if depth > 0:
            for decision_id in message_decisions(self.ro, message):
                self.edge(message_id, self._decision(decision_id, depth - 1), "decided in")
        return message_id

    def _schedule(self, schedule_id, depth):
        schedule = self.get("amortization_schedules", schedule_id)
        if schedule is None:
            return None
        months = json.loads(schedule["schedule_json"])
        self.node(schedule_id, "schedule", f"{schedule_id} {schedule['line_id']} {schedule['status']}",
                  amount_cents=schedule["total_cents"], months=schedule["months"], start=schedule["start_period"],
                  monthly_cents=months[0][1] if months else None, status=schedule["status"],
                  known_at=schedule["known_at"])
        if depth > 0:
            self.edge(schedule_id, self._line(schedule["line_id"], depth - 1), "amortizes")
        return schedule_id

    def _deal(self, deal_id, depth):
        deal = self.get("deals", deal_id)
        if deal is None:
            return None
        node_id = f"deal:{deal_id}"
        if not self.node(node_id, "deal", f"{deal_id} {deal['account']}", amount_cents=deal["crm_amount_cents"],
                         snippet={k: v for k, v in deal.items() if k not in HIDDEN}, known_at=deal["known_at"])                 or depth <= 0:
            return node_id
        for line in queries.fetch_all(self.ro, "SELECT id FROM commission_lines WHERE deal_id = ? ORDER BY id",
                                      (deal_id,)):
            self.edge(node_id, self._line(line["id"], depth - 1), "line")
        return node_id

    def _account(self, account, depth):
        """A trial-balance figure: the account's balance as of the slider, and the posted entries behind it."""
        if account not in config.ACCOUNTS:
            return None
        node_id = f"account:{account}"
        entries = [e for e in queries.journal(self.ro, self.as_of) if e["status"] == "posted"
                   and any(l["account"] == account for l in e["lines"])]
        balance = sum(l["debit_cents"] - l["credit_cents"] for e in entries for l in e["lines"] if l["account"] == account)
        if not self.node(node_id, "account", f"{account} {config.ACCOUNTS[account][0]}", amount_cents=balance,
                         entries=len(entries)) or depth <= 0:
            return node_id
        for entry in entries:
            self.edge(node_id, self._je(entry["id"], 0), "posted in")
        return node_id

    def _event(self, event_id, depth):
        event = self.get("events", event_id)
        if event is None:
            return None
        self.node(event_id, "event", f"{event_id} {event['type']}", payload=_loads(event["payload_json"]),
                  release_at=event["release_at"], known_at=event["known_at"])
        return event_id


def message_decisions(ro, message) -> list[str]:
    """The decisions a message belongs to: for a person's message, the decisions that read it; for a seat's
    message, the decisions that seat recorded at the same instant (the reply is written by the decision)."""
    read = queries.fetch_all(ro, "SELECT id FROM decisions WHERE json_extract(output_json, '$.args.message_id') = ? "
                                 "ORDER BY id", (message["id"],))
    same = queries.fetch_all(ro, "SELECT id FROM decisions WHERE seat = ? AND known_at = ? ORDER BY id",
                             (message["author"], message["known_at"])) if message["author"].startswith("seat:") else []
    return [d["id"] for d in read + same]


def trace(ro, kind: str, object_id: str, as_of: str | None = None, depth: int = 3) -> dict:
    graph = Graph(ro, as_of)
    if kind not in set(ID_KINDS.values()) | {"deal", "account"}:
        raise KeyError(f"unknown kind {kind}")
    root = graph.expand(kind, object_id, depth)
    return {"root": root, "requested": object_id, "kind": kind, "as_of": as_of, "known": root is not None,
            "nodes": list(graph.nodes.values()), "edges": graph.edges}


# ---------- ledger health ----------

def _gl(tb, account):
    return tb.get(account, 0)


def tie_outs(ro, t=None) -> list[dict]:
    """Subledger to GL: each pair is computed from different records, so they agree only if the books do."""
    tb = queries.trial_balance(ro, t)
    lines = [{**l, "status": queries.line_status(ro, l["id"], t)}
             for l in queries.fetch_all(ro, "SELECT * FROM commission_lines WHERE known_at <= ?", (t or queries.END_OF_TIME,))]
    payable = sum(l["amount_cents"] for l in lines if l["amount_cents"] > 0 and l["status"] == "approved")
    recoverable = sum(-l["amount_cents"] for l in lines if l["amount_cents"] < 0 and l["status"] == "approved")
    amortized: dict[str, int] = {}
    in_transit = 0
    for entry in queries.journal(ro, t):
        kind = queries.memo_kind(entry["memo"])
        for l in entry["lines"]:
            if kind == "amortization" and entry["status"] == "posted" and l["account"] == "6110":
                amortized[l["subledger_ref"]] = amortized.get(l["subledger_ref"], 0) + l["debit_cents"]
            if kind in ("clawback", "void_reversal") and entry["status"] != "posted" and l["account"] == "1350":
                in_transit += l["credit_cents"]
    schedules = queries.fetch_all(ro, "SELECT * FROM amortization_schedules WHERE known_at <= ?",
                                  (t or queries.END_OF_TIME,))
    cancelled = {s["id"][:-2] for s in schedules if s["status"] == "cancelled"}
    remaining = sum(s["total_cents"] - amortized.get(s["line_id"], 0) for s in schedules
                    if s["status"] == "active" and s["id"] not in cancelled)
    billing = queries.fetch_all(ro, "SELECT kind, amount_cents FROM billing WHERE known_at <= ?", (t or queries.END_OF_TIME,))
    receivable = sum(b["amount_cents"] if b["kind"] == "invoice" else -b["amount_cents"]
                     for b in billing if b["kind"] in ("invoice", "payment"))
    pairs = [
        ("Deferred commissions (1350)", "unamortized schedules + recoveries awaiting approval", _gl(tb, "1350"),
         remaining + in_transit),
        ("Commissions payable (2150)", "approved, unpaid commission lines", -_gl(tb, "2150"), payable),
        ("Due from employees (1290)", "approved recovery lines", _gl(tb, "1290"), recoverable),
        ("Accounts receivable (1200)", "invoices less payments in billing", _gl(tb, "1200"), receivable),
    ]
    return [{"name": n, "subledger": s, "gl_cents": g, "subledger_cents": v, "ok": g == v} for n, s, g, v in pairs]


def health(world) -> dict:
    ro = world.ro
    checks = invariants.check_all(ro)
    chain_ok, bad_seq = hashing.verify_chain(ro)
    tb = queries.trial_balance(ro)
    periods = {}
    for row in queries.fetch_all(ro, "SELECT * FROM periods ORDER BY known_at, rowid"):
        periods[row["period"]] = {"period": row["period"], "status": row["status"], "since": row["known_at"],
                                  "by": row["actor"]}
    return {
        "clock": world.clock.now(),
        "invariants": [{"name": name, "ok": not violations,
                        "violations": [{"subject": v.subject, "detail": v.detail} for v in violations]}
                       for name, violations in checks.items()]
                      + [{"name": "INV8", "ok": chain_ok, "violations": [] if chain_ok else
                          [{"subject": f"chain seq {bad_seq}", "detail": "hash chain breaks here"}]}],
        "chain": {"ok": chain_ok, "rows": hashing.chain_length(ro), "first_bad_seq": bad_seq},
        "trial_balance": {"accounts": [{"account": a, "name": config.ACCOUNTS[a][0], "balance_cents": b}
                                       for a, b in tb.items()],
                          "debits_cents": sum(b for b in tb.values() if b > 0),
                          "credits_cents": -sum(b for b in tb.values() if b < 0)},
        "tie_outs": tie_outs(ro),
        "periods": sorted(periods.values(), key=lambda p: p["period"]),
    }


# ---------- Today ----------

def today(world) -> dict:
    ro, now = world.ro, world.clock.now()
    day = now[:10]
    done = [{"id": d["id"], "kind": d["kind"], "subject": d["subject_id"], "outcome": d["outcome"],
             "explanation": d["explanation"], "seat": d["seat"], "at": d["known_at"]}
            for d in queries.fetch_all(ro, "SELECT * FROM decisions WHERE substr(known_at, 1, 10) = ? ORDER BY id", (day,))]
    posted = [{"id": e["id"], "memo": e["memo"], "amount_cents": sum(l["debit_cents"] for l in e["lines"])}
              for e in queries.journal(ro) if e["status"] == "posted" and e["known_at"][:10] == day]
    waiting: dict[str, dict] = {}
    for a in queries.pending_approvals(ro, now):
        who = waiting.setdefault(a["approver"], {"approver": a["approver"], "name": config.display_name(a["approver"]),
                                                 "title": config.title(a["approver"]), "items": [], "total_cents": 0})
        who["items"].append({"id": a["id"], "kind": a["kind"], "subject": a["subject_id"],
                             "amount_cents": a["amount_cents"], "since": a["known_at"]})
        who["total_cents"] += a["amount_cents"]
    held = []
    lines = {l["id"]: l for l in queries.fetch_all(ro, "SELECT * FROM commission_lines")}
    decisions = queries.fetch_all(ro, "SELECT * FROM decisions ORDER BY id")
    for d in decisions:
        # A hold stands until a later decision on the same subject says otherwise.
        if d["outcome"] != "held" or any(later["subject_id"] == d["subject_id"] and later["id"] > d["id"]
                                         and later["outcome"] != "held" for later in decisions):
            continue
        output = _loads(d["output_json"])
        if d["kind"] == "termination_review":
            ids = output["result"]["held"]
        elif d["kind"] == "dispute":
            if queries.line_status(ro, d["subject_id"]) != "draft":
                continue                                    # the line was approved, corrected or superseded since
            ids = [d["subject_id"]]
        else:
            ids = [l for l, row in lines.items() if row["deal_id"] == d["subject_id"]]
        if d["kind"] == "collection_hold" and queries.collected(ro, d["subject_id"], now):
            continue
        amount = (output["result"]["reply_fields"]["diff_cents"] if d["kind"] == "dispute"
                  else sum(lines[i]["amount_cents"] for i in ids))
        held.append({"decision": d["id"], "kind": d["kind"], "subject": d["subject_id"], "reason": d["explanation"],
                     "lines": ids, "amount_cents": amount, "since": d["known_at"]})
    return {"clock": now, "day": day, "done": done, "posted": posted,
            "waiting": sorted(waiting.values(), key=lambda w: -w["total_cents"]),
            "held": held, "held_total_cents": sum(h["amount_cents"] for h in held)}
