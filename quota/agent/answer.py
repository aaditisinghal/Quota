"""Answers to chat messages that are not a claim: a question ("why is my Acme commission $12,000?"), a check
("does this actually work?"), or small talk. The analyst replies to every message.

The model (claude-sonnet-5, or QUOTA_CHAT_MODEL) writes the answer from the facts it is given: the person's own
commission lines, holds, what waits on them, the signed plan's terms, the recent conversation, and a company
section. Finance and leadership get every rep's contracts, lines and totals and the approvals queue; a rep gets
only the contracts they are credited on and team totals (SPEC: a rep may view only their own statement). It
decides nothing and changes nothing. Every dollar amount and date in the
answer must appear in those facts, or the plain summary is sent instead. Without a model, the plain summary is
the answer.
"""

import json

from quota import clock as clk
from quota import config
from quota.agent.llm import LLMError, LLMUnavailable, reply_problems
from quota.engine import routing
from quota.ledger import queries
from quota.money import fmt_usd

QUESTION = ("what", "why", "how", "when", "where", "who", "which", "can", "could", "does", "do", "is", "are",
            "will", "should", "tell", "explain", "show")


def is_question(text: str) -> bool:
    """A typed question gets an answer even when Quota is waiting on this person for a yes or no."""
    words = text.strip().lower().split()
    return text.rstrip().endswith("?") or bool(words and words[0].strip(",.") in QUESTION)

ANSWER_SYSTEM = (
    "You are Quota's commissions analyst for this company: an expert on its signed sales compensation plan, its "
    "ledger of commission lines, and its approval rules. You reply in the company chat to the person in "
    "facts.person. Answer what they actually wrote, directly and in plain English, using only the facts given "
    "as JSON.\n\n"
    "How commissions work here (facts.plan and facts.approval_limits carry the exact figures): commission is "
    "paid on the contract's first-year ACV from the countersigned contract, not the CRM amount, and the two can "
    "differ. A deal books on the date the contract was countersigned (R-BOOKING-01), never on the CRM close "
    "date alone. The plan pays a base rate of 10% on credit up to 100% of quarterly quota and an accelerator of "
    "15% on credit above quota; split deals credit each rep their share. Commission is paid only once the "
    "customer has paid (collection hold); a line is also held out of payroll when the rep has left before the "
    "deal was collected or the deal's splits do not add up. A paid commission is clawed back if the customer "
    "churns, terminates for convenience or is fully refunded within the clawback window. Plan exceptions go to "
    "Marcus Lee (VP Sales Ops) up to his $5,000 limit, then to Priya Nair (Controller), with Grace Liu (CFO) as "
    "her backup.\n\n"
    "Who may see what (facts.viewer): a rep may view only their own statement. Never reveal another rep's "
    "individual commission, line, total or hold to a rep; if a rep asks about someone else's pay, say you can "
    "only discuss their own statement and the team totals. Finance and sales leadership see facts.company, "
    "which covers every rep, contract, line and the open approvals queue.\n\n"
    "If they ask how something was decided, explain it from the facts and name the rule or decision id. If the "
    "facts do not cover the question, say so and say what you can check. You cannot approve, pay or change "
    "anything in this reply and must never promise that anything will be paid: say which command or person "
    "does that (facts.commands). Copy every dollar amount and date exactly as written in the facts; never add, "
    "subtract or otherwise compute a new number. Plain text, no headings or bullet lists, at most 150 words.")
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
    rep_view = is_rep(seat, person)
    facts = {
        "person": {"id": person, "name": config.display_name(person), "title": spec.get("title")},
        "today": seat.now,
        "viewer": ("rep: may see only their own statement, the contracts they are credited on (deal facts, "
                   "co-sellers' names and shares) and team totals; never another rep's individual commission"
                   if rep_view else "finance or sales leadership: may see the whole company section"),
        "commission_lines": lines,
        "approvals_waiting_on_you": waiting,
        "plan_exceptions": exceptions[-6:],
        "booking_rule": "R-BOOKING-01: booking date = in-force precedent, then countersignature, then contract date, "
                        "then CRM close date; the CRM close date is never trusted alone",
        "plan": plan_terms(seat, person if rep_view else None),
        "approval_limits": approval_limits(),
        "recent_conversation": [{"from": config.display_name(m["author"]) if not m["author"].startswith("seat:")
                                 else "Quota", "at": m["known_at"], "text": m["text"]} for m in talk],
        "commands": COMMANDS,
    }
    facts["company"] = company_for_rep(seat, person) if rep_view else company(seat)
    return facts


