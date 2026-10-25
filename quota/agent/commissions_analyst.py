"""seat:commissions_analyst: commission runs, disputes, plan exceptions, payroll files, clawback lines, and
the checks that hold or flag commission when the facts are wrong.

Routes: job.due commission_run / payroll_file / churn_review / approver timers, message.posted to this seat (a
dispute, an escalation, an exception grant, a chat approval, or something to clarify), approval.decided on plan
exceptions, and the world's facts: deal.updated (backdating, voids), payment.overdue, rep.terminated,
rep.territory_changed, plan.amended.
"""

import json
import re

from quota import clock as clk
from quota import config
from quota.agent.extract import extract_claim, has_authority
from quota.agent.seat import Seat
from quota.engine import calculator, checks, disputes, ranking, routing
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
AMBIGUOUS_PREFIX = "I read that as no answer yet"
EVIDENCE_REF = re.compile(r"\b((?:contracts|deals|esign_events):[A-Za-z0-9-]+(?::v\d+)?)")


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


FACT_ROUTES = {"deal.updated": "deal_updated", "payment.overdue": "overdue", "rep.terminated": "terminated",
               "payment.received": "payment",
               "rep.territory_changed": "territory", "plan.amended": "plan_amended", "rep.leave": "leave"}


class CommissionsAnalyst(Seat):
    principal = ANALYST
    handles = frozenset({"message.posted", "approval.decided", "job.due", *FACT_ROUTES})

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
        return FACT_ROUTES.get(ev.type)

    # ---------- shared writes ----------

    def send(self, to, text, kind="MESSAGE") -> str:
        message_id = self.wp.add_message(CHANNEL, self.principal, to, text, None)
        self.emit(kind, f"{message_id} to {to}:\n{text}" if kind == "REPLY" else f"{message_id} to {to}: {text}")
        return message_id

    def flag(self, kind, subject_id, rows, output, outcome, text, notify=()) -> str:
        """Record a check's decision (held or escalated) and tell the people who must act on it."""
        decision_id = self.wp.record_decision(self.principal, kind, subject_id, queries.input_refs(rows), {}, [],
                                              output, outcome, text)
        self.emit("DECISION", f"{decision_id} {kind} {subject_id}: {text}")
        for to, message in notify:
            self.send(to, message)
        return decision_id

    def record_calc(self, rows, deal_id, rep_id, calc, corrects_id=None, key=None) -> tuple[str, str]:
        """A commission_calc decision and its draft line (correcting an earlier line if given). `key` is the
        booking key when it is not the deal itself (an amendment, "<deal>+v<n>")."""
        text = calc_text(calc)
        # Two sources can carry the same alternative (contract + e-sign countersignature); print it once.
        alternatives = list(dict.fromkeys(alt_text(d) for res in calc["resolved"].values() for d in res.disagreements))
        explanation = text + ("; disagreements: " + "; ".join(alternatives) if alternatives else "")
        # SPEC "When to ask": a disagreement is material above $500 or 2% of the payout. The source ranking is the
        # written rule that settles it, so nobody is asked; the record says it was material.
        effects = [abs(d["effect_cents"]) for res in calc["resolved"].values() for d in res.disagreements
                   if d["effect_cents"] is not None]
        threshold = min(config.MATERIALITY_CENTS,
                        abs(calc["result"].amount_cents) * config.MATERIALITY_BPS_OF_PAYOUT // 10000)
        if effects and max(effects) > threshold:
            explanation += (f"; material ({fmt_usd(max(effects))} > {fmt_usd(threshold)}), settled by the source "
                            f"ranking")
        subject = f"{key or deal_id}/{rep_id}"
        decision_id = self.wp.record_decision(
            self.principal, "commission_calc", subject, queries.input_refs(rows), calc["resolutions"],
            calc["precedent_ids"], calc["output"], "drafted", explanation)
        self.emit("DECISION", f"{decision_id} commission_calc {subject}: {text}"
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
        ctx.data["lined"] = {d["subject_id"] for kind in ("commission_calc", "opening_balance")
                             for d in self.decisions(kind=kind)}
        ctx.data["split_holds"] = {d["subject_id"] for d in self.decisions(kind="splits_check")}
        ctx.data["territory_holds"] = {d["subject_id"]: json.loads(d["output_json"])["args"]["target_rep"]
                                       for d in self.decisions(kind="territory_check")}
        ctx.data["no_rule_holds"] = {d["subject_id"] for d in self.decisions(kind="no_rule_check")}

    def reconcile_commission_run(self, ctx):
        """Bookings (deals, and amendments that raised ACV) in the run's window with no commission yet. A deal
        whose credit does not add up to 100% is held for a split decision; a deal left behind by a half-done
        territory move waits until its CRM owner is the move's target."""
        period = ctx.event.payload["period"]
        since = ctx.event.payload.get("since") or period
        todo, bad_splits, no_rule = [], {}, {}
        for rep, rows in ctx.data["rows"].items():
            deals = calculator.latest_by_id(rows["deals"])
            for key, booking in calculator.rep_bookings(rows, rep, self.now).items():
                if f"{key}/{rep}" in ctx.data["lined"] or not since <= clk.period_of(booking.booking_ts) <= period:
                    continue
                deal = deals[key.split("+")[0]]
                unknown = checks.check_no_rule(rows, deal["id"])
                if unknown["result"]["no_rule"]:
                    if deal["id"] not in ctx.data["no_rule_holds"]:
                        no_rule[deal["id"]] = (deal, rows, unknown)
                    continue
                if ranking.splits_total(ranking.resolve_splits(deal, [])) != 10000:
                    if deal["id"] not in ctx.data["split_holds"]:
                        bad_splits[deal["id"]] = deal
                    continue
                target = ctx.data["territory_holds"].get(deal["id"])
                if target is not None and deal["owner_id"] != target:
                    continue
                todo.append((rep, key))
        ctx.data["bad_splits"], ctx.data["no_rule"] = bad_splits, no_rule
        return todo

    def calculate_commission_run(self, ctx):
        return [(rep, key, calculator.commission_decision(ctx.data["rows"][rep], key, rep, self.now))
                for rep, key in ctx.res]

    def write_commission_run(self, ctx):
        decision_ids = []
        owner = routing.owner("splits")
        for deal_id, deal in sorted(ctx.data["bad_splits"].items()):
            rows = {"deals": [deal]}
            output = checks.check_splits(rows, deal_id)
            shares = " + ".join(f"{s['rep']} {s['bps'] / 100:g}%" for s in output["result"]["splits"])
            decision_ids.append(self.flag(
                "splits_check", deal_id, rows, output, "held",
                f"credit on {deal_id} adds to {output['result']['total_bps'] / 100:g}% ({shares}); no lines until "
                f"{owner} confirms the split",
                [(owner, f"{deal['account']} ({deal_id}) credits {shares}, which is "
                         f"{output['result']['total_bps'] / 100:g}%. Both lines are held. What is the approved split?")]))
        for deal_id, (deal, rows, output) in sorted(ctx.data["no_rule"].items()):
            unknown = ", ".join(output["result"]["unknown"])
            decision_ids.append(self.flag(
                "no_rule_check", deal_id, {"deals": [deal], "contracts": [c for c in rows["contracts"]
                                                                          if c["deal_id"] == deal_id]},
                output, "held", f"NO_RULE: {deal_id}'s contract has {unknown}, which no plan rule covers; no line "
                                f"until {routing.owner('plan_config')} says how to treat it",
                [(routing.owner("plan_config"), f"NO_RULE on {deal['account']} ({deal_id}): the signed contract has "
                                                f"{unknown}, and the plan has no rule for it. I have not guessed: no "
                                                f"commission line until you tell me how it should count. How should "
                                                f"it count?")]))
        for rep, key, calc in ctx.calc:
            decision_ids.append(self.record_calc(ctx.data["rows"][rep], key.split("+")[0], rep, calc, key=key)[0])
        # ELASTIC_TASKS E5: at booking, look for a promise the contract does not make (once per deal booked).
        for deal_id in sorted({key.split("+")[0] for _, key, _ in ctx.calc}):
            gated = self.commitment_gate(deal_id)
            if gated:
                decision_ids.append(gated)
        return decision_ids

    # ---------- message.posted ----------

    def gather_message(self, ctx):
        ctx.data["message"] = self.get("messages", ctx.event.payload["message_id"])
        ctx.data["deals"] = self.deal_names()

    def extract_message(self, ctx):
        message = ctx.data["message"]
        claim = extract_claim(self.extractor, message["text"], message["author"], self.now[:4], ctx.data["deals"])
        if claim.kind == "other" and claim.problem is None:
            # Thanks or chit-chat needs nothing. But a reply that is neither yes nor no to something Quota asked
            # this person ("sounds good, let's discuss") is no answer: one clarifying question.
            ctx.route = "ambiguous" if self.awaiting_answer(message) else "noted"
        elif claim.kind == "unavailable":
            ctx.route = "unavailable"
        else:
            ctx.route = CLAIM_ROUTES.get(claim.kind, "clarify")
        return claim

    def _last_sent_to(self, who) -> dict | None:
        rows = queries.fetch_all(self.ro, "SELECT * FROM messages WHERE author = ? AND to_principal = ? AND known_at <= ? "
                                          "ORDER BY known_at, id", (self.principal, who, self.now))
        return rows[-1] if rows else None

    def awaiting_answer(self, message) -> bool:
        """Quota is waiting on this person: an approval addressed to them, or Quota's last message to them asked."""
        author = message["author"]
        if any(a["approver"] == author for a in self.pending_approvals()):
            return True
        last = self._last_sent_to(author)
        return last is not None and last["text"].rstrip().endswith("?")

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
            # Evidence is what the message cites by record id (e.g. contracts:acme:v2); code reads it, not the model.
            cited = [r.lower() for r in EVIDENCE_REF.findall(message["text"])]
            engine_claim.update(deal=line["deal_id"], field=claim.field, asserted=asserted, evidence_refs=cited)
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
                    f"Held {len(held)}x ({', '.join(held)}). Do you grant it?")
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
        declined = self.declined_proposal(deal["id"], claim.field, value)
        if declined is not None:
            rows = {"deals": queries.fetch_all(self.ro, "SELECT * FROM deals WHERE id = ? AND known_at <= ?",
                                               (deal["id"], self.now)),
                    "contracts": queries.fetch_all(self.ro, "SELECT * FROM contracts WHERE deal_id = ? AND known_at <= ?",
                                                   (deal["id"], self.now)),
                    "esign_events": queries.fetch_all(self.ro, "SELECT * FROM esign_events WHERE deal_id = ? "
                                                               "AND known_at <= ?", (deal["id"], self.now)),
                    "messages": [message]}
            check = checks.check_resubmission(rows, deal["id"], claim.field, value, declined["approval_id"],
                                              declined["known_at"])
            if not check["result"]["resubmit"]:
                ctx.route = "declined_before"
                ctx.rows, ctx.data["declined"], ctx.data["check"] = rows, declined, check
                return None
        ctx.rows = queries.commission_rows(self.ro, deal["owner_id"], self.now)
        return {"deal_id": deal["id"], "rep_id": deal["owner_id"], "field": claim.field, "value": value}

    def declined_proposal(self, deal_id, field, value) -> dict | None:
        """The latest rejection of a plan exception proposing the same deal, field and value."""
        found = None
        for d in queries.fetch_all(self.ro, """
                SELECT d.*, a.subject_id FROM approval_decisions d JOIN approvals a ON a.id = d.approval_id
                WHERE a.kind = 'plan_exception' AND d.decision = 'rejected' AND d.known_at <= ?
                ORDER BY d.known_at, d.rowid""", (self.now,)):
            root = self.approval_root(d["approval_id"])
            source = self.get("messages", root["subject_id"])
            if source is None:
                continue
            claim = extract_claim(self.extractor, source["text"], source["author"], self.now[:4], self.deal_names())
            proposed = claim.amount_cents if claim.field == "base" else claim.asserted_value
            if (claim.kind, claim.deal_ref, claim.field, proposed) == ("exception_grant", deal_id, field, value):
                found = d
        return found

    def write_declined_before(self, ctx):
        message, declined, check = ctx.data["message"], ctx.data["declined"], ctx.data["check"]
        who = config.display_name(declined["decided_by"])
        args = check["args"]
        text = (f"not resubmitted: {declined['approval_id']} proposing {args['field']} = {args['value']} for "
                f"{args['deal_id']} was declined by {declined['decided_by']} at {declined['known_at']} and nothing "
                f"new is on record since")
        decision_id = self.wp.record_decision(self.principal, "resubmission_check", message["id"],
                                              queries.input_refs(ctx.rows), {}, [], check, "refused", text)
        self.emit("DECISION", f"{decision_id} resubmission_check {message['id']}: {text}")
        self.send(message["author"], f"{who} declined that exact exception ({declined['approval_id']}): "
                                     f"\"{declined['reason']}\". I resubmit only with new evidence, such as a new "
                                     f"contract, CRM or e-signature record for {args['deal_id']} ({decision_id}).",
                  "REPLY")
        return [decision_id]

    def calculate_exception(self, ctx):
        r = ctx.res
        override = ({"booking_ts": disputes.asserted_booking_ts(r["value"])} if r["field"] == "booking_date"
                    else {"base_cents": r["value"]})
        current = calculator.evaluate(ctx.rows, r["deal_id"], r["rep_id"], self.now)
        granted = calculator.evaluate(ctx.rows, r["deal_id"], r["rep_id"], self.now, **override)
        return {"current": current, "granted": granted, "delta": granted.amount_cents - current.amount_cents}

    def decide_exception(self, ctx):
        """The plan owner approves, unless they would benefit (they are credited on the deal, or manage someone
        who is): then it goes straight to the Controller, flagged (escalation contract #5)."""
        owner = routing.owner("plan_exception")
        deal = self.get("deals", ctx.res["deal_id"])
        credited = {s["rep"] for s in json.loads(deal["splits_json"])}
        managers = {(self.get("people", r) or {}).get("manager_id") for r in credited}
        if owner in credited | managers:
            return {"approver": routing.owner("material_flag"), "conflict": owner}
        return {"approver": owner}

    def write_exception(self, ctx):
        message, r, c, approver = ctx.data["message"], ctx.res, ctx.calc, ctx.decision["approver"]
        if ctx.decision.get("conflict"):
            who = ctx.decision["conflict"]
            self.send(approver, f"Integrity flag: {config.display_name(who)} would normally approve the exception in "
                                f"{message['id']} ({r['deal_id']} {r['field']} = {r['value']}, "
                                f"{fmt_usd(abs(c['delta']))}) but benefits from it (credited on the deal or managing "
                                f"a rep who is), so it comes to you instead.")
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

    def conflict_of(self, who, deal_id) -> str | None:
        """Someone credited on the deal, or the manager of a rep who is, benefits from an exception on it."""
        deal = self.get("deals", deal_id) if deal_id else None
        if deal is None:
            return None
        for share in json.loads(deal["splits_json"]):
            rep = self.get("people", share["rep"])
            if share["rep"] == who:
                return f"{config.display_name(who)} is credited on {deal['account']}"
            if rep and rep["manager_id"] == who:
                return (f"{config.display_name(who)} manages {config.display_name(share['rep'])}, who is credited on "
                        f"{deal['account']}")
        return None

    def write_authority(self, ctx):
        claim, message, owner = ctx.facts, ctx.data["message"], ctx.decision["owner"]
        author = config.display_name(message["author"])
        conflict = self.conflict_of(message["author"], claim.deal_ref)
        text = (f"{claim.kind} from {message['author']} routed to {owner}: needs approve:plan_exception, which "
                f"{message['author']} does not hold; nothing is paid on it")
        if conflict:
            text += f"; conflict of interest: {conflict}"
        rows = {"messages": [message]}
        decision_id = self.wp.record_decision(
            self.principal, "authority_check", message["id"], queries.input_refs(rows), {}, [],
            {"fn": "has_authority", "args": {"claim": claim.to_dict(), "author": message["author"]},
             "result": {"authorized": False, "routed_to": owner}}, "escalated", text)
        self.emit("DECISION", f"{decision_id} authority_check {message['id']}: {text}")
        why = f" Conflict of interest: {conflict}, so it could not be theirs to grant in any case." if conflict else ""
        self.send(owner, f"{author} wrote \"{claim.quote}\" ({message['id']}). Only someone who can approve plan "
                         f"exceptions can grant that, so nothing is paid on it.{why} Do you want to grant it?")
        self.send(message["author"], f"I've asked {config.display_name(owner)} ({config.title(owner)}); this needs "
                                     f"their approval before anything is paid."
                                     + (f" You can't grant it yourself: {conflict}." if conflict else ""), "REPLY")
        return [decision_id]

    # someone asks the seat to approve or reject in chat: the write path is the judge, so try and report

    def reconcile_chat_approval(self, ctx):
        ref = (ctx.facts.line_ref or "").upper()
        if not ref.startswith("A-"):
            # "Approve your own payout", "pay Taylor anyway": the payout line the message is about. The seat tries
            # it and the write path decides, so the refusal is on the record (SPEC judge phrases).
            line = self.payout_named(ctx.data["message"]) if ctx.facts.kind == "approval" else None
            ctx.route = "approve_line" if line else "clarify"
            return line
        approval = self.get("approvals", ref)
        if approval is None:
            ctx.route = "no_such_approval"
            ctx.data["ref"] = ref
        return approval

    def payout_named(self, message) -> dict | None:
        """The newest live payout line of a rep the message names (first name or id), else of its author."""
        text = message["text"].lower()
        named = [p for p, spec in config.PRINCIPALS.items() if spec.get("kind") == "human"
                 and (re.search(rf"\b{re.escape(spec['name'].split()[0].lower())}\b", text) or p in text)]
        for rep in named + [message["author"]]:
            live = [l for l in self.lines(rep_id=rep) if l["amount_cents"] > 0
                    and self.line_status(l["id"]) in ("draft", "approved")]
            if live:
                return live[-1]
        return None

    def hold_on(self, line) -> dict | None:
        """The standing hold decision keeping this line out of payroll, if any."""
        for decision in reversed(self.decisions(kind="termination_review")):
            if line["id"] in json.loads(decision["output_json"])["result"]["held"]:
                return decision
        for decision in reversed(self.decisions(kind="collection_hold", subject_id=line["deal_id"])):
            if not queries.collected(self.ro, line["deal_id"], self.now):
                return decision
        for decision in reversed(self.decisions(kind="splits_check", subject_id=line["deal_id"])):
            if self.splits_invalid(line["deal_id"]):
                return decision
        return None

    def write_approve_line(self, ctx):
        line, message = ctx.res, ctx.data["message"]
        approver, release = routing.owner("journal_entry"), routing.owner("payroll_release")
        try:
            self.wp.set_line_status(self.principal, line["id"], "approved")
        except Refused as refused:
            self.emit("REFUSED", f"{refused.refusal_id} set_line_status {line['id']} -> approved: "
                                 f"{', '.join(refused.reason_codes)}")
            hold = self.hold_on(line)
            exception_owner = routing.owner("plan_exception")
            held = (f" {line['id']} is also held: {hold['explanation']} ({hold['id']}). Paying it anyway is a plan "
                    f"exception for {config.display_name(exception_owner)} (up to "
                    f"{fmt_usd(routing.limit(exception_owner, 'plan_exception'))}) and "
                    f"{config.display_name(approver)} above that.") if hold else ""
            self.send(message["author"], f"I can't approve {line['id']} ({config.display_name(line['rep_id'])}, "
                                         f"{fmt_usd(line['amount_cents'])}): {', '.join(refused.reason_codes)} "
                                         f"({refused.refusal_id}). I draft payouts; {config.display_name(approver)} "
                                         f"({config.title(approver)}) approves them and "
                                         f"{config.display_name(release)} releases payroll.{held}", "REPLY")
        return []

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
            sender = (f" This came from {config.display_name(message['author'])}'s seat, so I read it as "
                      f"{config.display_name(message['author'])}, whatever it says: approvals come only from "
                      f"{config.display_name(approver)}'s own seat." if message["author"] != approver else "")
            self.send(message["author"], f"I can't decide {approval['id']}: {', '.join(refused.reason_codes)} "
                                         f"({refused.refusal_id}). It is waiting on {config.display_name(approver)} "
                                         f"({config.title(approver)}), who decides it in the approval queue.{sender}",
                      "REPLY")
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

    def write_ambiguous(self, ctx):
        author = ctx.data["message"]["author"]
        pending = [a for a in self.pending_approvals() if a["approver"] == author]
        if pending:
            a = pending[-1]
            item = a["id"]
            ask = (f"is that a yes on {a['id']} ({a['kind'].replace('_', ' ')} on {a['subject_id']}, "
                   f"{fmt_usd(a['amount_cents'])})? Reply \"approve {a['id']}\" or \"reject {a['id']}\" with a "
                   f"reason; until then it stays open, and silence never approves.")
        else:
            item = "do you grant it?"
            ask = "do you grant it? Say what you approve (the deal and the value), or say no. Until then nothing changes."
        last = self._last_sent_to(author)
        if last is not None and last["text"].startswith(AMBIGUOUS_PREFIX) and item in last["text"]:
            return []                    # one clarifying question per open item, never the same one twice
        self.send(author, f"{AMBIGUOUS_PREFIX}: {ask}", "REPLY")
        return []

    def write_unavailable(self, ctx):
        """An approver says they are away: what waits on them goes to their backup now, not after 2 days."""
        message = ctx.data["message"]
        author, until = message["author"], ctx.facts.asserted_value
        backup = routing.ESCALATION.get(author)
        moved = []
        for approval in [a for a in self.pending_approvals() if a["approver"] == author]:
            if backup is None or any(a["approver"] == backup and a["subject_id"] == approval["subject_id"]
                                     and a["kind"] == approval["kind"] for a in self.pending_approvals()):
                continue
            routed = self.wp.request_approval(self.principal, approval["kind"], approval["subject_id"],
                                              approval["amount_cents"], approver=backup)
            self.emit("APPROVAL", f"{routed} {approval['kind']} {approval['subject_id']} "
                                  f"{fmt_usd(approval['amount_cents'])} -> {backup}: {author} is away"
                                  + (f" until {until}" if until else "") + f" ({approval['id']})")
            moved.append((approval["id"], routed))
        away = config.display_name(author) + (f" is away until {until}" if until else " is away")
        if moved:
            self.send(backup, f"{away}, so these now wait on you: "
                      + ", ".join(f"{routed} (was {original})" for original, routed in moved) + ".")
        target = f"{config.display_name(backup)} ({config.title(backup)})" if backup else "nobody (no backup on record)"
        self.send(author, f"Noted. While you are away, approvals for you go to {target}"
                          + (f"; moved now: {', '.join(r for _, r in moved)}" if moved else "")
                          + ". Silence never approves.", "REPLY")
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
                decided = ctx.event.payload["approval_id"]
                root = ctx.data["root"]["id"]
                ids = decided if decided == root else f"{decided}, routed from {root}"
                self.send(line["rep_id"], f"{config.display_name(ctx.data['decided_by'])} declined the exception "
                                          f"({ids}): \"{ctx.data['reason']}\". {line['id']} stays at "
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
        decision_ids = [self.record_calc(rows, deal["id"], rep, calc, line["id"] if line else None)[0]]
        return decision_ids + self.propose_plan_change(claim.field) + self.flag_repeated_exceptions(rep)

    def flag_repeated_exceptions(self, rep) -> list[str]:
        """Escalation contract #5: the same rep receiving repeated exceptions goes to the Controller, once per
        new exception past the threshold."""
        precedents = queries.fetch_all(self.ro, "SELECT * FROM precedents WHERE known_at <= ? ORDER BY id", (self.now,))
        deals = [d for d in queries.latest_rows(self.ro, "deals", self.now)
                 if any(queries.loads(p["scope_json"]).get("deal_id") == d["id"] for p in precedents)]
        rows = {"precedents": [p for p in precedents if queries.loads(p["scope_json"]).get("deal_id")], "deals": deals}
        output = checks.check_rep_exceptions(rows, rep, config.PROPOSE_PLAN_CHANGE_AFTER)
        found = output["result"]["precedents"]
        seen = [set(json.loads(d["output_json"])["result"]["precedents"])
                for d in self.decisions(kind="integrity_check", subject_id=rep)]
        if not output["result"]["flag"] or any(set(found) <= before for before in seen):
            return []
        controller = routing.owner("material_flag")
        text = (f"integrity signal: {len(found)} exceptions granted for {rep} ({', '.join(found)}); for "
                f"{controller} to review")
        return [self.flag("integrity_check", rep, rows, output, "escalated", text,
                          [(controller, f"Integrity flag: {config.display_name(rep)} has had {len(found)} plan "
                                        f"exceptions granted ({', '.join(found)}). Each was approved within "
                                        f"authority; repeated exceptions for one rep are yours to review.")])]

    def propose_plan_change(self, field) -> list[str]:
        """The same kind of exception approved on several deals becomes a proposed plan change for Sales Ops to
        sign (SPEC "How does it learn?"). Each case still needs its own exception until a signed amendment."""
        precedents = [p for p in queries.fetch_all(self.ro, "SELECT * FROM precedents WHERE known_at <= ? ORDER BY id",
                                                   (self.now,)) if queries.loads(p["scope_json"]).get("field") == field]
        deal_ids = sorted({queries.loads(p["scope_json"])["deal_id"] for p in precedents})
        rows = {"precedents": precedents, "contracts": [c for d in deal_ids for c in queries.fetch_all(
            self.ro, "SELECT * FROM contracts WHERE deal_id = ? AND known_at <= ?", (d, self.now))]}
        output = checks.check_precedent_pattern(rows, field, config.PROPOSE_PLAN_CHANGE_AFTER)
        result = output["result"]
        proposed_before = [set(json.loads(d["output_json"])["result"]["deals"])
                           for d in self.decisions(kind="plan_change_proposal")
                           if json.loads(d["output_json"])["args"]["field"] == field]
        if not result["propose"] or any(set(result["deals"]) <= before for before in proposed_before):
            return []
        owner = routing.owner("plan_config")
        text = (f"{len(result['deals'])} approved {field} exceptions ({', '.join(result['precedents'])} on "
                f"{', '.join(result['deals'])}): proposed plan change for {owner} to sign: {result['proposal']}")
        return [self.flag("plan_change_proposal", field, rows, output, "escalated", text,
                          [(owner, f"Proposed plan change: {result['proposal']}. Why: the same exception was approved "
                                   f"{len(result['deals'])} times ({', '.join(result['precedents'])} on "
                                   f"{', '.join(result['deals'])}). Nothing changes until a signed plan amendment; "
                                   f"until then each case still needs its own exception. Do you want to draft it?")])]

    # ---------- job.due payroll_file ----------

    def payroll_held(self) -> set[str]:
        """Lines a hold decision has taken out of payroll: deals on collection hold (until the customer pays) and
        lines the signed plan does not pay after a termination."""
        held = set()
        for deal_id in self.commitment_holds():
            held |= {l["id"] for l in self.lines(deal_id=deal_id)}
        for decision in self.decisions(kind="collection_hold"):
            if not queries.collected(self.ro, decision["subject_id"], self.now):
                held |= {l["id"] for l in self.lines(deal_id=decision["subject_id"])}
        for decision in self.decisions(kind="termination_review"):
            held |= set(json.loads(decision["output_json"])["result"]["held"])
        for deal_id in {d["subject_id"] for d in self.decisions(kind="splits_check")}:
            if self.splits_invalid(deal_id):
                held |= {l["id"] for l in self.lines(deal_id=deal_id)}
        return held

    def reconcile_payroll_file(self, ctx):
        """Approved positive lines on collected deals (the plan pays on collection), plus recoveries netted
        against them. First, escalation contract #8: if the ledger fails its own checks, nothing is paid."""
        failures = self.self_check()
        if failures:
            ctx.route, ctx.data["failures"] = "self_check_failed", failures
            return []
        held = self.payroll_held() | self.in_open_payroll()
        payable = [l for l in self.lines() if l["amount_cents"] > 0 and l["id"] not in held
                   and self.line_status(l["id"]) == "approved" and queries.collected(self.ro, l["deal_id"], self.now)]
        # SPEC worked example: a rep's approved recovery (a clawback in 1290) is netted against their new
        # commission in the next payroll, oldest first, as far as that commission covers whole recoveries; the
        # rest stays in 1290 and Payroll is told.
        netted, left = [], []
        for rep in sorted({l["rep_id"] for l in self.lines() if l["amount_cents"] < 0}):
            room = sum(l["amount_cents"] for l in payable if l["rep_id"] == rep)
            for recovery in [l for l in self.lines(rep_id=rep) if l["amount_cents"] < 0
                             and self.line_status(l["id"]) == "approved"]:
                if room > 0 and -recovery["amount_cents"] <= room:
                    netted.append(recovery)
                    room += recovery["amount_cents"]
                else:
                    left.append(recovery)
        ctx.data["left_in_1290"] = left if payable else []
        return [l["id"] for l in payable + netted]

    def self_check(self) -> list[str]:
        """Every invariant (INV1 to INV7) and the hash chain (INV8), before any money is paid."""
        from quota.ledger import hashing, invariants
        failures = [f"{name}: {v.subject} {v.detail}" for name, found in invariants.check_all(self.ro).items()
                    for v in found]
        ok, bad_seq = hashing.verify_chain(self.ro)
        if not ok:
            failures.append(f"INV8: hash chain breaks at seq {bad_seq}")
        return failures

    def write_self_check_failed(self, ctx):
        failures = ctx.data["failures"]
        period = ctx.event.payload["period"]
        self.emit("HOLD", f"payroll {period} not created: the ledger failed its own checks ({len(failures)}): "
                          + "; ".join(failures[:5]))
        self.send(routing.owner("material_flag"), f"Payroll {period} is held: my own checks failed, so nothing is "
                                                  f"paid until this is fixed: " + "; ".join(failures[:10]) + ".")
        return []

    def write_payroll_file(self, ctx):
        # ELASTIC_TASKS E5: the last look before money moves. A promise found now holds the deal's lines (and any
        # recovery netted against them stays in 1290 for the next run).
        gated = [d for d in (self.commitment_gate(deal_id) for deal_id in
                             sorted({self.get("commission_lines", lid)["deal_id"] for lid in ctx.res
                                     if self.get("commission_lines", lid)["amount_cents"] > 0})) if d]
        holds = self.commitment_holds()
        if holds:
            reps = {l["rep_id"] for lid in ctx.res for l in [self.get("commission_lines", lid)] if l["deal_id"] in holds}
            ctx.res = [lid for lid in ctx.res for l in [self.get("commission_lines", lid)]
                       if l["deal_id"] not in holds and not (l["amount_cents"] < 0 and l["rep_id"] in reps)]
        if not ctx.res:
            return gated
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
        recovered = [lid for lid in ctx.res if self.get("commission_lines", lid)["amount_cents"] < 0]
        left = ctx.data.get("left_in_1290", [])
        if recovered or left:
            note = []
            if recovered:
                note.append("netted against new commission in this file: " + ", ".join(
                    f"{l['id']} {config.display_name(l['rep_id'])} {fmt_usd(-l['amount_cents'])}"
                    for l in (self.get("commission_lines", lid) for lid in recovered)))
            if left:
                note.append("still due in 1290 (no commission to net it against yet): " + ", ".join(
                    f"{l['id']} {config.display_name(l['rep_id'])} {fmt_usd(-l['amount_cents'])}" for l in left))
            self.send(routing.owner("payroll_release"), f"{file_id} recoveries: " + "; ".join(note) + ".")
        return gated

    # ---------- ELASTIC_TASKS E5: the off-contract promise gate ----------

    def commitment_holds(self) -> dict[str, dict]:
        """deal -> the commitment_check holding it: the deal's latest check held it, and nobody has since said
        the promise is not binding (its commitment_review approved) nor has a re-check found it covered."""
        latest = {}
        for d in self.decisions(kind="commitment_check"):
            latest[d["subject_id"]] = d
        out = {}
        for deal_id, d in latest.items():
            if d["outcome"] != "held":
                continue
            review = queries.fetch_one(self.ro, "SELECT id FROM approvals WHERE kind = 'commitment_review' AND "
                                                "subject_id = ? ORDER BY id", (d["id"],))
            if review and queries.satisfied_approval(self.ro, "commitment_review", d["id"], self.now):
                continue
            out[deal_id] = d
        return out

    def commitment_gate(self, deal_id) -> str | None:
        """Search the deal's messages since 30 days before booking for each promise intent, read the hits, and
        hold the deal's commission on any promise the signed contract does not make. Returns the decision id
        when it recorded one (a new hold, or a release when a re-check finds the contract now covers it)."""
        evidence = getattr(self.world, "evidence", None)
        if evidence is None:
            return None
        from quota.agent import commitments
        from quota.search.intents import INTENTS, merge
        deal = self.get("deals", deal_id)
        found = queries.fetch_one(self.ro, "SELECT id FROM contracts WHERE deal_id = ? AND known_at <= ? "
                                           "ORDER BY version DESC LIMIT 1", (deal_id, self.now))
        contract = self.get("contracts", found["id"]) if found else None
        if deal is None or contract is None or not contract["countersigned_at"]:
            return None
        after = clk.add_days(contract["countersigned_at"], -30)
        if hasattr(evidence, "account_messages"):        # offline: read the account's whole mailbox
            hits = evidence.account_messages(deal["account"], after=after, before=self.now)
        else:                                             # Elastic: hybrid + rerank, every intent, top 5 each
            hits = merge({intent: evidence.search(text, "hybrid", k=5, account=deal["account"], after=after,
                                                  before=self.now) for intent, text in INTENTS.items()})
        findings = [f for f in (commitments.classify(h, contract, self.world.commitment_llm) for h in hits) if f]
        previous = self.decisions(kind="commitment_check", subject_id=deal_id)
        last = previous[-1] if previous else None
        rows = {"deals": [deal], "contracts": [contract]}
        if not findings:
            return None
        output = checks.check_commitments(rows, deal_id, findings)
        held = output["result"]["held"]
        same = last is not None and sorted(json.loads(last["output_json"])["result"]["open"]) == \
            sorted(output["result"]["open"]) and last["outcome"] == ("held" if held else "posted")
        if same or (not held and (last is None or last["outcome"] != "held")):
            return None                    # nothing new: the hold stands, or there was never one to release
        refs = queries.input_refs(rows) + [
            {"table": "quota-comms", "id": f["doc_id"], "version": 1, "sha256": f["body_sha256"],
             "rank": f["retrieval"]["rank"], "mode": f["retrieval"]["mode"]} for f in findings]
        lines = [l for l in self.lines(deal_id=deal_id) if l["amount_cents"] > 0
                 and self.line_status(l["id"]) not in ("superseded", "clawed_back", "paid")]
        amount = sum(l["amount_cents"] for l in lines)
        if not held:
            text = f"the signed contract for {deal['account']} now gives what was promised; commission released"
            decision_id = self.wp.record_decision(self.principal, "commitment_check", deal_id, refs, {}, [], output,
                                                  "posted", text)
            self.emit("DECISION", f"{decision_id} commitment_check {deal_id}: {text}")
            return decision_id
        first = next(f for f in findings if f["doc_id"] in output["result"]["open"])
        who = config.display_name(first["promised_by"]) if first["promised_by"] in config.PRINCIPALS else first["promised_by"]
        text = (f"OFF_CONTRACT_COMMITMENT: {who} wrote \"{first['quote']}\" ({first['doc_id']}, {first['sent_at']}, "
                f"found by {first['retrieval']['mode']} #{first['retrieval']['rank']}); the signed contract has no "
                f"such term ({first['type']}). {fmt_usd(amount)} held" + (f"; {len(findings) - 1} more message(s)"
                                                                         if len(findings) > 1 else ""))
        decision_id = self.wp.record_decision(self.principal, "commitment_check", deal_id, refs, {}, [], output,
                                              "held", text)
        self.emit("DECISION", f"{decision_id} commitment_check {deal_id}: {text}")
        controller, vp = routing.owner("commitment_review"), routing.owner("splits")
        approval_id = self.wp.request_approval(self.principal, "commitment_review", decision_id, amount,
                                               approver=controller)
        self.emit("APPROVAL", f"{approval_id} commitment_review {decision_id} {fmt_usd(amount)} -> {controller}")
        self.send(controller, f"{deal['account']}: the customer may hold terms outside the contract; the revenue "
                              f"schedule may need review. {who} wrote \"{first['quote']}\" ({first['doc_id']}). "
                              f"{fmt_usd(amount)} of commission is held ({decision_id}). Approve {approval_id} if "
                              f"it is not binding; if it is, amend the contract and I will release it on the next "
                              f"check.")
        self.send(vp, f"Rep conduct: {who} promised {deal['account']} terms that are not in the signed contract "
                      f"(\"{first['quote']}\", {first['doc_id']}). The commission is held pending the Controller "
                      f"({decision_id}).")
        return decision_id

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
        if approval["id"] not in {a["id"] for a in self.pending_approvals()}:
            return []                    # decided, or settled by a backup approver in the meantime
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

    # ---------- the rep's side of a recovery (churn clawback, voided deal) ----------

    def pending_recoveries(self) -> list[tuple[dict, dict]]:
        """Recoverable clawback / void decisions whose line has no negative correcting line yet (a quarter
        true-up's line corrects the same line but is not its recovery)."""
        reversals = {d["id"] for kind in ("clawback", "void_reversal") for d in self.decisions(kind=kind)}
        corrected = {l["corrects_id"] for l in self.lines() if l["amount_cents"] < 0 and l["decision_id"] in reversals}
        return [(d, self.get("commission_lines", d["subject_id"]))
                for kind in ("clawback", "void_reversal") for d in self.decisions(kind=kind)
                if json.loads(d["output_json"])["args"]["recoverable"] and d["subject_id"] not in corrected]

    def true_ups_of(self, line_id) -> list[dict]:
        decided = {d["id"] for d in self.decisions(kind="true_up")}
        return [l for l in self.lines() if l["corrects_id"] == line_id and l["decision_id"] in decided]

    def write_recoveries(self, recoveries) -> list[str]:
        for decision, line in recoveries:
            # What the rep holds now: the line plus any quarter true-ups already taken back (R3).
            net = {k: line[k] + sum(t[k] for t in self.true_ups_of(line["id"]))
                   for k in ("credit_cents", "base_portion_cents", "accel_portion_cents", "amount_cents")}
            recovery = self.wp.add_commission_line(self.principal, {
                "rep_id": line["rep_id"], "deal_id": line["deal_id"], "booking_quarter": line["booking_quarter"],
                **{k: -v for k, v in net.items()}}, decision["id"], corrects_id=line["id"])
            self.emit("ENTRY", f"{recovery} {decision['kind'].replace('_', ' ')} line {fmt_usd(-net['amount_cents'])}"
                               f", corrects {line['id']} ({decision['id']})")
            self.wp.set_line_status(self.principal, line["id"], "clawed_back")
            self.emit("ENTRY", f"{line['id']} -> clawed_back")
        return self.write_true_up_lines()

    def write_true_up_lines(self) -> list[str]:
        """R3: the rep's side of a quarter recompute. A recovered decrease or an increase becomes a correcting line
        for the difference (an unrecoverable decrease has none: the rep keeps it, and the Controller was told)."""
        written = {l["decision_id"] for l in self.lines()}
        for decision in self.decisions(kind="true_up"):
            r = json.loads(decision["output_json"])["result"]
            if decision["id"] in written or r["delta"] is None or (r["delta"]["delta_cents"] < 0 and not r["entry"]):
                continue
            line = self.get("commission_lines", decision["subject_id"])
            held = [line, *self.true_ups_of(line["id"])]
            portions = {k: r["recomputed"][k] - sum(l[k] for l in held)
                        for k in ("base_portion_cents", "accel_portion_cents")}
            correcting = self.wp.add_commission_line(self.principal, {
                "rep_id": line["rep_id"], "deal_id": line["deal_id"], "booking_quarter": line["booking_quarter"],
                "credit_cents": 0, **portions, "amount_cents": r["delta"]["delta_cents"]},
                decision["id"], corrects_id=line["id"])
            self.emit("ENTRY", f"{correcting} true-up line {signed(r['delta']['delta_cents'])}, corrects {line['id']} "
                               f"({decision['id']})")
        return []

    def reconcile_clawback_lines(self, ctx):
        return self.pending_recoveries()

    def write_clawback_lines(self, ctx):
        return self.write_recoveries(ctx.res)

    # ---------- deal.updated: backdated close dates (E04), and the rep's side of a void (E15) ----------

    def gather_deal_updated(self, ctx):
        deal_id = ctx.event.payload["deal_id"]
        versions = queries.fetch_all(self.ro, "SELECT * FROM deals WHERE id = ? AND known_at <= ? ORDER BY version",
                                     (deal_id, self.now))
        current = versions[-1]
        ctx.rows = {"deals": versions[-2:]}
        ctx.data["locked_at"] = None
        close = current["crm_close_date"]
        if close and queries.period_locked(self.ro, clk.period_of(close), current["known_at"]):
            ctx.data["locked_at"] = queries.fetch_one(
                self.ro, "SELECT known_at FROM periods WHERE period = ? AND status = 'locked' AND known_at <= ? "
                         "ORDER BY known_at DESC, rowid DESC LIMIT 1", (clk.period_of(close), current["known_at"]))["known_at"]

    def calculate_deal_updated(self, ctx):
        return checks.check_backdating(ctx.rows, ctx.event.payload["deal_id"], ctx.data["locked_at"])

    def splits_invalid(self, deal_id) -> bool:
        deal = self.get("deals", deal_id)
        return deal is not None and ranking.splits_total(ranking.resolve_splits(deal, [])) != 10000

    def write_split_edit(self, ctx) -> list[str]:
        """E02 after booking: a CRM edit leaves credit not adding up to 100%. Hold the deal's lines out of payroll
        and ask the VP, proposing the last split that did add up."""
        deal_id = ctx.event.payload["deal_id"]
        versions = ctx.rows["deals"]
        if "splits" not in ctx.event.payload["fields"] or not self.splits_invalid(deal_id):
            return []
        output = checks.check_splits({"deals": versions[-1:]}, deal_id)
        shares = " + ".join(f"{s['rep']} {s['bps'] / 100:g}%" for s in output["result"]["splits"])
        previous = json.loads(versions[-2]["splits_json"]) if len(versions) > 1 else []
        proposal = (" + ".join(f"{s['rep']} {s['bps'] / 100:g}%" for s in previous)
                    if sum(s["bps"] for s in previous) == 10000 else None)
        held = [l["id"] for l in self.lines(deal_id=deal_id) if l["amount_cents"] > 0
                and self.line_status(l["id"]) in ("draft", "approved")]
        owner, deal = routing.owner("splits"), versions[-1]
        text = (f"credit on {deal_id} edited to {output['result']['total_bps'] / 100:g}% ({shares}) by {deal['source']}; "
                + (f"lines {', '.join(held)} held out of payroll" if held else "no lines")
                + f" until {owner} confirms the split")
        return [self.flag("splits_check", deal_id, {"deals": versions[-1:]}, output, "held", text,
                          [(owner, f"{deal['account']} ({deal_id}) now credits {shares}, which is "
                                   f"{output['result']['total_bps'] / 100:g}%. "
                                   + (f"Lines {', '.join(held)} are held. " if held else "")
                                   + (f"The last split that added up was {proposal}. " if proposal else "")
                                   + "What is the approved split?")])]

    def write_deal_updated(self, ctx):
        decision_ids = self.write_split_edit(ctx)
        result = ctx.calc["result"]
        if result["backdated"]:
            deal_id = ctx.event.payload["deal_id"]
            period = clk.period_of(result["close_date"])
            text = (f"BACKDATED: CRM close date moved {result['previous_close_date']} -> {result['close_date']} by "
                    f"{result['edited_by']} at {result['edited_at']}, after {period} was locked "
                    f"({ctx.data['locked_at']}); the booking on record stands")
            note = (f"{deal_id}: the CRM close date was changed to {result['close_date']} at {result['edited_at']}, "
                    f"after {period} was locked. I kept the booking on record and changed nothing. Please confirm.")
            decision_ids.append(self.flag("backdating_check", deal_id, ctx.rows, ctx.calc, "escalated", text,
                                          [(routing.owner("plan_exception"), note), (routing.owner("material_flag"), note)]))
        return decision_ids + self.write_recoveries(self.pending_recoveries())

    # ---------- payment.received: a short pay is AR's, not Quota's (R5) ----------

    def gather_payment(self, ctx):
        deal_id = ctx.event.payload["deal_id"]
        ctx.rows = {"billing": queries.fetch_all(self.ro, "SELECT * FROM billing WHERE deal_id = ? AND known_at <= ? "
                                                          "ORDER BY id", (deal_id, self.now))}

    def calculate_payment(self, ctx):
        return checks.check_short_pay(ctx.rows, ctx.event.payload["deal_id"], config.COLLECTION_TOLERANCE_BPS)

    def write_payment(self, ctx):
        r = ctx.calc["result"]
        if not r["short_cents"] or not ctx.rows["billing"]:
            return []
        deal = self.get("deals", ctx.event.payload["deal_id"])
        counts = ("counts as collected, so commission is released" if r["collected"] else
                  "is below the tolerance, so commission waits for the rest")
        text = (f"FYI to AR: {deal['account']} paid {fmt_usd(r['paid_cents'])} of {fmt_usd(r['invoiced_cents'])} "
                f"invoiced ({r['paid_bps'] / 100:.2f}%); the {fmt_usd(r['short_cents'])} shortfall is AR's to settle, "
                f"Quota does not correct it. {r['paid_bps'] / 100:.2f}% {counts} (tolerance "
                f"{config.COLLECTION_TOLERANCE_BPS / 100:.0f}%)")
        decision_id = self.flag("short_pay", deal["id"], ctx.rows, ctx.calc, "posted", text)
        self.emit("FYI", f"to AR: {deal['account']} short-paid {fmt_usd(r['short_cents'])} ({decision_id})")
        return [decision_id]

    # ---------- payment.overdue: collection hold (E07) ----------

    def gather_overdue(self, ctx):
        deal = self.get("deals", ctx.event.payload["deal_id"])
        ctx.rows = {"deals": [deal], "plans": queries.fetch_all(
            self.ro, "SELECT * FROM plans WHERE rep_id = ? AND known_at <= ? ORDER BY version", (deal["owner_id"], self.now))}
        payments = queries.fetch_all(self.ro, "SELECT * FROM billing WHERE deal_id = ? AND kind = 'payment' "
                                              "AND known_at <= ?", (deal["id"], self.now))
        if payments:
            ctx.rows["billing"] = payments

    def calculate_overdue(self, ctx):
        return checks.check_collection(ctx.rows, ctx.event.payload["deal_id"], ctx.event.payload["days"])

    def write_overdue(self, ctx):
        result, deal = ctx.calc["result"], ctx.rows["deals"][0]
        days = ctx.event.payload["days"]
        if result["paid"] is not None:
            paid = result["paid"]
            ref = f", {paid['external_ref']}" if paid.get("external_ref") not in (None, paid["id"]) else ""
            return [self.flag("collection_check", deal["id"], ctx.rows, ctx.calc, "posted",
                              f"no collection hold: a {days}-day overdue notice for {deal['account']}, but billing "
                              f"shows {fmt_usd(paid['amount_cents'])} paid at {paid['at']} ({paid['id']}{ref}); "
                              f"payment status comes from billing")]
        if not result["held"]:
            return []
        text = (f"collection hold: {deal['account']} is {days} days late (plan holds at {result['hold_days']}); "
                f"commission waits for payment")
        reps = sorted({s["rep"] for s in json.loads(deal["splits_json"])})
        return [self.flag("collection_hold", deal["id"], ctx.rows, ctx.calc, "held", text,
                          [(rep, f"{deal['account']} is {days} days past due, so your commission on {deal['id']} is on "
                                 f"collection hold. It is released when the customer pays.") for rep in reps])]

    # ---------- rep.terminated (E09) ----------

    def gather_terminated(self, ctx):
        rep_id = ctx.event.payload["rep_id"]
        lines = [l for l in self.lines(rep_id=rep_id) if l["amount_cents"] > 0
                 and self.line_status(l["id"]) not in ("superseded", "clawed_back")]
        origins = [self.get("decisions", l["decision_id"]) for l in lines]
        deal_ids = sorted({l["deal_id"] for l in lines})
        payments = [b for d in deal_ids for b in queries.fetch_all(
            self.ro, "SELECT * FROM billing WHERE deal_id = ? AND kind = 'payment' AND known_at <= ?", (d, self.now))]
        ctx.rows = {"people": [self.get("people", rep_id)], "commission_lines": lines, "decisions": origins,
                    "billing": payments}

    def calculate_terminated(self, ctx):
        return checks.check_termination(ctx.rows, ctx.event.payload["rep_id"], ctx.event.payload["end_date"])

    def write_terminated(self, ctx):
        p, result = ctx.event.payload, ctx.calc["result"]
        name = config.display_name(p["rep_id"])
        text = (f"{name} left {p['end_date']} ({p['state']}): the signed plan pays lines booked and collected before "
                f"then ({', '.join(result['eligible']) or 'none'}); held: {', '.join(result['held']) or 'none'}; no "
                f"clawback without a signed clause")
        decision_ids = [self.flag("termination_review", p["rep_id"], ctx.rows, ctx.calc,
                                  "held" if result["held"] else "posted", text,
                                  [(routing.owner("material_flag"), f"Policy flag: {text}.")])]
        self.final_pay(p["rep_id"], p["end_date"], result["eligible"])
        return decision_ids

    def in_open_payroll(self) -> set[str]:
        """Lines in a final-pay file that Payroll has not released yet, so the regular cycle does not pay them
        twice. A regular file whose release was refused (E16) does not count: its lines carry to the next one."""
        out = set()
        for payroll in queries.fetch_all(self.ro, "SELECT * FROM payroll_files WHERE id LIKE 'PR-FINAL-%' "
                                                  "AND known_at <= ?", (self.now,)):
            if queries.latest_status(self.ro, "payroll_status", "file_id", payroll["id"], self.now) != "released":
                out |= {pl["line_id"] for pl in json.loads(payroll["lines_json"])}
        return out

    def final_pay(self, rep, end_date, eligible) -> None:
        """Scope, Payroll: a same-day payout request for a departing rep's earned, approved commission, so final
        pay is on time. What is earned but not yet approved follows in the regular cycle once approved."""
        # A final-pay file honours every hold a regular one does (an off-contract promise, a collection hold).
        held = self.payroll_held()
        ready = [l for l in eligible if self.line_status(l) == "approved" and l not in self.in_open_payroll()
                 and l not in held]
        waiting = [l for l in eligible if self.line_status(l) == "draft"]
        if [l for l in eligible if l in held]:
            self.send(routing.owner("payroll_release"),
                      f"Final pay for {config.display_name(rep)} (left {end_date}) leaves out "
                      f"{', '.join(l for l in eligible if l in held)}: held pending the Controller.")
        if not ready:
            if waiting:
                self.send(routing.owner("payroll_release"),
                          f"Final pay for {config.display_name(rep)} (left {end_date}): {', '.join(waiting)} earned "
                          f"but not yet approved; they follow once the Controller approves them.")
            return
        file_id = self.wp.create_payroll_file(self.principal, f"FINAL-{rep}-{end_date}", ready)
        total = self.get("payroll_files", file_id)["total_cents"]
        self.emit("PAYROLL", f"{file_id} created: final pay {fmt_usd(total)} ({', '.join(ready)})")
        approver = routing.owner("payroll_file")
        approval_id = self.wp.request_approval(self.principal, "payroll_file", file_id, total, approver=approver)
        self.emit("APPROVAL", f"{approval_id} payroll_file {file_id} {fmt_usd(total)} -> {approver}")
        self.send(approver, f"{approval_id} is final pay for {config.display_name(rep)}, who left {end_date}: "
                            f"{fmt_usd(total)} ({', '.join(ready)}). Final wages are due promptly; please decide it "
                            f"today.")
        self.send(routing.owner("payroll_release"), f"Final pay file {file_id} ({fmt_usd(total)}) for "
                                                    f"{config.display_name(rep)} is waiting on {approval_id}; release it "
                                                    f"once approved."
                  + (f" Also earned but not yet approved: {', '.join(waiting)}." if waiting else ""))

    # ---------- rep.leave (escalation contract #4) ----------

    def gather_leave(self, ctx):
        rep_id = ctx.event.payload["rep_id"]
        ctx.rows = {"people": [self.get("people", rep_id)],
                    "commission_lines": [l for l in self.lines(rep_id=rep_id) if l["amount_cents"] > 0
                                         and self.line_status(l["id"]) in ("draft", "approved")]}

    def write_leave(self, ctx):
        p = ctx.event.payload
        output = checks.check_leave(ctx.rows, p["rep_id"], p["start_date"], p.get("end_date"))
        lines, total = output["result"]["lines"], output["result"]["total_cents"]
        name = config.display_name(p["rep_id"])
        span = f"from {p['start_date']}" + (f" to {p['end_date']}" if p.get("end_date") else "")
        text = (f"{name} on leave {span} ({p['state'] or 'state unknown'}): {len(lines)} unpaid line(s), "
                f"{fmt_usd(total)}; whether commission is paid during leave is a legal and policy question, so "
                f"nothing is changed until the Controller and HR say")
        decision_id = self.wp.record_decision(self.principal, "leave_review", p["rep_id"],
                                              queries.input_refs(ctx.rows), {}, [], output, "escalated", text)
        self.emit("DECISION", f"{decision_id} leave_review {p['rep_id']}: {text}")
        self.send(routing.owner("material_flag"), f"Leave of absence ({decision_id}): {text}. Please confirm with HR "
                                                  f"how {name}'s commission should be handled.")
        return [decision_id]

    # ---------- rep.territory_changed: a half-done move (E10) ----------

    def gather_territory(self, ctx):
        accounts = ctx.event.payload["accounts"]
        ctx.rows = {"deals": [d for d in (self.get("deals", a) for a in accounts) if d]}

    def calculate_territory(self, ctx):
        return checks.check_territory(ctx.rows, ctx.event.payload["rep_id"], ctx.event.payload["accounts"])

    def write_territory(self, ctx):
        p, result = ctx.event.payload, ctx.calc["result"]
        if not result["unchanged"]:
            return []
        owner = routing.owner("territory")
        decision_ids = [self.flag(
            "territory_check", deal_id, ctx.rows, ctx.calc, "held",
            f"territory move to {p['rep_id']} effective {p['effective_date']} lists {len(p['accounts'])} accounts but "
            f"only {len(result['moved'])} changed owner in the CRM; {deal_id} is held until its owner is updated")
            for deal_id in result["unchanged"]]
        self.send(owner, f"The territory move to {config.display_name(p['rep_id'])} effective {p['effective_date']} "
                         f"({p['actor']}) lists {len(p['accounts'])} accounts, but only {len(result['moved'])} changed "
                         f"owner in the CRM. Held: {', '.join(result['unchanged'])}. Please finish the move.")
        return decision_ids

    # ---------- plan.amended: an unsigned draft that differs from the signed plan (E14) ----------

    def gather_plan_amended(self, ctx):
        ctx.rows = {"plans": queries.fetch_all(self.ro, "SELECT * FROM plans WHERE rep_id = ? AND known_at <= ? "
                                                        "ORDER BY version", (ctx.event.payload["rep_id"], self.now))}

    def calculate_plan_amended(self, ctx):
        return checks.check_plan_drift(ctx.rows, ctx.event.payload["rep_id"], ctx.event.payload["version"])

    def write_plan_amended(self, ctx):
        p, result = ctx.event.payload, ctx.calc["result"]
        if not result["drift"]:
            return []
        owner = routing.owner("plan_config")
        text = (f"plan v{result['draft_version']} for {p['rep_id']} is an unsigned draft that differs from signed "
                f"v{result['signed_version']} ({', '.join(result['changed'])}); paying on the signed plan")
        return [self.flag("plan_drift", p["rep_id"], ctx.rows, ctx.calc, "escalated", text,
                          [(owner, f"Config drift: {text}. A change needs a signed amendment."),
                           (routing.owner("material_flag"), f"Unsigned plan (legally sensitive): {text}.")])]
