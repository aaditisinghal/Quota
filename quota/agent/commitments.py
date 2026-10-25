"""Is this message an off-contract promise? (ELASTIC_TASKS E4)

1. The model reads one message (with the thread it quotes) and says whether the sender promises the customer a
   benefit, of which kind, in which exact words. Cached in `.llm_cache/`; offline, a rule reader stands in.
2. Verbatim guard (code): the quote must appear in the message body, whitespace-normalized, or it is discarded.
3. Contract check (code): if the signed contract already gives what was promised (a long enough termination
   for convenience, a renewal price cap), the message is `covered`, not a finding.
"""

import re

from quota.search.intents import INTENTS

SCHEMA = {
    "type": "object",
    "properties": {"is_commitment": {"type": "boolean"}, "type": {"type": "string", "enum": [*INTENTS, "none"]},
                   "quote": {"type": "string"}, "promised_by": {"type": "string"}, "to_customer": {"type": "boolean"}},
    "required": ["is_commitment", "type", "quote", "promised_by", "to_customer"], "additionalProperties": False,
}
SYSTEM = ("You review one sales message (with any thread it quotes) for a commissions controller. A promise is an "
          "assurance, by someone selling for the company, of a benefit to the customer: a free exit or cancellation, "
          "free months, a credit or discount, a price lock, delayed payment, or a free add-on. Refusals, proposals "
          "to look into something, pointers to the contract, normal payment terms and product demos are not "
          "promises. A sales rep telling a colleague what they promised a customer is a promise. If it is a promise, "
          "copy the exact words that make it from the message (not from a quoted line) as `quote`; otherwise set "
          "type to none and quote to an empty string.")
CLAUSE = {"early_exit": "termination_for_convenience", "price_lock": "price_increase_cap"}


def flat(text: str) -> str:
    return " ".join(text.split())


def own_text(body: str) -> str:
    return "\n".join(line for line in body.splitlines() if not line.startswith(">"))


def quoted_text(body: str) -> str:
    return "\n".join(line[1:].strip() for line in body.splitlines() if line.startswith(">"))


def prompt(hit: dict) -> str:
    return (f"Kind: {hit.get('kind')}\nFrom: {hit.get('author')}\nTo: {', '.join(hit.get('to') or [])}\n"
            f"Account: {hit.get('account')}\nSubject: {hit.get('subject') or ''}\n\n{hit['body']}")


def model_verdict(llm, hit) -> dict:
    return {**llm.complete_json(SYSTEM, prompt(hit), SCHEMA), "read_by": "model"}


# ---------- the offline reader ----------

NOT_A_PROMISE = re.compile(r"\b(can't|cannot|never|i said no|would need|whether|let's ask|demo|"
                           r"are in section|están en la sección|is set by|only per|apply only|success plan)\b|\?$", re.I)
TYPES = [
    ("early_exit", re.compile(r"terminat|cancel|walk away|let you out|get out|out of year two|termination for "
                              r"convenience", re.I)),
    ("free_period", re.compile(r"on the house|free months|months free", re.I)),
    ("credit", re.compile(r"\bcredit you\b|\bcredit of\b", re.I)),
    ("price_lock", re.compile(r"price won't move|stay at this year's|price is locked|won't increase", re.I)),
    ("payment_terms", re.compile(r"\d+ days is fine|whenever finance", re.I)),
    ("free_addon", re.compile(r"for free.*not bill|not bill it|free of charge", re.I)),
]
AFFIRM = re.compile(r"^(yes|absolutely|of course|consider it done|done)\b", re.I)


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def rule_verdict(hit) -> dict:
    """A deterministic stand-in for the model: a sentence naming a benefit with no refusal, proposal or pointer
    in it, or an affirmative reply to a quoted request for one."""
    author = hit.get("author") or ""
    to_customer = hit.get("kind") == "email" and any("@" in t for t in hit.get("to") or [])
    for sentence in sentences(own_text(hit["body"])):
        if NOT_A_PROMISE.search(sentence):
            continue
        for kind, pattern in TYPES:
            if pattern.search(sentence):
                return {"is_commitment": True, "type": kind, "quote": sentence, "promised_by": author,
                        "to_customer": to_customer, "read_by": "rules"}
    asked = quoted_text(hit["body"])
    for sentence in sentences(own_text(hit["body"])):
        if AFFIRM.search(sentence):
            kind = next((k for k, p in TYPES if p.search(asked)), None)
            if kind:
                quote = " ".join(s for s in sentences(own_text(hit["body"])) if AFFIRM.search(s))
                return {"is_commitment": True, "type": kind, "quote": quote, "promised_by": author,
                        "to_customer": to_customer, "read_by": "rules"}
    return {"is_commitment": False, "type": "none", "quote": "", "promised_by": author, "to_customer": to_customer,
            "read_by": "rules"}


def verdict(hit, llm=None) -> dict:
    if llm is not None:
        from quota.agent.llm import LLMError, LLMUnavailable
        try:
            return model_verdict(llm, hit)
        except (LLMError, LLMUnavailable):
            pass
    return rule_verdict(hit)


# ---------- code: the guard and the contract ----------

def verbatim(quote: str, body: str) -> bool:
    return bool(quote.strip()) and flat(quote) in flat(body)


def promised_days(quote: str) -> int | None:
    m = re.search(r"(\d+)\s*(days|días)", quote, re.I)
    if m:
        return int(m.group(1))
    if re.search(r"first quarter|first 90", quote, re.I):
        return 90
    return None                       # "if it isn't working by Q3", "anytime": no period a clause could match


def covered(kind: str, quote: str, clauses: dict) -> bool:
    clause = clauses.get(CLAUSE.get(kind, ""))
    if not clause:
        return False
    if kind == "early_exit":
        days = promised_days(quote)
        return days is not None and int(clause.get("window_days", 0)) >= days
    return True


def assess(hit, clauses: dict, llm=None) -> dict:
    """{status: finding|covered|none|discarded, verdict, finding}."""
    v = verdict(hit, llm)
    if not v["is_commitment"] or v["type"] == "none":
        return {"status": "none", "verdict": v, "finding": None}
    if not verbatim(v["quote"], hit["body"]):
        return {"status": "discarded", "verdict": v, "finding": None}
    if covered(v["type"], v["quote"], clauses):
        return {"status": "covered", "verdict": v, "finding": None}
    finding = {"doc_id": hit["doc_id"], "body_sha256": hit["body_sha256"], "type": v["type"], "quote": v["quote"],
               "promised_by": v["promised_by"], "deal_id": hit.get("deal_id"), "account": hit.get("account"),
               "sent_at": hit.get("sent_at"), "read_by": v["read_by"],
               "retrieval": {"mode": hit.get("mode"), "rank": hit.get("rank"), "score": hit.get("score")}}
    return {"status": "finding", "verdict": v, "finding": finding}


def classify(hit, contract: dict | None, llm=None) -> dict | None:
    """The Finding, or None (not a promise, a quote not in the message, or already in the contract)."""
    clauses = contract.get("clauses_json") if contract else {}
    if isinstance(clauses, str):
        import json
        clauses = json.loads(clauses)
    return assess(hit, clauses or {}, llm)["finding"]