# ---------- the company section: every rep for finance and leadership, only their own contracts for a rep ----------

def is_rep(seat, person: str) -> bool:
    """A rep (SPEC: may view only their own statement): anyone who can dispute their own lines, or an AE in HR."""
    if "dispute:own_lines" in config.PRINCIPALS.get(person, {}).get("capabilities", frozenset()):
        return True
    row = seat.get("people", person)
    return bool(row and row["role"] == "AE")


def rep_ids(seat) -> list[str]:
    found = {p["id"] for p in queries.latest_rows(seat.ro, "people", seat.now) if p["role"] == "AE"}
    found |= {p for p, spec in config.PRINCIPALS.items() if "dispute:own_lines" in spec.get("capabilities", ())}
    return sorted(found)


def _pct(rate) -> str:
    return f"{float(rate) * 100:g}%"


def plan_terms(seat, rep: str | None) -> dict | None:
    """The signed plan's terms, read from the plan store (the rep's own plan, or any signed AE plan)."""
    sql = "SELECT * FROM plans WHERE signed_at IS NOT NULL AND known_at <= ?"
    params = [seat.now]
    if rep:
        sql, params = sql + " AND rep_id = ?", params + [rep]
    rows = queries.fetch_all(seat.ro, sql + " ORDER BY version DESC, id", tuple(params))
    if not rows:
        return None
    raw = queries.loads(rows[0]["config_json"])
    rates = {r.get("tier"): r for r in raw.get("rates", [])}
    base, accel = rates.get("base", {}), rates.get("accelerator", {})
    claw = raw.get("clawback") or {}
    override = raw.get("manager_override") or {}
    quota_cents = round(float((raw.get("quota") or {}).get("amount", 0)) * 100)
    return {
        "plan": f"{raw.get('plan_id')} v{raw.get('version')}, signed {raw.get('signed_at')}, effective "
                f"{(raw.get('effective') or {}).get('from')} to {(raw.get('effective') or {}).get('to')}",
        "quota_per_quarter": fmt_usd(quota_cents),
        "commissionable_base": f"{raw.get('commissionable_base')}: the countersigned contract's first-year ACV, "
                               "not the CRM amount",
        "booking_date": f"{raw.get('booking_date')} (R-BOOKING-01)",
        "base_rate": f"{_pct(base['rate'])} of credit up to {_pct(base.get('up_to_attainment', 1))} of quota"
        if base else None,
        "accelerator_rate": f"{_pct(accel['rate'])} of credit above {_pct(accel.get('above_attainment', 1))} of quota"
        if accel else None,
        "splits": "shares on a deal must sum to 100%; the VP Sales Ops approves splits",
        "pay_on": f"{raw.get('pay_on')}: a line is paid only after the customer pays "
                  f"(collection hold, {raw.get('collection_hold_days')} days)",
        "clawback": f"within {claw.get('window_months')} months on {', '.join(claw.get('triggers', []))}; "
                    f"basis {claw.get('basis')}" if claw else None,
        "termination": "a leaver is paid on deals booked and collected before their end date",
        "manager_override": (f"{_pct(override.get('rate', 0))} to {config.display_name(override['manager'])} "
                             "in the signed plan; not paid by Quota yet") if override.get("manager") else None,
    }


