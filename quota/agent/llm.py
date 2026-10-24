"""Every model call goes through here.

Calls are cached on disk by sha256(model + prompt + schema), so a rerun is free and deterministic.
Model id from QUOTA_MODEL (default claude-opus-5-5); credentials from ANTHROPIC_API_KEY or any other
source the Anthropic SDK resolves. Models only read and write words: every output is schema-checked here,
and callers verify content (quotes, numbers) before anything acts on it.
"""

import hashlib
import json
import os
import re
from pathlib import Path

from quota.engine.disputes import render_reply
from quota.money import fmt_usd

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[2] / ".llm_cache"
FALLBACK_BETA = "server-side-fallback-2026-07-01"   # re-run policy declines on Anthropic's recommended fallback
MAX_TOKENS = 16000

EXTRACT_SYSTEM = ("You turn one piece of text into structured data that matches the given JSON schema. "
                  "Report only what the text says; use null where it says nothing.")

REPLY_SYSTEM = (
    "You write the chat reply Quota, a company's commissions desk, sends to an employee about their "
    "commission. You are given the facts as JSON. Keep five parts in this order: the claim restated, the "
    "evidence, the rule, the dollar difference, and the next step. Copy every dollar amount and date exactly "
    "as it appears in the JSON and do not compute new ones. Plain text, no headings, under 120 words.")
REPLY_SCHEMA = {"type": "object", "properties": {"reply": {"type": "string"}}, "required": ["reply"],
                "additionalProperties": False}


class LLMError(RuntimeError):
    """The model answered, but not with something usable (refusal, truncation, off-schema)."""


class LLMUnavailable(RuntimeError):
    """No credentials or no connection. Never swallowed: a run that needs the model stops instead of guessing."""


# ---------- schema check (the subset of JSON Schema structured outputs use here) ----------

_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict, "array": list,
          "null": type(None)}


def schema_errors(value, schema, path="$") -> list[str]:
    if "anyOf" in schema:
        if any(not schema_errors(value, option, path) for option in schema["anyOf"]):
            return []
        return [f"{path} matches none of the allowed shapes"]
    expected = schema.get("type")
    if expected is not None:
        python_type = _TYPES[expected]
        if not isinstance(value, python_type) or (expected == "integer" and isinstance(value, bool)):
            return [f"{path} must be {expected}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path} must be one of {schema['enum']}"]
    errors = []
    if isinstance(value, dict):
        props = schema.get("properties", {})
        errors += [f"{path}.{k} is missing" for k in schema.get("required", []) if k not in value]
        if schema.get("additionalProperties") is False:
            errors += [f"{path}.{k} is not allowed" for k in value if k not in props]
        for key, sub in props.items():
            if key in value:
                errors += schema_errors(value[key], sub, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors += schema_errors(item, schema["items"], f"{path}[{i}]")
    return errors


# ---------- transport ----------

def anthropic_transport(client=None):
    """Calls Claude with a JSON-schema output format; returns the JSON text."""
    def call(model, system, prompt, schema, effort):
        nonlocal client
        import anthropic
        if client is None:
            client = anthropic.Anthropic()
        try:
            response = client.beta.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            )
        except TypeError as exc:   # the SDK raises this when no credential source resolves
            if "authentication" not in str(exc):
                raise
            raise LLMUnavailable(f"no Anthropic credentials ({exc})") from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise LLMUnavailable(f"credentials rejected ({exc.status_code})") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailable(f"cannot reach the Anthropic API ({exc})") from exc
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise LLMError(f"model declined ({getattr(details, 'category', None) or 'no category'})")
        if response.stop_reason == "max_tokens":
            raise LLMError("output was cut off at max_tokens")
        text = next((block.text for block in response.content if block.type == "text"), None)
        if text is None:
            raise LLMError(f"no text in response (stop_reason {response.stop_reason})")
        return text
    return call


