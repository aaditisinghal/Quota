"""Source-of-truth resolution. Each field walks a fixed ranking; every lower source that differs is a
disagreement, kept on the record with its effect in cents (filled by the caller via the calculator)."""

import json
from dataclasses import asdict, dataclass, replace

from quota import clock as clk

RULES = {
    "R-BOOKING-01": "booking date = in-force precedent, then countersignature, then contract date, "
                    "then CRM close date; CRM close date is never trusted alone",
    "R-AMOUNT-01": "commissionable base = precedent, then signed contract year-1 ACV, then CRM amount",
    "R-OWNER-01": "splits = VP-approved split record, then CRM splits",
}


@dataclass
class Resolution:
    field: str
    value: object
    source: str                 # e.g. "contracts:acme:v1" or "precedent:P-0001"
    rule_id: str
    disagreements: list[dict]   # [{"source","value","effect_cents","basis"}]
    basis: str = ""             # which rung won: countersignature, contract, crm_close_date, precedent ...


def to_dict(res: Resolution) -> dict:
    return asdict(res)


def _loads(value):
    return json.loads(value) if isinstance(value, str) else value


def _fact_source(table, row):
    return f"{table}:{row['id']}:v{row['version']}" if "version" in row else f"{table}:{row['id']}"


def precedent_in_force(precedent: dict, now: str) -> bool:
    return clk.in_window(precedent["effective_from"], precedent["expires_at"], now)


def matching_precedent(precedents, deal_id, field_name, now) -> dict | None:
    for p in sorted(precedents, key=lambda p: p["id"], reverse=True):
        scope = _loads(p["scope_json"]) if "scope_json" in p else p["scope"]
        if scope.get("deal_id") == deal_id and scope.get("field") == field_name and precedent_in_force(p, now):
            return p
    return None


def _override(precedent) -> dict:
    return _loads(precedent["override_json"]) if "override_json" in precedent else precedent["override"]


def _resolve(field_name, rule_id, candidates) -> Resolution:
    """candidates: [(basis, source, value)] highest rank first; None values are skipped."""
    present = [c for c in candidates if c[2] is not None]
    if not present:
        return Resolution(field_name, None, "none", rule_id, [], "none")
    basis, source, value = present[0]
    disagreements = [{"source": s, "value": v, "effect_cents": None, "basis": b}
                     for b, s, v in present[1:] if v != value]
    return Resolution(field_name, value, source, rule_id, disagreements, basis)


def resolve_booking(deal, contract, esign_events, precedents, now) -> Resolution:
    candidates = []
    precedent = matching_precedent(precedents, deal["id"], "booking_date", now)
    if precedent is not None:
        candidates.append(("precedent", f"precedent:{precedent['id']}",
                           clk.end_of_day(_override(precedent)["value"])))
    if contract is not None:
        candidates.append(("countersignature", _fact_source("contracts", contract), contract.get("countersigned_at")))
    countersigned = [e for e in esign_events or [] if e["event"] == "countersigned" and e["deal_id"] == deal["id"]]
    for event in countersigned:
        candidates.append(("countersignature", _fact_source("esign_events", event), event["at"]))
    if contract is not None and contract.get("contract_date"):
        candidates.append(("contract_date", _fact_source("contracts", contract),
                           clk.start_of_day(contract["contract_date"])))
    if deal.get("crm_close_date"):
        candidates.append(("crm_close_date", _fact_source("deals", deal), clk.end_of_day(deal["crm_close_date"])))
    return _resolve("booking_date", "R-BOOKING-01", candidates)


def resolve_base(deal, contract, plan, precedents, now) -> Resolution:
    candidates = []
    precedent = matching_precedent(precedents, deal["id"], "base", now)
    if precedent is not None:
        candidates.append(("precedent", f"precedent:{precedent['id']}", int(_override(precedent)["value"])))
    if contract is not None and plan.commissionable_base == "year1_acv":
        candidates.append(("contract", _fact_source("contracts", contract), contract["acv_cents"]))
    candidates.append(("crm", _fact_source("deals", deal), deal["crm_amount_cents"]))
    return _resolve("base", "R-AMOUNT-01", candidates)


def resolve_splits(deal, approved_split_records) -> Resolution:
    candidates = [("vp_split_record", r["source"], r["splits"]) for r in approved_split_records or []
                  if r["deal_id"] == deal["id"]]
    candidates.append(("crm", _fact_source("deals", deal), _loads(deal["splits_json"])))
    return _resolve("splits", "R-OWNER-01", candidates)


def with_effects(res: Resolution, amount_at) -> Resolution:
    """Fill effect_cents = amount at the alternative value - amount at the chosen value."""
    chosen = amount_at(res.value)
    return replace(res, disagreements=[{**d, "effect_cents": amount_at(d["value"]) - chosen}
                                       for d in res.disagreements])


def split_bps(splits_resolution: Resolution, rep_id: str) -> int:
    return sum(s["bps"] for s in splits_resolution.value or [] if s["rep"] == rep_id)
