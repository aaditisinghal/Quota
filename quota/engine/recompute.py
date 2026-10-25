"""Recompute a rep's quarter when one booking in it changes (pure).

When an earlier deal in a quarter is reversed (a churn clawback, a void) or re-booked (an amendment, a new
booking date), the deals after it can move between tiers. `recompute_quarter` re-runs `calc_commission` over
every live booking in (booking_ts, deal_id) order and returns the lines whose amount changed. `true_up_entry`
is the decision for one changed line; replay is `true_up_entry(rows, **args) == stored`.
"""

import json
from dataclasses import asdict, dataclass

from quota import clock as clk
from quota.engine import amortization
from quota.engine.calculator import Booking, calc_commission, latest_by_id, plan_for
from quota.engine.entries import _entry, _line, _rep, chargeback_code
from quota.engine.plans import Plan


@dataclass(frozen=True)
class LineDelta:
    line_id: str
    old_cents: int
    new_cents: int
    delta_cents: int


def _booking(b) -> Booking:
    return b if isinstance(b, Booking) else Booking(**b)


def recompute_quarter(rep_id, quarter, bookings, plan: Plan) -> list[LineDelta]:
    """bookings: [{line_id, amount_cents (what the line pays now), booking}] for the rep's live lines in the
    quarter. Prior credit accumulates in (booking_ts, deal_id) order; only changed lines are returned."""
    live = sorted(({**b, "booking": _booking(b["booking"])} for b in bookings),
                  key=lambda b: (b["booking"].booking_ts, b["booking"].deal_id))
    prior, out = 0, []
    for b in live:
        result = calc_commission(plan, b["booking"], prior)
        assert b["booking"].rep_id == rep_id and result.quarter == quarter, (b, quarter)
        prior += result.credit_cents
        if result.amount_cents != b["amount_cents"]:
            out.append(LineDelta(b["line_id"], b["amount_cents"], result.amount_cents,
                                 result.amount_cents - b["amount_cents"]))
    return out


def quarter_results(rep_id, quarter, bookings, plan: Plan) -> dict[str, dict]:
    """line_id -> the recomputed CommissionResult fields (portions, tier, attainment)."""
    live = sorted(((_booking(b["booking"]), b["line_id"]) for b in bookings),
                  key=lambda x: (x[0].booking_ts, x[0].deal_id))
    prior, out = 0, {}
    for booking, line_id in live:
        result = calc_commission(plan, booking, prior)
        prior += result.credit_cents
        out[line_id] = asdict(result)
    return out


def true_up_entry(rows, line_id, rep_id, quarter, bookings, through, date, caused_by=()) -> dict:
    """The true-up for one line after its quarter was recomputed. A lower amount is an advance the rep has
    not earned: recovered from future advances (R2 rules), less the amortization already taken on the excess.
    A higher amount is a new correcting line that is accrued like any other.

    `through`: the last period whose amortization is already posted. Capitalized line reduced by d with m_used
    of m_total months amortized: Dr 1290 d / Cr 1350 d*(m_total-m_used)//m_total / Cr 6110 the rest. Expensed
    line: Dr 1290 d / Cr 6115 d. `caused_by`: the deals whose reversal changed the quarter (recorded, not used)."""
    plan = plan_for(rows, rep_id, f"{quarter[:4]}-{(int(quarter[-1]) - 1) * 3 + 1:02d}-01")
    delta = next((d for d in recompute_quarter(rep_id, quarter, bookings, plan) if d.line_id == line_id), None)
    result = quarter_results(rep_id, quarter, bookings, plan)[line_id]
    line = latest_by_id(rows["commission_lines"])[line_id]
    schedule_row = next((s for s in rows.get("amortization_schedules", [])
                         if s["line_id"] == line_id and s["status"] == "active"), None)
    code, entry, schedule, m_used, m_total = None, None, None, None, None
    if delta is not None and delta.delta_cents < 0:
        d = -delta.delta_cents
        booking_date = clk.local_date(_booking(next(b for b in bookings if b["line_id"] == line_id)["booking"]).booking_ts)
        window_end = (clk.add_months(booking_date, plan.clawback_window_months)
                      if plan.clawback_window_months is not None else None)
        earned = window_end is not None and date > window_end       # past the clawback window: earned
        code = chargeback_code(plan, _rep(rows, rep_id), earned=earned)
        if code is None and schedule_row is not None:
            months = [list(m) for m in json.loads(schedule_row["schedule_json"])]
            m_total = len(months)
            m_used = sum(1 for period, _ in months if period <= through)
            to_1350 = d * (m_total - m_used) // m_total
            entry = _entry("true_up", date, f"{line_id} {quarter} recompute, recover from rep",
                           [_line("1290", debit=d, ref=rep_id), _line("1350", credit=to_1350, ref=line_id),
                            _line("6110", credit=d - to_1350, ref=line_id)])
            left = amortization.remaining_after(months, through) - to_1350
            rest = amortization.build_schedule(left, m_total - m_used, clk.add_periods(through, 1)) if m_total > m_used else []
            kept = [m for m in months if m[0] <= through]
            schedule = {"total_cents": sum(c for _, c in kept) + left, "months": m_total,
                        "start_period": schedule_row["start_period"], "schedule": kept + [list(m) for m in rest]}
        elif code is None:
            entry = _entry("true_up", date, f"{line_id} {quarter} recompute, recover from rep",
                           [_line("1290", debit=d, ref=rep_id), _line("6115", credit=d, ref=line_id)])
    return {"fn": "true_up_entry",
            "args": {"line_id": line_id, "rep_id": rep_id, "quarter": quarter, "bookings": bookings,
                     "through": through, "date": date, "caused_by": list(caused_by)},
            "result": {"delta": asdict(delta) if delta else None, "recomputed": result,
                       "deal_id": line["deal_id"], "chargeback_code": code, "m_used": m_used, "m_total": m_total,
                       "entry": entry, "schedule": schedule,
                       "attainment_after_bps": sum(_booking(b["booking"]).base_cents * _booking(b["booking"]).split_bps
                                                   // 10000 for b in bookings) * 10000 // plan.quota_cents}}
