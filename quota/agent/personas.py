"""Marcus, Priya, Dana and the reps; scripted or model-played.

People act only through PeopleAPI, the same approval and chat API a human would use, with the WritePath
capabilities of section 3. Every action is announced on the event queue (message.posted, approval.decided),
which is how the seats hear about it. Scripted people follow the demo script: `actions` they take on their own
at a time, and `responses` keyed by approval id or message id.
"""

import json

from quota import clock as clk
from quota import config
from quota.agent.llm import LLMError
from quota.engine import routing
from quota.ledger import queries
from quota.ledger.write_path import Refused
from quota.money import fmt_usd
from quota.timeline import entry_text

CHANNEL = "slack:dm"

PERSONAS = {
    "jordan.kim": "You are Jordan Kim, an account executive at Fernhill. You care about your commission and push "
                  "back when you think a deal was booked in the wrong quarter, but you accept a clear answer.",
    "marcus.lee": "You are Marcus Lee, VP Sales Ops at Fernhill. You own the comp plan and can approve plan "
                  "exceptions up to $5,000; bigger ones also need the Controller. You honor reasonable requests "
                  "from reps when the deal clearly closed with the customer.",
    "priya.nair": "You are Priya Nair, Controller at Fernhill. You approve journal entries, payroll files and plan "
                  "exceptions above $5,000. You approve what is supported by the evidence shown and reject what "
                  "is not.",
    "dana.ortiz": "You are Dana Ortiz, payroll manager at Fernhill. You release approved payroll files.",
    "morgan.diaz": "You are Morgan Diaz, a sales manager at Fernhill. You cannot approve anything.",
}
PERSONA_RULES = ("You are answering one item in your work inbox at a company that pays sales commissions. You can "
                 "only act through these options: approve or reject an approval request addressed to you (with a "
                 "one-sentence reason), send one short chat message, or do nothing. Decide as the person "
                 "described would.")
PERSONA_SCHEMA = {
    "type": "object",
    "properties": {"action": {"type": "string", "enum": ["approve", "reject", "message", "none"]},
                   "reason": {"type": "string"}, "text": {"type": "string"}},
    "required": ["action", "reason", "text"],
    "additionalProperties": False,
}


