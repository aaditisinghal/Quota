"""The facts an inbox carries, read the same way from the generator's ledger (the answer key) and from an intake
ledger (what was loaded), and the quarter's payouts computed from them (pure over rows)."""

import json

from quota.engine import calculator
from quota.engine.plans import load_plan_raw
from quota.ledger import queries
from quota.sim import seed_acme

END = queries.END_OF_TIME
COMPARE = {   # table -> the columns a round trip must reproduce
    "deals": ("id", "account", "crm_amount_cents", "crm_close_date", "stage", "owner_id", "splits_json"),
    "contracts": ("id", "deal_id", "acv_cents", "term_months", "contract_date", "countersigned_at", "clauses_json"),
    "esign_events": ("id", "envelope_id", "deal_id", "event", "at"),
    "billing": ("id", "kind", "deal_id", "amount_cents", "at"),
    "people": ("id", "name", "role", "state", "manager_id", "start_date", "end_date"),
}


def latest(conn, table) -> list[dict]:
    if table in queries.VERSIONED:
        return queries.latest_rows(conn, table, END)
    return queries.fetch_all(conn, f"SELECT * FROM {table} ORDER BY id")


def comparable(conn) -> dict[str, set[tuple]]:
    out = {}
    for table, columns in COMPARE.items():
        out[table] = {tuple(json.dumps(queries.loads(r[c]), sort_keys=True) if c.endswith("_json") else r[c]
                            for c in columns) for r in latest(conn, table)}
    return out


def plan_row(rep_id) -> dict:
    """Every Fernhill AE is on the standard plan (the generator's template); the inbox's PDFs are read separately."""
    raw = {**load_plan_raw(seed_acme.PLAN_PATH), "plan_id": f"AE-2026-H1-{rep_id}", "rep": rep_id}
    return {"id": raw["plan_id"], "version": raw["version"], "rep_id": rep_id, "effective_from": raw["effective"]["from"],
            "effective_to": raw["effective"]["to"], "config_json": json.dumps(raw, sort_keys=True, default=str),
            "signed_doc_ref": raw["signed_doc"],
            "signed_at": raw["signed_at"]}


def rep_rows(conn, rep_id) -> dict:
    """What the calculator reads for one rep, from inbox-shaped facts: latest deal and contract versions, the
    e-sign events, the standard plan. No precedents (they live in the approvals, not in any export)."""
    deals = [d for d in queries.latest_rows(conn, "deals", END) if queries.credits_rep(d, rep_id)]
    ids = {d["id"] for d in deals}
    return {"plans": [plan_row(rep_id)], "deals": deals,
            "contracts": [c for c in queries.latest_rows(conn, "contracts", END) if c["deal_id"] in ids],
            "esign_events": [e for e in queries.fetch_all(conn, "SELECT * FROM esign_events ORDER BY id")
                             if e["deal_id"] in ids],
            "precedents": []}


def rate_text(result) -> str:
    return {"base": "10%", "accelerator": "15%", "mixed": "10%/15%"}[result.tier]


def payouts(conn, rep_ids, quarter) -> list[dict]:
    """Each rep's commission on each booking in the quarter, in booking order."""
    out = []
    for rep in sorted(rep_ids):
        rows = rep_rows(conn, rep)
        bookings = calculator.rep_bookings(rows, rep, END)
        counted = calculator.attainment_bookings(rows, bookings)
        plan = calculator.plan_for(rows, rep, f"{quarter[:4]}-01-01")
        for key, booking in sorted(bookings.items(), key=lambda kv: (kv[1].booking_ts, kv[0])):
            if calculator.quarter_of(booking.booking_ts) != quarter or booking not in counted:
                continue
            result = calculator.calc_commission(plan, booking, calculator.prior_credit(counted, booking))
            out.append({"rep": rep, "deal": key, "quarter": quarter, "base_cents": result.credit_cents,
                        "rate": rate_text(result), "commission_cents": result.amount_cents})
    return out
