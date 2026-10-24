"""POST /chat: someone speaks as a principal; Quota's seats run; the reply comes back.

`approve A-0002 [reason]` / `reject A-0002 [reason]` is the approval queue's button, typed: it goes through
PeopleAPI as that principal, and the write path decides (an approver over their limit is routed on; anyone
else is refused with reason codes). Anything else is a chat message to the commissions analyst.

Without a model (`serve` without --live), chat is read by RuleExtractor: the demo script's table first, then a
few fixed patterns. It only proposes a claim; the same extraction guards as for a model decide whether it can
be used, and anything it cannot read gets the analyst's one clarifying question.
"""

import re

from quota import clock as clk
from quota.agent.extract import _mentioned
from quota.agent.llm import MONTHS
from quota.api import views
from quota.ledger import queries

ANALYST = "seat:commissions_analyst"
COMMAND = re.compile(r"^\s*(approve|reject)\s+(A-\d{4})\b[\s:,.\-]*(.*)$", re.I | re.S)

_MONTH = "|".join(sorted({m for full in MONTHS for m in (full, full[:3], "sept")}, key=len, reverse=True))
_ISO_DAY = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_NAMED_DAY = re.compile(rf"\b({_MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", re.I)
_DAY_OF = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH})\b", re.I)
_SLASH_DAY = re.compile(r"\b(\d{1,2})/(\d{1,2})\b")
_IN_MONTH = re.compile(rf"\b(?:in|during|for|of)\s+({_MONTH})\b|\b({'|'.join(m for m in MONTHS if m != 'may')})\b",
                       re.I)
_MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*([km]\b)?", re.I)
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")

GRANT_WORDS = re.compile(r"\b(honou?r|grant|make an exception|exception|allow)\b", re.I)
SPIFF_WORDS = re.compile(r"\b(spiff?s?|bonus)\b", re.I)


def month_number(name: str) -> int:
    return next(i for i, m in enumerate(MONTHS, start=1) if m.startswith(name.lower()[:3]))


def find_date(text: str, year: str) -> tuple[str, str] | None:
    """(ISO value, the words it came from): a day (2026-03-31) when one is written, else a month (2026-03)."""
    if m := _ISO_DAY.search(text):
        return m.group(0), m.group(0)
    if m := _NAMED_DAY.search(text):
        return f"{year}-{month_number(m.group(1)):02d}-{int(m.group(2)):02d}", m.group(0)
    if m := _DAY_OF.search(text):
        return f"{year}-{month_number(m.group(2)):02d}-{int(m.group(1)):02d}", m.group(0)
    if (m := _SLASH_DAY.search(text)) and 1 <= int(m.group(1)) <= 12:
        return f"{year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}", m.group(0)
    if m := _IN_MONTH.search(text):
        return f"{year}-{month_number(m.group(1) or m.group(2)):02d}", m.group(0)
    return None


def find_money(text: str) -> tuple[int, str] | None:
    if m := _MONEY.search(text):
        value = float(m.group(1).replace(",", "")) * {"k": 1_000, "m": 1_000_000}.get((m.group(2) or "").lower(), 1)
        return round(value * 100), m.group(0)
    return None


def sentence_with(text: str, words: str) -> str:
    """The sentence of `text` that contains `words`, verbatim (the claim's quote)."""
    for sentence in _SENTENCE.findall(text):
        if words in sentence:
            return sentence.strip()
    return text.strip()


def find_deal(text: str, deals: dict[str, str]) -> str | None:
    named = [d for d, account in deals.items() if _mentioned(d, text) or _mentioned(account, text)]
    return max(named, key=len) if named else None


def blank_claim(kind="other", **fields) -> dict:
    return {"kind": kind, "deal_ref": None, "line_ref": None, "field": None, "asserted_value": None,
            "amount_cents": None, "quote": "", **fields}


class RuleExtractor:
    def __init__(self, table: dict[str, dict] | None = None):
        self.table = dict(table or {})

    def raw_claim(self, text, author, year, deals) -> dict:
        if text in self.table:
            return dict(self.table[text])
        if m := re.search(r"\b(approve|reject)\b[^.!?]*?\b(A-\d{4})\b", text, re.I):
            kind = "approval" if m.group(1).lower() == "approve" else "rejection"
            return blank_claim(kind, line_ref=m.group(2).upper(), quote=m.group(0))
        if m := re.search(r"\bescalate\b", text, re.I):
            return blank_claim("escalate", quote=m.group(0))
        deal = find_deal(text, deals)
        line = re.search(r"\bCL-\d{4}\b", text, re.I)
        date, money = find_date(text, year), find_money(text)
        if spiff := SPIFF_WORDS.search(text):
            return blank_claim("spiff_claim", deal_ref=deal, field="amount", amount_cents=money[0] if money else None,
                               quote=sentence_with(text, money[1] if money else spiff.group(0)))
        kind = "exception_grant" if GRANT_WORDS.search(text) else "dispute"
        ref = {"deal_ref": deal, "line_ref": line.group(0).upper() if line else None}
        if date and (deal or line or re.search(r"\b(closed|signed|booked|count)\b", text, re.I)):
            return blank_claim(kind, **ref, field="booking_date", asserted_value=date[0],
                               quote=sentence_with(text, date[1]))
        if money and (deal or line):
            return blank_claim(kind, **ref, field="base", amount_cents=money[0], quote=sentence_with(text, money[1]))
        if re.search(r"\bapprove\b", text, re.I):
            return blank_claim("approval", quote=re.search(r"\bapprove\b", text, re.I).group(0))
        return blank_claim()


def chat(session, as_principal: str, text: str, to: str | None = None) -> dict:
    """Run one chat turn as `as_principal`. The clock moves one minute first, so the message is new."""
    world = session.world
    people = {p["id"] for p in session.principals()}
    if as_principal not in people:
        raise ValueError(f"{as_principal} is not a person in this world; choose one of {', '.join(sorted(people))}")
    text = (text or "").strip()
    if not text:
        raise ValueError("empty message")
    since = len(world.feed)
    world.run_until(clk.add_minutes(world.clock.now(), 1))
    started = world.clock.now()
    command = COMMAND.match(text)
    message_id = None
    if command and queries.as_of(world.ro, "approvals", command.group(2).upper(), None) is not None:
        verb, approval_id = command.group(1).lower(), command.group(2).upper()
        reason = command.group(3).strip() or f"{verb.capitalize()}d in chat."
        getattr(world.api, verb)(as_principal, approval_id, reason)
        action = {"kind": verb, "approval_id": approval_id}
    else:
        message_id = world.api.message(as_principal, to or ANALYST, text)
        action = {"kind": "message", "message_id": message_id, "to": to or ANALYST}
    world.settle()
    replies = [m for m in session.messages(as_principal)
               if m["to"] == as_principal and m["at"] >= started and m["author"].startswith("seat:")]
    return {"action": action, "clock": world.clock.now(), "replies": replies,
            "timeline": views.timeline(world, since)}
