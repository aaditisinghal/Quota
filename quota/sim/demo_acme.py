"""The scripted Acme timeline (IMPLEMENTATION.md section 9) — the Phase 1 acceptance test.

People act through scripted calls. The two seats are plain functions here: they read through a read-only
connection, decide with the pure engine, and write only through WritePath.
"""

import json
from pathlib import Path
from uuid import uuid4

from quota import clock as clk
from quota import config
from quota.engine import amortization, calculator, disputes, entries, routing
from quota.ledger import db, hashing, invariants, queries
from quota.ledger.write_path import Refused, WritePath
from quota.money import fmt_pct, fmt_usd
from quota.sim import seed_acme

ANALYST = "seat:commissions_analyst"
ACCOUNTANT = "seat:deferred_accountant"
BILLING = "system:billing"
SYSTEM = "system"
DEAL, REP = "acme", "jordan.kim"
CHANNEL = "slack:dm"
REPLAY_IDS = ("D-0001", "D-0005", "D-0007")


def format_line(clock_now, actor, kind, text) -> str:
    head = f"[{clock_now}] {actor:<26} {kind:<9} "
    first, *rest = text.split("\n")
    return "\n".join([head + first] + [" " * len(head) + line for line in rest])


def _signed(cents) -> str:
    return f"+{fmt_usd(cents)}" if cents >= 0 else fmt_usd(cents)


def _entry_text(lines) -> str:
    sides = [("Dr " if l["debit_cents"] else "Cr ") + l["account"] for l in lines]
    amounts = [l["debit_cents"] or l["credit_cents"] for l in lines]
    if len(set(amounts)) == 1:
        return f"{' / '.join(sides)} {fmt_usd(amounts[0])}"
    return " / ".join(f"{s} {fmt_usd(a)}" for s, a in zip(sides, amounts))


def _alt_text(d) -> str:
    value = d["value"]
    label = {"crm_close_date": f"CRM close {str(value)[:10]}", "contract_date": f"contract date {str(value)[:10]}",
             "countersignature": f"countersigned {value}", "crm": f"CRM amount {fmt_usd(value) if isinstance(value, int) else value}",
             "contract": f"contract ACV {fmt_usd(value) if isinstance(value, int) else value}"}.get(d["basis"], f"{d['basis']} {value}")
    effect = "" if d["effect_cents"] is None else f" {_signed(d['effect_cents'])}"
    return f"{label}{effect}"


