"""seat:deferred_accountant: accrual, amortization, clawback entries, and posting approved entries.

Routes: job.due commission_accrual / amortization / churn_review, and approval.decided on journal entries.
"""

import json

from quota import clock as clk
from quota import config
from quota.agent.seat import Seat
from quota.engine import amortization, entries, routing
from quota.ledger import queries
from quota.money import fmt_usd
from quota.timeline import entry_text

ACCOUNTANT = "seat:deferred_accountant"
JOB_ROUTES = {"commission_accrual": "accrual", "amortization": "amortization", "churn_review": "churn_review"}


class DeferredAccountant(Seat):
    principal = ACCOUNTANT
    handles = frozenset({"job.due", "approval.decided", "deal.updated"})

    def route(self, ev):
        if ev.type == "job.due":
            return JOB_ROUTES.get(ev.payload["job"])
        if ev.type == "approval.decided":
            return "entry_decided" if self.get("approvals", ev.payload["approval_id"])["kind"] == "journal_entry" else None
        if ev.type == "deal.updated":
            return "void" if ev.payload["fields"].get("stage") == "voided" else None
        return None

    # ---------- shared ----------

    def contract_for(self, deal_id):
        found = queries.fetch_one(self.ro, "SELECT id FROM contracts WHERE deal_id = ? AND known_at <= ? "
                                           "ORDER BY version DESC LIMIT 1", (deal_id, self.now))
        return self.get("contracts", found["id"]) if found else None

    def active_schedules(self) -> list[dict]:
        rows = queries.fetch_all(self.ro, "SELECT * FROM amortization_schedules WHERE known_at <= ? ORDER BY id",
                                 (self.now,))
        cancelled = {r["id"][:-2] for r in rows if r["status"] == "cancelled"}
        return [r for r in rows if r["status"] == "active" and r["id"] not in cancelled]

    def draft(self, entry, decision_id) -> str:
        return self.wp.draft_entry(self.principal, entry["effective_date"], entry["memo"], entry["lines"],
                                   decision_id=decision_id)

    def request_entry_approval(self, je_id, amount):
        approver = routing.owner("journal_entry")
        approval_id = self.wp.request_approval(self.principal, "journal_entry", je_id, amount, approver=approver)
        self.emit("APPROVAL", f"{approval_id} journal_entry {je_id} {fmt_usd(amount)} -> {approver}")
        return approval_id

    # ---------- job.due commission_accrual ----------

    def reconcile_accrual(self, ctx):
        """Positive lines not yet accrued (superseded and clawed-back lines are done with)."""
        accrued = {d["subject_id"] for d in self.decisions(kind="commission_accrual")}
        return [l for l in self.lines() if l["amount_cents"] > 0 and l["id"] not in accrued
                and self.line_status(l["id"]) in ("draft", "approved")
                and self.get("deals", l["deal_id"])["stage"] != "voided"]

    def calculate_accrual(self, ctx):
        out = []
        for line in ctx.res:
            rows = {"commission_lines": [line], "contracts": [self.contract_for(line["deal_id"])]}
            out.append((line, rows, entries.accrual_decision(rows, line["id"], self.now)))
        return out

    def write_accrual(self, ctx):
        decision_ids = []
        for line, rows, acc in ctx.calc:
            args = acc["output"]["args"]
            schedule_text = (f"schedule {args['term_months']} x {fmt_usd(acc['schedule'][0][1])} from "
                             f"{args['start_period']}" if acc["capitalized"] else "expensed now (term <= 12 months)")
            text = (f"{'capitalize' if acc['capitalized'] else 'expense'} {line['id']} {fmt_usd(line['amount_cents'])}: "
                    f"term {args['term_months']} months; {entry_text(acc['entry']['lines'])}; {schedule_text}")
            decision_id = self.wp.record_decision(self.principal, "commission_accrual", line["id"],
                                                  queries.input_refs(rows), {}, [], acc["output"], "drafted", text)
            self.emit("DECISION", f"{decision_id} commission_accrual {line['id']}: {text}")
            je_id = self.draft(acc["entry"], decision_id)
            self.emit("ENTRY", f"{je_id} drafted commission_accrual {line['id']}: {entry_text(acc['entry']['lines'])}")
            self.request_entry_approval(je_id, line["amount_cents"])
            decision_ids.append(decision_id)
        return decision_ids

    # ---------- approval.decided on a journal entry ----------

    def gather_entry_decided(self, ctx):
        p = ctx.event.payload
        root = self.approval_root(p["approval_id"])
        entry = self.get("journal_entries", root["subject_id"])
        ctx.data.update(root=root, entry=entry, decision=self.get("decisions", entry["decision_id"]),
                        result=p.get("result"), reason=p["reason"], decided_by=p["approver"])

    def decide_entry_decided(self, ctx):
        entry, root = ctx.data["entry"], ctx.data["root"]
        if ctx.data["result"] == "rejected":
            return "hold"
        if (queries.satisfied_approval(self.ro, "journal_entry", entry["id"], self.now) == root["id"]
                and queries.je_status(self.ro, entry["id"], self.now) != "posted"):
            return "post"
        return "wait"

    def write_entry_decided(self, ctx):
        entry, root, decision = ctx.data["entry"], ctx.data["root"], ctx.data["decision"]
        if ctx.decision == "hold":
            self.emit("ENTRY", f"{entry['id']} held: {root['id']} rejected by {ctx.data['decided_by']} "
                               f"({ctx.data['reason']})")
            return []
        if ctx.decision != "post":
            return []
        self.wp.post_entry(self.principal, entry["id"])
        self.emit("ENTRY", f"{entry['id']} posted ({root['id']})")
        if decision["kind"] == "commission_accrual":
            line_id = decision["subject_id"]
            self.wp.set_line_status(self.principal, line_id, "approved")
            self.emit("ENTRY", f"{line_id} -> approved (accrual {entry['id']}, {root['id']})")
            output = json.loads(decision["output_json"])
            if output["args"]["capitalized"]:
                args, schedule = output["args"], output["result"]["schedule"]
                schedule_id = self.wp.add_schedule(self.principal, line_id, args["amount_cents"], args["term_months"],
                                                   args["start_period"], schedule)
                self.emit("ENTRY", f"{schedule_id} amortization schedule {line_id}: {args['term_months']} x "
                                   f"{fmt_usd(schedule[0][1])} from {args['start_period']} ({decision['id']})")
        elif decision["kind"] in ("clawback", "void_reversal"):
            for line in self.lines():
                if line["decision_id"] == decision["id"] and line["amount_cents"] < 0:
                    self.wp.set_line_status(self.principal, line["id"], "approved")
                    self.emit("ENTRY", f"{line['id']} -> approved ({decision['kind']} {entry['id']}, {root['id']})")
        return []

    # ---------- deal.updated to voided (E15) ----------

    def gather_void(self, ctx):
        deal_id = ctx.event.payload["deal_id"]
        reversed_ = {d["subject_id"] for kind in ("clawback", "void_reversal") for d in self.decisions(kind=kind)}
        schedules = {s["line_id"]: s for s in self.active_schedules()}
        ctx.data["deal"] = self.get("deals", deal_id)
        ctx.data["todo"] = [(line, schedules.get(line["id"])) for line in self.lines(deal_id=deal_id)
                            if line["amount_cents"] > 0 and line["id"] not in reversed_
                            and self.line_status(line["id"]) in ("approved", "paid")]

    def calculate_void(self, ctx):
        out = []
        for line, schedule in ctx.data["todo"]:
            rows = {"deals": [ctx.data["deal"]], "commission_lines": [line],
                    "amortization_schedules": [schedule] if schedule else [],
                    "plans": queries.fetch_all(self.ro, "SELECT * FROM plans WHERE rep_id = ? AND known_at <= ? "
                                                        "ORDER BY id, version", (line["rep_id"], self.now)),
                    "people": [self.get("people", line["rep_id"])]}
            out.append((line, schedule, rows, entries.void_decision(rows, line["id"], self.now)))
        return out

    def escalate_chargeback(self, code, line, decision_id) -> None:
        """R2: no receivable could be booked; the Controller decides what, if anything, happens next."""
        controller = routing.owner("material_flag")
        why = ("the signed plan does not make commissions recoverable advances, or this one is already earned"
               if code == "CHARGEBACK_NOT_PERMITTED" else "the rep has left, so there is no future advance to net it "
                                                         "against")
        self.wp.add_message("slack:dm", self.principal, controller,
                            f"{code} on {line['id']} ({config.display_name(line['rep_id'])}, "
                            f"{fmt_usd(line['amount_cents'])}, {decision_id}): {why}. No receivable was booked; the "
                            f"remainder is expensed. Recovering anything now is your call.", None)
        self.emit("MESSAGE", f"to {controller}: {code} on {line['id']} ({decision_id})")

    def write_void(self, ctx):
        decision_ids = []
        for line, schedule, rows, void in ctx.calc:
            code = void["output"]["args"].get("chargeback_code")
            entry = void["entry"]
            if code:
                text = (f"{line['deal_id']} voided {void['output']['args']['date']}: the commission on {line['id']} was "
                        f"never earned, but {code}: no receivable from {line['rep_id']}"
                        + (f"; the unamortized asset is expensed ({entry_text(entry['lines'])})" if entry else ""))
            else:
                text = (f"{line['deal_id']} voided {void['output']['args']['date']}: the commission on {line['id']} "
                        f"was never earned; recover {fmt_usd(line['amount_cents'])} from {line['rep_id']} in the open "
                        f"period ({entry_text(entry['lines'])})")
            decision_id = self.wp.record_decision(self.principal, "void_reversal", line["id"], queries.input_refs(rows),
                                                  {}, [], void["output"], "escalated" if code else "drafted", text)
            self.emit("DECISION", f"{decision_id} void_reversal {line['id']}: {text}")
            if entry:
                je_id = self.draft(entry, decision_id)
                self.emit("ENTRY", f"{je_id} drafted void_reversal {line['id']}: {entry_text(entry['lines'])}")
                self.request_entry_approval(je_id, line["amount_cents"])
            if code:
                self.escalate_chargeback(code, line, decision_id)
            if schedule:
                self.emit("ENTRY", f"{schedule['id']} cancelled ({self.wp.cancel_schedule(self.principal, schedule['id'])})")
            decision_ids.append(decision_id)
        return decision_ids

    # ---------- job.due amortization ----------

    def reconcile_amortization(self, ctx):
        """Every schedule month up to this period not yet posted (a schedule that started late catches up)."""
        period = ctx.event.payload["period"]
        posted_memos = {e["memo"] for e in queries.fetch_all(self.ro, "SELECT memo FROM journal_entries")}
        due = []
        for schedule in self.active_schedules():
            for month, amount in json.loads(schedule["schedule_json"]):
                entry = entries.amortization_entry(schedule["line_id"], amount, clk.period_end(month))
                if month <= period and amount > 0 and entry["memo"] not in posted_memos:
                    due.append((schedule, month, entry))
        return due

    def write_amortization(self, ctx):
        for schedule, period, entry in ctx.res:
            accrual = self.decisions(kind="commission_accrual", subject_id=schedule["line_id"])[-1]
            je_id = self.draft(entry, accrual["id"])
            self.wp.post_entry(self.principal, je_id)
            standing = queries.standing_precedent(self.ro, "amortization", self.now)
            self.emit("ENTRY", f"{je_id} posted amortization {schedule['line_id']} {period}: "
                               f"{entry_text(entry['lines'])} (standing approval {standing})")
        return []

    # ---------- job.due churn_review ----------

    def gather_churn_review(self, ctx):
        end = clk.period_end(ctx.event.payload["period"])
        churns = queries.fetch_all(self.ro, "SELECT * FROM events WHERE type = 'customer.churned' AND release_at <= ? "
                                            "ORDER BY id", (self.now,))
        ctx.data["churns"] = [c for c in churns if json.loads(c["payload_json"])["effective_date"] <= end]

    def reconcile_churn_review(self, ctx):
        """Lines on churned deals that were earned (approved or paid) and not yet reviewed for clawback."""
        reviewed = {d["subject_id"] for d in self.decisions(kind="clawback")}
        schedules = {s["line_id"]: s for s in self.active_schedules()}
        todo = []
        for churn in ctx.data["churns"]:
            deal_id = json.loads(churn["payload_json"])["deal_id"]
            for line in self.lines(deal_id=deal_id):
                if (line["amount_cents"] > 0 and line["id"] not in reviewed and line["id"] in schedules
                        and self.line_status(line["id"]) in ("approved", "paid")):
                    rows = {**queries.commission_rows(self.ro, line["rep_id"], self.now), "commission_lines": [line],
                            "events": [churn], "amortization_schedules": [schedules[line["id"]]],
                            "people": [self.get("people", line["rep_id"])]}
                    todo.append((line, schedules[line["id"]], rows))
        return todo

    def calculate_churn_review(self, ctx):
        return [(line, schedule, rows, entries.clawback_decision(rows, line["id"], self.now))
                for line, schedule, rows in ctx.res]

    def write_churn_review(self, ctx):
        decision_ids = []
        for line, schedule, rows, cb in ctx.calc:
            a = cb["output"]["args"]
            precedent = f" ({cb['precedent_ids'][0]})" if cb["precedent_ids"] else ""
            within = "within" if a["window_end"] and a["churn_date"] <= a["window_end"] else "outside"
            signed_plan = "plan signed with clawback" if a["plan"]["signed_at"] else "plan not signed"
            text = (f"booking {a['booking_date']}{precedent}, churn {a['churn_date']} {within} {a['window_months']} "
                    f"months (window ends {a['window_end']}), {signed_plan} -> "
                    f"{'recoverable' if a['recoverable'] else 'not recoverable'}")
            if a.get("chargeback_code"):
                text += f" ({a['chargeback_code']}: no receivable; the unamortized remainder is expensed)"
            # A clawback always needs the Controller's sign-off on its entry: an escalation, not a routine draft.
            decision_id = self.wp.record_decision(self.principal, "clawback", line["id"], queries.input_refs(rows),
                                                  cb["resolutions"], cb["precedent_ids"], cb["output"], "escalated", text)
            self.emit("DECISION", f"{decision_id} clawback {line['id']}: {text}")
            je_id = self.draft(cb["entry"], decision_id)
            self.emit("ENTRY", f"{je_id} drafted clawback {line['id']}: {entry_text(cb['entry']['lines'])}")
            self.request_entry_approval(je_id, line["amount_cents"])
            if a.get("chargeback_code"):
                self.escalate_chargeback(a["chargeback_code"], line, decision_id)
            cancelled = self.wp.cancel_schedule(self.principal, schedule["id"])
            self.emit("ENTRY", f"{schedule['id']} cancelled ({cancelled})")
            decision_ids.append(decision_id)
        return decision_ids