class PeopleAPI:
    def __init__(self, world):
        self.world = world

    @property
    def wp(self):
        return self.world.wp

    def _get(self, table, row_id):
        return queries.as_of(self.world.ro, table, row_id, None)

    def message(self, who, to, text) -> str:
        message_id = self.wp.add_message(CHANNEL, who, to, text, None)
        self.world.emit(who, "MESSAGE", f'{message_id} to {to}: "{text}"')
        self.world.queue.emit_now("message.posted", {"message_id": message_id, "channel": CHANNEL, "author": who,
                                                     "to": to, "text": text})
        return message_id

    def approve(self, who, approval_id, reason) -> dict | None:
        return self._decide(who, approval_id, "approved", reason)

    def reject(self, who, approval_id, reason) -> dict | None:
        return self._decide(who, approval_id, "rejected", reason)

    def _decide(self, who, approval_id, decision, reason) -> dict | None:
        approval = self._get("approvals", approval_id)
        try:
            out = self.wp.decide_approval(who, approval_id, decision, reason)
        except Refused as refused:
            self.world.emit(who, "REFUSED", f"{refused.refusal_id} decide_approval {approval_id}: "
                                            f"{', '.join(refused.reason_codes)}")
            return None
        text = f"{approval_id} {approval['kind']} {approval['subject_id']} {decision} -> {out['result']}"
        if out["result"] == "over_limit_routed":
            routed = self._get("approvals", out["next_approval_id"])
            text += (f" (limit {fmt_usd(routing.limit(who, approval['kind']))} < {fmt_usd(approval['amount_cents'])});"
                     f" {routed['id']} -> {routed['approver']}")
        elif decision == "rejected":
            text += f": {reason}"
        self.world.emit(who, "APPROVAL", text)
        self.world.queue.emit_now("approval.decided", {
            "approval_id": approval_id, "approver": who, "decision": decision, "reason": reason,
            "result": out["result"], "next_approval_id": out["next_approval_id"]})
        return out

    def _refused(self, who, action, subject, refused: Refused) -> None:
        self.world.emit(who, "REFUSED", f"{refused.refusal_id} {action} {subject}: {', '.join(refused.reason_codes)}")

    def lock_period(self, who, period) -> None:
        try:
            self.wp.lock_period(who, period)
        except Refused as refused:
            self._refused(who, "lock_period", period, refused)
            return
        self.world.emit(who, "PERIOD", f"{period} locked")

    def release_payroll(self, who, file_id, export_adjust: dict | None = None) -> str | None:
        """Release from the payroll system's export. `export_adjust` ({deal_id, cents}) plants a hand edit in the
        export (E16); the write path compares the export to the approved file line by line."""
        payroll = self._get("payroll_files", file_id)
        if payroll is None:
            self.world.emit(who, "PAYROLL", f"{file_id}: no such payroll file, nothing released")
            return None
        exported = {pl["line_id"]: pl["amount_cents"] for pl in json.loads(payroll["lines_json"])}
        if export_adjust:
            edited = next((pl["line_id"] for pl in json.loads(payroll["lines_json"])
                           if self._get("commission_lines", pl["line_id"])["deal_id"] == export_adjust["deal_id"]), None)
            if edited:
                exported[edited] += export_adjust["cents"]
        try:
            je_id = self.wp.release_payroll(who, file_id, exported)
        except Refused as refused:
            self._refused(who, "release_payroll", file_id, refused)
            return None
        self.world.emit(who, "PAYROLL", f"{file_id} released")
        self.world.emit(who, "ENTRY", f"{je_id} posted payroll_payment {file_id}: "
                                      f"{entry_text(queries.lines_for(self.world.ro, je_id))}")
        for line in json.loads(self._get("payroll_files", file_id)["lines_json"]):
            self.world.emit(who, "ENTRY", f"{line['line_id']} -> paid ({file_id})")
        return je_id


class ScriptedPeople:
    """People who follow the demo script. Anything not in the script is silence, and silence never approves."""

    def __init__(self, world, script: dict):
        self.world = world
        self.api = world.api
        self.actions = list(script.get("actions") or [])
        self.responses = dict(script.get("responses") or {})
        self.done: set = set()

    def _exists(self, key) -> bool:
        table = {"A": "approvals", "M": "messages"}.get(key.split("-", 1)[0])
        return table is not None and queries.as_of(self.world.ro, table, key, self.world.clock.now()) is not None

    def due(self) -> list[tuple[object, dict]]:
        now = self.world.clock.now()
        items = [(("action", i), a) for i, a in enumerate(self.actions) if a["at"] <= now]
        items += [(("response", k), r) for k, r in self.responses.items() if r["at"] <= now and self._exists(k)]
        return [(key, item) for key, item in items if key not in self.done]

    def act(self) -> bool:
        acted = False
        for key, item in self.due():
            self.done.add(key)
            self.perform(item, responding_to=key[1] if key[0] == "response" else None)
            acted = True
        return acted

    def perform(self, item: dict, responding_to=None) -> None:
        who, do = item["who"], item["do"]
        if do == "message":
            to = item.get("to") or queries.as_of(self.world.ro, "messages", responding_to, None)["author"]
            self.api.message(who, to, item["text"])
        elif do in ("approve", "reject"):
            getattr(self.api, do)(who, item.get("approval") or responding_to, item.get("reason", ""))
        elif do == "lock_period":
            self.api.lock_period(who, item["period"])
        elif do == "release_payroll":
            self.api.release_payroll(who, item["file"], item.get("export_adjust"))
        else:
            raise ValueError(f"unknown scripted action {do!r}")

    def next_time(self) -> str | None:
        now = self.world.clock.now()
        times = [a["at"] for i, a in enumerate(self.actions) if ("action", i) not in self.done and a["at"] > now]
        times += [r["at"] for k, r in self.responses.items() if ("response", k) not in self.done and r["at"] > now]
        return min(times) if times else None


