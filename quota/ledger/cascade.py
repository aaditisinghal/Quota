"""SPEC "One event, eight numbers": what a customer's churn changed, read back from the ledger. The CLI demo
prints it; the Ledger health screen shows it for every churned deal."""

import json

from quota import clock as clk
from quota.ledger import queries
from quota.money import fmt_pct, fmt_usd


def _account_total(journal, account, subledger_ref, exclude=()):
    return sum(l["debit_cents"] - l["credit_cents"] for e in journal if e["id"] not in exclude
               for l in e["lines"] if l["account"] == account and l["subledger_ref"] == subledger_ref)


def churn_cascade(ro, deal_id) -> list[dict] | None:
    """Eight items {label, text, trace} for a churned deal with a posted refund and clawback, else None."""
    journal = [e for e in queries.journal(ro) if e["status"] == "posted"]
    refund = next((e for e in journal if queries.memo_kind(e["memo"]) == "refund"
                   and e["lines"][0]["subledger_ref"] == deal_id), None)
    clawback_line = next((l for l in queries.fetch_all(ro, "SELECT * FROM commission_lines ORDER BY id")
                          if l["deal_id"] == deal_id and l["amount_cents"] < 0), None)
    clawback = clawback_line and next((e for e in journal if queries.memo_kind(e["memo"]) == "clawback"
                                       and e["decision_id"] == clawback_line["decision_id"]), None)
    churn_row = queries.fetch_one(ro, "SELECT payload_json FROM events WHERE type = 'customer.churned' "
                                      "AND payload_json LIKE ?", (f'%"deal_id":"{deal_id}"%',))
    if refund is None or clawback is None or churn_row is None:
        return None
    churn = json.loads(churn_row["payload_json"])
    clawback_lines = {l["account"]: l for l in clawback["lines"]}
    contract = queries.as_of(ro, "contracts", deal_id, None)
    monthly = contract["acv_cents"] // 12
    recognized = len([e for e in journal if queries.memo_kind(e["memo"]) == "revenue"
                      and e["lines"][0]["subledger_ref"] == deal_id])
    remaining = monthly * (contract["term_months"] - recognized)
    clawed = queries.as_of(ro, "commission_lines", clawback_line["corrects_id"], None)
    booked = json.loads(queries.as_of(ro, "decisions", clawed["decision_id"], None)["output_json"])["result"]
    rep = clawback_line["rep_id"]
    plan = queries.fetch_one(ro, "SELECT config_json FROM plans WHERE rep_id = ? ORDER BY version DESC", (rep,))
    quota_cents = json.loads(plan["config_json"])["quota"]["amount"] * 100
    attainment_after = (booked["prior_credit_cents"] + booked["credit_cents"] + clawback_line["credit_cents"]) \
        * 10000 // quota_cents
    deferred_before = -_account_total(journal, "2400", deal_id, exclude=(refund["id"],))
    deferred_after = -_account_total(journal, "2400", deal_id)
    cash = sum(l["debit_cents"] - l["credit_cents"] for l in refund["lines"] if l["account"] == "1000")
    person = rep.split(".")[0].title()
    quarter = booked["quarter"]                                     # e.g. 2026Q1
    locked = queries.period_locked(ro, f"{quarter[:4]}-{int(quarter[-1]) * 3:02d}", None)
    return [
        {"label": "Deferred revenue", "trace": refund["id"],
         "text": f"{fmt_usd(deferred_before)} -> {fmt_usd(deferred_after)} (refund {refund['id']})"},
        {"label": "Cash", "trace": refund["id"], "text": f"{fmt_usd(cash)} ({refund['id']})"},
        {"label": f"{contract['id'].title()} customer balance", "trace": "account/1200",
         "text": f"{fmt_usd(_account_total(journal, '1200', deal_id))}, contract terminated {churn['effective_date']}"},
        {"label": "Revenue forecast", "trace": None,
         "text": f"{fmt_usd(-monthly)}/month from {clk.add_periods(churn['effective_date'][:7], 1)} "
                 f"({fmt_usd(-remaining)} remaining contract)"},
        {"label": f"{person} commission", "trace": clawback_line["id"],
         "text": f"{fmt_usd(clawback_line['amount_cents'])} clawback ({clawback_line['id']}, {clawback['id']})"},
        {"label": "Deferred commission", "trace": clawback["id"],
         "text": f"{fmt_usd(clawback_lines['1350']['credit_cents'])} written off ({clawback['id']})"},
        {"label": "Commission expense", "trace": clawback["id"],
         "text": f"{fmt_usd(-clawback_lines['6110']['credit_cents'])} amortization reversed ({clawback['id']})"},
        {"label": f"{person} {quarter[-2:]} attainment", "trace": clawed["decision_id"],
         "text": f"{fmt_pct(booked['attainment_after_bps'])} -> {fmt_pct(attainment_after)}"
                 + (f" (report only; {quarter[:4]}-{quarter[-2:]} periods locked)" if locked else "")},
    ]


def churned_deals(ro) -> list[str]:
    return sorted({json.loads(r["payload_json"])["deal_id"] for r in
                   queries.fetch_all(ro, "SELECT payload_json FROM events WHERE type = 'customer.churned'")})
