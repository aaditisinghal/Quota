"""message -> Claim ; contract text -> ContractFacts.

The model (or, under --scripted, the demo script) proposes; these guards decide whether the proposal may be
used. A claim's quote must be a verbatim substring of the message, every number it asserts must appear in
that quote, and refs must appear in the message. A failed claim becomes `other` and earns one clarifying
question. A claim only carries authority when its author holds the capability for it.
"""

import re
from dataclasses import dataclass, field

from quota import config
from quota.agent.llm import MONTHS, LLMError, schema_errors

CLAIM_KINDS = ["dispute", "exception_grant", "spiff_claim", "escalate", "approval", "rejection", "other"]
CLAIM_FIELDS = ["booking_date", "base", "splits", "amount"]
CLAIM_AUTHORITY = {"exception_grant": "approve:plan_exception", "spiff_claim": "approve:plan_exception"}


def _nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


CLAIM_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": CLAIM_KINDS},
        "deal_ref": _nullable({"type": "string"}),
        "line_ref": _nullable({"type": "string"}),
        "field": _nullable({"type": "string", "enum": CLAIM_FIELDS}),
        "asserted_value": _nullable({"type": "string"}),
        "amount_cents": _nullable({"type": "integer"}),
        "quote": {"type": "string"},
    },
    "required": ["kind", "deal_ref", "line_ref", "field", "asserted_value", "amount_cents", "quote"],
    "additionalProperties": False,
}

CLAIM_INSTRUCTIONS = """\
You read one chat message sent to Quota, the commissions desk of a software company, and record what the \
author claims. Quota's code checks every claim against contracts, the CRM and the comp plan; you only report \
what the message says.
- kind: dispute (a commission is wrong), exception_grant (someone grants an exception to the comp plan), \
spiff_claim (a promised bonus), escalate (the author wants the matter escalated), approval or rejection \
(approving or rejecting something in chat, or asking Quota to approve something), other (anything else, or \
unclear).
- quote: the shortest span of the message that carries the claim, copied character for character.
- asserted_value: the value the author says is right, only if it is written in the quote. Dates are \
YYYY-MM-DD, or YYYY-MM when only a month is given; use the year given with the message when none is written.
- amount_cents: a dollar amount written in the quote, times 100.
- deal_ref: the id of a listed deal whose account the message names. line_ref: a commission line (CL-0001) \
or approval id (A-0001) written in the message.
- Use null for anything the message does not say."""


@dataclass(frozen=True)
class Claim:
    kind: str
    deal_ref: str | None = None
    line_ref: str | None = None
    field: str | None = None
    asserted_value: str | None = None
    amount_cents: int | None = None
    quote: str = ""
    problem: str | None = None      # why a proposed claim was demoted to `other`

    def to_dict(self) -> dict:
        return {"kind": self.kind, "deal_ref": self.deal_ref, "line_ref": self.line_ref, "field": self.field,
                "asserted_value": self.asserted_value, "amount_cents": self.amount_cents, "quote": self.quote}


# ---------- guards ----------

_NUMBER = re.compile(r"\$?(\d[\d,]*(?:\.\d+)?)\s*([km]\b)?", re.I)
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ISO_MONTH = re.compile(r"^(\d{4})-(\d{2})$")


def numbers_in(text: str) -> set[float]:
    out = set()
    for digits, suffix in _NUMBER.findall(text):
        value = float(digits.replace(",", ""))
        out.add(value * {"k": 1_000, "m": 1_000_000}.get(suffix.lower(), 1))
    return out


def _month_pattern(month: int) -> str:
    name = MONTHS[month - 1]
    return rf"(?:{name}|{name[:3]}{'|sept' if month == 9 else ''})\.?"


def value_in_quote(value: str, quote: str) -> bool:
    """Dates match a written month/day ("Mar 31", "3/31", "March 31st"); a bare month matches its name;
    anything else needs every number in it to be written in the quote ($, commas and k/m normalized)."""
    date = _ISO_DATE.match(value)
    if date:
        y, m, d = (int(x) for x in date.groups())
        month = _month_pattern(m)
        forms = [rf"\b{month}\s+0?{d}(?:st|nd|rd|th)?\b", rf"\b0?{d}(?:st|nd|rd|th)?\s+(?:of\s+)?{month}\b",
                 rf"\b0?{m}/0?{d}\b", re.escape(value)]
        return any(re.search(f, quote, re.I) for f in forms)
    period = _ISO_MONTH.match(value)
    if period:
        y, m = (int(x) for x in period.groups())
        return bool(re.search(rf"\b{_month_pattern(m)}\b", quote, re.I) or value in quote)
    wanted = numbers_in(value)
    return bool(wanted) and wanted <= numbers_in(quote)


