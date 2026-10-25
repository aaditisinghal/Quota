"""Policy checks the seats run on facts (pure). Each takes the rows a decision records plus its arguments and
returns the decision's output {"fn", "args", "result"}, so replay is `check(rows, **args) == stored`."""

import json

from quota import clock as clk
from quota.engine import ranking
from quota.engine.calculator import latest_by_id, plan_for


def _loads(value):
    return json.loads(value) if isinstance(value, str) else value


def check_splits(rows, deal_id) -> dict:
    """E02: credit must add up to 100% (after the owner-of-record rule for duplicated full credit)."""
    deal = latest_by_id(rows["deals"])[deal_id]
    res = ranking.resolve_splits(deal, rows.get("split_records", []))
    total = ranking.splits_total(res)
    return {"fn": "check_splits", "args": {"deal_id": deal_id},
            "result": {"splits": res.value, "total_bps": total, "valid": total == 10000}}


def check_backdating(rows, deal_id, period_locked_at) -> dict:
    """E04: a CRM close date moved into a period that was already locked when the edit was made."""
    versions = sorted((d for d in rows["deals"] if d["id"] == deal_id), key=lambda d: d["version"])
    current, previous = versions[-1], (versions[-2] if len(versions) > 1 else None)
    close = current["crm_close_date"]
    moved = previous is not None and close is not None and close != previous["crm_close_date"]
    backdated = bool(moved and period_locked_at and period_locked_at < current["known_at"])
    return {"fn": "check_backdating", "args": {"deal_id": deal_id, "period_locked_at": period_locked_at},
            "result": {"backdated": backdated, "close_date": close, "previous_close_date":
                       previous["crm_close_date"] if previous else None, "edited_at": current["known_at"],
                       "edited_by": current["source"]}}


def check_collection(rows, deal_id, days) -> dict:
    """E07: the plan pays on collection and holds commission once a customer is this many days late. Payment
    status comes from billing (Stripe), not from the notice: a customer billing shows as paid is not held."""
    deal = latest_by_id(rows["deals"])[deal_id]
    plan = plan_for(rows, deal["owner_id"], deal["crm_close_date"] or "9999-12-31")
    hold_days = int(plan.raw.get("collection_hold_days", 90))
    payments = sorted((b for b in rows.get("billing", []) if b["deal_id"] == deal_id and b["kind"] == "payment"),
                      key=lambda b: b["at"])
    paid = payments[0] if payments else None
    return {"fn": "check_collection", "args": {"deal_id": deal_id, "days": days},
            "result": {"held": days >= hold_days and paid is None, "hold_days": hold_days,
                       "pay_on": plan.raw.get("pay_on"),
                       "paid": None if paid is None else {"id": paid["id"], "at": paid["at"],
                                                          "amount_cents": paid["amount_cents"],
                                                          "external_ref": paid.get("external_ref")}}}


def check_resubmission(rows, deal_id, field, value, rejected_approval_id, rejected_at) -> dict:
    """SPEC "Handling the answer": a declined proposal is resubmitted only with new evidence, i.e. a deal,
    contract or e-signature record for the deal that became known after the rejection."""
    newer = sorted(f"{table}:{r['id']}" + (f":v{r['version']}" if r.get("version") is not None else "")
                   for table in ("deals", "contracts", "esign_events") for r in rows.get(table, [])
                   if r.get("deal_id", r.get("id")) == deal_id and r["known_at"] > rejected_at)
    return {"fn": "check_resubmission",
            "args": {"deal_id": deal_id, "field": field, "value": value, "rejected_approval_id": rejected_approval_id,
                     "rejected_at": rejected_at},
            "result": {"resubmit": bool(newer), "new_evidence": newer}}


