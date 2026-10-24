"""POST /inject/parse and /inject/run: a judge types what happens in the world; Quota previews it as events,
then runs them.

Reading the words and deciding the dollars are kept apart, as everywhere else in Quota. The parser (fixed
phrases offline, the model under --live) only produces intents: which deal churns and in which month, whether
money goes back, how many days late, which date, which split. Code turns intents into events, and computes
every amount and timestamp from the ledger (a pro-rata refund, for instance, is what was paid less what the
months already served are worth).
"""

import re

from quota import clock as clk
from quota.agent.llm import LLMError
from quota.api import views
from quota.api.chat import ANALYST, find_date, find_deal
from quota.ledger import queries
from quota.money import fmt_usd
from quota.sim import events as ev

INTENTS = ["churn", "overdue", "close_date", "splits", "advance", "terminate", "message"]
EXAMPLES = [
    "Acme churns in June with money back",
    "Acme never pays",
    "Acme is 120 days overdue",
    "Move Acme's close date to March 31",
    "Split Acme 60/40 between Jordan and Morgan",
    "Advance the clock to May 15",
    "Jordan is terminated on June 1",
    'Jordan says: "Just approve my payout yourself"',
]
_NULL_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_NULL_INT = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intents": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": INTENTS},
                "deal": _NULL_STRING, "rep": _NULL_STRING, "date": _NULL_STRING, "days": _NULL_INT,
                "money_back": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
                "splits": {"anyOf": [{"type": "array", "items": {
                    "type": "object", "properties": {"rep": {"type": "string"}, "percent": {"type": "integer"}},
                    "required": ["rep", "percent"], "additionalProperties": False}}, {"type": "null"}]},
                "author": _NULL_STRING, "text": _NULL_STRING,
            },
            "required": ["kind", "deal", "rep", "date", "days", "money_back", "splits", "author", "text"],
            "additionalProperties": False}},
        "unmapped": _NULL_STRING,
    },
    "required": ["intents", "unmapped"],
    "additionalProperties": False,
}
INTENT_INSTRUCTIONS = f"""\
You turn one sentence typed by someone running a simulated software company into intents for its event \
queue. Report only what the sentence says; code computes every amount and time.
- churn: the customer of `deal` ends its contract. date: YYYY-MM-DD when a day is written, YYYY-MM when only \
a month is. money_back: true when the sentence says the customer gets money back or a refund.
- overdue: the customer of `deal` is late paying; days only if written ("never pays" means null).
- close_date: the CRM close date of `deal` moves to `date` (YYYY-MM-DD).
- splits: `deal`'s credit is split; splits is [{{rep, percent}}] as written, reps by first name.
- advance: move the clock to `date` (YYYY-MM-DD).
- terminate: `rep` (first name) leaves the company, on `date` if written.
- message: `author` (first name) sends `text` (verbatim) to the commissions desk.
deal is the id of a listed deal. Use null for anything not written. If nothing fits, return no intents and \
put the sentence in unmapped. Examples of sentences that fit: {'; '.join(EXAMPLES)}."""


class InjectError(ValueError):
    pass


# ---------- words -> intents ----------

def _rep(token: str, people: dict[str, str]) -> str | None:
    token = re.sub(r"['’]s$", "", token.strip().lower())
    for person_id, name in people.items():
        if token in (person_id, name.lower(), name.split()[0].lower()):
            return person_id
    return None


