"""--live: people (and extraction, and reply wording) are model calls. A fake model stands in for Claude so
the test needs no network; it answers by schema the way a sensible person would."""

import json

import pytest

from quota.agent.extract import CLAIM_SCHEMA
from quota.agent.llm import LLM, LLMError, REPLY_SCHEMA
from quota.agent.personas import PERSONA_SCHEMA, PERSONAS
from quota.ledger import invariants, queries
from quota.sim import demo_acme

EXTRACTIONS = demo_acme.load_script()["extractions"]
EXPECTED_TB = {"1000": 1_200_000, "1200": 0, "1290": 1_800_000, "1350": 0, "2150": 0, "2400": 0,
               "4000": -3_000_000, "6110": 0}
OTHER = {"kind": "other", "deal_ref": None, "line_ref": None, "field": None, "asserted_value": None,
         "amount_cents": None, "quote": ""}


class FakeModel:
    def __init__(self):
        self.calls = []

    def __call__(self, model, system, prompt, schema, effort):
        self.calls.append(schema)
        if schema == CLAIM_SCHEMA:
            return json.dumps(EXTRACTIONS.get(prompt.split("Message:\n", 1)[1], OTHER))
        if schema == REPLY_SCHEMA:
            f = json.loads(prompt)
            return json.dumps({"reply": f"You say: {f['claim']}. But {f['evidence']}. {f['rule']}. That is "
                                        f"{f['as_resolved']} vs {f['as_claimed']}, {f['difference']} apart. Reply "
                                        f"\"escalate\" to send it to {f['escalate_to_name']}."})
        if schema == PERSONA_SCHEMA:
            who = next(p for p, text in PERSONAS.items() if text in system)
            if prompt.startswith("Approval request"):
                return json.dumps({"action": "approve", "reason": "Supported by the record.", "text": ""})
            last = prompt.rsplit("\n", 2)[-2]
            if who == "jordan.kim" and 'Reply "escalate"' in last:
                return json.dumps({"action": "message", "reason": "", "text": "escalate"})
            if who == "marcus.lee" and "Plan exception request" in last:
                return json.dumps({"action": "message", "reason": "", "text": "Honor Mar 31 for Acme. Count it in Q1."})
            return json.dumps({"action": "none", "reason": "", "text": ""})
        raise AssertionError(f"unexpected schema {schema}")


def _refuse_to_call(*args):
    raise LLMError("the cache should have answered")


def test_live_demo_completes_with_model_played_people(tmp_path):
    fake = FakeModel()
    demo = demo_acme.run(out=lambda line: None, mode="live", llm=LLM(cache_dir=tmp_path, transport=fake))
    try:
        assert demo.ok
        assert queries.trial_balance(demo.ro) == EXPECTED_TB          # same books as the scripted run
        assert all(not v for v in invariants.check_all(demo.ro).values())
        kinds = {json.dumps(s, sort_keys=True) for s in fake.calls}
        assert {json.dumps(s, sort_keys=True) for s in (CLAIM_SCHEMA, REPLY_SCHEMA, PERSONA_SCHEMA)} <= kinds
        printed = "\n".join(demo.lines)
        assert "marcus.lee                 MESSAGE" in printed and "Honor Mar 31 for Acme" in printed
        assert "R-0001 decide_approval A-0002: NO_CAPABILITY, INV5_SELF_APPROVAL, INV5_WRONG_APPROVER" in printed
        first_lines = demo.lines
    finally:
        demo.close()

    rerun = demo_acme.run(out=lambda line: None, mode="live", llm=LLM(cache_dir=tmp_path, transport=_refuse_to_call))
    try:
        strip = lambda lines: [l for l in lines if not l.startswith("CHAIN")]
        assert strip(rerun.lines) == strip(first_lines)               # reruns are free and deterministic
    finally:
        rerun.close()


def test_live_people_cannot_exceed_their_capabilities(tmp_path):
    class Overreach(FakeModel):
        def __call__(self, model, system, prompt, schema, effort):
            if schema == PERSONA_SCHEMA and "You are Jordan Kim" in system:
                return json.dumps({"action": "message", "reason": "", "text": "escalate"})
            return super().__call__(model, system, prompt, schema, effort)

    demo = demo_acme.run(out=lambda line: None, mode="live", llm=LLM(cache_dir=tmp_path, transport=Overreach()))
    try:
        assert demo.ok
        to_seat = queries.fetch_all(demo.ro, "SELECT * FROM messages WHERE author = 'jordan.kim' "
                                             "AND to_principal = 'seat:commissions_analyst'")
        assert len(to_seat) <= 2 + 3       # two scripted messages, at most three model-played replies
        assert len(queries.fetch_all(demo.ro, "SELECT * FROM decisions WHERE kind = 'escalation'")) == 1
    finally:
        demo.close()