def check_termination(rows, rep_id, end_date) -> dict:
    """E09: the signed plan pays lines booked and collected before the end date; the rest are held."""
    origin = {d["id"]: _loads(d["output_json"]) for d in rows.get("decisions", [])}
    paid = {}
    for b in rows.get("billing", []):
        if b["kind"] == "payment":
            paid[b["deal_id"]] = min(paid.get(b["deal_id"], b["at"]), b["at"])
    eligible, held = [], []
    for line in sorted(rows.get("commission_lines", []), key=lambda l: l["id"]):
        booked = origin[line["decision_id"]]["args"]["booking"]["booking_ts"]
        collected = paid.get(line["deal_id"])
        ok = (clk.local_date(booked) <= end_date and collected is not None
              and clk.local_date(collected) <= end_date)
        (eligible if ok else held).append(line["id"])
    return {"fn": "check_termination", "args": {"rep_id": rep_id, "end_date": end_date},
            "result": {"eligible": eligible, "held": held}}


def check_territory(rows, target_rep, accounts) -> dict:
    """E10: a territory move lists N accounts; any whose CRM owner did not change is a half-done move."""
    deals = latest_by_id(rows["deals"])
    moved = [a for a in accounts if a in deals and deals[a]["owner_id"] == target_rep]
    unchanged = [a for a in accounts if a in deals and deals[a]["owner_id"] != target_rep]
    return {"fn": "check_territory", "args": {"target_rep": target_rep, "accounts": list(accounts)},
            "result": {"moved": moved, "unchanged": unchanged, "missing": [a for a in accounts if a not in deals]}}


def check_plan_drift(rows, rep_id, version) -> dict:
    """E14: a plan version that is not signed must not pay, and differs from what was signed."""
    plans = sorted((r for r in rows["plans"] if r["rep_id"] == rep_id), key=lambda r: r["version"])
    new = next(r for r in plans if r["version"] == version)
    signed = [r for r in plans if r["signed_at"]]
    signed_config = _loads(signed[-1]["config_json"]) if signed else {}
    new_config = _loads(new["config_json"])
    changed = sorted(k for k in set(new_config) | set(signed_config)
                     if k not in ("version", "signed_at", "signed_doc") and new_config.get(k) != signed_config.get(k))
    return {"fn": "check_plan_drift", "args": {"rep_id": rep_id, "version": version},
            "result": {"drift": not new["signed_at"] and bool(signed) and bool(changed), "draft_version": version,
                       "signed_version": signed[-1]["version"] if signed else None, "changed": changed}}


def _days_between(earlier: str, later: str) -> int:
    from datetime import date
    return (date.fromisoformat(later[:10]) - date.fromisoformat(earlier[:10])).days


def check_precedent_pattern(rows, field, threshold) -> dict:
    """SPEC "How does it learn?": approved exceptions are scoped precedents. When the same kind of exception has
    been approved on `threshold` or more deals, propose a plan change for Sales Ops to sign; never a silent rule."""
    precedents = sorted((p for p in rows.get("precedents", []) if _loads(p["scope_json"]).get("field") == field),
                        key=lambda p: p["id"])
    deals = sorted({_loads(p["scope_json"])["deal_id"] for p in precedents})
    result = {"propose": len(deals) >= threshold, "field": field, "precedents": [p["id"] for p in precedents],
              "deals": deals, "max_gap_days": None, "proposal": None}
    if field == "booking_date":
        contracts = latest_by_id(rows.get("contracts", []))
        gaps = [_days_between(_loads(p["override_json"])["value"], contracts[d]["countersigned_at"])
                for p in precedents if (d := _loads(p["scope_json"])["deal_id"]) in contracts
                and contracts[d].get("countersigned_at")]
        if gaps:
            result["max_gap_days"] = max(gaps)
            result["proposal"] = (f"book a deal in the quarter the customer signed when the countersignature follows "
                                  f"within {max(gaps)} days, instead of on the countersignature date")
    elif field == "base":
        result["proposal"] = "review the commissionable base rule: deals were paid on a base other than the signed ACV"
    return {"fn": "check_precedent_pattern", "args": {"field": field, "threshold": threshold}, "result": result}


# price_increase_cap is a renewal term: it changes revenue, not commission, so no plan rule is needed for it.
KNOWN_CLAUSES = {"billing", "termination_for_convenience", "amendment", "price_increase_cap"}