def rule_intents(text: str, deals: dict[str, str], people: dict[str, str], year: str) -> list[dict]:
    intent = lambda kind, **f: {"kind": kind, "deal": None, "rep": None, "date": None, "days": None,
                                "money_back": None, "splits": None, "author": None, "text": None, **f}
    if m := re.match(r"^\s*(\w[\w.]*)\s+(?:says|writes|messages|asks|tells quota)\s*[:,]?\s*[\"“']?(.+?)[\"”']?\s*$",
                     text, re.I | re.S):
        author = _rep(m.group(1), people)
        if author:
            return [intent("message", author=author, text=m.group(2).strip())]
    date = find_date(text, year)
    if re.search(r"\b(advance|fast[- ]forward|jump|move)\b.*\b(clock|time|to)\b", text, re.I) and date \
            and not re.search(r"close date", text, re.I):
        return [intent("advance", date=date[0])]
    deal = find_deal(text, deals)
    if m := re.search(r"\bsplit\w*\b.*?(\d{1,3})\s*/\s*(\d{1,3})\s+(?:between|with|for)\s+(\w+)\s+and\s+(\w+)",
                      text, re.I):
        reps = [_rep(m.group(3), people), _rep(m.group(4), people)]
        if deal and all(reps):
            return [intent("splits", deal=deal, splits=[{"rep": reps[0], "percent": int(m.group(1))},
                                                        {"rep": reps[1], "percent": int(m.group(2))}])]
    if deal and re.search(r"\bclose date\b", text, re.I) and date:
        return [intent("close_date", deal=deal, date=date[0])]
    if deal and re.search(r"\b(churn\w*|cancel\w*|terminat\w* (?:the|its) contract|leaves)\b", text, re.I):
        money_back = bool(re.search(r"\b(money back|refund\w*|pro[- ]rata)\b", text, re.I))
        return [intent("churn", deal=deal, date=date[0] if date else None, money_back=money_back)]
    if deal and re.search(r"\b(never pays|doesn'?t pay|not paid|overdue|late)\b", text, re.I):
        days = re.search(r"\b(\d{1,3})\s+days?\b", text, re.I)
        return [intent("overdue", deal=deal, days=int(days.group(1)) if days else None)]
    if m := re.search(r"\b(\w+)\s+(?:is|gets|was)?\s*(?:terminated|fired|let go|quits|resigns|leaves)\b", text, re.I):
        rep = _rep(m.group(1), people)
        if rep:
            return [intent("terminate", rep=rep, date=date[0] if date and len(date[0]) == 10 else None)]
    return []


def _grounded(intent: dict, text: str, deals, people) -> str | None:
    """Model intents get the extraction guards: every value must be written in (or named by) the sentence."""
    from quota.agent.extract import numbers_in, value_in_quote
    if intent["deal"] is not None and intent["deal"] not in deals:
        return f"no deal {intent['deal']}"
    if intent["date"] is not None and not value_in_quote(intent["date"], text):
        return f"{intent['date']} is not written in the sentence"
    if intent["days"] is not None and intent["days"] not in numbers_in(text):
        return f"{intent['days']} days is not written in the sentence"
    for split in intent["splits"] or []:
        if split["percent"] not in numbers_in(text) or _rep(split["rep"], people) is None:
            return f"split {split} is not written in the sentence"
    if intent["kind"] == "message" and (not intent["text"] or intent["text"] not in text):
        return "the message text is not in the sentence"
    return None


def model_intents(llm, text, deals, people, year) -> tuple[list[dict], str | None]:
    listed = "\n".join(f"- {d}: {a}" for d, a in sorted(deals.items()))
    staff = "\n".join(f"- {n}" for n in sorted(people.values()))
    out = llm.complete_json(INTENT_INSTRUCTIONS, f"Deals:\n{listed}\n\nPeople:\n{staff}\n\nYear: {year}\n"
                                                 f"Sentence:\n{text}", INTENT_SCHEMA, effort="low")
    kept, problems = [], []
    for intent in out["intents"]:
        for key in ("rep", "author"):
            if intent[key]:
                intent[key] = _rep(intent[key], people) or intent[key]
        for split in intent["splits"] or []:
            split["rep"] = _rep(split["rep"], people) or split["rep"]
        problem = _grounded(intent, text, deals, people)
        (problems.append(problem) if problem else kept.append(intent))
    return kept, "; ".join(problems) or out["unmapped"]


# ---------- intents -> events (code decides amounts and times) ----------

def _after(now: str, at: str) -> str:
    return at if at > now else clk.add_minutes(now, 1)