def cache_key(model, system, prompt, schema) -> str:
    material = "\n".join([model, system, prompt, json.dumps(schema, sort_keys=True)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class LLM:
    def __init__(self, model=None, cache_dir=None, transport=None):
        self.model = model or os.environ.get("QUOTA_MODEL") or DEFAULT_MODEL
        self.cache_dir = Path(cache_dir or os.environ.get("QUOTA_LLM_CACHE") or DEFAULT_CACHE_DIR)
        self.transport = transport or anthropic_transport()
        self.calls = 0      # uncached calls made by this instance
        self.hits = 0       # answers served from the disk cache

    def complete_json(self, system: str, prompt: str, schema: dict, effort: str = "low") -> dict:
        path = self.cache_dir / f"{cache_key(self.model, system, prompt, schema)}.json"
        if path.exists():
            self.hits += 1
            return json.loads(path.read_text(encoding="utf-8"))["output"]
        text = self.transport(self.model, system, prompt, schema, effort)
        self.calls += 1
        try:
            output = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"response is not JSON: {exc}") from exc
        problems = schema_errors(output, schema)
        if problems:
            raise LLMError("response does not match the schema: " + "; ".join(problems))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"model": self.model, "system": system, "prompt": prompt, "schema": schema,
                                    "output": output}, indent=2, sort_keys=True), encoding="utf-8")
        return output

    def extract(self, schema: dict, text: str, instructions: str = EXTRACT_SYSTEM) -> dict:
        return self.complete_json(instructions, text, schema, effort="low")

    def draft_reply(self, fields: dict) -> str:
        """The model may reword the dispute reply; if any $ amount or date isn't in the fields, use the template."""
        facts = {**fields, "as_resolved": fmt_usd(fields["as_resolved_cents"]),
                 "as_claimed": fmt_usd(fields["as_claimed_cents"]), "difference": fmt_usd(fields["diff_cents"])}
        try:
            reply = self.complete_json(REPLY_SYSTEM, json.dumps(facts, indent=1, sort_keys=True), REPLY_SCHEMA)["reply"]
        except LLMError:
            return render_reply(fields)
        return reply if not reply_problems(reply, facts) else render_reply(fields)


# ---------- reply guard ----------

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
_MONTH_RE = "|".join(sorted({m for full in MONTHS for m in (full, full[:3], "sept")}, key=len, reverse=True))
_MONEY = re.compile(r"(-?)\$(\d[\d,]*(?:\.\d+)?)")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})")
_NAMED = re.compile(rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b", re.I)


def _month_number(name: str) -> int:
    name = name.lower()
    return next(i for i, m in enumerate(MONTHS, start=1) if m.startswith(name[:3]))


def money_in(text: str) -> set[int]:
    return {(-1 if sign else 1) * round(float(amount.replace(",", "")) * 100) for sign, amount in _MONEY.findall(text)}


def dates_in(text: str) -> set[tuple[int | None, int, int]]:
    found = {(int(y), int(m), int(d)) for y, m, d in _ISO.findall(text)}
    found |= {(int(y) if y else None, _month_number(name), int(day)) for name, day, y in _NAMED.findall(text)}
    return found


def reply_problems(reply: str, facts: dict) -> list[str]:
    """Every $ amount and date in the reply must exist in the facts."""
    strings = [v for v in facts.values() if isinstance(v, str)]
    allowed_money = {v for k, v in facts.items() if k.endswith("_cents") and isinstance(v, int)}
    allowed_money |= {-c for c in allowed_money} | {c for s in strings for c in money_in(s)}
    allowed_dates = {d for s in strings for d in dates_in(s)}
    problems = [f"amount {fmt_usd(c)} is not in the facts" for c in sorted(money_in(reply)) if c not in allowed_money]
    for year, month, day in sorted(dates_in(reply), key=str):
        if not any(m == month and d == day and (year is None or y is None or y == year)
                   for y, m, d in allowed_dates):
            problems.append(f"date {year or '????'}-{month:02d}-{day:02d} is not in the facts")
    return problems
