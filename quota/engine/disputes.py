"""Dispute policy (pure, deterministic). Every dollar figure in a reply comes from these fields."""

import json
from dataclasses import asdict, dataclass

from quota import clock as clk
from quota import config
from quota.engine import calculator, ranking, routing
from quota.money import fmt_usd

REPLY_TEMPLATE = (
    "Claim: {claim_summary}\n"
    "Evidence: {evidence}\n"
    "Rule: {rule}\n"
    "Difference: {as_resolved} vs {as_claimed} = {diff}\n"
    "Next step: {next_step}"
)


@dataclass
class DisputeOutcome:
    outcome: str        # held|conceded|escalation_offered
    no_new_evidence: bool
    reply_fields: dict  # claim, evidence, rule, diff_cents, escalate_to


def _loads(value):
    return json.loads(value) if isinstance(value, str) else value


def evidence_key(ref: dict) -> str:
    """"contracts:acme:v1" for versioned rows, "esign_events:ENV-ACME-001-countersigned" otherwise."""
    key = f"{ref['table']}:{ref['id']}"
    return f"{key}:v{ref['version']}" if ref.get("version") is not None else key


def _prior_inputs(prior_decision) -> set[str]:
    inputs = _loads(prior_decision.get("inputs_json", prior_decision.get("inputs", [])))
    return {evidence_key(i) for i in inputs}


def _prior_value(prior_decision, field):
    resolutions = _loads(prior_decision.get("resolutions_json", prior_decision.get("resolutions", {})))
    return (resolutions.get(field) or {}).get("value")


def _matches(field, resolved, asserted) -> bool:
    if field == "booking_date":
        return resolved is not None and str(resolved)[:10] == str(asserted)[:10]
    return resolved == asserted


def _evidence(res: ranking.Resolution, no_new: bool, prior_id: str | None) -> str:
    chosen = f"{res.basis} {res.value} ({res.source})"
    below = " and ".join(f"{d['basis']} {d['value']} ({d['source']})" for d in res.disagreements)
    text = f"{chosen} outranks {below}" if below else chosen
    if no_new:
        return f"nothing new was provided since {prior_id}; {text}"
    return f"{text}; the message cites no new documents"


def evaluate_dispute(claim, prior_decision, fresh_resolutions, calc_at_claim, calc_at_resolved) -> DisputeOutcome:
    field = claim.get("field")
    res = fresh_resolutions[field]
    owner = routing.owner("plan_exception")
    prior_id = prior_decision.get("id")
    new_refs = [r for r in claim.get("evidence_refs") or [] if r not in _prior_inputs(prior_decision)]
    no_new = prior_decision.get("kind") == "dispute" and not new_refs

    if claim.get("kind") == "escalate":
        outcome = "escalation_offered"
    elif _matches(field, res.value, claim.get("asserted")):
        outcome = "conceded"
    elif new_refs and res.value != _prior_value(prior_decision, field):
        outcome = "conceded"
    else:
        outcome = "held"

    fields = {
        "outcome": outcome,
        "claim": (f"{claim.get('deal_name', claim.get('deal', 'the deal'))} {field.replace('_', ' ')} should be "
                  f"{claim.get('asserted')} ({calc_at_claim.quarter}, {calc_at_claim.tier} tier)"),
        "evidence": _evidence(res, no_new and outcome == "held", prior_id),
        "rule": f"{res.rule_id}: {ranking.RULES[res.rule_id]}",
        "as_resolved_cents": calc_at_resolved.amount_cents,
        "as_claimed_cents": calc_at_claim.amount_cents,
        "diff_cents": calc_at_claim.amount_cents - calc_at_resolved.amount_cents,
        "escalate_to": owner,
        "escalate_to_name": config.display_name(owner),
        "escalate_to_role": config.title(owner),
        "no_new_evidence": no_new and outcome == "held",
    }
    return DisputeOutcome(outcome, fields["no_new_evidence"], fields)


def render_reply(fields: dict) -> str:
    if fields["outcome"] == "conceded":
        next_step = "the line will be recomputed with the corrected value."
    elif fields["outcome"] == "escalation_offered":
        next_step = f"sent to {fields['escalate_to_name']} ({fields['escalate_to_role']}) as a plan exception."
    else:
        next_step = (f'reply "escalate" to send this to {fields["escalate_to_name"]} '
                     f'({fields["escalate_to_role"]}).')
    return REPLY_TEMPLATE.format(
        claim_summary=fields["claim"], evidence=fields["evidence"], rule=fields["rule"],
        as_resolved=fmt_usd(fields["as_resolved_cents"]), as_claimed=fmt_usd(fields["as_claimed_cents"]),
        diff=fmt_usd(fields["diff_cents"]), next_step=next_step)


# ---------- decision from fact rows (used by the seat and by replay) ----------

def dispute_decision(rows, line_id, now) -> dict:
    """rows carry the line, the claim message, the prior decision and the commission facts."""
    line = calculator.latest_by_id(rows.get("commission_lines"))[line_id]
    message = rows["messages"][0]
    prior = rows["decisions"][0]
    claim = dict(_loads(message["claim_json"]))
    if claim.get("kind") == "escalate" and prior["kind"] == "dispute":
        claim = {**json.loads(prior["output_json"])["args"]["claim"], "kind": "escalate"}
    deal = calculator.latest_by_id(rows.get("deals"))[line["deal_id"]]
    claim.setdefault("deal_name", deal["account"])
    resolved = calculator.resolve_deal(rows, deal, line["rep_id"], now)
    asserted_ts = clk.end_of_day(claim["asserted"]) if claim["field"] == "booking_date" else None
    at_claim = calculator.evaluate(rows, line["deal_id"], line["rep_id"], now, booking_ts=asserted_ts,
                                   base_cents=claim["asserted"] if claim["field"] == "base" else None)
    at_resolved = calculator.evaluate(rows, line["deal_id"], line["rep_id"], now)
    result = evaluate_dispute(claim, prior, resolved, at_claim, at_resolved)
    output = {
        "fn": "evaluate_dispute",
        "args": {"claim": claim, "line_id": line_id, "message_id": message["id"], "prior_decision_id": prior["id"]},
        "result": asdict(result),
    }
    return {"output": output, "outcome": result, "claim": claim,
            "resolutions": {claim["field"]: ranking.to_dict(resolved[claim["field"]])},
            "precedent_ids": [], "at_claim": at_claim, "at_resolved": at_resolved}