def _refund_cents(ro, deal_id, effective_date, now) -> int:
    """Pro rata: what the customer paid, less the months of the contract served through the churn month."""
    contract = queries.as_of(ro, "contracts", deal_id, now)
    billing = queries.fetch_all(ro, "SELECT kind, amount_cents FROM billing WHERE deal_id = ?", (deal_id,))
    paid = sum(b["amount_cents"] for b in billing if b["kind"] == "payment") - \
        sum(b["amount_cents"] for b in billing if b["kind"] == "refund")
    start, end = clk.period_of(contract["contract_date"]), clk.period_of(effective_date)
    served = (int(end[:4]) * 12 + int(end[5:])) - (int(start[:4]) * 12 + int(start[5:])) + 1
    return max(0, min(paid, paid - contract["acv_cents"] // 12 * served))


def build_events(session, intents: list[dict]) -> list[dict]:
    world, ro = session.world, session.ro
    now, today = world.clock.now(), world.clock.now()[:10]
    out = []
    for intent in intents:
        kind, deal = intent["kind"], intent["deal"]
        if kind in ("churn", "overdue", "close_date", "splits") and queries.as_of(ro, "deals", deal or "", now) is None:
            raise InjectError(f"no deal {deal!r} in this world")
        if kind == "churn":
            if queries.as_of(ro, "contracts", deal, now) is None:
                raise InjectError(f"{deal} has no signed contract to end")
            date = intent["date"] or today
            effective = clk.period_end(date) if len(date) == 7 else date
            if effective < today:
                raise InjectError(f"{effective} is before today ({today}); the ledger cannot be told about the past")
            refund = _refund_cents(ro, deal, effective, now) if intent["money_back"] else 0
            at = _after(now, f"{clk.add_days(effective + 'T17:00:00Z', -2)}")
            out.append({"at": at, "type": "customer.churned",
                        "payload": {"deal_id": deal, "effective_date": effective,
                                    "reason": "termination_for_convenience", "refund_cents": refund},
                        "summary": f"{deal} gives notice at {at}, effective {effective}; refund {fmt_usd(refund)} "
                                   f"(paid less months served)"})
            if refund:
                at = _after(at, f"{effective}T23:10:00Z")
                out.append({"at": at, "type": "refund.issued",
                            "payload": {"stripe_id": f"{deal}-ref-{effective}", "deal_id": deal,
                                        "amount_cents": refund, "date": effective},
                            "summary": f"billing refunds {fmt_usd(refund)} to {deal} at {at}"})
        elif kind == "overdue":
            billing = queries.fetch_all(ro, "SELECT kind, amount_cents FROM billing WHERE deal_id = ?", (deal,))
            due = sum(b["amount_cents"] if b["kind"] == "invoice" else -b["amount_cents"]
                      for b in billing if b["kind"] in ("invoice", "payment"))
            if due <= 0:
                raise InjectError(f"{deal} has no unpaid invoice to be late on")
            days = intent["days"] or 120
            at = clk.add_minutes(now, 1)
            out.append({"at": at, "type": "payment.overdue", "payload": {"deal_id": deal, "days": days},
                        "summary": f"{deal} is {days} days past due"})
        elif kind == "close_date":
            if not intent["date"] or len(intent["date"]) != 10:
                raise InjectError("a close date needs a day (e.g. March 31)")
            at = clk.add_minutes(now, 1)
            out.append({"at": at, "type": "deal.updated",
                        "payload": {"deal_id": deal, "fields": {"crm_close_date": intent["date"]}, "actor": "inject"},
                        "summary": f"CRM close date of {deal} edited to {intent['date']}"})
        elif kind == "splits":
            splits = [{"rep": s["rep"], "bps": s["percent"] * 100} for s in intent["splits"] or []]
            at = clk.add_minutes(now, 1)
            out.append({"at": at, "type": "deal.updated",
                        "payload": {"deal_id": deal, "fields": {"splits": splits}, "actor": "inject"},
                        "summary": f"CRM splits of {deal} edited to "
                                   + ", ".join(f"{s['rep']} {s['bps'] / 100:g}%" for s in splits)})
        elif kind == "advance":
            date = intent["date"] or ""
            target = clk.end_of_day(clk.period_end(date) if len(date) == 7 else date)
            if not date or target <= now:
                raise InjectError(f"the clock can only move forward from {now}")
            out.append({"at": target, "type": "clock.advance", "payload": {"to": target},
                        "summary": f"advance the clock to {target}"})
        elif kind == "terminate":
            person = queries.as_of(ro, "people", intent["rep"] or "", now)
            if person is None:
                raise InjectError(f"no person {intent['rep']!r} in this world")
            end = intent["date"] or today
            at = clk.add_minutes(now, 1)
            out.append({"at": at, "type": "rep.terminated",
                        "payload": {"rep_id": person["id"], "end_date": end, "state": person["state"] or ""},
                        "summary": f"HR records {person['name']} leaving on {end}"})
        elif kind == "message":
            at = clk.add_minutes(now, 1)
            out.append({"at": at, "type": "message.posted",
                        "payload": {"author": intent["author"], "to": ANALYST, "text": intent["text"]},
                        "summary": f"{intent['author']} writes to the commissions desk"})
    return out


def parse(session, text: str) -> dict:
    world = session.world
    deals = session.analyst.deal_names()
    people = {p["id"]: p["name"] for p in session.principals()}
    year = world.clock.now()[:4]
    source, problem = "rules", None
    if session.llm is not None:
        source = "model"
        try:
            intents, problem = model_intents(session.llm, text, deals, people, year)
        except LLMError as exc:
            intents, problem, source = rule_intents(text, deals, people, year), f"model unavailable: {exc}", "rules"
    else:
        intents = rule_intents(text, deals, people, year)
    if not intents:
        return {"unmapped": text, "reason": problem, "closest": closest(text), "source": source}
    try:
        events = build_events(session, intents)
    except InjectError as exc:
        return {"unmapped": text, "reason": str(exc), "closest": closest(text), "source": source}
    return {"events": events, "intents": intents, "source": source, "clock": world.clock.now()}


def closest(text: str, n: int = 3) -> list[str]:
    words = set(re.findall(r"[a-z]+", text.lower()))
    return sorted(EXAMPLES, key=lambda e: -len(words & set(re.findall(r"[a-z]+", e.lower()))))[:n]


# ---------- run ----------

def validate(session, events: list[dict]) -> list[str]:
    now = session.world.clock.now()
    problems = []
    for i, event in enumerate(events):
        at, kind, payload = event.get("at"), event.get("type"), event.get("payload") or {}
        if not isinstance(at, str) or at < now:
            problems.append(f"event {i}: at {at!r} is before the clock ({now})")
        if kind == "message.posted":
            people = {p["id"] for p in session.principals()}
            if payload.get("author") not in people or not payload.get("text"):
                problems.append(f"event {i}: a message needs a person as author and a text")
        elif kind == "clock.advance":
            problems += [f"event {i}: {p}" for p in ev.validate(kind, payload)]
        elif kind in ev.EVENT_SCHEMAS and kind not in ("approval.decided", "job.due", "period.lock_requested"):
            problems += [f"event {i}: {p}" for p in ev.validate(kind, payload)]
        else:
            problems.append(f"event {i}: {kind!r} cannot be injected")
    return problems


def run(session, events: list[dict]) -> dict:
    """Put the events on the calendar, then run the clock to the end of the day of the last one."""
    problems = validate(session, events)
    if problems:
        raise InjectError("; ".join(problems))
    world = session.world
    since = len(world.feed)
    targets = []
    for event in events:
        at, kind, payload = event["at"], event["type"], event["payload"]
        if kind == "clock.advance":
            targets.append(payload["to"])
            continue
        if kind == "message.posted":
            world.people.actions.append({"at": at, "who": payload["author"], "do": "message",
                                         "to": payload.get("to") or ANALYST, "text": payload["text"]})
        else:
            world.queue.schedule(at, kind, payload)
        targets.append(clk.end_of_day(at[:10]))
    world.run_until(max(targets))
    return {"clock": world.clock.now(), "timeline": views.timeline(world, since)}
