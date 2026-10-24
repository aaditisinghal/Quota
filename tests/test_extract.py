import json

import pytest

from quota.agent.extract import (CLAIM_SCHEMA, ExtractionFailed, ModelExtractor, ScriptedExtractor, extract_claim,
                                 extract_contract, has_authority, value_in_quote, verify_claim)
from quota.agent.llm import LLM

DEALS = {"acme": "Acme Robotics", "birch": "Birch Health"}
JORDAN = "Acme closed Mar 31. It should count in Q1 at the 1.5x accelerator."


def claim(**kw):
    base = {"kind": "dispute", "deal_ref": None, "line_ref": None, "field": None, "asserted_value": None,
            "amount_cents": None, "quote": ""}
    return {**base, **kw}


class Canned:
    def __init__(self, answer):
        self.answer, self.prompts = answer, []

    def __call__(self, model, system, prompt, schema, effort):
        self.prompts.append(prompt)
        return json.dumps(self.answer)


def test_dates_must_be_written_in_the_quote():
    assert value_in_quote("2026-03-31", "Acme closed Mar 31")
    assert value_in_quote("2026-03-31", "closed on March 31st")
    assert value_in_quote("2026-03-31", "signed 3/31")
    assert value_in_quote("2026-03", "everyone knows it closed in March")
    assert not value_in_quote("2026-03-30", "Acme closed Mar 31")
    assert not value_in_quote("2026-03-31", "it closed in March")
    assert value_in_quote("6000", "they promised me $6k")
    assert value_in_quote("120000", "ACV of $120,000")
    assert not value_in_quote("150000", "ACV of $120,000")


def test_grounded_claim_passes():
    c = verify_claim(claim(deal_ref="acme", field="booking_date", asserted_value="2026-03-31",
                           quote="Acme closed Mar 31"), JORDAN)
    assert (c.kind, c.deal_ref, c.asserted_value, c.problem) == ("dispute", "acme", "2026-03-31", None)


@pytest.mark.parametrize("raw, problem", [
    (claim(field="booking_date", asserted_value="2026-03-31", quote="Acme closed March 31"),
     "the quote is not in the message"),
    (claim(field="booking_date", asserted_value="2026-03-30", quote="Acme closed Mar 31"),
     "2026-03-30 is not written in the quote"),
    (claim(kind="spiff_claim", amount_cents=700_000, quote="Acme closed Mar 31"),
     "amount 700000 cents is not written in the quote"),
    (claim(deal_ref="birch", quote="Acme closed Mar 31"), "birch is not named in the message"),
    ({"kind": "dispute"}, "$.deal_ref is missing"),
])
def test_ungrounded_claims_become_other(raw, problem):
    c = verify_claim(raw, JORDAN)
    assert c.kind == "other"
    assert problem in c.problem


def test_authority_comes_from_capabilities():
    grant = verify_claim(claim(kind="exception_grant", deal_ref="acme", field="booking_date",
                               asserted_value="2026-03-31", quote="Honor Mar 31 for Acme"),
                         "Honor Mar 31 for Acme. Count it in Q1.")
    assert has_authority(grant, "marcus.lee")
    assert not has_authority(grant, "morgan.diaz")        # a manager cannot grant plan exceptions (E05)
    assert has_authority(verify_claim(claim(quote="Acme closed Mar 31"), JORDAN), "jordan.kim")


def test_scripted_extractor_uses_the_same_guards():
    table = {JORDAN: claim(deal_ref="acme", field="booking_date", asserted_value="2026-03-31",
                           quote="Acme closed Mar 31"),
             "Pay me": claim(asserted_value="2026-03-31", quote="Pay me")}
    extractor = ScriptedExtractor(table)
    assert extract_claim(extractor, JORDAN, "jordan.kim", "2026", DEALS).kind == "dispute"
    assert extract_claim(extractor, "Pay me", "jordan.kim", "2026", DEALS).kind == "other"
    assert extract_claim(extractor, "unscripted text", "jordan.kim", "2026", DEALS).kind == "other"


def test_model_extractor_prompt_and_guard(tmp_path):
    canned = Canned(claim(deal_ref="acme", field="booking_date", asserted_value="2026-03-31",
                          quote="Acme closed Mar 31"))
    extractor = ModelExtractor(LLM(cache_dir=tmp_path, transport=canned))
    c = extract_claim(extractor, JORDAN, "jordan.kim", "2026", DEALS)
    assert c.kind == "dispute"
    assert "acme: Acme Robotics" in canned.prompts[0] and JORDAN in canned.prompts[0]

    hallucinated = Canned(claim(deal_ref="acme", field="booking_date", asserted_value="2026-03-15",
                                quote="Acme closed Mar 31"))
    c = extract_claim(ModelExtractor(LLM(cache_dir=tmp_path / "h", transport=hallucinated)), JORDAN,
                      "jordan.kim", "2026", DEALS)
    assert c.kind == "other" and "2026-03-15" in c.problem


def test_claim_schema_is_strict_output_ready():
    assert CLAIM_SCHEMA["additionalProperties"] is False
    assert set(CLAIM_SCHEMA["required"]) == set(CLAIM_SCHEMA["properties"])


CONTRACT = ("MASTER SUBSCRIPTION AGREEMENT. Contract date: 2026-04-01. Annual subscription fee: $120,000 "
            "billed annually upfront. Term: 36 months from the contract date. Countersigned by the vendor on "
            "2026-04-02 at 17:14 UTC.")


def test_contract_extraction_requires_verbatim_numbers(tmp_path):
    good = {"acv_cents": 12_000_000, "term_months": 36, "contract_date": "2026-04-01",
            "countersigned_at": "2026-04-02T17:14:00Z", "billing": "annual_upfront",
            "quotes": {"acv": "Annual subscription fee: $120,000", "term": "Term: 36 months",
                       "contract_date": "Contract date: 2026-04-01",
                       "countersigned_at": "Countersigned by the vendor on 2026-04-02 at 17:14 UTC"}}
    facts = extract_contract(LLM(cache_dir=tmp_path, transport=Canned(good)), CONTRACT, "contracts/acme-msa.pdf#p3")
    assert (facts.acv_cents, facts.term_months, facts.doc_ref) == (12_000_000, 36, "contracts/acme-msa.pdf#p3")

    tcv = {**good, "acv_cents": 36_000_000}       # TCV instead of ACV: $360,000 is not in the contract
    with pytest.raises(ExtractionFailed, match="acv"):
        extract_contract(LLM(cache_dir=tmp_path / "tcv", transport=Canned(tcv)), CONTRACT, "x")
