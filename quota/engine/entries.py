"""Journal entry builders (pure). Each returns {"effective_date","memo","memo_kind","lines"};
memos are "<memo_kind>: <text>" so post_entry can match standing precedents by kind."""

import json

from quota import clock as clk
from quota.engine import amortization, ranking
from quota.engine.calculator import contract_for, latest_by_id, plan_for
from quota.engine.plans import Plan


def _id(obj):
    return obj if isinstance(obj, str) else obj["id"]


def _line(account, debit=0, credit=0, ref=None):
    return {"account": account, "debit_cents": debit, "credit_cents": credit, "subledger_ref": ref}


def _entry(memo_kind, effective_date, text, lines):
    return {"effective_date": effective_date, "memo": f"{memo_kind}: {text}", "memo_kind": memo_kind,
            "lines": [l for l in lines if l["debit_cents"] or l["credit_cents"]]}


def invoice_entry(deal, amount, date):
    deal_id = _id(deal)
    return _entry("invoice", date, f"{deal_id} invoice",
                  [_line("1200", debit=amount, ref=deal_id), _line("2400", credit=amount, ref=deal_id)])


def payment_entry(deal, amount, date):
    deal_id = _id(deal)
    return _entry("payment", date, f"{deal_id} customer payment",
                  [_line("1000", debit=amount, ref=deal_id), _line("1200", credit=amount, ref=deal_id)])


def revenue_entry(deal, amount, period_end):
    deal_id = _id(deal)
    return _entry("revenue", period_end, f"{deal_id} {period_end[:7]} subscription revenue",
                  [_line("2400", debit=amount, ref=deal_id), _line("4000", credit=amount, ref=deal_id)])


def refund_entry(deal, amount, date):
    deal_id = _id(deal)
    return _entry("refund", date, f"{deal_id} pro-rata refund",
                  [_line("2400", debit=amount, ref=deal_id), _line("1000", credit=amount, ref=deal_id)])


def commission_accrual_entry(line, date, capitalized=True):
    amount, line_id = line["amount_cents"], line["id"]
    expense_or_asset = "1350" if capitalized else "6115"
    return _entry("commission_accrual", date, f"{line_id} {line['deal_id']}/{line['rep_id']}",
                  [_line(expense_or_asset, debit=amount, ref=line_id), _line("2150", credit=amount, ref=line_id)])


def amortization_entry(line, amount, period_end):
    line_id = _id(line)
    return _entry("amortization", period_end, f"{line_id} {period_end[:7]}",
                  [_line("6110", debit=amount, ref=line_id), _line("1350", credit=amount, ref=line_id)])


def payroll_payment_entry(file, date):
    file_id, total = file["id"], file["total_cents"]
    return _entry("payroll_payment", date, file_id,
                  [_line("2150", debit=total, ref=file_id), _line("1000", credit=total, ref=file_id)])


def clawback_entry(line, total, amortized, date, recoverable: bool):
    line_id, rep_id = line["id"], line["rep_id"]
    unamortized = total - amortized
    if recoverable:
        lines = [_line("1290", debit=total, ref=rep_id), _line("1350", credit=unamortized, ref=line_id),
                 _line("6110", credit=amortized, ref=line_id)]
    else:
        lines = [_line("6110", debit=unamortized, ref=line_id), _line("1350", credit=unamortized, ref=line_id)]
    return _entry("clawback", date, f"{line_id} {'recover from rep' if recoverable else 'write off'}", lines)


def is_recoverable(plan: Plan, booking_date: str, churn_date: str, trigger: str) -> bool:
    return (plan.clawback_window_months is not None and plan.signed_at is not None
            and churn_date <= clk.add_months(booking_date[:10], plan.clawback_window_months)
            and trigger in plan.clawback_triggers)


# ---------- decisions from fact rows (used by the seat and by replay) ----------

def _find(rows, table, row_id):
    return latest_by_id(rows.get(table))[row_id]


def accrual_decision(rows, line_id, now) -> dict:
    line = _find(rows, "commission_lines", line_id)
    contract = contract_for(rows, line["deal_id"])
    capitalized = amortization.capitalize(contract["term_months"])
    start = clk.period_of(now)
    date = clk.period_end(start)
    entry = commission_accrual_entry(line, date, capitalized)
    schedule = amortization.build_schedule(line["amount_cents"], contract["term_months"], start) if capitalized else []
    output = {
        "fn": "commission_accrual_entry",
        "args": {"line_id": line_id, "amount_cents": line["amount_cents"], "capitalized": capitalized,
                 "term_months": contract["term_months"], "start_period": start, "date": date},
        "result": {"entry": entry, "schedule": [list(p) for p in schedule]},
    }
    return {"output": output, "entry": entry, "schedule": schedule, "capitalized": capitalized,
            "resolutions": {}, "precedent_ids": []}


def clawback_decision(rows, line_id, now) -> dict:
    line = _find(rows, "commission_lines", line_id)
    deal = _find(rows, "deals", line["deal_id"])
    contract = contract_for(rows, deal["id"])
    booking = ranking.resolve_booking(deal, contract, rows.get("esign_events", []), rows.get("precedents", []), now)
    booking_date = booking.value[:10]
    plan = plan_for(rows, line["rep_id"], booking_date)
    churn = next(e for e in rows.get("events", []) if e["type"] == "customer.churned"
                 and json.loads(e["payload_json"])["deal_id"] == deal["id"])
    payload = json.loads(churn["payload_json"])
    churn_date, trigger = payload["effective_date"], payload.get("reason") or "churn"
    schedule_row = next(s for s in rows.get("amortization_schedules", [])
                        if s["line_id"] == line_id and s["status"] == "active")
    amortized = amortization.amortized_through(json.loads(schedule_row["schedule_json"]), clk.period_of(churn_date))
    recoverable = is_recoverable(plan, booking_date, churn_date, trigger)
    window_end = (clk.add_months(booking_date, plan.clawback_window_months)
                  if plan.clawback_window_months is not None else None)
    entry = clawback_entry(line, line["amount_cents"], amortized, churn_date, recoverable)
    output = {
        "fn": "clawback_entry",
        "args": {"line_id": line_id, "total_cents": line["amount_cents"], "amortized_cents": amortized,
                 "date": churn_date, "recoverable": recoverable, "booking_date": booking_date,
                 "churn_date": churn_date, "trigger": trigger, "window_end": window_end,
                 "plan": {"id": plan.plan_id, "version": plan.version, "signed_at": plan.signed_at}},
        "result": entry,
    }
    return {"output": output, "entry": entry, "recoverable": recoverable, "booking": booking,
            "resolutions": {"booking_date": ranking.to_dict(booking)},
            "precedent_ids": [booking.source.split(":", 1)[1]] if booking.basis == "precedent" else [],
            "schedule_id": schedule_row["id"]}
