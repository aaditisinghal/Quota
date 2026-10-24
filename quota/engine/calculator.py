"""Commission math (pure). The lower half assembles calculator inputs from fact rows, so the seat and
replay run exactly the same code over the same rows."""

import json
from dataclasses import asdict, dataclass

from quota.engine import ranking
from quota.engine.plans import Plan, plan_from_row
from quota.money import apply_bps


@dataclass(frozen=True)
class Booking:
    deal_id: str
    rep_id: str
    booking_ts: str
    base_cents: int
    split_bps: int


@dataclass(frozen=True)
class CommissionResult:
    deal_id: str
    rep_id: str
    quarter: str
    credit_cents: int
    prior_credit_cents: int
    base_portion_cents: int
    accel_portion_cents: int
    amount_cents: int
    tier: str                     # base|accelerator|mixed
    attainment_before_bps: int
    attainment_after_bps: int


def quarter_of(ts: str) -> str:
    return f"{ts[:4]}Q{(int(ts[5:7]) - 1) // 3 + 1}"


def credit_of(booking: Booking) -> int:
    return booking.base_cents * booking.split_bps // 10000


def prior_credit(rep_bookings: list[Booking], this: Booking) -> int:
    """Sum of credits for the same rep and quarter with (booking_ts, deal_id) < this."""
    quarter = quarter_of(this.booking_ts)
    return sum(credit_of(b) for b in rep_bookings
               if b.rep_id == this.rep_id and quarter_of(b.booking_ts) == quarter
               and (b.booking_ts, b.deal_id) < (this.booking_ts, this.deal_id))


def calc_commission(plan: Plan, booking: Booking, prior_credit_cents: int) -> CommissionResult:
    credit = credit_of(booking)
    room = max(0, plan.quota_cents - prior_credit_cents)
    base_portion = min(credit, room)
    accel_portion = credit - base_portion
    amount = apply_bps(base_portion, plan.base_rate_bps) + apply_bps(accel_portion, plan.accel_rate_bps)
    if accel_portion == 0:
        tier = "base"
    elif base_portion == 0:
        tier = "accelerator"
    else:
        tier = "mixed"
    return CommissionResult(
        deal_id=booking.deal_id, rep_id=booking.rep_id, quarter=quarter_of(booking.booking_ts),
        credit_cents=credit, prior_credit_cents=prior_credit_cents,
        base_portion_cents=base_portion, accel_portion_cents=accel_portion, amount_cents=amount, tier=tier,
        attainment_before_bps=prior_credit_cents * 10000 // plan.quota_cents,
        attainment_after_bps=(prior_credit_cents + credit) * 10000 // plan.quota_cents,
    )


# ---------- from fact rows ----------
# `rows` maps table -> list of row dicts exactly as stored (the inputs a decision records).

def latest_by_id(table_rows) -> dict[str, dict]:
    latest = {}
    for row in table_rows or []:
        if row["id"] not in latest or row.get("version", 0) > latest[row["id"]].get("version", 0):
            latest[row["id"]] = row
    return latest


def contract_versions(rows, deal_id) -> list[dict]:
    return sorted((c for c in rows.get("contracts") or [] if c["deal_id"] == deal_id), key=lambda c: c["version"])


def amendment(contract) -> dict | None:
    clauses = contract["clauses_json"]
    return (json.loads(clauses) if isinstance(clauses, str) else clauses).get("amendment")


def contract_for(rows, deal_id) -> dict | None:
    """The latest contract version: its countersignature is the booking evidence."""
    versions = contract_versions(rows, deal_id)
    return versions[-1] if versions else None


def base_contract(rows, deal_id) -> dict | None:
    """The contract as signed (or corrected): amendments are separate bookings, not a new base."""
    versions = contract_versions(rows, deal_id)
    originals = [c for c in versions if not amendment(c)]
    return (originals or versions or [None])[-1]


def amendment_bookings(rows, deal_id) -> list[tuple[str, str, int, dict]]:
    """(booking key, signed_at, ACV increase, version) for each amendment that raised ACV mid-term."""
    out, previous = [], None
    for contract in contract_versions(rows, deal_id):
        terms = amendment(contract)
        if terms and terms.get("kind") == "amendment" and previous is not None \
                and contract["acv_cents"] > previous["acv_cents"]:
            out.append((f"{deal_id}+v{contract['version']}", terms["signed_at"],
                        contract["acv_cents"] - previous["acv_cents"], contract))
        previous = contract
    return out


def plan_for(rows, rep_id, on_date) -> Plan:
    """The signed plan version in effect on that date (an unsigned draft never pays; it is flagged elsewhere)."""
    mine = [p for p in (plan_from_row(r) for r in rows.get("plans") or []) if p.rep == rep_id]
    in_effect = [p for p in mine if p.effective_from <= on_date[:10] <= p.effective_to] or mine
    signed = [p for p in in_effect if p.signed_at] or in_effect
    return max(signed, key=lambda p: p.version)