def approval_limits() -> list[str]:
    out = []
    for who, backup in routing.ESCALATION.items():
        cap = config.PRINCIPALS.get(who, {}).get("limit")
        text = (f"{config.display_name(who)} ({config.title(who)}) approves "
                + (f"up to {fmt_usd(cap)}" if cap is not None else "with no limit"))
        if backup:
            text += (f"; above that, or unanswered for {config.ESCALATE_AFTER_BUSINESS_DAYS} business days, it goes "
                     f"to {config.display_name(backup)} ({config.title(backup)})")
        out.append(text)
    return out


def _contract(seat, deal: dict) -> dict:
    """The deal as booked: CRM fields, the countersigned contract's ACV and term, billing, who is credited."""
    versions = queries.fetch_all(seat.ro, "SELECT * FROM contracts WHERE deal_id = ? AND known_at <= ? "
                                          "ORDER BY version", (deal["id"], seat.now))
    originals = [c for c in versions if not queries.loads(c["clauses_json"]).get("amendment")]
    base = (originals or versions or [None])[-1]
    kinds = {b["kind"] for b in queries.fetch_all(seat.ro, "SELECT kind FROM billing WHERE deal_id = ? AND "
                                                           "known_at <= ?", (deal["id"], seat.now))}
    billing = ("paid" if queries.collected(seat.ro, deal["id"], seat.now)
               else "invoiced, not paid" if "invoice" in kinds else "not invoiced yet")
    amendments = []
    for c in versions:
        terms = queries.loads(c["clauses_json"]).get("amendment")
        if terms and terms.get("kind") == "amendment":
            amendments.append({"version": c["version"], "acv": fmt_usd(c["acv_cents"]),
                               "signed_at": terms.get("signed_at")})
    return {"deal": deal["id"], "account": deal["account"], "stage": deal["stage"],
            "crm_amount": fmt_usd(deal["crm_amount_cents"]), "crm_close_date": deal["crm_close_date"],
            "acv": fmt_usd(base["acv_cents"]) if base else None, "term_months": base["term_months"] if base else None,
            "countersigned_at": base["countersigned_at"] if base else None,
            "countersigned_local_date": clk.local_date(base["countersigned_at"])
            if base and base["countersigned_at"] else None,
            "billing": billing, "owner": config.display_name(deal["owner_id"]),
            "credited": [{"rep": config.display_name(s["rep"]), "share": f"{s['bps'] / 100:g}%"}
                         for s in json.loads(deal["splits_json"])],
            "amendments": amendments}


def _rep_lines(seat, rep: str, deals: dict) -> tuple[list[dict], dict]:
    """A rep's live commission lines (superseded ones left out) and their totals in cents."""
    out, totals = [], {"pending": 0, "paid": 0, "held": 0}
    for line in seat.lines(rep_id=rep):
        status = seat.line_status(line["id"])
        if status == "superseded":
            continue
        hold = seat.hold_on(line) if status in ("draft", "approved") else None
        if status in ("draft", "approved"):
            totals["pending"] += line["amount_cents"]
        if status == "paid":
            totals["paid"] += line["amount_cents"]
        if hold:
            totals["held"] += line["amount_cents"]
        out.append({"line": line["id"], "deal": f"{line['deal_id']} ({deals.get(line['deal_id'], line['deal_id'])})",
                    "quarter": line["booking_quarter"], "amount": fmt_usd(line["amount_cents"]),
                    "status": status.replace("_", " "), "corrects": line["corrects_id"],
                    "held": f"{hold['id']} {hold['kind'].replace('_', ' ')}: {hold['explanation']}" if hold else None})
    return out, totals


def _money(totals: dict) -> dict:
    return {"pending_draft_or_approved_not_paid": fmt_usd(totals["pending"]), "paid": fmt_usd(totals["paid"]),
            "held_out_of_payroll": fmt_usd(totals["held"])}