class PolicyPeople(ScriptedPeople):
    """Rule-played people for generated worlds: the script's actions at their times, and approvers who approve
    what is addressed to them `delay_minutes` after it arrives. The eval measures Quota, not the approvers."""

    def __init__(self, world, actions: list[dict], approves: dict[str, set[str]], delay_minutes=30):
        super().__init__(world, {"actions": actions})
        self.approves, self.delay_minutes = approves, delay_minutes

    def _due_approvals(self):
        return [(clk.add_minutes(a["known_at"], self.delay_minutes), a)
                for a in queries.pending_approvals(self.world.ro, self.world.clock.now())
                if a["kind"] in self.approves.get(a["approver"], ())]

    def act(self) -> bool:
        acted = super().act()
        now = self.world.clock.now()
        for at, approval in self._due_approvals():
            if at <= now:
                self.api.approve(approval["approver"], approval["id"], "Reviewed; consistent with the plan.")
                acted = True
        return acted

    def next_time(self) -> str | None:
        now = self.world.clock.now()
        times = [t for t in [super().next_time()] + [at for at, _ in self._due_approvals()] if t and t > now]
        return min(times) if times else None


class LivePeople(ScriptedPeople):
    """--live: the script's own actions still happen, but every response is a model call with a fixed persona
    prompt, answering through PeopleAPI only. A response is due at the script's time for the same item id when
    there is one, otherwise `delay_minutes` after the item arrived. A persona answers each chat partner at most
    `max_replies` times, so two talkative parties cannot loop forever."""

    def __init__(self, world, script: dict, llm, delay_minutes=30, max_replies=3):
        super().__init__(world, {"actions": script.get("actions")})
        self.llm = llm
        self.scripted_at = {k: r["at"] for k, r in (script.get("responses") or {}).items()}
        self.delay_minutes, self.max_replies = delay_minutes, max_replies
        self.answered: set[str] = set()
        self.replies: dict[tuple[str, str], int] = {}

    def inbox(self) -> list[tuple[str, str, dict]]:
        """(item id, persona, row) for undecided approvals and messages addressed to a persona."""
        ro, now = self.world.ro, self.world.clock.now()
        items = [(a["id"], a["approver"], a) for a in queries.fetch_all(
                     ro, "SELECT * FROM approvals WHERE known_at <= ? ORDER BY id", (now,))
                 if a["approver"] in PERSONAS and queries.approval_decision(ro, a["id"], now) is None]
        items += [(m["id"], m["to_principal"], m) for m in queries.fetch_all(
                      ro, "SELECT * FROM messages WHERE known_at <= ? ORDER BY id", (now,))
                  if m["to_principal"] in PERSONAS
                  and self.replies.get((m["to_principal"], m["author"]), 0) < self.max_replies]
        return [item for item in items if item[0] not in self.answered]

    def respond_at(self, item_id, row) -> str:
        scripted = self.scripted_at.get(item_id)
        if scripted and scripted >= row["known_at"]:   # the same id may name a different item in a live run
            return scripted
        return clk.add_minutes(row["known_at"], self.delay_minutes)

    def act(self) -> bool:
        acted = super().act()
        now = self.world.clock.now()
        for item_id, who, row in self.inbox():
            if self.respond_at(item_id, row) <= now:
                self.answered.add(item_id)
                self.respond(item_id, who, row)
                acted = True
        return acted

    def next_time(self) -> str | None:
        now = self.world.clock.now()
        times = [t for t in [super().next_time()] + [self.respond_at(i, r) for i, _, r in self.inbox()]
                 if t and t > now]
        return min(times) if times else None

    def _line_evidence(self, line_id) -> str | None:
        ro = self.world.ro
        line = queries.as_of(ro, "commission_lines", line_id, None)
        if line is None:
            return None
        origin = queries.as_of(ro, "decisions", line["decision_id"], None)
        return (f"Commission line {line['id']}: {fmt_usd(line['amount_cents'])} for {line['deal_id']}/{line['rep_id']} "
                f"in {line['booking_quarter']}, calculated in {origin['id']}: {origin['explanation']}")

    def evidence(self, approval) -> list[str]:
        """What a person sees when they open the approval: the subject and the reasoning behind it."""
        ro, subject = self.world.ro, approval["subject_id"]
        out = []
        entry = queries.as_of(ro, "journal_entries", subject, None)
        if entry:
            out.append(f"Entry {subject} dated {entry['effective_date']}: {entry['memo']}; "
                       f"{entry_text(queries.lines_for(ro, subject))}.")
            decision = queries.as_of(ro, "decisions", entry["decision_id"], None) if entry["decision_id"] else None
            if decision:
                out.append(f"Why ({decision['id']}): {decision['explanation']}")
                out.append(self._line_evidence(decision["subject_id"]) or "")
        payroll = queries.as_of(ro, "payroll_files", subject, None)
        if payroll:
            out.append(f"Payroll file {subject}, total {fmt_usd(payroll['total_cents'])}:")
            out += [self._line_evidence(pl["line_id"]) or pl["line_id"] for pl in json.loads(payroll["lines_json"])]
        message = queries.as_of(ro, "messages", subject, None)
        if message:
            out.append(f"{config.display_name(message['author'])} wrote ({message['id']}): \"{message['text']}\"")
        parent = approval["parent_id"]
        while parent:
            decided = queries.approval_decision(ro, parent)
            if decided:
                out.append(f"{parent} was {decided['decision']} by {config.display_name(decided['decided_by'])} "
                           f"({decided['result']}): {decided['reason']}")
            parent = queries.as_of(ro, "approvals", parent, None)["parent_id"]
        return [line for line in out if line]

    def situation(self, item_id, who, row) -> str:
        ro = self.world.ro
        if item_id.startswith("A-"):
            limit = routing.limit(who, row["kind"])
            evidence = "\n".join(f"- {line}" for line in self.evidence(row))
            return (f"Approval request {item_id} for you: {row['kind']} on {row['subject_id']}, amount "
                    f"{fmt_usd(row['amount_cents'])}, requested by {row['requested_by']}.\nEvidence:\n{evidence}\n"
                    f"Your approval limit: {'none' if limit is None else fmt_usd(limit)}.")
        thread = [m for m in queries.fetch_all(ro, "SELECT * FROM messages WHERE known_at <= ? ORDER BY id",
                                               (self.world.clock.now(),))
                  if {m["author"], m["to_principal"]} == {who, row["author"]}][-6:]
        lines = "\n".join(f"{config.display_name(m['author'])}: {m['text']}" for m in thread)
        return f"Chat with {config.display_name(row['author'])} (latest last):\n{lines}\nReply to the last message."

    def respond(self, item_id, who, row) -> None:
        try:
            answer = self.llm.complete_json(f"{PERSONA_RULES}\n\n{PERSONAS[who]}", self.situation(item_id, who, row),
                                            PERSONA_SCHEMA, effort="medium")
        except LLMError as exc:
            self.world.emit(who, "MESSAGE", f"(no answer from the model for {item_id}: {exc})")
            return
        action = answer["action"]
        if item_id.startswith("A-") and action in ("approve", "reject"):
            getattr(self.api, action)(who, item_id, answer["reason"] or answer["text"])
        elif action == "message" and answer["text"].strip():
            to = row["requested_by"] if item_id.startswith("A-") else row["author"]
            self.replies[(who, to)] = self.replies.get((who, to), 0) + 1
            self.api.message(who, to, answer["text"].strip())
