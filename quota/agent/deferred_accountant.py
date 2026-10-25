"""seat:deferred_accountant: accrual, amortization, clawback entries, and posting approved entries.

Routes: job.due commission_accrual / amortization / churn_review, and approval.decided on journal entries.
"""

import json

from quota import clock as clk
from quota import config
from quota.agent.seat import Seat
from quota.engine import calculator, entries, recompute, routing
from quota.ledger import queries
from quota.money import fmt_usd
from quota.timeline import entry_text, signed

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

    def true_ups_of(self, line_id) -> list[dict]:
        """Quarter true-up lines that correct this line (R3)."""
        decided = {d["id"] for d in self.decisions(kind="true_up")}
        return [l for l in self.lines() if l["corrects_id"] == line_id and l["decision_id"] in decided]

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
        elif decision["kind"] in ("clawback", "void_reversal", "true_up"):
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
            rows = {"deals": [ctx.data["deal"]], "commission_lines": [line, *self.true_ups_of(line["id"])],
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
        reversed_ = [line for line, _, _, void in ctx.calc if void["output"]["args"]["recoverable"]]
        void_date = clk.local_date(self.now)
        return decision_ids + self.true_up(reversed_, clk.add_periods(clk.period_of(void_date), -1), void_date)

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
        true_ups = {d["id"] for d in self.decisions(kind="true_up")}     # a true-up line is not reviewed on its own
        schedules = {s["line_id"]: s for s in self.active_schedules()}
        todo = []
        for churn in ctx.data["churns"]:
            deal_id = json.loads(churn["payload_json"])["deal_id"]
            for line in self.lines(deal_id=deal_id):
                # Expensed lines (term <= 12 months, Q1 opening lines) have no schedule; they are reviewed too.
                if (line["amount_cents"] > 0 and line["id"] not in reviewed and line["decision_id"] not in true_ups
                        and self.line_status(line["id"]) in ("approved", "paid")):
                    schedule = schedules.get(line["id"])
                    rows = {**queries.commission_rows(self.ro, line["rep_id"], self.now),
                            "commission_lines": [line, *self.true_ups_of(line["id"])],
                            "events": [churn], "amortization_schedules": [schedule] if schedule else [],
                            "people": [self.get("people", line["rep_id"])]}
                    todo.append((line, schedule, rows))
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
            if cb["entry"]:
                je_id = self.draft(cb["entry"], decision_id)
                self.emit("ENTRY", f"{je_id} drafted clawback {line['id']}: {entry_text(cb['entry']['lines'])}")
                self.request_entry_approval(je_id, a["total_cents"])
            if a.get("chargeback_code"):
                self.escalate_chargeback(a["chargeback_code"], line, decision_id)
            if schedule:
                cancelled = self.wp.cancel_schedule(self.principal, schedule["id"])
                self.emit("ENTRY", f"{schedule['id']} cancelled ({cancelled})")
            decision_ids.append(decision_id)
        period = ctx.event.payload["period"]
        reversed_ = [line for line, _, _, cb in ctx.calc if cb["output"]["args"]["recoverable"]]
        return decision_ids + self.true_up(reversed_, period, clk.period_end(period), self.rebooked_in(period))

    # ---------- R3: recompute the quarter a reversal touched ----------

    def live_lines(self, rep_id, quarter, reversed_ids) -> list[dict]:
        """The rep's commission lines that still count in the quarter: calculated (or opening) lines that are not
        superseded, clawed back, voided or being reversed right now."""
        calcs = {d["id"]: d for kind in ("commission_calc", "opening_balance") for d in self.decisions(kind=kind)}
        return [l for l in self.lines(rep_id=rep_id) if l["booking_quarter"] == quarter and l["amount_cents"] > 0
                and l["decision_id"] in calcs and l["id"] not in reversed_ids
                and self.line_status(l["id"]) not in ("superseded", "clawed_back")
                and self.get("deals", l["deal_id"])["stage"] != "voided"]

    def rebooked_in(self, period) -> list[dict]:
        """Lines the analyst re-booked this period (an amendment's increment, a corrected booking date or base)
        and the lines they replaced: the quarters both sit in may have moved."""
        calcs = {d["id"]: d for d in self.decisions(kind="commission_calc")}
        new = [l for l in self.lines() if l["decision_id"] in calcs and clk.period_of(l["known_at"]) == period
               and (l["corrects_id"] or "+" in calcs[l["decision_id"]]["subject_id"])]
        return new + [self.get("commission_lines", l["corrects_id"]) for l in new if l["corrects_id"]]

    def true_up(self, reversed_lines, through, date, rebooked=()) -> list[str]:
        """For each (rep, quarter) a reversal or re-booking touched, re-run the quarter and record a true_up
        decision per line whose amount changed: a lower amount is recovered (an entry for the Controller), a
        higher one becomes a new correcting line the analyst writes and the next accrual books."""
        reversed_ids = {l["id"] for l in reversed_lines}
        decision_ids = []
        touched = [*reversed_lines, *rebooked]
        for rep_id, quarter in sorted({(l["rep_id"], l["booking_quarter"]) for l in touched}):
            live = self.live_lines(rep_id, quarter, reversed_ids)
            bookings = [{"line_id": l["id"], "booking": json.loads(self.get("decisions", l["decision_id"])["output_json"])
                         ["args"]["booking"], "amount_cents": l["amount_cents"] + sum(
                             t["amount_cents"] for t in self.true_ups_of(l["id"]))} for l in live]
            if not bookings:
                continue
            plans = queries.fetch_all(self.ro, "SELECT * FROM plans WHERE rep_id = ? AND known_at <= ? "
                                               "ORDER BY id, version", (rep_id, self.now))
            plan = calculator.plan_for({"plans": plans}, rep_id, f"{quarter[:4]}-{(int(quarter[-1]) - 1) * 3 + 1:02d}-01")
            deltas = recompute.recompute_quarter(rep_id, quarter, bookings, plan)
            gone = sum(l["credit_cents"] for l in reversed_lines if (l["rep_id"], l["booking_quarter"]) == (rep_id, quarter))
            credit = sum(b["booking"]["base_cents"] * b["booking"]["split_bps"] // 10000 for b in bookings)
            before, after = (credit + gone) * 10000 // plan.quota_cents, credit * 10000 // plan.quota_cents
            for delta in deltas:
                decision_ids.append(self.write_true_up(delta, rep_id, quarter, bookings, plans, through, date,
                                                       before, after, touched))
        return decision_ids

    def write_true_up(self, delta, rep_id, quarter, bookings, plans, through, date, before, after, touched):
        line = self.get("commission_lines", delta.line_id)
        schedule = next((s for s in self.active_schedules() if s["line_id"] == line["id"]), None)
        rows = {"commission_lines": [line, *self.true_ups_of(line["id"])], "plans": plans,
                "people": [self.get("people", rep_id)], "amortization_schedules": [schedule] if schedule else []}
        caused_by = sorted({l["deal_id"] for l in touched if l["rep_id"] == rep_id})
        output = recompute.true_up_entry(rows, line["id"], rep_id, quarter, bookings, through, date, caused_by)
        r = output["result"]
        cause = ", ".join(caused_by)
        text = (f"{quarter} recomputed after {cause} changed: {line['id']} ({line['deal_id']}) "
                f"{fmt_usd(delta.old_cents)} -> {fmt_usd(delta.new_cents)} ({signed(delta.delta_cents)}), now "
                f"{r['recomputed']['tier']} tier; attainment {before / 100:.2f}% -> {after / 100:.2f}%")
        if r["entry"]:
            months = f", {r['m_used']} of {r['m_total']} months amortized" if r["m_total"] else ""
            text += f"; recover from future advances ({entry_text(r['entry']['lines'])}{months})"
        elif r["chargeback_code"]:
            text += f"; {r['chargeback_code']}: no receivable"
        else:
            text += "; a correcting line is accrued with the next run"
        decision_id = self.wp.record_decision(self.principal, "true_up", line["id"], queries.input_refs(rows), {}, [],
                                              output, "escalated" if delta.delta_cents < 0 else "drafted", text)
        self.emit("DECISION", f"{decision_id} true_up {line['id']}: {text}")
        if r["entry"]:
            je_id = self.draft(r["entry"], decision_id)
            self.emit("ENTRY", f"{je_id} drafted true_up {line['id']}: {entry_text(r['entry']['lines'])}")
            self.request_entry_approval(je_id, -delta.delta_cents)
        if r["schedule"]:
            self.emit("ENTRY", f"{schedule['id']} cancelled ({self.wp.cancel_schedule(self.principal, schedule['id'])})")
            s = r["schedule"]
            schedule_id = self.wp.add_schedule(self.principal, line["id"], s["total_cents"], s["months"],
                                               s["start_period"], s["schedule"])
            self.emit("ENTRY", f"{schedule_id} amortization schedule {line['id']} re-spread: "
                               f"{s['months'] - r['m_used']} x {fmt_usd(s['schedule'][r['m_used']][1])} from "
                               f"{s['schedule'][r['m_used']][0]} ({decision_id})")
        if r["chargeback_code"]:
            self.escalate_chargeback(r["chargeback_code"], {**line, "amount_cents": -delta.delta_cents}, decision_id)
        return decision_id
