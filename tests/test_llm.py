import json

import anthropic
import httpx2
import pytest

from quota.agent import llm as llm_mod
from quota.agent.llm import LLM, LLMError, anthropic_transport, reply_problems
from quota.engine.disputes import render_reply

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"],
          "additionalProperties": False}
FIELDS = {"outcome": "held", "claim": "Acme Robotics booking date should be 2026-03-31 (2026Q1, accelerator tier)",
          "evidence": "countersignature 2026-04-02T17:14:00Z (contracts:acme:v1) outranks crm_close_date "
                      "2026-03-31T23:59:59Z (deals:acme:v1)",
          "rule": "R-BOOKING-01: booking date = countersignature", "as_resolved_cents": 1_200_000,
          "as_claimed_cents": 1_800_000, "diff_cents": 600_000, "escalate_to": "marcus.lee",
          "escalate_to_name": "Marcus Lee", "escalate_to_role": "VP Sales Ops", "no_new_evidence": False}


class FakeTransport:
    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []

    def __call__(self, model, system, prompt, schema, effort):
        self.requests.append({"model": model, "system": system, "prompt": prompt, "schema": schema,
                              "effort": effort})
        return self.answers.pop(0)


def test_calls_are_cached_on_disk(tmp_path):
    transport = FakeTransport('{"answer": "yes"}', '{"answer": "other schema"}')
    first = LLM(cache_dir=tmp_path, transport=transport)
    assert first.complete_json("sys", "prompt", SCHEMA) == {"answer": "yes"}
    assert first.complete_json("sys", "prompt", SCHEMA) == {"answer": "yes"}
    assert (first.calls, first.hits) == (1, 1)

    second = LLM(cache_dir=tmp_path, transport=FakeTransport())   # a rerun: no transport calls at all
    assert second.complete_json("sys", "prompt", SCHEMA) == {"answer": "yes"}
    assert (second.calls, second.hits) == (0, 1)

    other = {**SCHEMA, "description": "changed"}
    assert first.complete_json("sys", "prompt", other) == {"answer": "other schema"}
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_model_comes_from_env(tmp_path, monkeypatch):
    monkeypatch.delenv("QUOTA_MODEL", raising=False)
    assert LLM(cache_dir=tmp_path, transport=FakeTransport()).model == "claude-opus-5-5"
    monkeypatch.setenv("QUOTA_MODEL", "claude-sonnet-5-5")
    assert LLM(cache_dir=tmp_path, transport=FakeTransport()).model == "claude-sonnet-5-5"


def test_off_schema_answers_raise_and_are_not_cached(tmp_path):
    model = LLM(cache_dir=tmp_path, transport=FakeTransport('{"answer": 3}', "not json"))
    with pytest.raises(LLMError, match="must be string"):
        model.complete_json("sys", "p", SCHEMA)
    with pytest.raises(LLMError, match="not JSON"):
        model.complete_json("sys", "p", SCHEMA)
    assert list(tmp_path.glob("*.json")) == []


def _mock_client(captured, stop_reason="end_turn", text='{"answer": "ok"}'):
    def handler(request):
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        content = [{"type": "text", "text": text}] if text is not None else []
        return httpx2.Response(200, json={
            "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": content, "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5}})
    return anthropic.Anthropic(api_key="test-key", max_retries=0,
                               http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))


def test_request_shape_sent_to_the_messages_api(tmp_path):
    captured = {}
    model = LLM(cache_dir=tmp_path, transport=anthropic_transport(_mock_client(captured)))
    assert model.complete_json("You extract.", "Message: hi", SCHEMA) == {"answer": "ok"}
    body = captured["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in captured["headers"]["anthropic-beta"]
    assert body["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}}
    assert body["system"] == "You extract."
    assert body["messages"] == [{"role": "user", "content": "Message: hi"}]
    assert "thinking" not in body and "temperature" not in body


def test_refusals_and_truncation_raise(tmp_path):
    for stop_reason in ("refusal", "max_tokens"):
        model = LLM(cache_dir=tmp_path / stop_reason,
                    transport=anthropic_transport(_mock_client({}, stop_reason=stop_reason)))
        with pytest.raises(LLMError):
            model.complete_json("s", "p", SCHEMA)


def test_reply_guard_accepts_faithful_rewording():
    good = ("You say Acme should book on 2026-03-31. The countersignature is dated April 2, 2026, which "
            "outranks the CRM close date (R-BOOKING-01). That is $12,000.00 vs $18,000.00, a $6,000.00 "
            "difference. Reply \"escalate\" to send it to Marcus Lee.")
    assert reply_problems(good, {**FIELDS, "as_resolved": "$12,000.00"}) == []


def test_reply_guard_rejects_invented_numbers(tmp_path):
    bad = "Acme booked April 3 and you are owed $6,500.00 more."
    assert reply_problems(bad, FIELDS) == ["amount $6,500.00 is not in the facts", "date ????-04-03 is not in the facts"]

    model = LLM(cache_dir=tmp_path, transport=FakeTransport(json.dumps({"reply": bad})))
    assert model.draft_reply(FIELDS) == render_reply(FIELDS)        # falls back to the Phase 1 template

    good = "Booked 2026-04-02 per countersignature: $12,000.00 vs $18,000.00 = $6,000.00. Reply escalate."
    model = LLM(cache_dir=tmp_path / "good", transport=FakeTransport(json.dumps({"reply": good})))
    assert model.draft_reply(FIELDS) == good


def test_schema_checker_handles_nullable_and_enums():
    schema = {"type": "object", "properties": {"x": {"anyOf": [{"type": "string", "enum": ["a"]}, {"type": "null"}]}},
              "required": ["x"], "additionalProperties": False}
    assert llm_mod.schema_errors({"x": None}, schema) == []
    assert llm_mod.schema_errors({"x": "a"}, schema) == []
    assert llm_mod.schema_errors({"x": "b"}, schema) == ["$.x matches none of the allowed shapes"]
    assert llm_mod.schema_errors({}, schema) == ["$.x is missing"]
