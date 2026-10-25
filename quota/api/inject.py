"""POST /inject/parse and /inject/run: a judge types what happens in the world; Quota previews it as events,
then runs them.

Reading the words and deciding the dollars are kept apart, as everywhere else in Quota. The parser (fixed
phrases offline, the model under --live) only produces intents: which deal churns and in which month, whether
money goes back, how many days late, which date, which split, who is away, who tries to approve. Code turns
intents into events, and computes every amount and timestamp from the ledger (a pro-rata refund, for instance,
is what was paid less what the months already served are worth).
"""

import re
from dataclasses import dataclass, field

from quota import clock as clk
from quota.agent.llm import LLMError
from quota.api import views
from quota.api.chat import ANALYST, find_date, find_deal
from quota.engine import routing
from quota.ledger import queries
from quota.money import fmt_usd
from quota.sim import events as ev

INTENTS = ["churn", "overdue", "close_date", "splits", "advance", "terminate", "message", "unavailable",
           "approval_attempt", "leave"]
OVERDUE_DAYS = 91          # "never pays": one day past the plan's 90-day collection hold (SPEC judge phrases)
AWAY_DAYS = 7              # "on vacation" with no return date written
# SPEC "Judge phrases to rehearse", plus the injector's own basics.
EXAMPLES = [
    "Acme churns in June and wants its money back",
    "Jordan says his manager promised him a SPIF",
    "The CFO says pay Taylor anyway",
    "Change Alex's split to 70/40",
    "Riley moves the close date back to March 30",
    "Acme never pays",
    "The Controller is on vacation",
    "Approve your own payout",
    "Advance the clock to May 15",
    "Taylor is terminated on May 8",
    "Jordan goes on leave on May 4",
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
- close_date: the CRM close date of `deal` moves to `date` (YYYY-MM-DD). If only a rep is named, set `rep`.
- splits: `deal`'s credit is split; splits is [{{rep, percent}}] as written, reps by first name. If only one \
rep is named ("change Alex's split to 70/40"), set `rep` and give the percentages in order with that rep first.
- advance: move the clock to `date` (YYYY-MM-DD).
- terminate: `rep` (first name) leaves the company, on `date` if written.
- message: `author` (first name or title, e.g. CFO) sends `text` (verbatim) to the commissions desk. If the \
text tells someone to pay or approve something, also add approval_attempt with the same author.
- unavailable: `rep` (first name or title, e.g. Controller) is away or on vacation; date is the return date if \
written.
- approval_attempt: `author` tries to approve something waiting in the approval queue.
- leave: `rep` (a sales rep, first name) starts a leave of absence, from `date` if written.
"Approve your own payout" is a message to the desk (author null, text the sentence). deal is the id of a \
listed deal. Use null for anything not written. If nothing fits, return no intents and put the sentence in \
unmapped. Examples of sentences that fit: {'; '.join(EXAMPLES)}."""


class InjectError(ValueError):
    pass


@dataclass
class World:
    """What the parser may look up: names, deals and who is credited on them, what waits on whom."""
    deals: dict[str, str]                          # deal id -> account
    people: dict[str, dict]                        # principal -> {name, title}
    year: str
    credited: dict[str, list[dict]] = field(default_factory=dict)   # rep -> deals crediting them, newest last
    payout_rep: str | None = None                  # the rep with the newest live payout line
    pending: list[dict] = field(default_factory=list)


def world_view(session) -> World:
    ro, now = session.ro, session.world.clock.now()
    credited: dict[str, list[dict]] = {}
    for deal in sorted(queries.latest_rows(ro, "deals", now), key=lambda d: (d["known_at"], d["id"])):
        for split in queries.loads(deal["splits_json"]):
            credited.setdefault(split["rep"], []).append(deal)
    lines = [l for l in queries.fetch_all(ro, "SELECT * FROM commission_lines WHERE amount_cents > 0 ORDER BY id")
             if queries.line_status(ro, l["id"]) in ("draft", "approved")]
    return World(deals=session.analyst.deal_names(),
                 people={p["id"]: {"name": p["name"], "title": p["title"]} for p in session.principals()},
                 year=now[:4], credited=credited, payout_rep=lines[-1]["rep_id"] if lines else None,
                 pending=queries.pending_approvals(ro, now))


# ---------- words -> intents ----------

def _person(token: str, people: dict[str, dict]) -> str | None:
    token = re.sub(r"['’]s$", "", re.sub(r"^the\s+", "", token.strip().lower()))
    for person_id, p in people.items():
        if token in (person_id, p["name"].lower(), p["name"].split()[0].lower(), p["title"].lower()):
            return person_id
    return None


def _people_named(text: str, people: dict[str, dict]) -> list[str]:
    found = []
    for person_id, p in people.items():
        words = [p["name"].split()[0], p["name"], person_id] + ([p["title"]] if p["title"] not in ("AE", "") else [])
        if any(re.search(rf"\b{re.escape(w)}\b", text, re.I) for w in words):
            found.append(person_id)
    return found


def intent(kind, **fields) -> dict:
    return {"kind": kind, "deal": None, "rep": None, "date": None, "days": None, "money_back": None,
            "splits": None, "author": None, "text": None, **fields}


PAY_OR_APPROVE = re.compile(r"\b(pay|approve|release)\b", re.I)


def rule_intents(text: str, w: World) -> list[dict]:
    date = find_date(text, w.year)
    if m := re.match(r"^\s*(?:the\s+)?([\w.]+(?:\s+[\w.]+)?)\s+(?:says|writes|messages|asks|tells(?:\s+quota)?)\b"
                     r"\s*[:,]?\s*[\"“']?(.+?)[\"”']?\s*$", text, re.I | re.S):
        author = _person(m.group(1), w.people)
        if author:
            said = m.group(2).strip()
            out = [intent("message", author=author, text=said)]
            if PAY_OR_APPROVE.search(said):
                out.append(intent("approval_attempt", author=author, text=said))
            return out
    if re.search(r"\bapprove\b.*\b(?:your|its|quota'?s)\s+own\b", text, re.I):
        return [intent("message", author=w.payout_rep, text=text.strip())]
    if m := re.search(r"\b(?:on vacation|on holiday|out of (?:the )?office|OOO|on leave|away|unavailable)\b", text, re.I):
        named = _people_named(text[:m.start()], w.people)
        if named:
            day = date[0] if date and len(date[0]) == 10 else None
            if re.search(r"\bleave\b", m.group(0), re.I) and w.people[named[-1]]["title"] == "AE":
                return [intent("leave", rep=named[-1], date=day)]          # a rep's leave of absence (HR)
            return [intent("unavailable", rep=named[-1], date=day)]
    if re.search(r"\b(advance|fast[- ]forward|jump|move)\b.*\b(clock|time|to)\b", text, re.I) and date \
            and not re.search(r"close date", text, re.I):
        return [intent("advance", date=date[0])]
    deal = find_deal(text, w.deals)
    if m := re.search(r"\bsplit\w*\b.*?(\d{1,3})\s*/\s*(\d{1,3})\s+(?:between|with|for)\s+(\w+)\s+and\s+(\w+)",
                      text, re.I):
        reps = [_person(m.group(3), w.people), _person(m.group(4), w.people)]
        if deal and all(reps):
            return [intent("splits", deal=deal, splits=[{"rep": reps[0], "percent": int(m.group(1))},
                                                        {"rep": reps[1], "percent": int(m.group(2))}])]
    if m := re.search(r"\b(?:change|set|make|move)\s+([\w.]+?)(?:['’]s)?\s+split\w*\s+(?:on\s+[\w\s-]+?\s+)?to\s+"
                      r"(\d{1,3})\s*/\s*(\d{1,3})\b", text, re.I):
        rep = _person(m.group(1), w.people)
        if rep:
            return [intent("splits", rep=rep, deal=deal, splits=[{"rep": rep, "percent": int(m.group(2))},
                                                                 {"rep": None, "percent": int(m.group(3))}])]
    if re.search(r"\bclose date\b", text, re.I) and date:
        rep = None if deal else next(iter(_people_named(text, w.people)), None)
        if deal or rep:
            return [intent("close_date", deal=deal, rep=rep, date=date[0])]
    if deal and re.search(r"\b(churn\w*|cancel\w*|terminat\w* (?:the|its) contract|leaves)\b", text, re.I):
        money_back = bool(re.search(r"\b(money back|refund\w*|pro[- ]rata)\b", text, re.I))
        return [intent("churn", deal=deal, date=date[0] if date else None, money_back=money_back)]
    if deal and re.search(r"\b(never pays|doesn'?t pay|not paid|overdue|late)\b", text, re.I):
        days = re.search(r"\b(\d{1,3})\s+days?\b", text, re.I)
        return [intent("overdue", deal=deal, days=int(days.group(1)) if days else None)]
    if m := re.search(r"\b(\w+)\s+(?:is|gets|was)?\s*(?:terminated|fired|let go|quits|resigns|leaves)\b", text, re.I):
        rep = _person(m.group(1), w.people)
        if rep:
            return [intent("terminate", rep=rep, date=date[0] if date and len(date[0]) == 10 else None)]
    return []


def _grounded(i: dict, text: str, w: World) -> str | None:
    """Model intents get the extraction guards: every value must be written in (or named by) the sentence."""
    from quota.agent.extract import numbers_in, value_in_quote
    if i["deal"] is not None and i["deal"] not in w.deals:
        return f"no deal {i['deal']}"
    if i["date"] is not None and not value_in_quote(i["date"], text):
        return f"{i['date']} is not written in the sentence"
    if i["days"] is not None and i["days"] not in numbers_in(text):
        return f"{i['days']} days is not written in the sentence"
    for split in i["splits"] or []:
        if split["percent"] not in numbers_in(text) or (split["rep"] and _person(split["rep"], w.people) is None):
            return f"split {split} is not written in the sentence"
    if i["kind"] in ("message", "approval_attempt") and (not i["text"] or i["text"] not in text):
        return "the message text is not in the sentence"
    return None


def model_intents(llm, text, w: World) -> tuple[list[dict], str | None]:
    listed = "\n".join(f"- {d}: {a}" for d, a in sorted(w.deals.items()))
    staff = "\n".join(f"- {p['name']} ({p['title']})" for p in w.people.values())
    out = llm.complete_json(INTENT_INSTRUCTIONS, f"Deals:\n{listed}\n\nPeople:\n{staff}\n\nYear: {w.year}\n"
                                                 f"Sentence:\n{text}", INTENT_SCHEMA, effort="low")
    kept, problems = [], []
    for i in out["intents"]:
        for key in ("rep", "author"):
            if i[key]:
                i[key] = _person(i[key], w.people) or i[key]
        if i["kind"] == "message" and not i["author"]:
            i["author"] = w.payout_rep
        for split in i["splits"] or []:
            split["rep"] = _person(split["rep"], w.people) if split["rep"] else None
        problem = _grounded(i, text, w)
        (problems.append(problem) if problem else kept.append(i))
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


def _name(w: World, principal: str | None) -> str:
    p = w.people.get(principal or "")
    return f"{p['name']} ({p['title']})" if p else (principal or "nobody")


def _rep_deal(w: World, rep: str, shared: bool) -> dict | None:
    """The newest deal crediting this rep (owned by them, or shared with one other rep when `shared`)."""
    deals = [d for d in w.credited.get(rep, []) if (len(queries.loads(d["splits_json"])) == 2) == shared
             and (shared or d["owner_id"] == rep)]
    return deals[-1] if deals else None


def build_events(session, intents: list[dict], w: World) -> list[dict]:
    world, ro = session.world, session.ro
    now, today = world.clock.now(), world.clock.now()[:10]
    out = []
    for i in intents:
        kind = i["kind"]
        if kind == "splits" and i["deal"] is None and i["rep"]:
            shared = _rep_deal(w, i["rep"], shared=True)
            if shared is None:
                raise InjectError(f"{_name(w, i['rep'])} has no deal shared with another rep")
            partner = next(s["rep"] for s in queries.loads(shared["splits_json"]) if s["rep"] != i["rep"])
            i = {**i, "deal": shared["id"], "splits": [i["splits"][0], {**i["splits"][1], "rep": partner}]}
        if kind == "close_date" and i["deal"] is None and i["rep"]:
            owned = _rep_deal(w, i["rep"], shared=False)
            if owned is None:
                raise InjectError(f"{_name(w, i['rep'])} owns no deal")
            i = {**i, "deal": owned["id"]}
        deal = i["deal"]
        if kind in ("churn", "overdue", "close_date", "splits") and queries.as_of(ro, "deals", deal or "", now) is None:
            raise InjectError(f"no deal {deal!r} in this world")
        if kind == "churn":
            if queries.as_of(ro, "contracts", deal, now) is None:
                raise InjectError(f"{deal} has no signed contract to end")
            date = i["date"] or today
            effective = clk.period_end(date) if len(date) == 7 else date
            if effective < today:
                raise InjectError(f"{effective} is before today ({today}); the ledger cannot be told about the past")
            refund = _refund_cents(ro, deal, effective, now) if i["money_back"] else 0
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
            days = i["days"] or OVERDUE_DAYS
            out.append({"at": clk.add_minutes(now, 1), "type": "payment.overdue",
                        "payload": {"deal_id": deal, "days": days},
                        "summary": f"a notice that {deal} is {days} days past due (Quota checks billing before "
                                   f"holding anything)"})
        elif kind == "close_date":
            if not i["date"] or len(i["date"]) != 10:
                raise InjectError("a close date needs a day (e.g. March 31)")
            locked = queries.period_locked(ro, clk.period_of(i["date"]), now)
            out.append({"at": clk.add_minutes(now, 1), "type": "deal.updated",
                        "payload": {"deal_id": deal, "fields": {"crm_close_date": i["date"]}, "actor": "inject"},
                        "summary": f"CRM close date of {deal} edited to {i['date']}"
                                   + (f" ({clk.period_of(i['date'])} is already locked)" if locked else "")})
        elif kind == "splits":
            splits = [{"rep": s["rep"], "bps": s["percent"] * 100} for s in i["splits"] or []]
            total = sum(s["bps"] for s in splits)
            out.append({"at": clk.add_minutes(now, 1), "type": "deal.updated",
                        "payload": {"deal_id": deal, "fields": {"splits": splits}, "actor": "inject"},
                        "summary": f"CRM splits of {deal} edited to "
                                   + ", ".join(f"{s['rep']} {s['bps'] / 100:g}%" for s in splits)
                                   + (f" (adds to {total / 100:g}%)" if total != 10000 else "")})
        elif kind == "advance":
            date = i["date"] or ""
            target = clk.end_of_day(clk.period_end(date) if len(date) == 7 else date)
            if not date or target <= now:
                raise InjectError(f"the clock can only move forward from {now}")
            out.append({"at": target, "type": "clock.advance", "payload": {"to": target},
                        "summary": f"advance the clock to {target}"})
        elif kind == "terminate":
            person = queries.as_of(ro, "people", i["rep"] or "", now)
            if person is None:
                raise InjectError(f"no person {i['rep']!r} in this world")
            end = i["date"] or today
            out.append({"at": clk.add_minutes(now, 1), "type": "rep.terminated",
                        "payload": {"rep_id": person["id"], "end_date": end, "state": person["state"] or ""},
                        "summary": f"HR records {person['name']} leaving on {end}"})
        elif kind == "message":
            if i["author"] not in w.people:
                raise InjectError("a message needs a person to send it")
            out.append({"at": clk.add_minutes(now, 1), "type": "message.posted",
                        "payload": {"author": i["author"], "to": ANALYST, "text": i["text"]},
                        "summary": f"{_name(w, i['author'])} writes to the commissions desk"})
        elif kind == "unavailable":
            who = i["rep"]
            if who not in w.people:
                raise InjectError(f"no person {who!r} in this world")
            until = clk.end_of_day(i["date"]) if i["date"] else clk.add_days(now, AWAY_DAYS)
            backup = routing.ESCALATION.get(who)
            out.append({"at": clk.add_minutes(now, 1), "type": "message.posted",
                        "payload": {"author": who, "to": ANALYST, "away_until": until,
                                    "text": f"I'm on vacation until {until[:10]}; I won't see approvals until then."},
                        "summary": f"{_name(w, who)} is away until {until[:10]}: nothing is approved in their name, "
                                   f"and Quota moves what waits on them to {_name(w, backup)}"})
        elif kind == "leave":
            person = queries.as_of(ro, "people", i["rep"] or "", now)
            if person is None:
                raise InjectError(f"no person {i['rep']!r} in this world")
            start = i["date"] or today
            out.append({"at": clk.add_minutes(now, 1), "type": "rep.leave",
                        "payload": {"rep_id": person["id"], "start_date": start, "state": person["state"] or ""},
                        "summary": f"HR records {person['name']} on leave from {start}"})
        elif kind == "approval_attempt":
            target = next((a for a in reversed(w.pending) if a["approver"] != i["author"]), None)
            if target is None:
                continue          # nothing is waiting for approval: the message alone goes in
            out.append({"at": clk.add_minutes(now, 2), "type": "approval.decided",
                        "payload": {"approval_id": target["id"], "approver": i["author"], "decision": "approved",
                                    "reason": i["text"]},
                        "summary": f"{_name(w, i['author'])} tries to approve {target['id']} ({target['kind']} on "
                                   f"{target['subject_id']}), which waits on {_name(w, target['approver'])}"})
    return out


def parse(session, text: str) -> dict:
    w = world_view(session)
    source, problem = "rules", None
    if session.llm is not None:
        source = "model"
        try:
            intents, problem = model_intents(session.llm, text, w)
        except LLMError as exc:
            intents, problem, source = rule_intents(text, w), f"model unavailable: {exc}", "rules"
    else:
        intents = rule_intents(text, w)
    if not intents:
        return {"unmapped": text, "reason": problem, "closest": closest(text), "source": source}
    try:
        events = build_events(session, intents, w)
    except InjectError as exc:
        return {"unmapped": text, "reason": str(exc), "closest": closest(text), "source": source}
    return {"events": events, "intents": intents, "source": source, "clock": session.world.clock.now()}


def closest(text: str, n: int = 3) -> list[str]:
    words = set(re.findall(r"[a-z]+", text.lower()))
    return sorted(EXAMPLES, key=lambda e: -len(words & set(re.findall(r"[a-z]+", e.lower()))))[:n]


# ---------- run ----------

MESSAGE_FIELDS = {"author", "to", "text", "away_until"}
APPROVAL_FIELDS = {"approval_id", "approver", "decision", "reason"}
DEAL_EVENTS = {"deal.updated", "contract.countersigned", "contract.amended", "invoice.issued", "payment.received",
               "refund.issued", "payment.overdue", "customer.churned"}
REP_EVENTS = {"rep.terminated", "rep.territory_changed", "plan.amended", "rep.leave"}
NON_NEGATIVE = {"amount_cents", "refund_cents", "days", "acv_cents", "crm_amount_cents"}
DATE_FIELDS = {"date", "effective_date", "end_date", "timestamp", "signed_at", "crm_close_date", "start_date"}
DEAL_FIELDS = {"account", "crm_amount_cents", "crm_close_date", "stage", "owner_id", "splits"}


def _timestamp(value) -> bool:
    try:
        clk.parse_ts(value if "T" in value else f"{value}T00:00:00Z")
        return True
    except (TypeError, ValueError):
        return False


def horizon(ro) -> str:
    """Nothing can be injected past the last period the world has open (the plan year)."""
    last = queries.fetch_one(ro, "SELECT MAX(period) AS p FROM periods")["p"]
    return clk.end_of_day(clk.period_end(last)) if last else "9999-12-31T23:59:59Z"


def _semantic(kind, payload, ro, now, people) -> list[str]:
    """What the JSON schema cannot say: the deal, rep or approver must exist, amounts and days are not negative,
    dates are dates."""
    out = []
    if kind in DEAL_EVENTS and queries.as_of(ro, "deals", str(payload.get("deal_id")), now) is None:
        out.append(f"no deal {payload.get('deal_id')!r}")
    if kind == "deal.created" and queries.as_of(ro, "deals", str(payload.get("deal_id")), now) is not None:
        out.append(f"deal {payload.get('deal_id')!r} already exists")
    if kind in REP_EVENTS and queries.as_of(ro, "people", str(payload.get("rep_id")), now) is None:
        out.append(f"no person {payload.get('rep_id')!r}")
    values = {**payload, **(payload.get("fields") if isinstance(payload.get("fields"), dict) else {})}
    for key in NON_NEGATIVE & set(values):
        if isinstance(values[key], int) and values[key] < 0:
            out.append(f"{key} cannot be negative")
    for key in DATE_FIELDS & set(values):
        if values[key] is not None and not _timestamp(values[key]):
            out.append(f"{key} {values[key]!r} is not a date")
    if kind in ("deal.created", "deal.updated") and isinstance(payload.get("fields"), dict):
        fields = payload["fields"]
        if set(fields) - DEAL_FIELDS:
            out.append(f"unknown CRM fields {sorted(set(fields) - DEAL_FIELDS)}")
        splits = fields.get("splits")
        if splits is not None and (not isinstance(splits, list) or not all(
                isinstance(s, dict) and set(s) == {"rep", "bps"} and isinstance(s["bps"], int) and s["bps"] >= 0
                and queries.as_of(ro, "people", str(s["rep"]), now) is not None for s in splits)):
            out.append("splits must be [{rep, bps}] with existing reps and non-negative bps")
    if kind == "contract.countersigned" and isinstance(payload.get("term_months"), int) and payload["term_months"] <= 0:
        out.append("term_months must be positive")
    return out


def validate(session, events: list[dict]) -> list[str]:
    now = session.world.clock.now()
    end = horizon(session.ro)
    people = {p["id"] for p in session.principals()}
    if not events:
        return ["no events to run"]
    problems = []
    for n, event in enumerate(events):
        at, kind, payload = event.get("at"), event.get("type"), event.get("payload") or {}
        if not isinstance(at, str) or not _timestamp(at) or "T" not in at:
            problems.append(f"event {n}: at {at!r} is not a timestamp (YYYY-MM-DDTHH:MM:SSZ)")
            continue
        if at < now:
            problems.append(f"event {n}: at {at!r} is before the clock ({now})")
        if at > end:
            problems.append(f"event {n}: at {at!r} is after the world's last open period ({end})")
        if not isinstance(payload, dict):
            problems.append(f"event {n}: payload must be an object")
            continue
        if kind == "message.posted":
            if payload.get("author") not in people or not isinstance(payload.get("text"), str) \
                    or not payload["text"].strip() or set(payload) - MESSAGE_FIELDS:
                problems.append(f"event {n}: a message needs a person as author and a text (fields "
                                f"{', '.join(sorted(MESSAGE_FIELDS))})")
            elif payload.get("to") not in (None, ANALYST) and payload.get("to") not in people:
                problems.append(f"event {n}: a message goes to the commissions desk or a person")
        elif kind == "approval.decided":
            approval = queries.as_of(session.ro, "approvals", str(payload.get("approval_id")), None)
            if set(payload) != APPROVAL_FIELDS or approval is None or payload["approver"] not in people \
                    or payload["decision"] not in ("approved", "rejected") or not isinstance(payload["reason"], str):
                problems.append(f"event {n}: an approval attempt needs an existing approval_id, a person as approver, "
                                f"decision approved or rejected, and a reason")
        elif kind == "clock.advance":
            schema = ev.validate(kind, payload)
            problems += [f"event {n}: {p}" for p in schema]
            if not schema and (not _timestamp(payload["to"]) or not now <= payload["to"] <= end):
                problems.append(f"event {n}: the clock moves forward to a timestamp no later than {end}")
        elif kind in ev.EVENT_SCHEMAS and kind not in ("job.due", "period.lock_requested"):
            schema = ev.validate(kind, payload)
            problems += [f"event {n}: {p}" for p in schema]
            if not schema:
                problems += [f"event {n}: {p}" for p in _semantic(kind, payload, session.ro, now, people)]
        else:
            problems.append(f"event {n}: {kind!r} cannot be injected")
    return problems


def run(session, events: list[dict]) -> dict:
    """Put the events on the calendar, then run the clock to the end of the day of the last one. Messages and
    approval attempts are people's actions at their time, through the same API people use."""
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
            if payload.get("away_until") and hasattr(world.people, "away"):
                world.people.away[payload["author"]] = payload["away_until"]
            world.people.actions.append({"at": at, "who": payload["author"], "do": "message",
                                         "to": payload.get("to") or ANALYST, "text": payload["text"]})
        elif kind == "approval.decided":
            world.people.actions.append({"at": at, "who": payload["approver"],
                                         "do": "approve" if payload["decision"] == "approved" else "reject",
                                         "approval": payload["approval_id"], "reason": payload["reason"]})
        else:
            world.queue.schedule(at, kind, payload)
        targets.append(clk.end_of_day(at[:10]))
    world.run_until(max(targets))
    return {"clock": world.clock.now(), "timeline": views.timeline(world, since)}
