"""WritePath: the ONLY writer.

Every method checks every rule it owns, collects every violated reason code, and either writes
(one transaction, one chain row per inserted row) or logs a refusal and raises Refused.
"""

import json
from typing import NoReturn

from quota import clock as clk
from quota import config
from quota.engine import entries, ranking, routing
from quota.money import fmt_usd
from quota.ledger import hashing, queries
from quota.ledger.invariants import entry_balanced

FACT_TABLES = {"people", "deals", "contracts", "esign_events", "plans", "billing", "events", "periods",
               "precedents"}
FACT_CAPABILITY = {"billing": "post:source_entry", "events": "emit:event"}   # all other fact tables: write:fact
LINE_STATUS_CAPABILITY = {
    "superseded": ("draft:commission_line",), "clawed_back": ("draft:commission_line",),
    # A seat may mark a line approved only once the Controller approved a posted entry for it (checked below).
    "approved": ("approve:journal_entry", "draft:journal_entry"), "paid": ("release:payroll_file",),
}
ID_PREFIX = {"journal_entries": "JE", "decisions": "D", "approvals": "A", "commission_lines": "CL",
             "precedents": "P", "refusals": "R", "events": "EV", "messages": "M",
             "amortization_schedules": "AS"}


class Refused(Exception):
    def __init__(self, refusal_id: str, reason_codes: list[str], detail: str):
        super().__init__(f"{refusal_id}: {', '.join(reason_codes)} ({detail})")
        self.refusal_id = refusal_id
        self.reason_codes = list(reason_codes)
        self.detail = detail


def _encode_json_columns(row: dict) -> dict:
    return {k: (json.dumps(v, sort_keys=True, separators=(",", ":"))
                if k.endswith("_json") and not isinstance(v, str) else v)
            for k, v in row.items()}


