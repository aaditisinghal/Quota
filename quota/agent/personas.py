"""Marcus, Priya, Dana and the reps; scripted or model-played.

People act only through PeopleAPI, the same approval and chat API a human would use, with the WritePath
capabilities of section 3. Every action is announced on the event queue (message.posted, approval.decided),
which is how the seats hear about it. Scripted people follow the demo script: `actions` they take on their own
at a time, and `responses` keyed by approval id or message id.
"""

import json

from quota.engine import routing
from quota.ledger import queries
from quota.ledger.write_path import Refused
from quota.money import fmt_usd
from quota.timeline import entry_text

CHANNEL = "slack:dm"


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

    def lock_period(self, who, period) -> None:
        self.wp.lock_period(who, period)
        self.world.emit(who, "PERIOD", f"{period} locked")

    def release_payroll(self, who, file_id) -> str:
        je_id = self.wp.release_payroll(who, file_id)
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
            self.api.release_payroll(who, item["file"])
        else:
            raise ValueError(f"unknown scripted action {do!r}")

    def next_time(self) -> str | None:
        now = self.world.clock.now()
        times = [a["at"] for i, a in enumerate(self.actions) if ("action", i) not in self.done and a["at"] > now]
        times += [r["at"] for k, r in self.responses.items() if ("response", k) not in self.done and r["at"] > now]
        return min(times) if times else None