def _about(seat, approval: dict) -> str:
    """What an approval is about, in words."""
    subject = approval["subject_id"]
    line = seat.get("commission_lines", subject)
    if line:
        return f"line {subject}: {config.display_name(line['rep_id'])}, deal {line['deal_id']}"
    message = seat.get("messages", subject)
    if message:
        return f"{subject} by {config.display_name(message['author'])}: \"{message['text'][:240]}\""
    deal = seat.get("deals", subject)
    return f"deal {subject} ({deal['account']})" if deal else subject


def _queue_entry(seat, a: dict) -> dict:
    return {"approval": a["id"], "kind": a["kind"].replace("_", " "),
            "waiting_on": f"{config.display_name(a['approver'])} ({config.title(a['approver'])})",
            "requested_by": config.display_name(a["requested_by"]) if not a["requested_by"].startswith("seat:")
            else "Quota", "amount": fmt_usd(a["amount_cents"]), "since": a["known_at"], "about": _about(seat, a)}


def company(seat) -> dict:
    """Finance and leadership: every rep with their contracts, lines and totals, and the open approvals queue."""
    deals = seat.deal_names()
    all_deals = queries.latest_rows(seat.ro, "deals", seat.now)
    reps, team = [], {"pending": 0, "paid": 0, "held": 0}
    for rep in rep_ids(seat):
        person = seat.get("people", rep) or {}
        lines, totals = _rep_lines(seat, rep, deals)
        for k in team:
            team[k] += totals[k]
        reps.append({"rep": rep, "name": config.display_name(rep), "title": config.title(rep) or person.get("role"),
                     "state": person.get("state"), "left": person.get("end_date"),
                     "contracts": [d["id"] for d in all_deals if queries.credits_rep(d, rep)],
                     "commission_lines": lines, "totals": _money(totals)})
    return {"as_of": seat.now, "reps": reps, "team_totals": {"reps": len(reps), **_money(team)},
            "contracts": [_contract(seat, d) for d in all_deals],
            "approvals_queue": [_queue_entry(seat, a) for a in seat.pending_approvals()]}


def company_for_rep(seat, person: str) -> dict:
    """A rep: the contracts they are credited on (co-sellers' names and shares, not their pay), the team's totals,
    and open approvals about their own lines or messages. Their own lines are in facts.commission_lines."""
    deals = seat.deal_names()
    team = {"pending": 0, "paid": 0, "held": 0}
    reps = rep_ids(seat)
    for rep in reps:
        for k, v in _rep_lines(seat, rep, deals)[1].items():
            team[k] += v
    _, mine = _rep_lines(seat, person, deals)
    own_lines = {l["id"] for l in seat.lines(rep_id=person)}
    about_me = []
    for a in seat.pending_approvals():
        subject = seat.get("messages", a["subject_id"])
        if a["subject_id"] in own_lines or a["requested_by"] == person or (subject and subject["author"] == person):
            about_me.append(_queue_entry(seat, a))
    return {"as_of": seat.now, "your_totals": _money(mine),
            "team_totals": {"reps": len(reps), **_money(team)},
            "your_contracts": [_contract(seat, d) for d in queries.latest_rows(seat.ro, "deals", seat.now)
                               if queries.credits_rep(d, person)],
            "open_approvals_about_you": about_me}


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


def chat_llm(llm):
    """Chat answers run on the chat model (claude-sonnet-5, or QUOTA_CHAT_MODEL) with the analyst's cache and
    transport; extraction and personas keep the analyst's own model. A stand-in without for_chat is used as is."""
    return llm.for_chat() if hasattr(llm, "for_chat") else llm


def answer(llm, text: str, facts: dict) -> str:
    """The model's answer when it keeps to the facts; the plain summary otherwise."""
    if llm is None:
        return plain_answer(facts)
    llm = chat_llm(llm)
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