def resolve_deal(rows, deal, rep_id, now) -> dict[str, ranking.Resolution]:
    contract = contract_for(rows, deal["id"])
    precedents = rows.get("precedents", [])
    booking = ranking.resolve_booking(deal, contract, rows.get("esign_events", []), precedents, now)
    plan = plan_for(rows, rep_id, booking.value or now)
    return {
        "booking_date": booking,
        "base": ranking.resolve_base(deal, base_contract(rows, deal["id"]), plan, precedents, now),
        "splits": ranking.resolve_splits(deal, rows.get("split_records", [])),
    }


def rep_bookings(rows, rep_id, now, overrides=None) -> dict[str, Booking]:
    """booking key -> Booking for every deal crediting this rep, plus "<deal>+v<n>" for each ACV-raising
    amendment. overrides: {key: {booking_ts, base_cents}}."""
    out = {}
    for deal in latest_by_id(rows.get("deals")).values():
        resolved = resolve_deal(rows, deal, rep_id, now)
        bps = ranking.split_bps(resolved["splits"], rep_id)
        if bps <= 0 or resolved["booking_date"].value is None:
            continue
        out[deal["id"]] = Booking(deal["id"], rep_id, resolved["booking_date"].value, resolved["base"].value, bps)
        for key, signed_at, increase, _ in amendment_bookings(rows, deal["id"]):
            out[key] = Booking(key, rep_id, signed_at, increase, bps)
    for key, changes in (overrides or {}).items():
        for field_name, value in (changes or {}).items():
            if value is not None and key in out:
                out[key] = Booking(**{**asdict(out[key]), field_name: value})
    return out


def evaluate(rows, deal_id, rep_id, now, booking_ts=None, base_cents=None) -> CommissionResult:
    """Commission for one deal, optionally with the booking timestamp or base replaced."""
    bookings = rep_bookings(rows, rep_id, now, {deal_id: {"booking_ts": booking_ts, "base_cents": base_cents}})
    this = bookings[deal_id]
    plan = plan_for(rows, rep_id, this.booking_ts)
    return calc_commission(plan, this, prior_credit(list(bookings.values()), this))


def _increment_resolutions(rows, key, rep_id, now) -> dict[str, ranking.Resolution]:
    deal_id = key.split("+")[0]
    _, signed_at, increase, contract = next(a for a in amendment_bookings(rows, deal_id) if a[0] == key)
    source = f"contracts:{deal_id}:v{contract['version']}"
    deal = latest_by_id(rows.get("deals"))[deal_id]
    return {"booking_date": ranking.Resolution("booking_date", signed_at, source, "R-BOOKING-01", [], "amendment"),
            "base": ranking.Resolution("base", increase, source, "R-AMOUNT-01", [], "amendment_increase"),
            "splits": ranking.resolve_splits(deal, rows.get("split_records", []))}


def commission_decision(rows, deal_id, rep_id, now) -> dict:
    """Everything a commission_calc decision records; replay compares `output`. `deal_id` may be an amendment
    booking key ("<deal>+v<n>"): commission on the ACV increase, booked when the amendment was signed."""
    if "+" in deal_id:
        resolved = _increment_resolutions(rows, deal_id, rep_id, now)
        booking_res, base_res = resolved["booking_date"], resolved["base"]
    else:
        deal = latest_by_id(rows.get("deals"))[deal_id]
        resolved = resolve_deal(rows, deal, rep_id, now)
        booking_res = ranking.with_effects(
            resolved["booking_date"], lambda v: evaluate(rows, deal_id, rep_id, now, booking_ts=v).amount_cents)
        base_res = ranking.with_effects(
            resolved["base"], lambda v: evaluate(rows, deal_id, rep_id, now, base_cents=v).amount_cents)
    bookings = rep_bookings(rows, rep_id, now)
    this = bookings[deal_id]
    prior = prior_credit(list(bookings.values()), this)
    plan = plan_for(rows, rep_id, this.booking_ts)
    result = calc_commission(plan, this, prior)
    output = {
        "fn": "calc_commission",
        "args": {"plan": {"id": plan.plan_id, "version": plan.version}, "booking": asdict(this),
                 "prior_credit_cents": prior},
        "result": asdict(result),
    }
    resolutions = {"booking_date": booking_res, "base": base_res, "splits": resolved["splits"]}
    return {
        "output": output,
        "resolutions": {k: ranking.to_dict(v) for k, v in resolutions.items()},
        "precedent_ids": [r.source.split(":", 1)[1] for r in resolutions.values() if r.basis == "precedent"],
        "result": result, "booking": this, "plan": plan, "resolved": resolutions,
    }
