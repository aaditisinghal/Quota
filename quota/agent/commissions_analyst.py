"""seat:commissions_analyst: commission runs, disputes, plan exceptions, payroll files, clawback lines.

Routes: job.due commission_run / payroll_file / churn_review, message.posted to this seat (a dispute, an
escalation, an exception grant, a chat approval, or something to clarify) and approval.decided on plan
exceptions.
"""

import json

from quota import clock as clk
from quota import config
from quota.agent.extract import extract_claim, has_authority
from quota.agent.seat import Seat
from quota.engine import calculator, disputes, ranking, routing
from quota.ledger import queries
from quota.ledger.write_path import Refused
from quota.money import fmt_usd
from quota.timeline import signed

ANALYST = "seat:commissions_analyst"
CHANNEL = "slack:dm"
CLAIM_ROUTES = {"dispute": "dispute", "escalate": "dispute", "exception_grant": "exception",
                "spiff_claim": "authority", "approval": "chat_approval", "rejection": "chat_approval"}
JOB_ROUTES = {"commission_run": "commission_run", "payroll_file": "payroll_file", "churn_review": "clawback_lines",
              "approver_digest": "digest", "approval_followup": "followup"}
DISPUTE_OUTCOMES = {"held": "held", "conceded": "conceded", "escalation_offered": "escalated"}
CLARIFY_PREFIX = "I couldn't turn that into something I can check"


def alt_text(d) -> str:
    value = d["value"]
    money = fmt_usd(value) if isinstance(value, int) else value
    label = {"crm_close_date": f"CRM close {str(value)[:10]}", "contract_date": f"contract date {str(value)[:10]}",
             "countersignature": f"countersigned {value}", "crm": f"CRM amount {money}",
             "contract": f"contract ACV {money}"}.get(d["basis"], f"{d['basis']} {value}")
    return label + ("" if d["effect_cents"] is None else f" {signed(d['effect_cents'])}")


def calc_text(calc) -> str:
    r, res = calc["result"], calc["resolved"]
    booking, base = res["booking_date"], res["base"]
    precedent = f" {booking.source.split(':', 1)[1]}" if booking.basis == "precedent" else ""
    crm = next((d for d in base.disagreements if d["basis"] == "crm"), None)
    base_text = f"base={base.basis} {fmt_usd(base.value)}" + (f" over CRM {fmt_usd(crm['value'])}" if crm else "")
    prior = f"; prior {fmt_usd(r.prior_credit_cents)}" if r.prior_credit_cents else ""
    return (f"{r.quarter} {r.tier} tier {fmt_usd(r.amount_cents)} (booking={booking.basis}{precedent} "
            f"{booking.rule_id}; {base_text} {base.rule_id}{prior})")