class WritePath:
    def __init__(self, conn, clock, principals: dict | None = None):
        self.conn = conn
        self.clock = clock
        self.principals = principals if principals is not None else config.principals()

    # ---------- internals ----------

    def _can(self, actor, *capabilities) -> bool:
        return any(config.has_capability(self.principals, actor, c) for c in capabilities)

    def _codes(self, actor, *capabilities) -> list[str]:
        """NO_CAPABILITY and CLOCK_BACKWARDS, checked by every method."""
        codes = []
        if capabilities and not self._can(actor, *capabilities):
            codes.append("NO_CAPABILITY")
        _, last_known_at = hashing.last_link(self.conn)
        if last_known_at is not None and self.clock.now() < last_known_at:
            codes.append("CLOCK_BACKWARDS")
        return codes

    def _check(self, actor, action, subject_id, codes, detail) -> None:
        if codes:
            self._refuse(actor, action, subject_id, codes, detail)

    def _next_id(self, table) -> str:
        prefix = ID_PREFIX[table]
        numbers = [int(r["id"][len(prefix) + 1:]) for r in
                   queries.fetch_all(self.conn, f"SELECT id FROM {table} WHERE id LIKE ?", (f"{prefix}-%",))
                   if r["id"][len(prefix) + 1:].isdigit()]
        return f"{prefix}-{max(numbers, default=0) + 1:04d}"

    def _insert(self, table, row, known_at=None) -> dict:
        """Insert one row and append its chain link. Caller owns the transaction."""
        cols = list(row)
        cur = self.conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                [row[c] for c in cols])
        stored = dict(self.conn.execute(f"SELECT * FROM {table} WHERE rowid = ?", (cur.lastrowid,)).fetchone())
        hashing.chain_append(self.conn, table, hashing.ref_id_for(table, stored, cur.lastrowid), stored,
                             known_at or self.clock.now())
        return stored

    def _refuse(self, actor, action, subject_id, reason_codes, detail) -> NoReturn:
        self.conn.rollback()
        now = self.clock.now()
        _, last_known_at = hashing.last_link(self.conn)
        if last_known_at is not None and now < last_known_at:
            # Keep the chain's known_at monotonic; the attempted time goes in the detail.
            detail = f"{detail} (attempted at {now})"
            now = last_known_at
        with self.conn:
            refusal_id = self._next_id("refusals")
            self._insert("refusals", {
                "id": refusal_id, "actor": actor, "action": action, "subject_id": subject_id,
                "reason_codes_json": json.dumps(list(reason_codes)), "detail": detail, "known_at": now,
            }, known_at=now)
        raise Refused(refusal_id, list(reason_codes), detail)

    def _approval(self, approval_id) -> dict:
        found = queries.as_of(self.conn, "approvals", approval_id, None)
        if found is None:
            raise KeyError(f"unknown approval {approval_id}")
        return found

    def _insert_approval(self, kind, subject_id, amount_cents, requested_by, approver, parent_id) -> str:
        approval_id = self._next_id("approvals")
        self._insert("approvals", {
            "id": approval_id, "kind": kind, "subject_id": subject_id, "amount_cents": amount_cents,
            "requested_by": requested_by, "approver": approver, "parent_id": parent_id,
            "known_at": self.clock.now(),
        })
        if kind == "journal_entry":
            self._insert("je_status", {"je_id": subject_id, "status": "pending_approval",
                                       "actor": requested_by, "known_at": self.clock.now()})
        return approval_id

    def _insert_entry(self, actor, effective_date, memo, lines, decision_id, source_ref, corrects_je_id) -> str:
        je_id = self._next_id("journal_entries")
        self._insert("journal_entries", {
            "id": je_id, "period": clk.period_of(effective_date), "effective_date": effective_date,
            "memo": memo, "decision_id": decision_id, "source_ref": source_ref,
            "corrects_je_id": corrects_je_id, "created_by": actor, "known_at": self.clock.now(),
        })
        for line_no, line in enumerate(lines, start=1):
            self._insert("je_lines", {
                "je_id": je_id, "line_no": line_no, "account": line["account"],
                "debit_cents": line.get("debit_cents", 0), "credit_cents": line.get("credit_cents", 0),
                "subledger_ref": line.get("subledger_ref"),
            })
        return je_id

    def _decision_exists(self, decision_id) -> bool:
        return decision_id is not None and queries.as_of(self.conn, "decisions", decision_id, None) is not None

    def _deal_splits_ok(self, deal_id) -> bool:
        """INV7 on the effective splits (a duplicated full credit resolves to the owner of record)."""
        deal = queries.as_of(self.conn, "deals", deal_id, self.clock.now())
        return deal is not None and ranking.splits_total(ranking.resolve_splits(deal, [])) == 10000

    # ---------- facts ----------

    def append_fact(self, table: str, row: dict, actor: str) -> dict:
        """Append a fact row; sets version (versioned tables) and known_at."""
        if table not in FACT_TABLES:
            raise ValueError(f"{table} is not a fact table")
        codes = self._codes(actor, FACT_CAPABILITY.get(table, "write:fact"))
        self._check(actor, f"append_fact:{table}", row.get("id") or row.get("period"), codes,
                    f"append to {table}")
        row = _encode_json_columns(dict(row))
        if table == "events" and "id" not in row:
            row["id"] = self._next_id("events")
        if table in queries.VERSIONED:
            current = self.conn.execute(f"SELECT MAX(version) FROM {table} WHERE id = ?", (row["id"],)).fetchone()[0]
            if row.get("version") is None:
                row["version"] = (current or 0) + 1
            elif current is not None and row["version"] <= current:
                raise ValueError(f"{table} {row['id']} version {row['version']} is not after v{current}")
        row["known_at"] = self.clock.now()
        with self.conn:
            return self._insert(table, row)

    def mark_event(self, actor, event_id, status) -> None:
        """Record that `actor` picked up an event: handled, ignored as not its business, or failed (a served
        world logs the error and moves on rather than retrying the same event forever)."""
        if status not in ("handled", "ignored", "failed"):
            raise ValueError(f"event status must be handled, ignored or failed, not {status}")
        event = queries.as_of(self.conn, "events", event_id, None)
        if event is None:
            raise KeyError(f"unknown event {event_id}")
        codes = self._codes(actor, "claim:event")
        if event["release_at"] > self.clock.now():
            codes.append("EVENT_NOT_RELEASED")
        self._check(actor, "mark_event", event_id, codes, f"{event['type']} released at {event['release_at']}")
        with self.conn:
            self._insert("event_status", {"event_id": event_id, "status": status, "actor": actor,
                                          "known_at": self.clock.now()})

    def add_message(self, channel, author, to_principal, text, claim: dict | None) -> str:
        codes = self._codes(author, "message:send", "draft:message")
        self._check(author, "add_message", to_principal, codes, "send message")
        with self.conn:
            message_id = self._next_id("messages")
            self._insert("messages", {
                "id": message_id, "channel": channel, "author": author, "to_principal": to_principal,
                "text": text, "claim_json": json.dumps(claim, sort_keys=True) if claim is not None else None,
                "sent_at": self.clock.now(), "known_at": self.clock.now(),
            })
        return message_id

    # ---------- decisions ----------

    def record_decision(self, actor, kind, subject_id, inputs, resolutions, precedent_ids, output, outcome,
                        explanation, model=None, prompt_sha=None, decision_id=None) -> str:
        codes = self._codes(actor, "record:decision")
        required = {"table", "id", "version", "sha256"}
        if not inputs or any(not required <= set(i) for i in inputs):
            codes.append("INV4_NO_DECISION")
        self._check(actor, "record_decision", subject_id, codes, f"{kind} decision needs non-empty inputs")
        with self.conn:
            decision_id = decision_id or self._next_id("decisions")
            self._insert("decisions", {
                "id": decision_id, "seat": actor, "kind": kind, "subject_id": subject_id,
                "inputs_json": json.dumps(inputs, sort_keys=True),
                "resolutions_json": json.dumps(resolutions, sort_keys=True),
                "precedent_ids_json": json.dumps(list(precedent_ids)),
                "output_json": json.dumps(output, sort_keys=True), "outcome": outcome,
                "explanation": explanation, "model": model, "prompt_sha": prompt_sha,
                "known_at": self.clock.now(),
            })
        return decision_id

    # ---------- commission lines ----------

    def add_commission_line(self, actor, line: dict, decision_id: str, corrects_id: str | None = None,
                            line_id: str | None = None) -> str:
        """`line_id` is given only for opening balances (CL-OB-<deal>), so the ledger's own numbering is not
        shifted by history."""
        codes = self._codes(actor, "draft:commission_line")
        posting_period = clk.period_of(self.clock.now())
        if queries.period_locked(self.conn, posting_period, self.clock.now()):
            codes.append("INV2_PERIOD_LOCKED")
        if not self._decision_exists(decision_id):
            codes.append("INV4_NO_DECISION")
        if not self._deal_splits_ok(line["deal_id"]):
            codes.append("INV7_SPLITS")
        self._check(actor, "add_commission_line", line["deal_id"], codes,
                    f"commission line {line['deal_id']}/{line['rep_id']} posting in {posting_period}")
        if corrects_id is not None and queries.as_of(self.conn, "commission_lines", corrects_id, None) is None:
            raise KeyError(f"unknown commission line {corrects_id}")
        status = line.get("status", "draft")
        with self.conn:
            line_id = line_id or self._next_id("commission_lines")
            self._insert("commission_lines", {
                "id": line_id, "rep_id": line["rep_id"], "deal_id": line["deal_id"],
                "booking_quarter": line["booking_quarter"], "credit_cents": line["credit_cents"],
                "base_portion_cents": line["base_portion_cents"],
                "accel_portion_cents": line["accel_portion_cents"], "amount_cents": line["amount_cents"],
                "status": status, "decision_id": decision_id, "corrects_id": corrects_id,
                "known_at": self.clock.now(),
            })
            self._insert("commission_line_status", {"line_id": line_id, "status": status, "actor": actor,
                                                    "known_at": self.clock.now()})
        return line_id

    def _line_entry_approved(self, line) -> bool:
        """A posted entry for this line (by the line's decision, or a decision about the line) whose
        journal_entry approval chain is satisfied."""
        decision_ids = {line["decision_id"]} | {d["id"] for d in queries.fetch_all(
            self.conn, "SELECT id FROM decisions WHERE subject_id = ?", (line["id"],))}
        marks = ", ".join("?" * len(decision_ids))
        for entry in queries.fetch_all(self.conn, f"SELECT id FROM journal_entries WHERE decision_id IN ({marks})",
                                       tuple(decision_ids)):
            if (queries.je_status(self.conn, entry["id"]) == "posted"
                    and queries.satisfied_approval(self.conn, "journal_entry", entry["id"], self.clock.now())):
                return True
        return False

    def set_line_status(self, actor, line_id, status) -> None:
        if status not in LINE_STATUS_CAPABILITY:
            raise ValueError(f"unknown line status {status}")
        line = queries.as_of(self.conn, "commission_lines", line_id, None)
        if line is None:
            raise KeyError(f"unknown commission line {line_id}")
        codes = self._codes(actor, *LINE_STATUS_CAPABILITY[status])
        if (status == "approved" and not self._can(actor, "approve:journal_entry")
                and not self._line_entry_approved(line)):
            codes.append("INV5_NOT_APPROVED")
        self._check(actor, "set_line_status", line_id, codes, f"{line_id} -> {status}")
        with self.conn:
            self._insert("commission_line_status", {"line_id": line_id, "status": status, "actor": actor,
                                                    "known_at": self.clock.now()})

    # ---------- journal entries ----------

    def draft_entry(self, actor, effective_date, memo, lines: list[dict], decision_id=None, source_ref=None,
                    corrects_je_id=None) -> str:
        is_source = source_ref is not None
        codes = self._codes(actor, "post:source_entry" if is_source else "draft:journal_entry")
        if not entry_balanced(lines):
            codes.append("INV1_UNBALANCED")
        if queries.period_locked(self.conn, clk.period_of(effective_date), self.clock.now()):
            codes.append("INV2_PERIOD_LOCKED")
        if not is_source and not self._decision_exists(decision_id):
            codes.append("INV4_NO_DECISION")
        self._check(actor, "draft_entry", source_ref or decision_id, codes,
                    f"entry dated {effective_date}: {memo}")
        with self.conn:
            je_id = self._insert_entry(actor, effective_date, memo, lines, decision_id, source_ref, corrects_je_id)
            self._insert("je_status", {"je_id": je_id, "status": "draft", "actor": actor,
                                       "known_at": self.clock.now()})
        return je_id

    def post_entry(self, actor, je_id) -> None:
        entry = queries.as_of(self.conn, "journal_entries", je_id, None)
        if entry is None:
            raise KeyError(f"unknown journal entry {je_id}")
        if queries.je_status(self.conn, je_id) == "posted":
            raise ValueError(f"{je_id} is already posted")
        is_source = entry["source_ref"] is not None
        caps = ("post:source_entry",) if is_source else ("draft:journal_entry", "approve:journal_entry")
        codes = self._codes(actor, *caps)
        if not entry_balanced(queries.lines_for(self.conn, je_id)):
            codes.append("INV1_UNBALANCED")
        if queries.period_locked(self.conn, entry["period"], self.clock.now()):
            codes.append("INV2_PERIOD_LOCKED")
        if not is_source and entry["created_by"].startswith("seat:"):
            approved = queries.satisfied_approval(self.conn, "journal_entry", je_id, self.clock.now())
            standing = queries.standing_precedent(self.conn, queries.memo_kind(entry["memo"]), self.clock.now())
            if approved is None and standing is None:
                codes.append("INV5_NOT_APPROVED")
        self._check(actor, "post_entry", je_id, codes, f"post {je_id}")
        with self.conn:
            self._insert("je_status", {"je_id": je_id, "status": "posted", "actor": actor,
                                       "known_at": self.clock.now()})

    # ---------- approvals ----------

    def request_approval(self, actor, kind, subject_id, amount_cents, approver, parent_id=None) -> str:
        caps = ("draft:approval_request", "draft:journal_entry") if kind == "journal_entry" \
            else ("draft:approval_request",)
        codes = self._codes(actor, *caps)
        rejected = queries.fetch_one(self.conn, """
            SELECT a.id FROM approvals a JOIN approval_decisions d ON d.approval_id = a.id
            WHERE a.kind = ? AND a.subject_id = ? AND d.decision = 'rejected'""", (kind, subject_id))
        if rejected:   # the same proposal again: a resubmission needs a new input (a new subject)
            codes.append("RESUBMIT_NO_NEW_EVIDENCE")
        self._check(actor, "request_approval", subject_id, codes,
                    f"{kind} approval for {subject_id}" + (f"; {rejected['id']} was rejected" if rejected else ""))
        with self.conn:
            return self._insert_approval(kind, subject_id, amount_cents, actor, approver, parent_id)

    def decide_approval(self, actor, approval_id, decision, reason) -> dict:
        """Returns {"result": "final"|"over_limit_routed"|"rejected", "next_approval_id": str|None}."""
        if decision not in ("approved", "rejected"):
            raise ValueError(f"decision must be approved or rejected, not {decision}")
        approval = self._approval(approval_id)
        codes = self._codes(actor, f"approve:{approval['kind']}")
        if actor == approval["requested_by"]:
            codes.append("INV5_SELF_APPROVAL")
        if actor != approval["approver"]:
            codes.append("INV5_WRONG_APPROVER")
        # A decision is final: no second decision on the same approval (a rejection cannot be flipped), and none
        # on a copy whose matter another approver (a backup) has already settled.
        if queries.fetch_one(self.conn, "SELECT 1 FROM approval_decisions WHERE approval_id = ?", (approval_id,)):
            codes.append("ALREADY_DECIDED")
        elif queries.fetch_one(self.conn, """
                SELECT 1 FROM approvals b JOIN approval_decisions e ON e.approval_id = b.id
                WHERE b.kind = ? AND b.subject_id = ? AND b.id != ? AND e.result IN ('final', 'rejected')""",
                               (approval["kind"], approval["subject_id"], approval_id)):
            codes.append("ALREADY_SETTLED")
        self._check(actor, "decide_approval", approval_id, codes,
                    f"{approval['kind']} {approval['subject_id']} requested by {approval['requested_by']}, "
                    f"approver {approval['approver']}")
        next_approver = None
        if decision == "rejected":
            result = "rejected"
        else:
            cap = routing.limit(actor, approval["kind"])
            if cap is None or approval["amount_cents"] <= cap:
                result = "final"
            else:
                result = "over_limit_routed"
                next_approver = routing.next_approver(approval["kind"], approval["amount_cents"], after=actor)
        next_approval_id = None
        with self.conn:
            self._insert("approval_decisions", {
                "approval_id": approval_id, "decided_by": actor, "decision": decision, "result": result,
                "reason": reason, "known_at": self.clock.now(),
            })
            if next_approver is not None:
                next_approval_id = self._insert_approval(
                    approval["kind"], approval["subject_id"], approval["amount_cents"],
                    approval["requested_by"], next_approver, approval_id)
        return {"result": result, "next_approval_id": next_approval_id}

    # ---------- precedents ----------

    def add_precedent(self, actor, statement, scope, override, approval_ids, effective_from, expires_at) -> str:
        # The authority is the satisfied approval chain; an approver or a seat recording decisions may write it down.
        codes = self._codes(actor, "approve:plan_exception", "record:decision")
        if not queries.approvals_form_satisfied_chain(self.conn, approval_ids, self.clock.now()):
            codes.append("INV5_NOT_APPROVED")
        self._check(actor, "add_precedent", ",".join(approval_ids), codes, statement)
        with self.conn:
            precedent_id = self._next_id("precedents")
            self._insert("precedents", {
                "id": precedent_id, "statement": statement, "scope_json": json.dumps(scope, sort_keys=True),
                "override_json": json.dumps(override, sort_keys=True),
                "approvals_json": json.dumps(list(approval_ids)), "effective_from": effective_from,
                "expires_at": expires_at, "known_at": self.clock.now(),
            })
        return precedent_id

    # ---------- amortization schedules ----------

    def add_schedule(self, actor, line_id, total_cents, months, start_period, schedule) -> str:
        codes = self._codes(actor, "draft:journal_entry")
        self._check(actor, "add_schedule", line_id, codes, f"schedule for {line_id}")
        with self.conn:
            schedule_id = self._next_id("amortization_schedules")
            self._insert("amortization_schedules", {
                "id": schedule_id, "line_id": line_id, "total_cents": total_cents, "months": months,
                "start_period": start_period, "schedule_json": json.dumps([list(p) for p in schedule]),
                "status": "active", "known_at": self.clock.now(),
            })
        return schedule_id

    def cancel_schedule(self, actor, schedule_id) -> str:
        """Cancel = a new row with id suffix -c and status cancelled."""
        schedule = queries.as_of(self.conn, "amortization_schedules", schedule_id, None)
        if schedule is None:
            raise KeyError(f"unknown schedule {schedule_id}")
        codes = self._codes(actor, "draft:journal_entry")
        self._check(actor, "cancel_schedule", schedule_id, codes, f"cancel {schedule_id}")
        with self.conn:
            self._insert("amortization_schedules", {**schedule, "id": f"{schedule_id}-c", "status": "cancelled",
                                                    "known_at": self.clock.now()})
        return f"{schedule_id}-c"

    # ---------- payroll and periods ----------

    def create_payroll_file(self, actor, period, line_ids) -> str:
        codes = self._codes(actor, "draft:payroll_file")
        lines = [queries.as_of(self.conn, "commission_lines", lid, None) for lid in line_ids]
        if any(line is None for line in lines):
            raise KeyError(f"unknown commission line in {line_ids}")
        not_approved = [l["id"] for l in lines if queries.line_status(self.conn, l["id"]) != "approved"]
        if not_approved or not lines:
            codes.append("INV6_PAYROLL_MISMATCH")
        self._check(actor, "create_payroll_file", f"PR-{period}", codes,
                    f"lines not approved: {', '.join(not_approved) or 'none'}")
        payroll_lines = [{"line_id": l["id"], "rep_id": l["rep_id"], "amount_cents": l["amount_cents"]}
                         for l in lines]
        file_id = f"PR-{period}"
        with self.conn:
            self._insert("payroll_files", {
                "id": file_id, "period": period, "lines_json": json.dumps(payroll_lines, sort_keys=True),
                "total_cents": sum(l["amount_cents"] for l in lines), "known_at": self.clock.now(),
            })
            self._insert("payroll_status", {"file_id": file_id, "status": "draft", "actor": actor,
                                            "known_at": self.clock.now()})
        return file_id

    def release_payroll(self, actor, file_id, exported_lines: dict[str, int] | None = None) -> str:
        """Release an approved payroll file; returns the payment JE id. `exported_lines` is what the payroll
        system is about to pay ({line_id: cents}); it must match the approved file exactly."""
        payroll = queries.as_of(self.conn, "payroll_files", file_id, None)
        if payroll is None:
            raise KeyError(f"unknown payroll file {file_id}")
        if queries.latest_status(self.conn, "payroll_status", "file_id", file_id) == "released":
            raise ValueError(f"{file_id} is already released")
        codes = self._codes(actor, "release:payroll_file")
        payroll_lines = json.loads(payroll["lines_json"])
        mismatched = []
        for pl in payroll_lines:
            line = queries.as_of(self.conn, "commission_lines", pl["line_id"], None)
            if (line is None or line["amount_cents"] != pl["amount_cents"]
                    or queries.line_status(self.conn, pl["line_id"]) != "approved"):
                mismatched.append(pl["line_id"])
            elif exported_lines is not None and exported_lines.get(pl["line_id"]) != pl["amount_cents"]:
                exported = exported_lines.get(pl["line_id"])
                mismatched.append(f"{pl['line_id']} (export {fmt_usd(exported) if exported is not None else 'missing'}"
                                  f" vs approved {fmt_usd(pl['amount_cents'])})")
        if exported_lines is not None:
            mismatched += [f"{l} (export only)" for l in exported_lines if l not in {p['line_id'] for p in payroll_lines}]
        if mismatched or payroll["total_cents"] != sum(pl["amount_cents"] for pl in payroll_lines):
            codes.append("INV6_PAYROLL_MISMATCH")
        if queries.satisfied_approval(self.conn, "payroll_file", file_id, self.clock.now()) is None:
            codes.append("INV5_NOT_APPROVED")
        today = clk.local_date(self.clock.now())
        if queries.period_locked(self.conn, clk.period_of(today), self.clock.now()):
            codes.append("INV2_PERIOD_LOCKED")
        self._check(actor, "release_payroll", file_id, codes,
                    f"release {file_id}; mismatched lines: {', '.join(mismatched) or 'none'}")
        entry = entries.payroll_payment_entry(payroll, today)
        with self.conn:
            je_id = self._insert_entry(actor, entry["effective_date"], entry["memo"], entry["lines"], None,
                                       file_id, None)
            self._insert("je_status", {"je_id": je_id, "status": "posted", "actor": actor,
                                       "known_at": self.clock.now()})
            for pl in payroll_lines:
                self._insert("commission_line_status", {"line_id": pl["line_id"], "status": "paid",
                                                        "actor": actor, "known_at": self.clock.now()})
            self._insert("payroll_status", {"file_id": file_id, "status": "released", "actor": actor,
                                            "known_at": self.clock.now()})
        return je_id

    def lock_period(self, actor, period) -> None:
        codes = self._codes(actor, "lock:period")
        self._check(actor, "lock_period", period, codes, f"lock {period}")
        with self.conn:
            self._insert("periods", {"period": period, "status": "locked", "actor": actor,
                                     "known_at": self.clock.now()})