def verify_claim(raw: dict, text: str) -> Claim:
    """Accept a proposed claim only if it is grounded in the message; otherwise demote it to `other`."""
    problems = schema_errors(raw, CLAIM_SCHEMA)
    if problems:
        return Claim("other", problem="; ".join(problems))
    claim = Claim(**raw)
    if claim.kind == "other":
        return claim
    problem = None
    if not claim.quote or claim.quote not in text:
        problem = "the quote is not in the message"
    elif claim.asserted_value is not None and not value_in_quote(claim.asserted_value, claim.quote):
        problem = f"{claim.asserted_value} is not written in the quote"
    elif claim.amount_cents is not None and claim.amount_cents / 100 not in numbers_in(claim.quote):
        problem = f"amount {claim.amount_cents} cents is not written in the quote"
    else:
        for ref in (claim.deal_ref, claim.line_ref):
            if ref is not None and ref.lower() not in text.lower():
                problem = f"{ref} is not named in the message"
    if problem:
        return Claim("other", quote=claim.quote, problem=problem)
    return claim


def has_authority(claim: Claim, author: str) -> bool:
    """A claim only becomes authority when its author holds the capability for it."""
    needed = CLAIM_AUTHORITY.get(claim.kind)
    return needed is None or config.has_capability(config.PRINCIPALS, author, needed)


# ---------- extractors ----------

def claim_prompt(text: str, author: str, year: str, deals: dict[str, str]) -> str:
    listed = "\n".join(f"- {deal_id}: {account}" for deal_id, account in sorted(deals.items()))
    return f"{CLAIM_INSTRUCTIONS}\n\nDeals:\n{listed}\n\nYear: {year}\nAuthor: {author}\nMessage:\n{text}"


class ScriptedExtractor:
    """--scripted: what the model would return, written in the demo script and keyed by message text.
    It goes through the same guards as a model answer."""

    def __init__(self, table: dict[str, dict]):
        self.table = table

    def raw_claim(self, text, author, year, deals) -> dict:
        found = self.table.get(text)
        return dict(found) if found else {"kind": "other", "deal_ref": None, "line_ref": None, "field": None,
                                          "asserted_value": None, "amount_cents": None, "quote": ""}


class ModelExtractor:
    def __init__(self, llm):
        self.llm = llm

    def raw_claim(self, text, author, year, deals) -> dict:
        return self.llm.extract(CLAIM_SCHEMA, claim_prompt(text, author, year, deals))


def extract_claim(extractor, text: str, author: str, year: str, deals: dict[str, str]) -> Claim:
    try:
        raw = extractor.raw_claim(text, author, year, deals)
    except LLMError as exc:
        return Claim("other", problem=f"extraction failed: {exc}")
    return verify_claim(raw, text)


# ---------- contracts ----------

CONTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "acv_cents": {"type": "integer"},
        "term_months": {"type": "integer"},
        "contract_date": _nullable({"type": "string"}),
        "countersigned_at": _nullable({"type": "string"}),
        "billing": _nullable({"type": "string"}),
        "quotes": {
            "type": "object",
            "properties": {k: _nullable({"type": "string"}) for k in
                           ("acv", "term", "contract_date", "countersigned_at")},
            "required": ["acv", "term", "contract_date", "countersigned_at"],
            "additionalProperties": False,
        },
    },
    "required": ["acv_cents", "term_months", "contract_date", "countersigned_at", "billing", "quotes"],
    "additionalProperties": False,
}

CONTRACT_INSTRUCTIONS = """\
Read this signed customer contract and extract its commercial terms. acv_cents is the first-year annual \
contract value in dollars times 100; term_months is the subscription term. contract_date is the date field \
written in the contract (YYYY-MM-DD). countersigned_at is the vendor's countersignature time as \
YYYY-MM-DDTHH:MM:SSZ, or null if the text does not show it. For each value, put the exact sentence or phrase \
it comes from, copied character for character, in quotes."""


class ExtractionFailed(ValueError):
    pass


@dataclass(frozen=True)
class ContractFacts:
    acv_cents: int
    term_months: int
    contract_date: str | None
    countersigned_at: str | None
    billing: str | None
    doc_ref: str
    quotes: dict = field(default_factory=dict)


def verify_contract(raw: dict, text: str, doc_ref: str) -> ContractFacts:
    problems = schema_errors(raw, CONTRACT_SCHEMA)
    quotes = raw.get("quotes") or {}
    checks = [("acv", str(raw.get("acv_cents", 0) / 100)), ("term", str(raw.get("term_months"))),
              ("contract_date", raw.get("contract_date")), ("countersigned_at", raw.get("countersigned_at"))]
    for key, value in checks:
        if problems or value is None:
            continue
        quote = quotes.get(key)
        if not quote or quote not in text:
            problems.append(f"{key}: quote is not in the contract")
        elif key == "countersigned_at":
            if not (value[:10] in quote or value_in_quote(value[:10], quote)):
                problems.append(f"{key}: {value} is not written in its quote")
        elif not value_in_quote(value, quote):
            problems.append(f"{key}: {value} is not written in its quote")
    if problems:
        raise ExtractionFailed("; ".join(problems))
    return ContractFacts(raw["acv_cents"], raw["term_months"], raw["contract_date"], raw["countersigned_at"],
                         raw["billing"], doc_ref, quotes)


def extract_contract(llm, text: str, doc_ref: str) -> ContractFacts:
    return verify_contract(llm.extract(CONTRACT_SCHEMA, f"{CONTRACT_INSTRUCTIONS}\n\nContract ({doc_ref}):\n{text}"),
                           text, doc_ref)