def check_no_rule(rows, deal_id) -> dict:
    """SPEC escalation contract #3: a contract clause or currency that no plan rule covers (NO_RULE). Ramps,
    opt-outs, non-USD contracts and anything else unrecognised are not guessed at."""
    contract = latest_by_id(rows.get("contracts", [])).get(deal_id)
    clauses = _loads(contract["clauses_json"]) if contract else {}
    unknown = sorted(k for k in clauses if k not in KNOWN_CLAUSES | {"currency"})
    currency = str(clauses.get("currency", "USD")).upper()
    if currency != "USD":
        unknown.append(f"currency {currency}")
    return {"fn": "check_no_rule", "args": {"deal_id": deal_id},
            "result": {"no_rule": bool(unknown), "unknown": unknown}}


def check_rep_exceptions(rows, rep_id, threshold) -> dict:
    """SPEC escalation contract #5: repeated exceptions for one rep are an integrity signal for the Controller."""
    owners = {d["id"]: d["owner_id"] for d in latest_by_id(rows.get("deals", [])).values()}
    mine = sorted(p["id"] for p in rows.get("precedents", [])
                  if owners.get(_loads(p["scope_json"]).get("deal_id")) == rep_id)
    return {"fn": "check_rep_exceptions", "args": {"rep_id": rep_id, "threshold": threshold},
            "result": {"flag": len(mine) >= threshold, "precedents": mine}}


def check_leave(rows, rep_id, start_date, end_date) -> dict:
    """SPEC escalation contract #4: a leave of absence is legally sensitive. The check states what is at stake
    (the rep's unpaid lines); the Controller and HR decide."""
    lines = sorted((l for l in rows.get("commission_lines", []) if l["rep_id"] == rep_id and l["amount_cents"] > 0),
                   key=lambda l: l["id"])
    return {"fn": "check_leave", "args": {"rep_id": rep_id, "start_date": start_date, "end_date": end_date},
            "result": {"lines": [l["id"] for l in lines], "total_cents": sum(l["amount_cents"] for l in lines)}}


def check_short_pay(rows, deal_id, tolerance_bps) -> dict:
    """R5: a payment of at least tolerance_bps of what was invoiced counts as collected for commission. The
    shortfall (wire fees, say) is AR's to settle; Quota only says so."""
    billing = [b for b in rows.get("billing", []) if b["deal_id"] == deal_id]
    invoiced = sum(b["amount_cents"] for b in billing if b["kind"] == "invoice")
    paid = sum(b["amount_cents"] for b in billing if b["kind"] == "payment")
    paid_bps = paid * 10000 // invoiced if invoiced else 10000
    return {"fn": "check_short_pay", "args": {"deal_id": deal_id, "tolerance_bps": tolerance_bps},
            "result": {"invoiced_cents": invoiced, "paid_cents": paid, "short_cents": max(0, invoiced - paid),
                       "paid_bps": paid_bps, "collected": paid > 0 and paid_bps >= tolerance_bps}}


def check_commitments(rows, deal_id, findings) -> dict:
    """ELASTIC_TASKS E5: a message promising the customer something the signed contract does not say holds the
    deal's commission (OFF_CONTRACT_COMMITMENT). Replay re-applies the contract check to the stored findings."""
    from quota.agent.commitments import covered
    contract = latest_by_id(rows.get("contracts", [])).get(deal_id)
    clauses = _loads(contract["clauses_json"]) if contract else {}
    open_ = [f["doc_id"] for f in findings if not covered(f["type"], f["quote"], clauses)]
    return {"fn": "check_commitments", "args": {"deal_id": deal_id, "findings": findings},
            "result": {"held": bool(open_), "open": open_, "covered": [f["doc_id"] for f in findings
                                                                      if f["doc_id"] not in open_],
                       "reason_code": "OFF_CONTRACT_COMMITMENT" if open_ else None}}


CHECKS = {fn.__name__: fn for fn in (check_splits, check_backdating, check_collection, check_termination,
                                     check_territory, check_plan_drift, check_resubmission, check_precedent_pattern,
                                     check_no_rule, check_rep_exceptions, check_leave, check_short_pay,
                                     check_commitments)}
