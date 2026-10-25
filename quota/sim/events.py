"""Event dataclasses + a JSON schema per type (SPEC "Event types").

Events are how the world reaches the seats. A seat never sees an event before the clock passes its
release_at. `job.due` is the one type SPEC does not list: the close calendar (commission run, accrual,
amortization, payroll, churn review) and the approver timers, so scheduled seat work is event-driven too.
"""

import json
from dataclasses import dataclass, field

from quota.money import fmt_usd

STRING = {"type": "string"}
INTEGER = {"type": "integer"}
OBJECT = {"type": "object"}
STRINGS = {"type": "array", "items": STRING}

JOBS = ("commission_run", "revenue_recognition", "commission_accrual", "amortization", "payroll_file",
        "churn_review", "approver_digest", "approval_followup")


def _schema(required: dict, optional: dict | None = None) -> dict:
    return {"type": "object", "properties": {**required, **(optional or {})}, "required": sorted(required),
            "additionalProperties": False}


_BILLING = _schema({"stripe_id": STRING, "deal_id": STRING, "amount_cents": INTEGER, "date": STRING})
_DEAL_CHANGE = _schema({"deal_id": STRING, "fields": OBJECT, "actor": STRING})

EVENT_SCHEMAS = {
    "deal.created": _DEAL_CHANGE,
    "deal.updated": _DEAL_CHANGE,
    "contract.countersigned": _schema({"deal_id": STRING, "timestamp": STRING, "acv_cents": INTEGER,
                                       "term_months": INTEGER, "clauses": OBJECT}, {"ramp": OBJECT}),
    "contract.amended": _schema({"deal_id": STRING, "terms": OBJECT, "signed_at": STRING}),
    "invoice.issued": _BILLING,
    "payment.received": _BILLING,
    "refund.issued": _BILLING,
    "payment.overdue": _schema({"deal_id": STRING, "days": INTEGER}),
    # ELASTIC_TASKS E6: the promise sweep found a message that passed Quota's own checks; the seat's gate decides.
    "evidence.found": _schema({"doc_id": STRING, "deal_id": STRING, "account": STRING, "type": STRING,
                               "quote": STRING, "finding_id": STRING}),
    "customer.churned": _schema({"deal_id": STRING, "effective_date": STRING, "reason": STRING,
                                 "refund_cents": INTEGER}),
    "rep.terminated": _schema({"rep_id": STRING, "end_date": STRING, "state": STRING}),
    # Scope, escalation contract #4: a leave of absence is legally sensitive; Quota asks, it does not decide.
    "rep.leave": _schema({"rep_id": STRING, "start_date": STRING, "state": STRING}, {"end_date": STRING}),
    "rep.territory_changed": _schema({"rep_id": STRING, "accounts": STRINGS, "effective_date": STRING,
                                      "actor": STRING}),
    "plan.amended": _schema({"rep_id": STRING, "version": INTEGER, "changes": OBJECT, "signed_at": STRING}),
    "message.posted": _schema({"message_id": STRING, "channel": STRING, "author": STRING, "to": STRING,
                               "text": STRING}),
    "approval.decided": _schema({"approval_id": STRING, "approver": STRING, "decision": STRING,
                                 "reason": STRING}, {"result": STRING, "next_approval_id": STRING}),
    "period.lock_requested": _schema({"period": STRING}),
    "clock.advance": _schema({"to": STRING}),
    "job.due": _schema({"job": {"type": "string", "enum": list(JOBS)}},
                       {"period": STRING, "date": STRING, "approval_id": STRING, "since": STRING}),
}

_PY_TYPES = {"string": str, "integer": int, "object": dict, "array": list}


def validate(event_type: str, payload: dict) -> list[str]:
    """Every problem with this payload; empty means valid."""
    schema = EVENT_SCHEMAS.get(event_type)
    if schema is None:
        return [f"unknown event type {event_type!r}"]
    errors = [f"missing {k}" for k in schema["required"] if k not in payload]
    for key, value in payload.items():
        spec = schema["properties"].get(key)
        if spec is None:
            errors.append(f"unexpected field {key}")
            continue
        if value is None and key not in schema["required"]:
            continue
        expected = _PY_TYPES[spec["type"]]
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            errors.append(f"{key} must be {spec['type']}")
        elif "enum" in spec and value not in spec["enum"]:
            errors.append(f"{key} must be one of {', '.join(spec['enum'])}")
    return errors


@dataclass(frozen=True)
class Event:
    type: str
    payload: dict = field(default_factory=dict)
    release_at: str = ""
    id: str | None = None
    created_by: str = "system"
    known_at: str | None = None

    @classmethod
    def from_row(cls, row: dict) -> "Event":
        return cls(type=row["type"], payload=json.loads(row["payload_json"]), release_at=row["release_at"],
                   id=row["id"], created_by=row["created_by"], known_at=row["known_at"])


def make(event_type: str, payload: dict, release_at: str) -> Event:
    errors = validate(event_type, payload)
    if errors:
        raise ValueError(f"invalid {event_type} event: {'; '.join(errors)}")
    return Event(event_type, dict(payload), release_at)


def describe(event: Event) -> str:
    """One line for the timeline."""
    p = event.payload
    if event.type in ("invoice.issued", "payment.received", "refund.issued"):
        return f"{event.type} {p['deal_id']} {fmt_usd(p['amount_cents'])} ({p['stripe_id']})"
    if event.type == "customer.churned":
        return (f"customer.churned {p['deal_id']}: effective {p['effective_date']}, reason {p['reason']}, "
                f"refund {fmt_usd(p['refund_cents'])}")
    if event.type == "job.due":
        detail = " ".join(str(p[k]) for k in ("period", "date", "approval_id") if p.get(k))
        return f"job.due {p['job']}{' ' + detail if detail else ''}"
    return f"{event.type} " + ", ".join(f"{k}={v}" for k, v in sorted(p.items()))