class CommissionsAnalyst(Seat):
    principal = ANALYST
    handles = frozenset({"message.posted", "approval.decided", "job.due"})

    def __init__(self, world, extractor, llm=None):
        super().__init__(world)
        self.extractor = extractor
        self.llm = llm          # None under --scripted: replies use the Phase 1 template

    def route(self, ev):
        p = ev.payload
        if ev.type == "message.posted":
            return "message" if p["to"] == self.principal else None
        if ev.type == "approval.decided":
            return "exception_decided" if self.get("approvals", p["approval_id"])["kind"] == "plan_exception" else None
        if ev.type == "job.due":
            return JOB_ROUTES.get(p["job"])
        return None

    # ---------- shared writes ----------

    def send(self, to, text, kind="MESSAGE") -> str:
        message_id = self.wp.add_message(CHANNEL, self.principal, to, text, None)
        self.emit(kind, f"{message_id} to {to}:\n{text}" if kind == "REPLY" else f"{message_id} to {to}: {text}")
        return message_id

    def record_calc(self, rows, deal_id, rep_id, calc, corrects_id=None) -> tuple[str, str]:
        """A commission_calc decision and its draft line (correcting an earlier line if given)."""
        text = calc_text(calc)
        # Two sources can carry the same alternative (contract + e-sign countersignature); print it once.
        alternatives = list(dict.fromkeys(alt_text(d) for res in calc["resolved"].values() for d in res.disagreements))
        explanation = text + ("; disagreements: " + "; ".join(alternatives) if alternatives else "")
        decision_id = self.wp.record_decision(
            self.principal, "commission_calc", f"{deal_id}/{rep_id}", queries.input_refs(rows), calc["resolutions"],
            calc["precedent_ids"], calc["output"], "drafted", explanation)
        self.emit("DECISION", f"{decision_id} commission_calc {deal_id}/{rep_id}: {text}"
                  + (f"\ndisagreements: {'; '.join(alternatives)}" if alternatives else ""))
        r = calc["result"]
        line_id = self.wp.add_commission_line(self.principal, {
            "rep_id": rep_id, "deal_id": deal_id, "booking_quarter": r.quarter, "credit_cents": r.credit_cents,
            "base_portion_cents": r.base_portion_cents, "accel_portion_cents": r.accel_portion_cents,
            "amount_cents": r.amount_cents}, decision_id, corrects_id=corrects_id)
        corrects = f", corrects {corrects_id}" if corrects_id else ""
        self.emit("ENTRY", f"{line_id} draft commission line {deal_id}/{rep_id} {r.quarter} "
                           f"{fmt_usd(r.amount_cents)} ({decision_id}{corrects})")
        if corrects_id:
            self.wp.set_line_status(self.principal, corrects_id, "superseded")
            self.emit("ENTRY", f"{corrects_id} -> superseded by {line_id}")
        return decision_id, line_id

    def deal_names(self) -> dict[str, str]:
        return {d: self.get("deals", d)["account"] for d in queries.ids(self.ro, "deals", self.now)}

    def pending_approvals(self, now=None) -> list[dict]:
        """Approvals nobody has decided yet. Silence never approves; it only earns reminders."""
        return queries.pending_approvals(self.ro, now or self.now)

    def granted_by_precedent(self, deal_id, field, value) -> str | None:
        for p in queries.precedents_in_force(self.ro, self.now):
            if (queries.loads(p["scope_json"]) == {"deal_id": deal_id, "field": field}
                    and queries.loads(p["override_json"]).get("value") == value):
                return p["id"]
        return None

    # ---------- timers: the approver calendar ----------

    def timers(self, now) -> list[tuple[str, str, dict]]:
        """While anything is pending: the next two 09:00Z digests, and a follow-up for each approval
        ESCALATE_AFTER_BUSINESS_DAYS after it was requested."""
        pending = self.pending_approvals(now)
        if not pending:
            return []
        first = f"{now[:10]}T{config.DIGEST_TIME}"
        if first < now:
            first = clk.add_days(first, 1)
        out = [(t, "job.due", {"job": "approver_digest", "date": t[:10]}) for t in (first, clk.add_days(first, 1))]
        out += [(clk.add_business_days(a["known_at"], config.ESCALATE_AFTER_BUSINESS_DAYS), "job.due",
                 {"job": "approval_followup", "approval_id": a["id"]}) for a in pending]
        return out

    # ---------- job.due commission_run ----------

    def gather_commission_run(self, ctx):
        reps = sorted({r["rep_id"] for r in queries.fetch_all(self.ro, "SELECT rep_id FROM plans WHERE known_at <= ?",
                                                              (self.now,))})
        ctx.data["rows"] = {rep: queries.commission_rows(self.ro, rep, self.now) for rep in reps}
        ctx.data["lined"] = {(l["deal_id"], l["rep_id"]) for l in self.lines()}

    def reconcile_commission_run(self, ctx):
        """Deals whose resolved booking falls in the run's period and that have no line yet."""
        period = ctx.event.payload["period"]
        todo = []
        for rep, rows in ctx.data["rows"].items():
            for deal in calculator.latest_by_id(rows["deals"]).values():
                if (deal["id"], rep) in ctx.data["lined"]:
                    continue
                resolved = calculator.resolve_deal(rows, deal, rep, self.now)
                booking = resolved["booking_date"].value
                if booking and clk.period_of(booking) == period and ranking.split_bps(resolved["splits"], rep) > 0:
                    todo.append((rep, deal["id"]))
        return todo

    def calculate_commission_run(self, ctx):
        return [(rep, deal_id, calculator.commission_decision(ctx.data["rows"][rep], deal_id, rep, self.now))
                for rep, deal_id in ctx.res]

    def write_commission_run(self, ctx):
        return [self.record_calc(ctx.data["rows"][rep], deal_id, rep, calc)[0] for rep, deal_id, calc in ctx.calc]

    # ---------- message.posted ----------

    def gather_message(self, ctx):
        ctx.data["message"] = self.get("messages", ctx.event.payload["message_id"])
        ctx.data["deals"] = self.deal_names()

    def extract_message(self, ctx):
        message = ctx.data["message"]
        claim = extract_claim(self.extractor, message["text"], message["author"], self.now[:4], ctx.data["deals"])
        if claim.kind == "other" and claim.problem is None:
            ctx.route = "noted"          # thanks, acceptance, chit-chat: nothing to check, nothing to say
        else:
            ctx.route = CLAIM_ROUTES.get(claim.kind, "clarify")
        return claim

    # dispute / escalate

    def _line_for(self, claim, author):
        if claim.line_ref and claim.line_ref.upper().startswith("CL-"):
            return self.get("commission_lines", claim.line_ref.upper())
        if claim.deal_ref:
            return self.current_line(claim.deal_ref, author)
        if claim.kind in ("dispute", "escalate"):   # no ref written: the author's most recent dispute thread
            for decision in reversed(self.decisions(kind="dispute")):
                message = self.get("messages", json.loads(decision["output_json"])["args"]["message_id"])
                if message["author"] == author:
                    return self.get("commission_lines", decision["subject_id"])
        return None

    def _latest_decision_for_line(self, line):
        candidates = [self.get("decisions", line["decision_id"])] + self.decisions(subject_id=line["id"])
        return max(candidates, key=lambda d: (d["known_at"], d["id"]))

    def reconcile_dispute(self, ctx):
        claim, message = ctx.facts, ctx.data["message"]
        line = self._line_for(claim, message["author"])
        asserted = claim.amount_cents if claim.field == "base" else claim.asserted_value
        if line is None or (claim.kind == "dispute" and (claim.field not in ("booking_date", "base") or asserted is None)):
            ctx.route = "clarify"
            return None
        ctx.data["line"] = line
        prior = self._latest_decision_for_line(line)
        if claim.kind == "escalate":
            if self.decisions(kind="escalation", subject_id=line["id"]):
                ctx.route = "already_escalated"      # never ask the same question twice for the same scope
                return None
            disputed = self.decisions(kind="dispute", subject_id=line["id"])
            if not disputed:
                ctx.route = "clarify"
                return None
            prior = disputed[-1]                     # escalate the claim that was held
        ctx.rows = {**queries.commission_rows(self.ro, line["rep_id"], self.now), "commission_lines": [line],
                    "messages": [message], "decisions": [prior]}
        engine_claim = {"kind": claim.kind, "line": line["id"], "quote": claim.quote}
        if claim.kind == "dispute":
            engine_claim.update(deal=line["deal_id"], field=claim.field, asserted=asserted, evidence_refs=[])
        return engine_claim

    def calculate_dispute(self, ctx):
        return disputes.dispute_decision(ctx.rows, ctx.data["line"]["id"], self.now, claim=ctx.res)

    def decide_dispute(self, ctx):
        result = ctx.calc["outcome"]
        return {"outcome": DISPUTE_OUTCOMES[result.outcome],
                "kind": "escalation" if ctx.facts.kind == "escalate" else "dispute"}

    def write_dispute(self, ctx):
        out, line, message = ctx.calc, ctx.data["line"], ctx.data["message"]
        result, fields = out["outcome"], out["outcome"].reply_fields
        res = out["resolutions"][out["claim"]["field"]]
        if result.outcome == "escalation_offered":
            text = f"{result.outcome} -> {fields['escalate_to']} ({fields['escalate_to_role']})"
        else:
            text = (f"{result.outcome}" + (", no_new_evidence: true" if result.no_new_evidence else "")
                    + f" ({out['claim']['field']} asserted {out['claim']['asserted']}; resolved {res['value']} "
                      f"via {res['basis']} {res['rule_id']})")
        kind = ctx.decision["kind"]
        decision_id = self.wp.record_decision(
            self.principal, kind, line["id"], queries.input_refs(ctx.rows), out["resolutions"], out["precedent_ids"],
            out["output"], ctx.decision["outcome"], text)
        self.emit("DECISION", f"{decision_id} {kind} {line['id']}: {text}")
        if result.outcome == "escalation_offered":
            held = [d["id"] for d in self.decisions(kind="dispute", subject_id=line["id"])]
            note = (f"Plan exception request from {config.display_name(message['author'])} on {line['id']} "
                    f"({out['claim']['deal_name']}): count booking {out['claim']['asserted']} in "
                    f"{out['at_claim'].quarter}. As resolved {fmt_usd(fields['as_resolved_cents'])} "
                    f"({out['at_resolved'].quarter}, {res['basis']} {res['value']}, {res['rule_id']}); as claimed "
                    f"{fmt_usd(fields['as_claimed_cents'])}. Difference {fmt_usd(fields['diff_cents'])}. "
                    f"Held {len(held)}x ({', '.join(held)}).")
            self.send(fields["escalate_to"], note)
            return [decision_id]
        reply = self.llm.draft_reply(fields) if self.llm else disputes.render_reply(fields)
        self.send(message["author"], reply, "REPLY")
        if result.outcome == "conceded":   # the sources say Quota was wrong: correct the line in the open period
            rows = queries.commission_rows(self.ro, line["rep_id"], self.now)
            calc = calculator.commission_decision(rows, line["deal_id"], line["rep_id"], self.now)
            if calc["result"].amount_cents != line["amount_cents"]:
                return [decision_id, self.record_calc(rows, line["deal_id"], line["rep_id"], calc, line["id"])[0]]
        return [decision_id]

    def write_already_escalated(self, ctx):
        line, message = ctx.data["line"], ctx.data["message"]
        escalation = self.decisions(kind="escalation", subject_id=line["id"])[-1]
        owner = routing.owner("plan_exception")
        self.send(message["author"], f"{line['id']} is already with {config.display_name(owner)} "
                                     f"({config.title(owner)}) since {escalation['id']}.", "REPLY")
        return []

    # exception_grant

    def reconcile_exception(self, ctx):
        claim, message = ctx.facts, ctx.data["message"]
        if not has_authority(claim, message["author"]):
            ctx.route = "authority"
            return None
        deal = self.get("deals", claim.deal_ref) if claim.deal_ref else None
        value = claim.amount_cents if claim.field == "base" else claim.asserted_value
        if deal is None or claim.field not in ("booking_date", "base") or value is None:
            ctx.route = "clarify"
            return None
        ctx.data["granted_by"] = self.granted_by_precedent(deal["id"], claim.field, value)
        if ctx.data["granted_by"]:
            ctx.route = "already_granted"
            return None
        ctx.rows = queries.commission_rows(self.ro, deal["owner_id"], self.now)
        return {"deal_id": deal["id"], "rep_id": deal["owner_id"], "field": claim.field, "value": value}

    def calculate_exception(self, ctx):
        r = ctx.res
        override = ({"booking_ts": disputes.asserted_booking_ts(r["value"])} if r["field"] == "booking_date"
                    else {"base_cents": r["value"]})
        current = calculator.evaluate(ctx.rows, r["deal_id"], r["rep_id"], self.now)
        granted = calculator.evaluate(ctx.rows, r["deal_id"], r["rep_id"], self.now, **override)
        return {"current": current, "granted": granted, "delta": granted.amount_cents - current.amount_cents}

    def decide_exception(self, ctx):
        return {"approver": routing.owner("plan_exception")}

    def write_exception(self, ctx):
        message, r, c, approver = ctx.data["message"], ctx.res, ctx.calc, ctx.decision["approver"]
        try:
            approval_id = self.wp.request_approval(self.principal, "plan_exception", message["id"], abs(c["delta"]),
                                                   approver=approver)
        except Refused as refused:
            self.emit("REFUSED", f"{refused.refusal_id} request_approval {message['id']}: "
                                 f"{', '.join(refused.reason_codes)}")
            self.send(message["author"], f"That exception was already declined; I can resubmit it only with new "
                                         f"evidence ({refused.refusal_id}).", "REPLY")
            return []
        self.emit("APPROVAL", f"{approval_id} plan_exception {r['deal_id']} {r['field']}={r['value']} "
                              f"({c['granted'].quarter}) -> {approver}: delta {fmt_usd(c['delta'])} "
                              f"({fmt_usd(c['current'].amount_cents)} -> {fmt_usd(c['granted'].amount_cents)}), "
                              f"from {message['id']}")
        return []

    def write_already_granted(self, ctx):
        self.send(ctx.data["message"]["author"], f"That is already granted under {ctx.data['granted_by']}.", "REPLY")
        return []

    # a claim from someone without the authority for it (E05)

    def decide_authority(self, ctx):
        return {"owner": routing.owner("plan_exception")}

    def write_authority(self, ctx):
        claim, message, owner = ctx.facts, ctx.data["message"], ctx.decision["owner"]
        author = config.display_name(message["author"])
        text = (f"{claim.kind} from {message['author']} routed to {owner}: needs approve:plan_exception, which "
                f"{message['author']} does not hold; nothing is paid on it")
        rows = {"messages": [message]}
        decision_id = self.wp.record_decision(
            self.principal, "authority_check", message["id"], queries.input_refs(rows), {}, [],
            {"fn": "has_authority", "args": {"claim": claim.to_dict(), "author": message["author"]},
             "result": {"authorized": False, "routed_to": owner}}, "escalated", text)
        self.emit("DECISION", f"{decision_id} authority_check {message['id']}: {text}")
        self.send(owner, f"{author} wrote \"{claim.quote}\" ({message['id']}). Only someone who can approve plan "
                         f"exceptions can grant that, so nothing is paid on it. Do you want to grant it?")
        self.send(message["author"], f"I've asked {config.display_name(owner)} ({config.title(owner)}); this needs "
                                     f"their approval before anything is paid.", "REPLY")
        return [decision_id]

    # someone asks the seat to approve or reject in chat: the write path is the judge, so try and report

    def reconcile_chat_approval(self, ctx):
        ref = (ctx.facts.line_ref or "").upper()
        if not ref.startswith("A-"):
            ctx.route = "clarify"
            return None
        approval = self.get("approvals", ref)
        if approval is None:
            ctx.route = "no_such_approval"
            ctx.data["ref"] = ref
        return approval

    def write_no_such_approval(self, ctx):
        self.send(ctx.data["message"]["author"], f"There is no approval {ctx.data['ref']}; nothing was approved.",
                  "REPLY")
        return []

    def write_chat_approval(self, ctx):
        claim, message, approval = ctx.facts, ctx.data["message"], ctx.res
        decision = "approved" if claim.kind == "approval" else "rejected"
        try:
            self.wp.decide_approval(self.principal, approval["id"], decision,
                                    f"asked in chat by {message['author']} ({message['id']})")
        except Refused as refused:
            self.emit("REFUSED", f"{refused.refusal_id} decide_approval {approval['id']}: "
                                 f"{', '.join(refused.reason_codes)}")
            approver = approval["approver"]
            self.send(message["author"], f"I can't decide {approval['id']}: {', '.join(refused.reason_codes)} "
                                         f"({refused.refusal_id}). It is waiting on {config.display_name(approver)} "
                                         f"({config.title(approver)}), who decides it in the approval queue.", "REPLY")
        return []

    # anything that did not survive extraction: one clarifying question, never the same one twice in a row

    def write_clarify(self, ctx):
        claim, message = ctx.facts, ctx.data["message"]
        sent = queries.fetch_all(self.ro, "SELECT text FROM messages WHERE author = ? AND to_principal = ? "
                                          "AND known_at <= ? ORDER BY id", (self.principal, message["author"], self.now))
        if sent and sent[-1]["text"].startswith(CLARIFY_PREFIX):
            return []                    # already asked and not yet answered with something checkable
        reason = f" ({claim.problem})" if claim.problem else ""
        self.send(message["author"], f"{CLARIFY_PREFIX}{reason}. Which deal or commission line do you mean, and what "
                                     f"do you think the right value is?", "REPLY")
        return []

    # ---------- approval.decided on a plan exception ----------

    def gather_exception_decided(self, ctx):
        p = ctx.event.payload
        root = self.approval_root(p["approval_id"])
        ctx.data.update(root=root, chain=queries.chain_ids(self.ro, root["id"], self.now), result=p.get("result"),
                        reason=p["reason"], decided_by=p["approver"], message=self.get("messages", root["subject_id"]),
                        deals=self.deal_names())

    def extract_exception_decided(self, ctx):
        message = ctx.data["message"]   # the same extraction as when the exception was requested
        return extract_claim(self.extractor, message["text"], message["author"], self.now[:4], ctx.data["deals"])

    def reconcile_exception_decided(self, ctx):
        claim, result = ctx.facts, ctx.data["result"]
        if result == "rejected":
            return "rejected"
        root = ctx.data["root"]
        if (result != "final" or not queries.chain_satisfied(self.ro, root["id"], self.now)
                # a backup approval for the same proposal may have been satisfied first
                or queries.satisfied_approval(self.ro, root["kind"], root["subject_id"], self.now) != root["id"]):
            return "waiting"
        if claim.kind != "exception_grant" or not claim.deal_ref:
            return "waiting"
        value = claim.amount_cents if claim.field == "base" else claim.asserted_value
        if self.granted_by_precedent(claim.deal_ref, claim.field, value):
            return "waiting"
        deal = self.get("deals", claim.deal_ref)
        ctx.rows = queries.commission_rows(self.ro, deal["owner_id"], self.now)
        ctx.data.update(deal=deal, rep=deal["owner_id"], line=self.current_line(deal["id"], deal["owner_id"]))
        return "granted"

    def calculate_exception_decided(self, ctx):
        if ctx.res != "granted":
            return None
        claim, deal, rep = ctx.facts, ctx.data["deal"], ctx.data["rep"]
        value = claim.amount_cents if claim.field == "base" else claim.asserted_value
        override = ({"booking_ts": disputes.asserted_booking_ts(value)} if claim.field == "booking_date"
                    else {"base_cents": value})
        granted = calculator.evaluate(ctx.rows, deal["id"], rep, self.now, **override)
        plan = calculator.plan_for(ctx.rows, rep, override.get("booking_ts", self.now))
        return {"value": value, "quarter": granted.quarter, "expires_at": plan.effective_to}

    def write_exception_decided(self, ctx):
        if ctx.res == "rejected":
            line = self.current_line(ctx.facts.deal_ref, self.get("deals", ctx.facts.deal_ref)["owner_id"]) \
                if ctx.facts.deal_ref else None
            if line is not None:
                self.send(line["rep_id"], f"{config.display_name(ctx.data['decided_by'])} declined the exception "
                                          f"({ctx.data['root']['id']}): \"{ctx.data['reason']}\". {line['id']} stays at "
                                          f"{fmt_usd(line['amount_cents'])}.", "REPLY")
            return []
        if ctx.res != "granted":
            return []
        claim, deal, rep, c, chain = ctx.facts, ctx.data["deal"], ctx.data["rep"], ctx.calc, ctx.data["chain"]
        approvers = ", ".join(f"{a} {queries.approval_decision(self.ro, a)['decided_by']}" for a in chain)
        precedent = self.wp.add_precedent(
            self.principal, f"{deal['account']} ({deal['id']}) {claim.field} = {c['value']} ({c['quarter']}), granted "
                            f"in {ctx.data['message']['id']} by {config.display_name(ctx.data['message']['author'])}; "
                            f"approved {approvers}",
            {"deal_id": deal["id"], "field": claim.field}, {"value": c["value"], "quarter": c["quarter"]}, chain,
            self.now, c["expires_at"])
        self.emit("APPROVAL", f"{precedent} precedent {deal['id']} {claim.field}={c['value']} ({c['quarter']}), "
                              f"expires {c['expires_at']} [{', '.join(chain)}]")
        rows = queries.commission_rows(self.ro, rep, self.now)
        calc = calculator.commission_decision(rows, deal["id"], rep, self.now)
        line = ctx.data["line"]
        return [self.record_calc(rows, deal["id"], rep, calc, line["id"] if line else None)[0]]

    # ---------- job.due payroll_file ----------

    def payroll_held(self) -> set[str]:
        """Lines a decision has taken out of payroll (subclasses of hold add to this)."""
        return set()

    def reconcile_payroll_file(self, ctx):
        """Approved positive lines on collected deals (the plan pays on collection). Recoveries (negative lines)
        stay in 1290 rather than netting through payroll."""
        held = self.payroll_held()
        return [l["id"] for l in self.lines() if l["amount_cents"] > 0 and l["id"] not in held
                and self.line_status(l["id"]) == "approved" and queries.collected(self.ro, l["deal_id"], self.now)]

    def write_payroll_file(self, ctx):
        if not ctx.res:
            return []
        file_id = self.wp.create_payroll_file(self.principal, ctx.event.payload["period"], ctx.res)
        total = self.get("payroll_files", file_id)["total_cents"]
        self.emit("PAYROLL", f"{file_id} created: {fmt_usd(total)} ({', '.join(ctx.res)})")
        approver = routing.owner("payroll_file")
        approval_id = self.wp.request_approval(self.principal, "payroll_file", file_id, total, approver=approver)
        self.emit("APPROVAL", f"{approval_id} payroll_file {file_id} {fmt_usd(total)} -> {approver}")
        pay_date = f"{ctx.event.payload['period']}-{config.PAYROLL_DAY:02d}"
        if clk.add_days(self.now, config.PAYROLL_BLOCKING_DAYS)[:10] >= pay_date:   # blocks a payroll: don't wait
            self.send(approver, f"{approval_id} blocks payroll {file_id} ({fmt_usd(total)}), which releases on "
                                f"{pay_date}. Please decide it before then.")
        return []

    # ---------- job.due approver_digest / approval_followup ----------

    def write_digest(self, ctx):
        by_approver: dict[str, list[dict]] = {}
        for approval in self.pending_approvals():
            by_approver.setdefault(approval["approver"], []).append(approval)
        for approver, waiting in sorted(by_approver.items()):
            items = "; ".join(f"{a['id']} {a['kind']} {a['subject_id']} {fmt_usd(a['amount_cents'])} "
                              f"(waiting since {a['known_at']})" for a in waiting)
            self.send(approver, f"Daily digest: {len(waiting)} approval(s) waiting on you: {items}.")
        return []

    def write_followup(self, ctx):
        approval = self.get("approvals", ctx.event.payload["approval_id"])
        if queries.approval_decision(self.ro, approval["id"], self.now) is not None:
            return []
        waited = f"{approval['id']} ({approval['kind']} {approval['subject_id']}) has waited " \
                 f"{config.ESCALATE_AFTER_BUSINESS_DAYS} business days"
        backup = routing.ESCALATION.get(approval["approver"])
        already = [a for a in self.pending_approvals() if a["kind"] == approval["kind"]
                   and a["subject_id"] == approval["subject_id"] and a["approver"] == backup]
        if backup is None or already:
            self.send(approval["approver"], f"Reminder: {waited} for your decision. Nothing happens until you decide.")
            return []
        routed = self.wp.request_approval(self.principal, approval["kind"], approval["subject_id"],
                                          approval["amount_cents"], approver=backup)
        self.emit("APPROVAL", f"{routed} {approval['kind']} {approval['subject_id']} {fmt_usd(approval['amount_cents'])} "
                              f"-> {backup}: no answer from {approval['approver']} on {approval['id']}")
        self.send(backup, f"{waited} with no answer from {config.display_name(approval['approver'])}; it is now "
                          f"with you as {routed}.")
        self.send(approval["approver"], f"{waited}; it is now also with {config.display_name(backup)} as {routed}. "
                                        f"Silence is not approval.")
        return []

    # ---------- job.due churn_review: the rep's side of a recoverable clawback ----------

    def reconcile_clawback_lines(self, ctx):
        corrected = {l["corrects_id"] for l in self.lines() if l["amount_cents"] < 0}
        return [(d, self.get("commission_lines", d["subject_id"])) for d in self.decisions(kind="clawback")
                if json.loads(d["output_json"])["args"]["recoverable"] and d["subject_id"] not in corrected]

    def write_clawback_lines(self, ctx):
        for decision, line in ctx.res:
            clawback = self.wp.add_commission_line(self.principal, {
                "rep_id": line["rep_id"], "deal_id": line["deal_id"], "booking_quarter": line["booking_quarter"],
                "credit_cents": -line["credit_cents"], "base_portion_cents": -line["base_portion_cents"],
                "accel_portion_cents": -line["accel_portion_cents"], "amount_cents": -line["amount_cents"]},
                decision["id"], corrects_id=line["id"])
            self.emit("ENTRY", f"{clawback} clawback line {fmt_usd(-line['amount_cents'])}, corrects {line['id']} "
                               f"({decision['id']})")
            self.wp.set_line_status(self.principal, line["id"], "clawed_back")
            self.emit("ENTRY", f"{line['id']} -> clawed_back")
        return []
