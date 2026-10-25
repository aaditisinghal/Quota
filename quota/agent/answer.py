"""Answers to chat messages that are not a claim: a question ("why is my Acme commission $12,000?"), a check
("does this actually work?"), or small talk. The analyst replies to every message.

The model writes the answer from the facts it is given (the person's own commission lines, holds, what waits on
them, and the recent conversation); it decides nothing and changes nothing. Every dollar amount and date in the
answer must appear in those facts, or the plain summary is sent instead. Without a model, the plain summary is
the answer.
"""

import json

from quota import config
from quota.agent.llm import LLMError, LLMUnavailable, reply_problems
from quota.ledger import queries
from quota.money import fmt_usd

QUESTION = ("what", "why", "how", "when", "where", "who", "which", "can", "could", "does", "do", "is", "are",
            "will", "should", "tell", "explain", "show")


def is_question(text: str) -> bool:
    """A typed question gets an answer even when Quota is waiting on this person for a yes or no."""
    words = text.strip().lower().split()
    return text.rstrip().endswith("?") or bool(words and words[0].strip(",.") in QUESTION)

ANSWER_SYSTEM = (
    "You are Quota's commissions analyst, replying in a company chat to the person named in the facts. Answer "
    "what they actually wrote, directly and in plain English, using only the facts given as JSON: their "
    "commission lines, holds, approvals waiting on them, and the recent conversation. If they ask how something "
    "was decided, explain it from the facts. If the facts do not cover the question, say so and say what you "
    "can check. You cannot approve, pay or change anything in this reply: say which command or person does "
    "that (the facts list the commands). Copy every dollar amount and date exactly as written in the facts and "
    "never compute new ones. No headings or bullet lists; at most 120 words.")
ANSWER_SCHEMA = {"type": "object", "properties": {"reply": {"type": "string"}}, "required": ["reply"],
                 "additionalProperties": False}

COMMANDS = [
    "To dispute a commission, name the deal and the value you expect (\"Acme closed Mar 31, count it in Q1\").",
    "After a dispute is held, reply \"escalate\" to send it to the plan owner.",
    "An approver decides with \"approve A-0001 <reason>\" or \"reject A-0001 <reason>\".",
    "The payroll manager releases a payroll file with \"release PR-2026-05\".",
]
RECENT = 6


def facts_for(seat, person: str) -> dict:
    """What this person may be told: their own lines and holds, what waits on them, the recent conversation."""
    deals = seat.deal_names()
    lines = []
    for line in seat.lines(rep_id=person):
        status = seat.line_status(line["id"])
        if status == "superseded":
            continue
        hold = seat.hold_on(line) if status in ("draft", "approved") else None
        calc = seat.get("decisions", line["decision_id"])
        disputes = [d for kind in ("dispute", "escalation") for d in seat.decisions(kind=kind, subject_id=line["id"])]
        lines.append({"line": line["id"], "deal": deals.get(line["deal_id"], line["deal_id"]),
                      "quarter": line["booking_quarter"], "amount": fmt_usd(line["amount_cents"]),
                      "status": status.replace("_", " "),
                      "corrects": line["corrects_id"],
                      "how_calculated": f"{calc['id']}: {calc['explanation']}" if calc else None,
                      "disputes": [f"{d['id']} {d['kind']} ({d['outcome']}): {d['explanation']}" for d in disputes[-3:]],
                      "held": f"{hold['id']}: {hold['explanation']}" if hold else None})
    waiting = [{"approval": a["id"], "kind": a["kind"].replace("_", " "), "subject": a["subject_id"],
                "amount": fmt_usd(a["amount_cents"]), "since": a["known_at"]}
               for a in seat.pending_approvals() if a["approver"] == person]
    exceptions = []
    for a in queries.fetch_all(seat.ro, "SELECT * FROM approvals WHERE kind = 'plan_exception' AND known_at <= ? "
                                        "ORDER BY known_at, id", (seat.now,)):
        asked = queries.as_of(seat.ro, "messages", a["subject_id"], seat.now)
        involved = {a["approver"], a["requested_by"], asked["author"] if asked else None}
        if person not in involved and not any(l["line"] and asked and l["deal"] in asked["text"] for l in lines):
            continue
        decided = queries.fetch_all(seat.ro, "SELECT * FROM approval_decisions WHERE approval_id = ? AND known_at <= ?",
                                    (a["id"], seat.now))
        exceptions.append({"approval": a["id"], "approver": config.display_name(a["approver"]),
                           "amount": fmt_usd(a["amount_cents"]),
                           "asked_in": f"{asked['id']} by {config.display_name(asked['author'])}: \"{asked['text']}\""
                           if asked else a["subject_id"],
                           "decision": (f"{decided[-1]['decision']} -> {decided[-1]['result']} by "
                                        f"{config.display_name(decided[-1]['decided_by'])}: {decided[-1]['reason']}")
                           if decided else "pending"})
    talk = seat.conversation(person)[-RECENT:]
    spec = config.PRINCIPALS.get(person, {})
    return {
        "person": {"id": person, "name": config.display_name(person), "title": spec.get("title")},
        "today": seat.now,
        "commission_lines": lines,
        "approvals_waiting_on_you": waiting,
        "plan_exceptions": exceptions[-6:],
        "booking_rule": "R-BOOKING-01: booking date = in-force precedent, then countersignature, then contract date, "
                        "then CRM close date; the CRM close date is never trusted alone",
        "recent_conversation": [{"from": config.display_name(m["author"]) if not m["author"].startswith("seat:")
                                 else "Quota", "at": m["known_at"], "text": m["text"]} for m in talk],
        "commands": COMMANDS,
    }


def plain_answer(facts: dict) -> str:
    """The answer without a model: where this person's commissions stand, and what to type next."""
    name = facts["person"]["name"].split()[0]
    lines = facts["commission_lines"]
    parts = [f"{name}, I read that as a question, not a claim about a commission."]
    if lines:
        shown = "; ".join(f"{l['line']} {l['deal']} {l['quarter']} {l['amount']} ({l['status']}"
                          + (f", held by {l['held'].split(':')[0]}" if l["held"] else "") + ")" for l in lines[-4:])
        parts.append(f"Your commission lines: {shown}.")
    waiting = facts["approvals_waiting_on_you"]
    if waiting:
        parts.append("Waiting on you: " + "; ".join(f"{a['approval']} {a['kind']} {a['amount']}" for a in waiting) + ".")
    if not lines and not waiting:
        parts.append("Nothing of yours is in the commission ledger yet, and nothing is waiting on you.")
    parts.append(COMMANDS[0] if lines else COMMANDS[2] if waiting else COMMANDS[0])
    return " ".join(parts)


def answer(llm, text: str, facts: dict) -> str:
    """The model's answer when it keeps to the facts; the plain summary otherwise."""
    if llm is None:
        return plain_answer(facts)
    prompt = json.dumps({"message": text, "facts": facts}, indent=1, sort_keys=True)
    try:
        reply = llm.complete_json(ANSWER_SYSTEM, prompt, ANSWER_SCHEMA, effort="low")["reply"].strip()
    except (LLMError, LLMUnavailable):
        return plain_answer(facts)
    except Exception as exc:
        # A rate limit or server error from the API still gets the person an answer, just the plain one.
        if not type(exc).__module__.startswith("anthropic"):
            raise
        return plain_answer(facts)
    if not reply or reply_problems(reply, {"facts": json.dumps(facts), "message": text}):
        return plain_answer(facts)
    return reply