class AcmeDemo:
    def __init__(self, db_path=None, out=print):
        if db_path and Path(db_path).exists():
            raise FileExistsError(f"{db_path} already exists; the ledger is append-only, pick a new path")
        self.uri = db_path or db.memory_uri(f"quota-demo-{uuid4().hex}")
        self.conn = db.connect(self.uri)
        db.init_db(self.conn)
        self.ro = db.connect(self.uri, readonly=True)
        self.clock = clk.SimClock(seed_acme.SEED_START)
        self.wp = WritePath(self.conn, self.clock, config.principals(with_seed=True))
        self.out = out
        self.lines: list[str] = []
        self.ids: dict[str, str] = {}
        self.results: dict = {}

    # ---------- plumbing ----------

    def emit(self, actor, kind, text):
        line = format_line(self.clock.now(), actor, kind, text)
        self.lines.append(line)
        self.out(line)

    def say(self, text=""):
        self.lines.append(text)
        self.out(text)

    def at(self, ts):
        self.clock.advance_to(ts)

    @property
    def now(self):
        return self.clock.now()

    def close(self):
        self.ro.close()
        self.conn.close()

    def _get(self, table, row_id):
        return queries.as_of(self.ro, table, row_id, None)

    # ---------- system:billing ----------

    def _post_source(self, entry, source_ref):
        je_id = self.wp.draft_entry(BILLING, entry["effective_date"], entry["memo"], entry["lines"],
                                    source_ref=source_ref)
        self.wp.post_entry(BILLING, je_id)
        return je_id

    def bill(self, kind, amount, billing_id):
        self.wp.append_fact("billing", {"id": billing_id, "kind": kind, "deal_id": DEAL, "amount_cents": amount,
                                        "at": self.now, "external_ref": None}, BILLING)
        builder = {"invoice": entries.invoice_entry, "payment": entries.payment_entry,
                   "refund": entries.refund_entry}[kind]
        entry = builder(DEAL, amount, self.now[:10])
        je_id = self._post_source(entry, billing_id)
        self.emit(BILLING, "ENTRY", f"{je_id} posted {kind} {DEAL} ({billing_id}): {_entry_text(entry['lines'])}")
        return je_id

    def revenue(self, period):
        contract = self._get("contracts", DEAL)
        entry = entries.revenue_entry(DEAL, contract["acv_cents"] // 12, clk.period_end(period))
        je_id = self._post_source(entry, f"revenue:{DEAL}:{period}")
        self.emit(BILLING, "ENTRY", f"{je_id} posted revenue {DEAL} {period}: {_entry_text(entry['lines'])}")
        return je_id

    # ---------- people (scripted) ----------

    def message(self, author, text, claim, to=ANALYST):
        message_id = self.wp.add_message(CHANNEL, author, to, text, claim)
        claim_text = ""
        if claim:
            detail = " ".join(f"{k}={v}" for k, v in claim.items() if k != "kind" and v not in ([], None))
            claim_text = f" [claim {claim['kind']}{' ' + detail if detail else ''}]"
        self.emit(author, "MESSAGE", f'{message_id} to {to}: "{text}"{claim_text}')
        return message_id

    def approve(self, who, approval_id, reason):
        approval = self._get("approvals", approval_id)
        out = self.wp.decide_approval(who, approval_id, "approved", reason)
        text = f"{approval_id} {approval['kind']} {approval['subject_id']} approved -> {out['result']}"
        if out["result"] == "over_limit_routed":
            next_approval = self._get("approvals", out["next_approval_id"])
            text += (f" (limit {fmt_usd(routing.limit(who, approval['kind']))} < {fmt_usd(approval['amount_cents'])});"
                     f" {next_approval['id']} -> {next_approval['approver']}")
        self.emit(who, "APPROVAL", text)
        return out

    # ---------- seat:commissions_analyst ----------

    def _calc_text(self, calc) -> str:
        r, res = calc["result"], calc["resolved"]
        booking, base = res["booking_date"], res["base"]
        precedent = f" {booking.source.split(':', 1)[1]}" if booking.basis == "precedent" else ""
        crm = next((d for d in base.disagreements if d["basis"] == "crm"), None)
        base_text = f"base={base.basis} {fmt_usd(base.value)}" + (f" over CRM {fmt_usd(crm['value'])}" if crm else "")
        prior = f"; prior {fmt_usd(r.prior_credit_cents)}" if r.prior_credit_cents else ""
        return (f"{r.quarter} {r.tier} tier {fmt_usd(r.amount_cents)} (booking={booking.basis}{precedent} "
                f"{booking.rule_id}; {base_text} {base.rule_id}{prior})")

    def commission_calc(self, corrects_id=None):
        rows = queries.commission_rows(self.ro, REP, self.now)
        calc = calculator.commission_decision(rows, DEAL, REP, self.now)
        text = self._calc_text(calc)
        # Two sources can carry the same alternative (contract + e-sign countersignature); print it once.
        alternatives = list(dict.fromkeys(_alt_text(d) for res in calc["resolved"].values()
                                          for d in res.disagreements))
        explanation = text + ("; disagreements: " + "; ".join(alternatives) if alternatives else "")
        decision_id = self.wp.record_decision(
            ANALYST, "commission_calc", f"{DEAL}/{REP}", queries.input_refs(rows), calc["resolutions"],
            calc["precedent_ids"], calc["output"], "drafted", explanation)
        self.emit(ANALYST, "DECISION", f"{decision_id} commission_calc {DEAL}/{REP}: {text}"
                  + (f"\ndisagreements: {'; '.join(alternatives)}" if alternatives else ""))
        r = calc["result"]
        line_id = self.wp.add_commission_line(ANALYST, {
            "rep_id": REP, "deal_id": DEAL, "booking_quarter": r.quarter, "credit_cents": r.credit_cents,
            "base_portion_cents": r.base_portion_cents, "accel_portion_cents": r.accel_portion_cents,
            "amount_cents": r.amount_cents}, decision_id, corrects_id=corrects_id)
        corrects = f", corrects {corrects_id}" if corrects_id else ""
        self.emit(ANALYST, "ENTRY", f"{line_id} draft commission line {DEAL}/{REP} {r.quarter} "
                                    f"{fmt_usd(r.amount_cents)} ({decision_id}{corrects})")
        if corrects_id:
            self.wp.set_line_status(ANALYST, corrects_id, "superseded")
            self.emit(ANALYST, "ENTRY", f"{corrects_id} -> superseded by {line_id}")
        self.ids["line"] = line_id
        return decision_id, line_id

    def _latest_decision_for_line(self, line):
        candidates = [self._get("decisions", line["decision_id"])] + queries.fetch_all(
            self.ro, "SELECT * FROM decisions WHERE subject_id = ? ORDER BY known_at, id", (line["id"],))
        return max(candidates, key=lambda d: (d["known_at"], d["id"]))

    def handle_dispute(self, message_id):
        message = self._get("messages", message_id)
        claim = json.loads(message["claim_json"])
        line = self._get("commission_lines", claim["line"])
        prior = self._latest_decision_for_line(line)
        rows = {**queries.commission_rows(self.ro, REP, self.now), "commission_lines": [line],
                "messages": [message], "decisions": [prior]}
        out = disputes.dispute_decision(rows, line["id"], self.now)
        result, fields = out["outcome"], out["outcome"].reply_fields
        outcome = {"held": "held", "conceded": "conceded", "escalation_offered": "escalated"}[result.outcome]
        kind = "escalation" if claim["kind"] == "escalate" else "dispute"
        res = out["resolutions"][out["claim"]["field"]]
        if result.outcome == "escalation_offered":
            text = f"{result.outcome} -> {fields['escalate_to']} ({fields['escalate_to_role']})"
        else:
            text = (f"{result.outcome}" + (", no_new_evidence: true" if result.no_new_evidence else "")
                    + f" ({out['claim']['field']} asserted {out['claim']['asserted']}; resolved {res['value']} "
                      f"via {res['basis']} {res['rule_id']})")
        decision_id = self.wp.record_decision(
            ANALYST, kind, line["id"], queries.input_refs(rows), out["resolutions"], out["precedent_ids"],
            out["output"], outcome, text)
        self.emit(ANALYST, "DECISION", f"{decision_id} {kind} {line['id']}: {text}")
        if result.outcome == "escalation_offered":
            held = [d["id"] for d in queries.fetch_all(
                self.ro, "SELECT id FROM decisions WHERE subject_id = ? AND kind = 'dispute' ORDER BY id",
                (line["id"],))]
            note = (f"Plan exception request from {config.display_name(REP)} on {line['id']} "
                    f"({out['claim']['deal_name']}): count booking {out['claim']['asserted']} in "
                    f"{out['at_claim'].quarter}. As resolved {fmt_usd(fields['as_resolved_cents'])} "
                    f"({out['at_resolved'].quarter}, {res['basis']} {res['value']}, {res['rule_id']}); as claimed "
                    f"{fmt_usd(fields['as_claimed_cents'])}. Difference {fmt_usd(fields['diff_cents'])}. "
                    f"Held {len(held)}x ({', '.join(held)}).")
            sent = self.wp.add_message(CHANNEL, ANALYST, fields["escalate_to"], note, None)
            self.emit(ANALYST, "MESSAGE", f"{sent} to {fields['escalate_to']}: {note}")
        else:
            reply = disputes.render_reply(fields)
            sent = self.wp.add_message(CHANNEL, ANALYST, message["author"], reply, None)
            self.emit(ANALYST, "REPLY", f"{sent} to {message['author']}:\n{reply}")
        return decision_id

    def handle_exception_grant(self, message_id):
        message = self._get("messages", message_id)
        claim = json.loads(message["claim_json"])
        rows = queries.commission_rows(self.ro, REP, self.now)
        current = calculator.evaluate(rows, claim["deal"], REP, self.now)
        granted = calculator.evaluate(rows, claim["deal"], REP, self.now, booking_ts=clk.end_of_day(claim["value"]))
        delta = granted.amount_cents - current.amount_cents
        approver = routing.owner("plan_exception")
        approval_id = self.wp.request_approval(ANALYST, "plan_exception", claim["deal"], delta, approver=approver)
        self.emit(ANALYST, "APPROVAL",
                  f"{approval_id} plan_exception {claim['deal']} {claim['field']}={claim['value']} "
                  f"({granted.quarter}) -> {approver}: delta {fmt_usd(delta)} "
                  f"({fmt_usd(current.amount_cents)} -> {fmt_usd(granted.amount_cents)}), from {message_id}")
        self.ids["exception"] = approval_id
        return approval_id, claim

    def payroll_file(self, period):
        approved = [l["id"] for l in queries.fetch_all(self.ro, "SELECT id FROM commission_lines ORDER BY id")
                    if queries.line_status(self.ro, l["id"]) == "approved"]
        file_id = self.wp.create_payroll_file(ANALYST, period, approved)
        total = self._get("payroll_files", file_id)["total_cents"]
        self.emit(ANALYST, "PAYROLL", f"{file_id} created: {fmt_usd(total)} ({', '.join(approved)})")
        approval_id = self.wp.request_approval(ANALYST, "payroll_file", file_id, total,
                                               approver=routing.owner("payroll_file"))
        self.emit(ANALYST, "APPROVAL", f"{approval_id} payroll_file {file_id} {fmt_usd(total)} -> "
                                       f"{routing.owner('payroll_file')}")
        return file_id, approval_id

    # ---------- seat:deferred_accountant ----------

    def _seat_entry(self, entry, decision_id):
        return self.wp.draft_entry(ACCOUNTANT, entry["effective_date"], entry["memo"], entry["lines"],
                                   decision_id=decision_id)

    def _request_je_approval(self, je_id, amount):
        approver = routing.owner("journal_entry")
        approval_id = self.wp.request_approval(ACCOUNTANT, "journal_entry", je_id, amount, approver=approver)
        self.emit(ACCOUNTANT, "APPROVAL", f"{approval_id} journal_entry {je_id} {fmt_usd(amount)} -> {approver}")
        return approval_id

    def accrue(self, line_id):
        line = self._get("commission_lines", line_id)
        rows = {"commission_lines": [line], "contracts": [self._get("contracts", line["deal_id"])]}
        acc = entries.accrual_decision(rows, line_id, self.now)
        args = acc["output"]["args"]
        schedule_text = (f"schedule {args['term_months']} x {fmt_usd(acc['schedule'][0][1])} from {args['start_period']}"
                         if acc["capitalized"] else "expensed now (term <= 12 months)")
        text = (f"{'capitalize' if acc['capitalized'] else 'expense'} {line_id} {fmt_usd(line['amount_cents'])}: "
                f"term {args['term_months']} months; {_entry_text(acc['entry']['lines'])}; {schedule_text}")
        decision_id = self.wp.record_decision(ACCOUNTANT, "commission_accrual", line_id, queries.input_refs(rows),
                                              {}, [], acc["output"], "drafted", text)
        self.emit(ACCOUNTANT, "DECISION", f"{decision_id} commission_accrual {line_id}: {text}")
        je_id = self._seat_entry(acc["entry"], decision_id)
        self.emit(ACCOUNTANT, "ENTRY", f"{je_id} drafted commission_accrual {line_id}: "
                                       f"{_entry_text(acc['entry']['lines'])}")
        approval_id = self._request_je_approval(je_id, line["amount_cents"])
        self.ids.update(accrual_decision=decision_id, accrual_je=je_id, accrual_approval=approval_id)
        return decision_id, je_id, approval_id

    def post_approved(self, je_id, approval_id):
        self.wp.post_entry(ACCOUNTANT, je_id)
        self.emit(ACCOUNTANT, "ENTRY", f"{je_id} posted ({approval_id})")

    def start_schedule(self, decision_id, line_id):
        output = json.loads(self._get("decisions", decision_id)["output_json"])
        schedule = output["result"]["schedule"]
        args = output["args"]
        schedule_id = self.wp.add_schedule(ACCOUNTANT, line_id, args["amount_cents"], args["term_months"],
                                           args["start_period"], schedule)
        self.emit(ACCOUNTANT, "ENTRY", f"{schedule_id} amortization schedule {line_id}: {args['term_months']} x "
                                       f"{fmt_usd(schedule[0][1])} from {args['start_period']} ({decision_id})")
        self.ids["schedule"] = schedule_id
        return schedule_id

    def amortize(self, period):
        schedule = self._get("amortization_schedules", self.ids["schedule"])
        amount = amortization.amount_for(json.loads(schedule["schedule_json"]), period)
        entry = entries.amortization_entry(schedule["line_id"], amount, clk.period_end(period))
        je_id = self._seat_entry(entry, self.ids["accrual_decision"])
        self.wp.post_entry(ACCOUNTANT, je_id)
        standing = queries.standing_precedent(self.ro, "amortization", self.now)
        self.emit(ACCOUNTANT, "ENTRY", f"{je_id} posted amortization {schedule['line_id']} {period}: "
                                       f"{_entry_text(entry['lines'])} (standing approval {standing})")
        return je_id

    def clawback_check(self):
        line = self._get("commission_lines", self.ids["line"])
        churn = next(e for e in queries.fetch_all(self.ro, "SELECT * FROM events WHERE type = 'customer.churned'")
                     if json.loads(e["payload_json"])["deal_id"] == DEAL)
        schedule = self._get("amortization_schedules", self.ids["schedule"])
        rows = {**queries.commission_rows(self.ro, REP, self.now), "commission_lines": [line], "events": [churn],
                "amortization_schedules": [schedule]}
        cb = entries.clawback_decision(rows, line["id"], self.now)
        a = cb["output"]["args"]
        precedent = f" ({cb['precedent_ids'][0]})" if cb["precedent_ids"] else ""
        within = "within" if a["window_end"] and a["churn_date"] <= a["window_end"] else "outside"
        signed = "plan signed with clawback" if a["plan"]["signed_at"] else "plan not signed"
        text = (f"booking {a['booking_date']}{precedent}, churn {a['churn_date']} {within} {a['window_months']} "
                f"months (window ends {a['window_end']}), {signed} -> "
                f"{'recoverable' if a['recoverable'] else 'not recoverable'}")
        decision_id = self.wp.record_decision(ACCOUNTANT, "clawback", line["id"], queries.input_refs(rows),
                                              cb["resolutions"], cb["precedent_ids"], cb["output"], "drafted", text)
        self.emit(ACCOUNTANT, "DECISION", f"{decision_id} clawback {line['id']}: {text}")
        je_id = self._seat_entry(cb["entry"], decision_id)
        self.emit(ACCOUNTANT, "ENTRY", f"{je_id} drafted clawback {line['id']}: {_entry_text(cb['entry']['lines'])}")
        approval_id = self._request_je_approval(je_id, line["amount_cents"])
        clawback_line = self.wp.add_commission_line(ANALYST, {
            "rep_id": line["rep_id"], "deal_id": line["deal_id"], "booking_quarter": line["booking_quarter"],
            "credit_cents": -line["credit_cents"], "base_portion_cents": -line["base_portion_cents"],
            "accel_portion_cents": -line["accel_portion_cents"], "amount_cents": -line["amount_cents"]},
            decision_id, corrects_id=line["id"])
        self.emit(ANALYST, "ENTRY", f"{clawback_line} clawback line {fmt_usd(-line['amount_cents'])}, corrects "
                                    f"{line['id']} ({decision_id})")
        self.wp.set_line_status(ANALYST, line["id"], "clawed_back")
        self.emit(ANALYST, "ENTRY", f"{line['id']} -> clawed_back")
        cancelled = self.wp.cancel_schedule(ACCOUNTANT, schedule["id"])
        self.emit(ACCOUNTANT, "ENTRY", f"{schedule['id']} cancelled ({cancelled})")
        self.ids.update(clawback_decision=decision_id, clawback_je=je_id, clawback_approval=approval_id,
                        clawback_line=clawback_line)
        return decision_id, je_id, approval_id, clawback_line

    # ---------- the timeline ----------

    def run(self):
        seed_acme.seed(self.wp, self.clock, self.emit)

        self.at("2026-04-02T18:00:00Z")                                       # 1
        self.bill("invoice", 12_000_000, "acme-inv-1")

        self.at("2026-04-10T20:00:00Z")                                       # 2
        for period in ("2026-01", "2026-02", "2026-03"):
            self.wp.lock_period("priya.nair", period)
            self.emit("priya.nair", "PERIOD", f"{period} locked")

        self.at("2026-04-15T17:00:00Z")                                       # 3
        self.bill("payment", 12_000_000, "acme-pay-1")

        self.at("2026-04-27T16:00:00Z")                                       # 4
        self.commission_calc()

        self.at("2026-04-27T18:05:00Z")                                       # 5
        claim = {"kind": "dispute", "line": "CL-0001", "field": "booking_date", "asserted": "2026-03-31",
                 "evidence_refs": []}
        self.handle_dispute(self.message(
            REP, "Acme closed Mar 31. It should count in Q1 at the 1.5x accelerator.", claim))

        self.at("2026-04-27T18:20:00Z")                                       # 6
        self.handle_dispute(self.message(REP, "Come on, everyone knows it closed in March.", claim))

        self.at("2026-04-27T18:30:00Z")                                       # 7
        self.handle_dispute(self.message(REP, "escalate", {"kind": "escalate", "line": "CL-0001"}))

        self.at("2026-04-28T15:00:00Z")                                       # 8
        grant = {"kind": "exception_grant", "deal": "acme", "field": "booking_date", "value": "2026-03-31",
                 "quarter": "2026Q1"}
        first, grant = self.handle_exception_grant(
            self.message("marcus.lee", "Honor Mar 31 for Acme. Count it in Q1.", grant))
        routed = self.approve("marcus.lee", first, "Honor Mar 31 for Acme. Count it in Q1.")["next_approval_id"]

        self.at("2026-04-28T15:01:00Z")                                       # 9
        try:
            self.wp.decide_approval(ANALYST, routed, "approved", "seat tries to clear its own request")
        except Refused as refused:
            self.emit(ANALYST, "REFUSED", f"{refused.refusal_id} decide_approval {routed}: "
                                          f"{', '.join(refused.reason_codes)}")

        self.at("2026-04-29T16:00:00Z")                                       # 10
        self.approve("priya.nair", routed, "Over Marcus's limit; approve Q1 treatment for Acme only.")
        plan = calculator.plan_for(queries.commission_rows(self.ro, REP, self.now), REP, grant["value"])
        precedent = self.wp.add_precedent(
            "priya.nair", f"Acme ({DEAL}) books on {grant['value']} ({grant['quarter']}) per Marcus Lee; "
                          "over-limit, approved by the Controller",
            {"deal_id": DEAL, "field": grant["field"]}, {"value": grant["value"], "quarter": grant["quarter"]},
            [first, routed], self.now, plan.effective_to)
        self.emit("priya.nair", "APPROVAL", f"{precedent} precedent {DEAL} {grant['field']}={grant['value']} "
                                           f"({grant['quarter']}), expires {plan.effective_to} [{first}, {routed}]")
        self.commission_calc(corrects_id="CL-0001")

        self.at("2026-04-30T23:00:00Z")                                       # 11
        self.revenue("2026-04")

        self.at("2026-04-30T23:05:00Z")                                       # 12
        accrual_decision, accrual_je, accrual_approval = self.accrue(self.ids["line"])

        self.at("2026-04-30T23:30:00Z")                                       # 13
        self.approve("priya.nair", accrual_approval, "Accrual matches CL-0002.")
        self.post_approved(accrual_je, accrual_approval)
        self.wp.set_line_status("priya.nair", self.ids["line"], "approved")
        self.emit("priya.nair", "APPROVAL", f"{self.ids['line']} -> approved (accrual {accrual_je}, "
                                            f"{accrual_approval})")
        self.start_schedule(accrual_decision, self.ids["line"])

        self.at("2026-04-30T23:31:00Z")                                       # 14
        self.amortize("2026-04")

        self.at("2026-05-14T17:00:00Z")                                       # 15
        payroll, payroll_approval = self.payroll_file("2026-05")

        self.at("2026-05-14T18:00:00Z")                                       # 16
        self.approve("priya.nair", payroll_approval, "May commissions OK.")

        self.at("2026-05-15T17:00:00Z")                                       # 17
        payment_je = self.wp.release_payroll("dana.ortiz", payroll)
        self.emit("dana.ortiz", "PAYROLL", f"{payroll} released")
        self.emit("dana.ortiz", "ENTRY", f"{payment_je} posted payroll_payment {payroll}: "
                                         f"{_entry_text(queries.lines_for(self.ro, payment_je))}")
        self.emit("dana.ortiz", "ENTRY", f"{self.ids['line']} -> paid ({payroll})")

        self.at("2026-05-31T23:00:00Z")                                       # 18
        self.revenue("2026-05")
        self.amortize("2026-05")

        self.at("2026-06-28T17:00:00Z")                                       # 19
        payload = {"deal_id": DEAL, "effective_date": "2026-06-30", "reason": "termination_for_convenience",
                   "refund_cents": 9_000_000}
        event = self.wp.append_fact("events", {"type": "customer.churned", "payload_json": payload,
                                               "release_at": self.now, "created_by": SYSTEM}, SYSTEM)
        self.emit(SYSTEM, "EVENT", f"{event['id']} customer.churned {DEAL}: effective {payload['effective_date']}, "
                                   f"reason {payload['reason']}, refund {fmt_usd(payload['refund_cents'])}")

        self.at("2026-06-30T23:00:00Z")                                       # 20
        self.revenue("2026-06")
        self.amortize("2026-06")

        self.at("2026-06-30T23:10:00Z")                                       # 21
        self.ids["refund_je"] = self.bill("refund", payload["refund_cents"], "acme-ref-1")

        self.at("2026-06-30T23:20:00Z")                                       # 22
        _, clawback_je, clawback_approval, clawback_line = self.clawback_check()

        self.at("2026-06-30T23:40:00Z")                                       # 23
        self.approve("priya.nair", clawback_approval, "Clawback per signed plan clause.")
        self.post_approved(clawback_je, clawback_approval)
        self.wp.set_line_status("priya.nair", clawback_line, "approved")
        self.emit("priya.nair", "APPROVAL", f"{clawback_line} -> approved (clawback {clawback_je}, "
                                            f"{clawback_approval})")

        self.at("2026-06-30T23:45:00Z")                                       # 24
        self.report()
        return self

    # ---------- step 24: report ----------

    def report(self):
        self.say()
        self.say(f"REPORT as of {self.now}")
        self.say()
        self.say("JOURNAL")
        for entry in queries.journal(self.ro):
            self.say(f"  {entry['id']}  {entry['effective_date']}  {entry['status']:<8} {entry['memo']}")
            for l in entry["lines"]:
                debit = fmt_usd(l["debit_cents"]) if l["debit_cents"] else ""
                credit = fmt_usd(l["credit_cents"]) if l["credit_cents"] else ""
                self.say(f"           {'Dr' if debit else 'Cr'} {l['account']} {config.ACCOUNTS[l['account']][0]:<36}"
                         f"{debit:>14}{credit:>14}".rstrip())
        self.say()
        self.say("TRIAL BALANCE")
        for line in queries.format_trial_balance(queries.trial_balance(self.ro), config.ACCOUNTS):
            self.say(line)
        self.say()
        for line in self.cascade():
            self.say(line)
        self.say()
        checks = invariants.check_all(self.ro)
        chain_ok, bad_seq = hashing.verify_chain(self.ro)
        replays = {d: queries.replay_decision(self.ro, d)[0] for d in REPLAY_IDS}
        self.results = {"invariants": checks, "chain": (chain_ok, bad_seq), "replays": replays}
        self.say(invariants.summary_line(checks))
        self.say(f"CHAIN       {'OK' if chain_ok else f'FAIL at seq {bad_seq}'} ({hashing.chain_length(self.ro)} rows)")
        self.say("REPLAY      " + "  ".join(f"{d} {'MATCH' if ok else 'MISMATCH'}" for d, ok in replays.items()))

    def _account_total(self, account, subledger_ref, exclude=()):
        total = 0
        for entry in queries.journal(self.ro):
            if entry["status"] != "posted" or entry["id"] in exclude:
                continue
            total += sum(l["debit_cents"] - l["credit_cents"] for l in entry["lines"]
                         if l["account"] == account and l["subledger_ref"] == subledger_ref)
        return total

    def cascade(self) -> list[str]:
        refund_je, clawback_je = self.ids["refund_je"], self.ids["clawback_je"]
        refund_lines = queries.lines_for(self.ro, refund_je)
        clawback_lines = {l["account"]: l for l in queries.lines_for(self.ro, clawback_je)}
        churn = json.loads(queries.fetch_one(self.ro, "SELECT payload_json FROM events WHERE type = 'customer.churned'")
                           ["payload_json"])
        contract = self._get("contracts", DEAL)
        monthly = contract["acv_cents"] // 12
        recognized = len([e for e in queries.journal(self.ro) if queries.memo_kind(e["memo"]) == "revenue"
                          and e["status"] == "posted" and e["lines"][0]["subledger_ref"] == DEAL])
        remaining = monthly * (contract["term_months"] - recognized)
        clawback_line = self._get("commission_lines", self.ids["clawback_line"])
        recompute = json.loads(self._get("decisions", "D-0005")["output_json"])["result"]
        plan = calculator.plan_for(queries.commission_rows(self.ro, REP, self.now), REP, "2026-03-31")
        q1_before = recompute["attainment_after_bps"]
        q1_after = (recompute["prior_credit_cents"] + recompute["credit_cents"] + clawback_line["credit_cents"]) \
            * 10000 // plan.quota_cents
        deferred_before = -self._account_total("2400", DEAL, exclude=(refund_je,))
        deferred_after = -self._account_total("2400", DEAL)
        cash_effect = sum(l["debit_cents"] - l["credit_cents"] for l in refund_lines if l["account"] == "1000")
        items = [
            ("Deferred revenue", f"{fmt_usd(deferred_before)} -> {fmt_usd(deferred_after)} (refund {refund_je})"),
            ("Cash", f"{fmt_usd(cash_effect)} ({refund_je})"),
            ("Acme customer balance", f"{fmt_usd(self._account_total('1200', DEAL))}, contract terminated "
                                      f"{churn['effective_date']}"),
            ("Revenue forecast", f"{fmt_usd(-monthly)}/month from {clk.add_periods(churn['effective_date'][:7], 1)} "
                                 f"({fmt_usd(-remaining)} remaining contract)"),
            ("Jordan commission", f"{fmt_usd(clawback_line['amount_cents'])} clawback ({clawback_line['id']}, "
                                  f"{clawback_je})"),
            ("Deferred commission", f"{fmt_usd(clawback_lines['1350']['credit_cents'])} written off ({clawback_je})"),
            ("Commission expense", f"{fmt_usd(-clawback_lines['6110']['credit_cents'])} amortization reversed "
                                   f"({clawback_je})"),
            ("Jordan Q1 attainment", f"{fmt_pct(q1_before)} -> {fmt_pct(q1_after)} (report only; 2026-Q1 periods "
                                     "locked)"),
        ]
        return ["Acme churn: what changed"] + [f"  {n}. {label:<24}{text}" for n, (label, text) in
                                               enumerate(items, start=1)]

    @property
    def ok(self) -> bool:
        return (not any(self.results["invariants"].values()) and self.results["chain"][0]
                and all(self.results["replays"].values()))


def run(db_path=None, out=print) -> AcmeDemo:
    return AcmeDemo(db_path, out).run()
